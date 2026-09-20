"""Provider-neutral WorkItem control plane and Slice projection.

This module is the integration boundary around :class:`ProviderRuntime`:

* every new provider run becomes a durable RunAttempt;
* provider events update execution/artifact/completion facts;
* focus is persisted per UI surface;
* wallpaper canvases are projected through the selected WorkItem so a
  background run cannot steal or contaminate a pinned task view.

It owns Git worktree allocation because durable workspace identity belongs to
the WorkItem, not to any execution Provider. Providers receive the final cwd.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import shutil
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any, Awaitable, Callable, Iterable

from config import settings as app_settings
from agent_host.provider_authoring import (
    auip_authoring_bundle_metrics,
    materialize_auip_runtime_assets,
    stage_auip_authoring_bundle,
)
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_identity import (
    MAIN_ROLE_NAME_METADATA_KEY,
    PARENT_CONTEXT_DELIVERED_EVENT,
    PARENT_CONTEXT_DELIVERY_METADATA_KEY,
    SOURCE_CONTEXT_SCOPE_METADATA_KEY,
    parent_conversation_context_delivery,
    validated_parent_context_delivery,
)
from agent_host.provider_types import (
    ProviderRecoveryContext,
    ProviderRunRequest,
    ProviderSessionHandle,
)
from agent_host.provider_workspace import workspace_route_authority
from agent_host.work_ledger_store import (
    WorkLedgerConflict,
    WorkLedgerNotFound,
    WorkLedgerStore,
    new_ledger_id,
)
from agent_host.work_ledger_types import (
    CompletionDecision,
    PermissionRequestRecord,
    RunAttemptRecord,
    WorkItemRecord,
    canonicalize_path,
    path_is_within,
)
from server.ai_os_schema import (
    diff_canvas_payload,
    presentation_message,
    work_note_payload,
    work_signal,
)
from server.event_bus import bus
from server.project_registry import (
    cwd_in_project_registry,
    project_registry_entries,
)
from server.provider_session_binding import resolve_provider_session_attachment
from server.provider_event_ingestion import ProviderEventIngestor
from server.work_intake import (
    EXISTING_ITEM_CONTINUATIONS,
    persist_work_intake,
    plan_work_intake,
    resolve_intake_reference,
)
from server.work_read_model import WorkReadModel
from server.work_destination_service import (
    WORKSPACE_ROUTING_SURFACE,
    WorkDestinationService,
)
from server import workspace_provisioner
from server.protocol import Method
from server.scratch_workspace import (
    ScratchUnavailable,
    create_scratch_workspace,
    ensure_scratch_root,
    is_scratch_path,
    is_scratch_root,
)
from server.unified_diff import parse_unified_diff
from server.work_activity_snapshot import (
    ACTIVITY_METADATA_KEY,
    project_host_steering,
)
from server.outcome_verification import (
    OUTCOME_VERDICT_METADATA_KEY,
    assess_provider_outcome,
    observe_required_host_outcome,
)
from agent_host.provider_outcome import OUTCOME_EVIDENCE_METADATA_KEY
from server.work_artifact_registry import WorkArtifactRegistry, collect_git_delta
from server.work_completion import CompletionEvidence, assess_completion
from server.work_context import add_work_note
from server.work_export_service import ExportResolution, WorkExportService
from server.work_permission_service import WorkPermissionService

logger = logging.getLogger(__name__)


DEFAULT_WORK_SURFACE = "wallpaper.slice"
_ACTIVE_EXECUTION = frozenset({"queued", "running"})
_TERMINAL_EXECUTION = frozenset({"succeeded", "failed", "cancelled"})
_MAX_AMENDMENT_TEXT = 2000
_TERMINAL_NOTICE_OUTBOX_KEY = "terminal_work_notice_outbox"
_MAX_TERMINAL_NOTICE_RECORDS = 16
_PROJECT_IDENTITY_VERSION = 1
_current_coordinator: WorkLedgerCoordinator | None = None


def _ledger_owns_terminal_narration() -> bool:
    """Read at call time so the owner can be switched without a restart."""

    from config import settings as _settings

    return bool(getattr(_settings, "WORK_LEDGER_OWNS_TERMINAL_NARRATION", False))


def get_work_ledger_coordinator() -> WorkLedgerCoordinator | None:
    """Return the configured runtime coordinator for chat workspace routing."""

    return _current_coordinator


def _activity_row_facts(activity: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten the canonical Slice activity shape for the report fact row."""

    source = activity if isinstance(activity, dict) else {}
    return {
        "activity_phase": str(source.get("phase") or ""),
        "activity_elapsed_seconds": float(source.get("elapsedSeconds") or 0.0),
        "activity_silent_seconds": float(source.get("silentSeconds") or 0.0),
        "activity_last_event_at": str(source.get("lastEventAt") or ""),
        "activity_last_provider_event_at": str(
            source.get("lastProviderEventAt") or source.get("lastEventAt") or ""
        ),
        "activity_last_semantic_progress_at": str(
            source.get("lastSemanticProgressAt") or ""
        ),
        "activity_last_directional_update_at": str(
            source.get("lastDirectionalUpdateAt") or ""
        ),
        "activity_last_event_type": str(source.get("lastEventType") or ""),
        "activity_semantic_summary": str(source.get("semanticSummary") or ""),
        "activity_semantic_source": str(source.get("semanticSource") or ""),
        "activity_semantic_verified": source.get("semanticVerified") is True,
        "activity_semantic_milestone": str(source.get("semanticMilestone") or ""),
        "activity_direction_summary": str(source.get("directionSummary") or ""),
        "activity_direction_source": str(source.get("directionSource") or ""),
        "activity_milestones": dict(source.get("milestones") or {}),
        "activity_last_tool": str(source.get("lastTool") or ""),
        "activity_tool_count": int(source.get("toolCount") or 0),
        "activity_artifact_count": int(source.get("artifactCount") or 0),
        "activity_liveness": dict(source.get("liveness") or {}),
        "activity_steering": dict(source.get("steering") or {}),
        "activity_uncertainty": str(source.get("uncertainty") or ""),
    }


def _project_registry_root_for(path: str) -> str:
    """Collapse a path onto the trusted Project root that contains it.

    A project is a destination; a subdirectory of one is not a second
    destination. Without this, naming `<repo>/server` as a cwd once would
    register it as its own project and it would appear in the routing
    candidates forever -- the same list-grows-with-history shape that made
    worktree isolation unusable (P1 work order section 11).

    Containment only, never git identity: a worktree shares its repository's
    identity but is a genuinely different workspace, and collapsing one onto
    its main checkout would send work to the wrong tree.
    """

    try:
        target = Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        return path
    for entry in project_registry_entries():
        try:
            root = Path(entry).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if root in target.parents:
            return str(root)
    return str(target)


class WorkLedgerCoordinator:
    """Coordinate provider attempts, durable task facts, and UI focus."""

    def __init__(
        self,
        store: WorkLedgerStore,
        *,
        default_surface: str = DEFAULT_WORK_SURFACE,
        clock=time.time,
        artifact_registry: WorkArtifactRegistry | None = None,
        export_service: WorkExportService | None = None,
        auto_accept_approved_exports: bool | None = None,
        workspace_provisioner=None,
        provider_start: Callable[[ProviderRunRequest], Awaitable[Any]] | None = None,
        provider_cancel: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        current_session_id: Callable[[], str | None] | None = None,
    ) -> None:
        self.store = store
        self.default_surface = str(default_surface or DEFAULT_WORK_SURFACE)
        self._clock = clock
        self.destination = WorkDestinationService(
            store,
            registry_check=lambda path: cwd_in_project_registry(path),
            scratch_root_provider=lambda: ensure_scratch_root(),
        )
        self.artifact_registry = artifact_registry or WorkArtifactRegistry(store)
        self.export_service = export_service or WorkExportService(store)
        self.auto_accept_approved_exports = (
            app_settings.WORK_AUTO_ACCEPT_APPROVED_EXPORTS
            if auto_accept_approved_exports is None
            else bool(auto_accept_approved_exports)
        )
        self.permission_service = WorkPermissionService(
            store,
            self.export_service,
            auto_accept_approved_exports=self.auto_accept_approved_exports,
        )
        self.read_model = WorkReadModel(
            store,
            clock=clock,
            is_unkept_draft=self.destination.is_unkept_draft,
            is_desktop_export_permission=(
                self.permission_service.is_desktop_export_permission
            ),
            can_resume_authorized_export=self.export_service.can_resume_authorized,
        )
        self.event_ingestor = ProviderEventIngestor(
            store,
            clock=clock,
            default_surface=self.default_surface,
        )
        # Host workspace allocation remains injectable so semantic tests do
        # not need to mutate real Git repositories.
        self.workspace_provisioner = workspace_provisioner
        self._provider_start = provider_start
        self._provider_cancel = provider_cancel
        self._current_session_id = current_session_id or (lambda: "")
        self._pending_provider_recoveries: dict[str, dict[str, Any]] = {}
        self._subscribed = False
        self._provider_snapshot_min_interval_s = max(
            0.0,
            float(
                getattr(
                    app_settings,
                    "WORK_PROVIDER_SNAPSHOT_MIN_INTERVAL_S",
                    1.0,
                )
            ),
        )
        self._provider_snapshot_last_at = 0.0
        self._provider_snapshot_task: asyncio.Task | None = None
        self._provider_snapshot_reason = ""
        self._provider_fact_queue: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None
        self._provider_fact_task: asyncio.Task | None = None

    def configure(self) -> None:
        global _current_coordinator
        if self._subscribed:
            _current_coordinator = self
            return
        # A Provider approval is a checkpoint inside one Attempt, not durable
        # authority that may outlive it.  Recover the narrow crash gap where a
        # terminal result was committed but the adapter never emitted its
        # final permission.expired event before the process stopped.
        self._expire_terminal_provider_permissions()
        self._recover_incomplete_provider_recoveries()
        self._recover_unclaimed_staged_exports()
        self._recover_authorized_exports()
        bus.on(Method.PROVIDER_EVENT, self._enqueue_provider_event)
        bus.on(Method.PROVIDER_RESULT, self._enqueue_provider_result)
        bus.on(Method.CHAT_WORK_NOTE_DELIVERED, self._on_terminal_work_notice_delivered)
        self._subscribed = True
        _current_coordinator = self

    def _expire_terminal_provider_permissions(
        self,
        attempt: RunAttemptRecord | None = None,
        *,
        resolution: str = "attempt_terminal",
    ) -> int:
        """Close Provider-owned approval checkpoints at their Attempt boundary.

        Host product permissions (for example a proposed Desktop export) are
        deliberately excluded: those authorize a durable side effect after
        execution and therefore may remain actionable after the Provider has
        finished.  The discriminator already belongs to the canonical
        permission record, so this does not infer lifecycle from a provider
        name, capability string, or UI state.
        """

        attempts: list[RunAttemptRecord]
        if attempt is not None:
            attempts = [attempt]
        else:
            attempts = [
                candidate
                for item in self.store.list_work_items(limit=1000)
                for candidate in self.store.list_attempts(item.work_item_id)
                if candidate.execution_status in _TERMINAL_EXECUTION
            ]
        expired = 0
        for candidate in attempts:
            expired += self.permission_service.expire_provider_checkpoints(
                candidate,
                resolution=resolution,
        )
        if expired:
            logger.info(
                "expired %d provider permission checkpoint(s) at terminal Attempt boundary",
                expired,
            )
        return expired

    def _recover_incomplete_provider_recoveries(self) -> None:
        """Reconcile the crash gap around one bounded successor Attempt.

        A `claimed` predecessor means the Host had reserved the one recovery
        slot. If intake durably created its successor before the process
        stopped, restore that link. Otherwise close the claim as failed so the
        task is observable instead of remaining in a permanent pseudo-running
        state. This recovery never starts Provider execution on boot.
        """

        for item in self.store.list_work_items(limit=1000):
            attempts = self.store.list_attempts(item.work_item_id)
            for index, attempt in enumerate(attempts):
                completion = (
                    attempt.metadata.get("provider_completion")
                    if isinstance(attempt.metadata.get("provider_completion"), dict)
                    else {}
                )
                if (
                    completion.get("classification")
                    != "progress_only_completion"
                    or completion.get("recovery_state") != "claimed"
                ):
                    continue
                successor = next(
                    (
                        candidate
                        for candidate in attempts[index + 1 :]
                        if isinstance(
                            candidate.metadata.get("provider_recovery"), dict
                        )
                        and candidate.metadata["provider_recovery"].get(
                            "predecessor_attempt_id"
                        )
                        == attempt.attempt_id
                        and candidate.metadata["provider_recovery"].get("root_attempt_id")
                        == attempt.attempt_id
                        and candidate.metadata["provider_recovery"].get("reason")
                        == "progress_only_completion"
                        and candidate.metadata["provider_recovery"].get("ordinal") == 1
                        and candidate.provider == attempt.provider
                        and candidate.operation_id == attempt.operation_id
                        and candidate.attempt_number == attempt.attempt_number + 1
                        and bool(candidate.provider_run_id)
                    ),
                    None,
                )
                reconciled = dict(completion)
                if successor is not None:
                    reconciled.update(
                        {
                            "recovery_state": "started",
                            "recovery_reconciled_at": float(self._clock()),
                            "successor_attempt_id": successor.attempt_id,
                            "successor_run_id": successor.provider_run_id,
                        }
                    )
                else:
                    reconciled.update(
                        {
                            "recovery_state": "failed",
                            "recovery_failed_at": float(self._clock()),
                            "recovery_error": "host_restarted_before_successor_intake",
                        }
                    )
                _updated, swapped = self.store.compare_and_set_attempt_metadata(
                    attempt.attempt_id,
                    key="provider_completion",
                    expected_present=True,
                    expected_value=completion,
                    value=reconciled,
                )
                if swapped:
                    logger.info(
                        "reconciled progress-only recovery predecessor=%s state=%s successor=%s",
                        attempt.attempt_id,
                        reconciled["recovery_state"],
                        reconciled.get("successor_attempt_id") or "",
                    )

    def pending_provider_recoveries(self) -> list[dict[str, Any]]:
        """Return Host recovery reservations that can still be retracted."""

        return [
            dict(value)
            for value in self._pending_provider_recoveries.values()
            if not value.get("cancelled")
        ]

    def cancel_pending_provider_recovery(self, attempt_id: str) -> bool:
        """Cancel one Host-owned recovery before its successor becomes visible."""

        clean_attempt_id = str(attempt_id or "").strip()
        pending = self._pending_provider_recoveries.get(clean_attempt_id)
        if pending is None or pending.get("cancelled"):
            return False
        attempt = self.store.get_attempt(clean_attempt_id)
        completion = (
            attempt.metadata.get("provider_completion")
            if attempt is not None
            and isinstance(attempt.metadata.get("provider_completion"), dict)
            else {}
        )
        if completion.get("recovery_state") != "claimed":
            return False
        cancelled = dict(completion)
        cancelled.update(
            {
                "recovery_state": "cancelled",
                "recovery_cancelled_at": float(self._clock()),
                "recovery_error": "user_retracted",
            }
        )
        _updated, swapped = self.store.compare_and_set_attempt_metadata(
            clean_attempt_id,
            key="provider_completion",
            expected_present=True,
            expected_value=completion,
            value=cancelled,
        )
        if not swapped:
            return False
        pending["cancelled"] = True
        pending["cancelled_at"] = cancelled["recovery_cancelled_at"]
        return True

    def _recover_unclaimed_staged_exports(self) -> None:
        """Close the crash gap between provider success and permission creation.

        Only the latest attempt of each open WorkItem is eligible.  Staging is
        attempt-owned, so this recovery can never aggregate older or sibling
        batches merely because their filenames happen to match.
        """

        for item in self.store.list_work_items(limit=1000):
            if item.state in {"accepted", "archived"}:
                continue
            attempts = self.store.list_attempts(item.work_item_id)
            if not attempts:
                continue
            attempt = attempts[-1]
            if attempt.execution_status != "succeeded":
                continue
            plan = attempt.metadata.get("export_plan")
            if not isinstance(plan, dict):
                continue
            existing = self.store.list_permission_requests(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
            )
            desktop_permissions = [
                request
                for request in existing
                if self._is_desktop_export_permission(request)
            ]
            if desktop_permissions and desktop_permissions[-1].status != "pending":
                continue
            try:
                outcome = self.export_service.discover_staged_exports(
                    attempt,
                    item,
                    plan,
                )
            except Exception:
                logger.exception(
                    "failed to recover staged Desktop export for %s",
                    attempt.attempt_id,
                )
                continue
            permission = outcome.get("permission")
            if not isinstance(permission, PermissionRequestRecord):
                continue
            prior_delta = (
                attempt.metadata.get("git_delta")
                if isinstance(attempt.metadata.get("git_delta"), dict)
                else {}
            )
            export_delta = {
                "available": bool(outcome.get("available")),
                "reason": str(outcome.get("reason") or "external_export_unavailable"),
                "changed_files": [str(value) for value in outcome.get("changed_files") or []],
                "untracked": [str(value) for value in outcome.get("changed_files") or []],
                "patch": str(outcome.get("patch") or ""),
                "ambiguous_paths": [],
                "conflicts": [],
                "pending_export": bool(outcome.get("pending_export")),
                "external_export_pending": bool(outcome.get("pending_export")),
                "recovery_required": bool(outcome.get("recovery_required")),
                "permission_request_id": permission.request_id,
                "artifact_type": "business.proposed_export",
                "baseline_head": prior_delta.get("baseline_head"),
                "current_head": prior_delta.get("current_head"),
            }
            self.store.update_attempt(
                attempt.attempt_id,
                metadata={
                    "export_delta": export_delta,
                    "git_delta": dict(export_delta),
                },
            )

    def _recover_authorized_exports(self) -> None:
        """Finish durable Desktop authorizations interrupted before commit."""

        for item in self.store.list_work_items(limit=1000):
            permissions = self.store.list_permission_requests(
                item.work_item_id,
                status="allowed",
            )
            for permission in permissions:
                if not self._is_desktop_export_permission(permission):
                    continue
                entries = permission.metadata.get("entries")
                if not isinstance(entries, list):
                    continue
                attempt = self.store.get_attempt(permission.attempt_id)
                if attempt is None:
                    continue
                committed = self.export_service.is_committed_export(permission, entries)
                # A committed journal is the durable no-replay receipt.  Do
                # not hash every historical Desktop artifact during startup;
                # a large ledger would otherwise make boot proportional to all
                # exported bytes.  Current integrity is checked lazily when
                # that attempt's Diff is explicitly opened.
                targets_intact = committed
                if committed:
                    export_state = attempt.metadata.get("export_resolution")
                    exported_paths = (
                        tuple(str(path) for path in export_state.get("exported_paths") or [])
                        if isinstance(export_state, dict)
                        else ()
                    )
                    resolution = ExportResolution(
                        permission=permission,
                        exported_paths=exported_paths,
                    )
                elif not self.export_service.can_resume_authorized(permission):
                    # Terminal non-committed receipts such as an explicitly
                    # abandoned recovery must never be resumed at startup.
                    continue
                else:
                    try:
                        resolution = self.export_service.resume_authorized(
                            permission.request_id,
                        )
                    except Exception as exc:
                        logger.exception(
                            "failed to recover authorized Desktop export %s",
                            permission.request_id,
                        )
                        self._mark_export_delta_resolved(
                            attempt.attempt_id,
                            status="failed",
                            reason="external_export_failed",
                        )
                        latest = self.store.latest_completion(item.work_item_id)
                        if not (
                            latest is not None
                            and latest.attempt_id == attempt.attempt_id
                            and latest.evidence.get("permission_request_id") == permission.request_id
                            and latest.evidence.get("resolution") == "export_recovery_failed"
                        ):
                            self.store.record_completion(
                                item.work_item_id,
                                CompletionDecision(
                                    execution_status=attempt.execution_status,
                                    completeness="partial",
                                    attention="conflict",
                                    work_item_state="open",
                                    rationale=(
                                        "The Desktop export was authorized but could not be "
                                        "recovered safely; no existing target was overwritten."
                                    ),
                                    terminal=True,
                                ),
                                attempt_id=attempt.attempt_id,
                                source="host",
                                evidence={
                                    "permission_request_id": permission.request_id,
                                    "resolution": "export_recovery_failed",
                                    "error": exc.__class__.__name__,
                                },
                            )
                        continue

                self._mark_export_delta_resolved(
                    attempt.attempt_id,
                    status="missing" if committed and not targets_intact else resolution.permission.status,
                    reason=(
                        "external_export_drift"
                        if committed and not targets_intact
                        else "external_export_complete"
                    ),
                )
                current_item = self.store.get_work_item(item.work_item_id) or item
                if (
                    attempt.execution_status in _ACTIVE_EXECUTION
                    or current_item.state in {"accepted", "archived"}
                ):
                    continue
                if self._auto_accept_approved_export(
                    request=permission,
                    resolved=resolution.permission,
                    attempt=attempt,
                    exported_paths=resolution.exported_paths,
                ):
                    continue
                latest = self.store.latest_completion(item.work_item_id)
                if committed and not targets_intact:
                    # A committed allow-once receipt is never replayed after a
                    # user deletes or edits the Desktop file.  Preserve an
                    # existing successful completion; if the process crashed
                    # before recording one, replace the stale permission state
                    # with an actionable conflict instead of silently copying.
                    if (
                        latest is not None
                        and latest.attempt_id == attempt.attempt_id
                        and latest.evidence.get("permission_request_id") == permission.request_id
                        and latest.attention == "review"
                    ):
                        continue
                    self.store.record_completion(
                        item.work_item_id,
                        CompletionDecision(
                            execution_status=attempt.execution_status,
                            completeness="partial",
                            attention="conflict",
                            work_item_state="open",
                            rationale=(
                                "The export transaction committed, but the Desktop artifact "
                                "is now missing or changed; allow-once was not replayed."
                            ),
                            terminal=True,
                        ),
                        attempt_id=attempt.attempt_id,
                        source="host",
                        evidence={
                            "permission_request_id": permission.request_id,
                            "resolution": "committed_export_drift",
                        },
                    )
                    continue
                if (
                    latest is not None
                    and latest.attempt_id == attempt.attempt_id
                    and latest.evidence.get("permission_request_id") == permission.request_id
                    and latest.attention == "review"
                    and latest.work_item_state == "review_ready"
                ):
                    continue
                self.store.record_completion(
                    item.work_item_id,
                    CompletionDecision(
                        execution_status=attempt.execution_status,
                        completeness="partial",
                        attention="review",
                        work_item_state="review_ready",
                        rationale=(
                            "The previously authorized staged deliverable was recovered, "
                            "verified, and exported to Desktop; user review is still required."
                        ),
                        terminal=True,
                    ),
                    attempt_id=attempt.attempt_id,
                    source="host",
                    evidence={
                        "permission_request_id": permission.request_id,
                        "resolution": "recovered_export",
                        "exported_paths": list(resolution.exported_paths),
                    },
                )

    def close(self) -> None:
        global _current_coordinator
        if self._subscribed:
            bus.off(Method.PROVIDER_EVENT, self._enqueue_provider_event)
            bus.off(Method.PROVIDER_RESULT, self._enqueue_provider_result)
            bus.off(Method.CHAT_WORK_NOTE_DELIVERED, self._on_terminal_work_notice_delivered)
            self._subscribed = False
        if _current_coordinator is self:
            _current_coordinator = None
        if self._provider_snapshot_task is not None:
            self._provider_snapshot_task.cancel()
            self._provider_snapshot_task = None
        if self._provider_fact_task is not None:
            self._provider_fact_task.cancel()
            self._provider_fact_task = None
        self.store.close()

    # -- Provider intake -------------------------------------------------

    def _latest_parent_context_delivery(
        self,
        work_item_id: str,
        session: ProviderSessionHandle | None,
    ) -> dict[str, str]:
        """Find the latest delivered cursor for this exact native Session.

        A later failed Attempt may inherit the same attachable handle without
        ever sending its prompt. Skip such planned attachments and retain the
        last adapter-acknowledged cursor instead.
        """

        if session is None or not str(work_item_id or "").strip():
            return {}
        for attempt in reversed(self.store.list_attempts(work_item_id)):
            receipt = validated_parent_context_delivery(
                attempt.metadata.get(PARENT_CONTEXT_DELIVERY_METADATA_KEY)
            )
            if not receipt:
                continue
            raw_session = attempt.metadata.get("provider_session")
            if not isinstance(raw_session, dict):
                result = (
                    attempt.metadata.get("provider_result")
                    if isinstance(attempt.metadata.get("provider_result"), dict)
                    else {}
                )
                raw_session = result.get("provider_session")
            if not isinstance(raw_session, dict):
                continue
            try:
                delivered_session = ProviderSessionHandle.from_dict(raw_session)
            except (TypeError, ValueError):
                continue
            if delivered_session == session:
                return receipt
        return {}

    def prepare_request(self, request: ProviderRunRequest) -> ProviderRunRequest:
        """Bind a new provider run to a WorkItem and a fresh RunAttempt.

        ``ProviderRuntime.start`` calls this hook for *all* start paths, not
        only WebSocket requests. A new user goal creates a WorkItem. A later
        instruction for that goal creates a WorkOperation; Retry and steer
        replacement create another Attempt for the same Operation.
        """

        original_task = str(request.task or "")
        # Provider Session attachment is a host/ledger decision.  Even a
        # correctly typed handle supplied by an intake caller cannot skip the
        # WorkItem/provider lineage checks below.
        request.session = None
        metadata = dict(request.metadata or {})
        # Presentation language is snapshotted per Attempt so every provider
        # uses the same reporting contract as the Slice that will display it.
        # It affects narration copy only; task authority and artifact content
        # remain exactly as supplied by the user/provider.
        if not str(metadata.get("presentation_locale") or "").strip():
            from server import presentation_runtime

            metadata["presentation_locale"] = presentation_runtime.get_presentation_locale()
        incoming_work = metadata.get("work") if isinstance(metadata.get("work"), dict) else {}
        declared_work_item_id = str(
            incoming_work.get("work_item_id")
            or incoming_work.get("workItemId")
            or metadata.get("work_item_id")
            or ""
        ).strip()
        legacy_amend_ref = str(metadata.get("related_work_item_id") or "").strip()
        intake_reference = resolve_intake_reference(
            continuation=str(metadata.get("continuation") or "new"),
            intent=str(metadata.get("intent") or ""),
            work_item_id=declared_work_item_id,
            related_work_item_id=legacy_amend_ref,
            related_work_item_exists=bool(
                legacy_amend_ref
                and self.store.get_work_item(legacy_amend_ref) is not None
            ),
        )
        work_item_id = intake_reference.work_item_id
        continuation = intake_reference.continuation
        if intake_reference.legacy_amend_promoted:
            metadata["continuation"] = "amend"
            metadata["legacy_amend_reference_promoted"] = True
        provider_manifest = (
            metadata.get("provider_manifest")
            if isinstance(metadata.get("provider_manifest"), dict)
            else {}
        )
        provider_capabilities = (
            provider_manifest.get("capabilities")
            if isinstance(provider_manifest.get("capabilities"), dict)
            else {}
        )
        manifest_provider = str(
            provider_manifest.get("provider_id") or ""
        ).strip().lower()
        request_provider = request.provider.strip().lower()
        if manifest_provider and manifest_provider != request_provider:
            raise WorkLedgerConflict(
                "provider manifest does not match the provider request"
            )
        workspace_ownership = str(
            provider_capabilities.get("workspace_ownership") or "none"
        ).strip().lower()
        workspace_authority = workspace_route_authority(
            workspace_ownership  # type: ignore[arg-type]
        )
        requested_workspace_access = str(
            request.requirements.workspace_access
            if request.requirements is not None
            else provider_capabilities.get("workspace_access") or "none"
        ).strip().lower()
        workspace_not_applicable = (
            bool(provider_manifest)
            and manifest_provider == request_provider
            and provider_manifest.get("declared") is True
            and workspace_authority == "not_applicable"
            and requested_workspace_access == "none"
        )
        host_routes_workspace = workspace_authority == "host"
        if host_routes_workspace and continuation not in EXISTING_ITEM_CONTINUATIONS:
            pinned = self.workspace_routing_focus()
            route_attrs = {**incoming_work, **metadata}
            if request.cwd:
                route_attrs["cwd"] = request.cwd
            stable_route_supplied = any(
                route_attrs.get(key) not in (None, "")
                for key in ("project_id", "projectId", "workspace_ref", "workspaceRef")
            )
            # A global lock is hard authority and a missing cwd needs safe
            # automatic resolution. A stable Project/workspace reference must
            # also be resolved even when cwd is explicit, so a caller cannot
            # pair Project A's identity with Project B's workspace path.
            if pinned.get("mode") == "pinned" or not request.cwd or stable_route_supplied:
                route = self.resolve_workspace_route(route_attrs)
                if route.get("status") != "resolved":
                    reason = str(route.get("reason") or "workspace_unresolved")
                    raise WorkLedgerConflict(
                        "Provider workspace could not be selected safely "
                        f"({reason}); lock a historical workspace or specify a project path"
                    )
                routed_workspace = str(route.get("cwd") or "")
                routed_project_id = str(route.get("projectId") or "")
                request.cwd = routed_workspace
                metadata["cwd"] = routed_workspace
                metadata["workspace_path"] = routed_workspace
                metadata["workspace_routing_source"] = str(route.get("source") or "")
                incoming_work = {
                    **incoming_work,
                    "workspace_path": routed_workspace,
                    **({"project_id": routed_project_id} if routed_project_id else {}),
                }
                metadata["work"] = incoming_work
        existing_item = self.store.get_work_item(work_item_id) if work_item_id else None
        if work_item_id and existing_item is None:
            raise WorkLedgerNotFound(f"unknown work item: {work_item_id}")
        existing_workspace_path = (
            str(existing_item.workspace_path or "") if existing_item is not None else ""
        )
        workspace_path = str(
            request.cwd
            or incoming_work.get("workspace_path")
            or metadata.get("cwd")
            or metadata.get("workspace_path")
            or metadata.get("project_path")
            # For an explicit continuation, the durable WorkItem owns the
            # workspace. Requiring every Provider/caller to repeat cwd creates
            # two competing authorities and breaks cross-Provider amendments.
            or existing_workspace_path
            # No Path.cwd() fallback: it resolved to the server's launch
            # directory, which is the user's own repository, so work that named
            # no destination was written into the one place it must not be.
            # Nothing reaches here without a workspace now -- routing ends at
            # the scratch root (_scratch_route) rather than running out of
            # options -- and if something ever does, refusing beats guessing.
            or ""
        )
        if not workspace_path and not workspace_not_applicable:
            raise WorkLedgerConflict(
                "no workspace could be determined for this request; "
                "refusing rather than defaulting to the current directory"
            )
        if workspace_not_applicable:
            # The Provider contract explicitly says this run has no filesystem
            # workspace.  Preserve that fact all the way through the ledger;
            # never substitute the server cwd just to satisfy an older row
            # shape, because Git and artifact discovery would then attribute
            # unrelated repository state to the run.
            request.cwd = None
            workspace_path = ""
            metadata.pop("cwd", None)
            metadata.pop("workspace_path", None)
            metadata["workspace_routing_source"] = "not_applicable"
        project_path = str(metadata.get("project_path") or workspace_path)
        project_id = str(
            incoming_work.get("project_id")
            or incoming_work.get("projectId")
            or metadata.get("project_id")
            or ""
        ).strip()
        workspace_mode = str(
            incoming_work.get("workspace_mode")
            or metadata.get("workspace_mode")
            or (existing_item.workspace_mode if existing_item is not None else "")
            or ("none" if workspace_not_applicable else "local")
        ).strip().lower() or ("none" if workspace_not_applicable else "local")
        if workspace_not_applicable:
            workspace_mode = "none"
        write_intent = self._request_has_write_intent(request, metadata)
        workspace_write_intent = write_intent and workspace_mode != "none"
        if (
            metadata.get("read_only") is True
            and request.requirements is not None
            and request.requirements.workspace_access == "write"
        ):
            raise WorkLedgerConflict(
                "read-only metadata conflicts with a workspace-write Provider contract"
            )
        if workspace_write_intent:
            requirements = request.requirements
            if requirements is not None:
                request.requirements = replace(
                    requirements,
                    workspace_access="write",
                )
            elif provider_manifest.get("declared") is True:
                request.requirements = ProviderRequirements(
                    task_kind="general",
                    workspace_access="write",
                    ownership=request.ownership,
                )
            # Undeclared compatibility adapters predate ProviderRequirements;
            # keep their legacy contract instead of inventing capabilities
            # that Runtime would correctly reject on its second compatibility
            # check. Declared Providers must never receive the split contract.
            if request.requirements is not None:
                metadata["provider_requirements"] = request.requirements.to_dict()
        session_id = str(
            metadata.get("session_id")
            or metadata.get("sessionId")
            or metadata.get("chat_session_id")
            or ""
        ).strip()
        if (
            str(metadata.get("source_user_text") or "").strip()
            and session_id
            and not str(metadata.get(SOURCE_CONTEXT_SCOPE_METADATA_KEY) or "").strip()
        ):
            metadata[SOURCE_CONTEXT_SCOPE_METADATA_KEY] = f"chat:{session_id}"

        continuation_lineage: dict[str, Any] = {}
        previous_attempt: RunAttemptRecord | None = None
        previous_attempts: list[RunAttemptRecord] = []
        if existing_item is not None:
            previous_attempts = self.store.list_attempts(existing_item.work_item_id)
            previous_attempt = previous_attempts[-1] if previous_attempts else None
            if (
                continuation in EXISTING_ITEM_CONTINUATIONS
                and not isinstance(metadata.get("host_outcome_requirement"), dict)
            ):
                inherited_requirement = self._latest_host_outcome_requirement(
                    previous_attempts
                )
                if inherited_requirement is not None:
                    # The required artifact shape belongs to the WorkItem, not
                    # to whichever wording happened to route its next amend.
                    # In particular an AUIP repair must remain one validated
                    # application bundle; otherwise its official SDK sidecars
                    # are misclassified as Provider prose and can exhaust the
                    # approval-preview budget before a permission card exists.
                    metadata["host_outcome_requirement"] = inherited_requirement
        recovery = request.recovery
        if recovery is not None:
            if not isinstance(recovery, ProviderRecoveryContext):
                raise WorkLedgerConflict("provider recovery context is invalid")
            if continuation != "retry" or existing_item is None or previous_attempt is None:
                raise WorkLedgerConflict(
                    "provider recovery requires a Retry of an existing latest Attempt"
                )
            if recovery.predecessor_attempt_id != previous_attempt.attempt_id:
                raise WorkLedgerConflict(
                    "provider recovery must reference the latest predecessor Attempt"
                )
            predecessor_completion = (
                previous_attempt.metadata.get("provider_completion")
                if isinstance(previous_attempt.metadata.get("provider_completion"), dict)
                else {}
            )
            if (
                recovery.root_attempt_id != previous_attempt.attempt_id
                or recovery.reason != "progress_only_completion"
                or predecessor_completion.get("classification")
                != "progress_only_completion"
                or predecessor_completion.get("recovery_state") != "claimed"
                or predecessor_completion.get("recovery_root_attempt_id")
                != previous_attempt.attempt_id
                or predecessor_completion.get("recovery_ordinal") != 1
            ):
                raise WorkLedgerConflict(
                    "provider recovery lineage is not authorized by the predecessor Attempt"
                )
        predecessor_key = (
            "retry_of" if continuation == "retry" else "replaces_attempt_id"
        )
        predecessor_id = (
            str(metadata.get(predecessor_key) or "").strip()
            if continuation in {"retry", "steer_replacement"}
            else ""
        )
        intake_plan = plan_work_intake(
            continuation=continuation,
            declared_intent=str(metadata.get("intent") or ""),
            existing_item=existing_item,
            previous_attempt=previous_attempt,
            request_provider=request.provider,
            request_mode=request.mode,
            predecessor_attempt_id=predecessor_id,
        )
        if intake_plan.previous_operation_id:
            metadata["previous_operation_id"] = intake_plan.previous_operation_id
        if existing_item is not None:
            request.cwd = existing_item.workspace_path or None
        if not intake_plan.creates_operation:
            assert existing_item is not None and previous_attempt is not None
            continuation_lineage = self._validate_continuation_instruction(
                existing_item,
                previous_attempt,
                request.task,
                metadata,
                label=intake_plan.lineage_label,
            )
            metadata.update(continuation_lineage)
        if existing_item is not None and continuation == "amend":
            self._supersede_pending_export_for_amend(existing_item, metadata)
        if existing_item is not None and self.store.list_permission_requests(
            existing_item.work_item_id,
            status="pending",
        ):
            raise WorkLedgerConflict(
                f"work item {existing_item.work_item_id} has a pending permission; resolve it before starting another attempt"
            )
        if existing_item is not None and any(
            self._is_desktop_export_permission(permission)
            and self.export_service.can_resume_authorized(permission)
            for permission in self.store.list_permission_requests(
                existing_item.work_item_id,
                status="allowed",
            )
        ):
            raise WorkLedgerConflict(
                f"work item {existing_item.work_item_id} has an interrupted authorized export; recover it before starting another attempt"
            )
        if existing_item is not None and any(
            attempt.execution_status in _ACTIVE_EXECUTION
            for attempt in self.store.list_attempts(existing_item.work_item_id)
        ):
            raise WorkLedgerConflict(
                f"work item {existing_item.work_item_id} already has an active attempt; use Resume or wait"
            )
        provider_session = resolve_provider_session_attachment(
            has_existing_item=existing_item is not None,
            previous_attempt=previous_attempt,
            continuation=continuation,
            provider_capabilities=provider_capabilities,
            request_provider=request_provider,
            recovery_reason=(recovery.reason if recovery is not None else ""),
        )
        request.session = provider_session.session
        provider_session_attach = provider_session.audit
        source_context = str(metadata.get("source_user_context") or "")
        previous_delivery = self._latest_parent_context_delivery(
            str(existing_item.work_item_id if existing_item is not None else ""),
            request.session,
        )
        delivered_context, context_mode = parent_conversation_context_delivery(
            source_context,
            source_scope=str(metadata.get(SOURCE_CONTEXT_SCOPE_METADATA_KEY) or ""),
            previous_delivery=previous_delivery,
            continuity_verified=request.session is not None,
        )
        if delivered_context:
            metadata["source_user_context"] = delivered_context
        else:
            metadata.pop("source_user_context", None)
        metadata["source_context_mode"] = context_mode
        if previous_delivery and request.session is not None:
            base_turn_id = str(previous_delivery.get("source_turn_id") or "").strip()
            if base_turn_id:
                metadata["source_context_base_turn_id"] = base_turn_id[:200]
        ensured_workspace: dict[str, Any] | None = None
        work_item_id_for_create = ""
        # The container needs turning into a real per-task directory; a draft
        # directory already is one. Testing for any scratch path instead of the
        # container itself sent same-session continuation -- the host resolves
        # the earlier draft and hands back its directory -- into a fresh empty
        # one, which is exactly the behaviour drafts exist to provide.
        routed_to_scratch = (
            str(metadata.get("workspace_routing_source") or "") == "scratch_default"
            or is_scratch_root(workspace_path)
        )
        if existing_item is None and workspace_write_intent and routed_to_scratch:
            # Give this task its own repository under the scratch root. Sharing
            # one directory would let two unrelated one-offs overwrite each
            # other, and would leave nothing separable to promote later.
            work_item_id_for_create = new_ledger_id("work")
            try:
                scratch_cwd = create_scratch_workspace(
                    self._task_title(request.task),
                    unique_id=work_item_id_for_create,
                )
            except ScratchUnavailable as exc:
                raise WorkLedgerConflict(
                    f"scratch workspace could not be created ({exc}); "
                    "refusing rather than writing into a real project"
                ) from exc
            workspace_path = str(scratch_cwd)
            request.cwd = workspace_path
            metadata["cwd"] = workspace_path
            metadata["workspace_path"] = workspace_path
        elif (
            existing_item is None
            and workspace_write_intent
            and workspace_mode not in {"worktree", "none"}
            and bool(app_settings.WORK_WORKTREE_ISOLATION)
        ):
            # Scratch tasks skip this: each already owns a private repository,
            # so carving another worktree would isolate what is already isolated.
            work_item_id_for_create = new_ledger_id("work")
            provisioner = (
                self.workspace_provisioner
                or workspace_provisioner.ensure_workspace
            )
            try:
                envelope = provisioner(
                    work_item_external_id=work_item_id_for_create,
                    project_cwd=project_path,
                    policy="worktree",
                    name=self._task_title(request.task),
                )
            except workspace_provisioner.WorkspaceProvisioningError as exc:
                # R11: worktree isolation never falls back silently to the
                # shared project directory; the write task is refused.
                raise WorkLedgerConflict(
                    "worktree isolation is enabled but workspace ensure "
                    f"failed ({exc.code or 'error'}): {exc}"
                ) from exc
            ensured = (
                envelope.get("workspace")
                if isinstance(envelope.get("workspace"), dict)
                else {}
            )
            ensured_cwd = str(ensured.get("cwd") or "").strip()
            if not ensured_cwd:
                raise WorkLedgerConflict(
                    "workspace ensure returned no cwd; refusing to start the write task"
                )
            ensured_workspace = {
                "external_id": work_item_id_for_create,
                "allocation_id": str(ensured.get("allocationId") or ""),
                "backend": str(ensured.get("backend") or ""),
                "policy": str(ensured.get("policy") or ""),
                "base_ref": str(ensured.get("baseRef") or ""),
            }
            workspace_mode = (
                "worktree" if ensured_workspace["policy"] == "worktree" else "local"
            )
            workspace_path = ensured_cwd
            request.cwd = ensured_cwd
            metadata["cwd"] = ensured_cwd
            metadata["workspace_path"] = ensured_cwd
            if ensured.get("gitBranch"):
                metadata["branch"] = str(ensured.get("gitBranch") or "")
        self._assert_workspace_available(
            existing_item.workspace_path if existing_item is not None else workspace_path,
            write_intent=workspace_write_intent,
        )
        if existing_item is not None:
            if existing_item.state == "archived" or (
                existing_item.state == "accepted" and continuation != "amend"
            ):
                raise WorkLedgerConflict(
                    f"work item {existing_item.work_item_id} must be reopened before {continuation}"
                )
            item = existing_item
            project = self.store.get_project(item.project_id)
            if project is None:  # pragma: no cover - protected by FK
                raise WorkLedgerNotFound(f"unknown project: {item.project_id}")
        else:
            if workspace_mode == "none":
                project = self._workspace_less_project()
            elif project_id:
                project = self.store.get_project(project_id)
                if project is None:
                    raise WorkLedgerNotFound(f"unknown project: {project_id}")
            else:
                project = self.store.create_or_get_project(
                    self._project_root_for(project_path)
                )
            item = self.store.create_work_item(
                project.project_id,
                title=self._task_title(request.task),
                goal=request.task,
                workspace_mode=workspace_mode,
                workspace_path=workspace_path,
                branch=str(metadata.get("branch") or ""),
                base_revision=str(metadata.get("base_revision") or ""),
                work_item_id=work_item_id_for_create,
                metadata={
                    "source": str(metadata.get("source") or "provider_runtime"),
                    **(
                        {"source_user_text": str(metadata["source_user_text"])[:4000]}
                        if str(metadata.get("source_user_text") or "").strip()
                        else {}
                    ),
                    **({"session_id": session_id} if session_id else {}),
                    **(
                        {"intent": str(metadata.get("intent") or "execute")}
                        if metadata.get("intent") not in (None, "")
                        else {}
                    ),
                    **(
                        {"focus_applied": True}
                        if metadata.get("focus_applied") is True
                        else {}
                    ),
                    **(
                        {"amend_inferred": True}
                        if metadata.get("amend_inferred") is True
                        else {}
                    ),
                    **(
                        {
                            "related_work_item_id": str(
                                metadata.get("related_work_item_id") or ""
                            ).strip()
                        }
                        if str(metadata.get("related_work_item_id") or "").strip()
                        else {}
                    ),
                    "workspace_policy": self._workspace_policy(
                        workspace_mode=workspace_mode,
                        write_intent=workspace_write_intent,
                        worktree_requested=metadata.get("useWorktree") is True,
                        ensured=ensured_workspace,
                    ),
                    **(
                        {"workspace_allocation": ensured_workspace}
                        if ensured_workspace
                        else {}
                    ),
                },
            )
            # One line per new task saying where it went and why. Naming a
            # project is now the only thing keeping work out of the scratch
            # area, so the ratio between these two branches is what says
            # whether the model names one when it means one.
            logger.info(
                "[WORK-DESTINATION] branch=%s provider=%s named_project=%s work_item=%s",
                (
                    "none"
                    if workspace_mode == "none"
                    else "scratch" if is_scratch_path(workspace_path) else "project"
                ),
                request.provider.strip().lower(),
                bool(project_id),
                item.work_item_id,
            )

        attempt_metadata = {
            "source": str(metadata.get("source") or "provider_runtime"),
            "presentation_locale": str(metadata.get("presentation_locale") or "en-US"),
            **(
                {"turn_id": str(metadata["turn_id"])[:200]}
                if str(metadata.get("turn_id") or "").strip()
                else {}
            ),
            **(
                {"source_user_text": str(metadata["source_user_text"])[:4000]}
                if str(metadata.get("source_user_text") or "").strip()
                else {}
            ),
            "source_context_mode": str(metadata.get("source_context_mode") or "none"),
            **(
                {
                    SOURCE_CONTEXT_SCOPE_METADATA_KEY: str(
                        metadata[SOURCE_CONTEXT_SCOPE_METADATA_KEY]
                    )[:800]
                }
                if str(metadata.get(SOURCE_CONTEXT_SCOPE_METADATA_KEY) or "").strip()
                else {}
            ),
            **(
                {
                    "source_context_base_turn_id": str(
                        metadata["source_context_base_turn_id"]
                    )[:200]
                }
                if str(metadata.get("source_context_base_turn_id") or "").strip()
                else {}
            ),
            "continuation": continuation,
            "write_intent": write_intent,
            "provider_ownership": str(
                metadata.get("provider_ownership") or request.ownership or "managed"
            ),
            **(
                {"provider_requirements": dict(metadata["provider_requirements"])}
                if isinstance(metadata.get("provider_requirements"), dict)
                else {}
            ),
            **(
                {"provider_selection": dict(metadata["provider_selection"])}
                if isinstance(metadata.get("provider_selection"), dict)
                else {}
            ),
            **(
                {
                    "host_outcome_requirement": dict(
                        metadata["host_outcome_requirement"]
                    )
                }
                if isinstance(metadata.get("host_outcome_requirement"), dict)
                else {}
            ),
            **({"session_id": session_id} if session_id else {}),
            **(
                {"intent": str(metadata.get("intent") or "execute")}
                if metadata.get("intent") not in (None, "")
                else {}
            ),
            **(
                {"focus_applied": True}
                if metadata.get("focus_applied") is True
                else {}
            ),
            **(
                {"amend_inferred": True}
                if metadata.get("amend_inferred") is True
                else {}
            ),
            **(
                {"project_source_amend": True}
                if metadata.get("project_source_amend") is True
                else {}
            ),
            **(
                {
                    "related_work_item_id": str(
                        metadata.get("related_work_item_id") or ""
                    ).strip()
                }
                if str(metadata.get("related_work_item_id") or "").strip()
                else {}
            ),
            **continuation_lineage,
            **(
                {"provider_recovery": recovery.to_dict()}
                if recovery is not None
                else {}
            ),
            **(
                {"provider_session": request.session.to_dict()}
                if request.session is not None
                else {}
            ),
            **(
                {"provider_session_attach": provider_session_attach}
                if provider_session_attach
                else {}
            ),
            **(
                {
                    "replaces_attempt_id": previous_attempt.attempt_id,
                    "steer_replacement": dict(metadata["steer_replacement"]),
                }
                if continuation == "steer_replacement"
                and isinstance(metadata.get("steer_replacement"), dict)
                else {}
            ),
        }
        operation_metadata: dict[str, Any] = {}
        if intake_plan.creates_operation:
            operation_metadata = {
                "source": str(metadata.get("source") or "provider_runtime"),
                **(
                    {"turn_id": str(metadata["turn_id"])[:200]}
                    if str(metadata.get("turn_id") or "").strip()
                    else {}
                ),
                **(
                    {"source_user_text": str(metadata["source_user_text"])[:4000]}
                    if str(metadata.get("source_user_text") or "").strip()
                    else {}
                ),
                **({"session_id": session_id} if session_id else {}),
                **(
                    {"previous_operation_id": str(metadata["previous_operation_id"])}
                    if str(metadata.get("previous_operation_id") or "").strip()
                    else {}
                ),
                **(
                    {"amend_inferred": True}
                    if metadata.get("amend_inferred") is True
                    else {}
                ),
                **(
                    {"project_source_amend": True}
                    if metadata.get("project_source_amend") is True
                    else {}
                ),
            }
        operation, attempt = persist_work_intake(
            self.store,
            item_id=item.work_item_id,
            plan=intake_plan,
            original_task=original_task,
            provider_task=request.task,
            provider=request.provider,
            mode=request.mode,
            operation_metadata=operation_metadata,
            attempt_metadata=attempt_metadata,
        )
        if existing_item is not None and continuation == "steer_replacement":
            successor_control = {
                **(
                    dict(metadata.get("steer_replacement") or {})
                    if isinstance(metadata.get("steer_replacement"), dict)
                    else {}
                ),
                "state": "prepared",
                "successor_attempt_id": attempt.attempt_id,
            }
            metadata["steer_replacement"] = successor_control
            self.store.update_attempt(
                attempt.attempt_id,
                metadata={"steer_replacement": successor_control},
            )
        lease = None
        if workspace_write_intent:
            try:
                lease = self.store.acquire_writer_lease(
                    item.work_item_id,
                    attempt.attempt_id,
                    workspace_path=item.workspace_path,
                    metadata={"provider": request.provider, "mode": request.mode},
                )
            except WorkLedgerConflict as exc:
                self.store.update_attempt(
                    attempt.attempt_id,
                    execution_status="cancelled",
                    error=str(exc),
                    metadata={"start_rejected": "writer_lease_conflict"},
                )
                self.store.record_completion(
                    item.work_item_id,
                    CompletionDecision(
                        execution_status="cancelled",
                        completeness="incomplete",
                        attention="conflict",
                        work_item_state="open",
                        rationale=str(exc),
                        terminal=True,
                    ),
                    attempt_id=attempt.attempt_id,
                    source="host",
                    evidence={"writer_lease_conflict": True},
                )
                if existing_item is not None and continuation == "steer_replacement":
                    previous_control = (
                        previous_attempt.metadata.get("steer_replacement")
                        if isinstance(previous_attempt.metadata.get("steer_replacement"), dict)
                        else {}
                    )
                    failed_activity = project_host_steering(
                        (
                            previous_attempt.metadata.get(ACTIVITY_METADATA_KEY)
                            if isinstance(
                                previous_attempt.metadata.get(ACTIVITY_METADATA_KEY),
                                dict,
                            )
                            else {}
                        ),
                        state="failed",
                        revision=max(1, int(previous_control.get("revision") or 0)),
                        observed_at=float(self._clock()),
                        predecessor_attempt_id=previous_attempt.attempt_id,
                        successor_attempt_id=attempt.attempt_id,
                        reason="writer_lease_conflict",
                    )
                    self.store.update_attempt(
                        previous_attempt.attempt_id,
                        metadata={
                            "steer_replacement": {
                                **previous_control,
                                "state": "failed",
                                "successor_attempt_id": attempt.attempt_id,
                                "reason": "writer_lease_conflict",
                            },
                            ACTIVITY_METADATA_KEY: failed_activity,
                        },
                    )
                raise
        if existing_item is not None and continuation == "steer_replacement":
            successor_control = {
                **(
                    dict(metadata.get("steer_replacement") or {})
                    if isinstance(metadata.get("steer_replacement"), dict)
                    else {}
                ),
                "state": "restarted",
                "successor_attempt_id": attempt.attempt_id,
            }
            metadata["steer_replacement"] = successor_control
            self.store.update_attempt(
                attempt.attempt_id,
                metadata={"steer_replacement": successor_control},
            )
            previous_control = (
                previous_attempt.metadata.get("steer_replacement")
                if isinstance(previous_attempt.metadata.get("steer_replacement"), dict)
                else {}
            )
            replacement_revision = max(
                1,
                int(previous_control.get("revision") or 0),
                int(successor_control.get("revision") or 0),
            )
            previous_activity = (
                previous_attempt.metadata.get(ACTIVITY_METADATA_KEY)
                if isinstance(previous_attempt.metadata.get(ACTIVITY_METADATA_KEY), dict)
                else {}
            )
            replaced_activity = project_host_steering(
                previous_activity,
                state="replaced",
                revision=replacement_revision,
                observed_at=float(self._clock()),
                safe_boundary="confirmed_cancel_then_restart",
                predecessor_attempt_id=previous_attempt.attempt_id,
                successor_attempt_id=attempt.attempt_id,
            )
            self.store.update_attempt(
                previous_attempt.attempt_id,
                metadata={
                    "steer_replacement": {
                        **previous_control,
                        "state": "replaced",
                        "successor_attempt_id": attempt.attempt_id,
                    },
                    ACTIVITY_METADATA_KEY: replaced_activity,
                },
            )
        work_binding = {
            "project_id": project.project_id,
            "work_item_id": item.work_item_id,
            "operation_id": operation.operation_id,
            "operation_number": operation.operation_number,
            "attempt_id": attempt.attempt_id,
            "attempt_number": attempt.attempt_number,
            "attempt_epoch": attempt.attempt_number,
            "workspace_mode": item.workspace_mode,
            "workspace_path": item.workspace_path,
            "branch": item.branch,
        }
        if lease is not None:
            work_binding["writer_lease_id"] = lease.lease_id
        metadata["work"] = work_binding
        metadata.pop("work_item_id", None)
        if session_id:
            self.store.set_session_active_work_item(
                session_id,
                item.work_item_id,
                metadata={
                    "source": "provider_intake",
                    "operation_id": operation.operation_id,
                },
            )
        try:
            export_plan = self.export_service.prepare_plan(
                provider=request.provider,
                mode=request.mode,
                task=original_task,
                item=item,
                attempt=attempt,
                metadata=metadata,
                provider_capabilities=provider_capabilities or None,
            )
        except Exception:
            if lease is not None:
                self.store.release_writer_lease(attempt.attempt_id, status="released")
            self.store.update_attempt(
                attempt.attempt_id,
                execution_status="cancelled",
                error="failed to prepare the bounded external export",
                metadata={"start_rejected": "export_plan_error"},
            )
            raise
        if export_plan is not None:
            requirement = (
                metadata.get("host_outcome_requirement")
                if isinstance(metadata.get("host_outcome_requirement"), dict)
                else {}
            )
            if (
                str(requirement.get("facet") or "").strip().lower()
                == "auip.application"
            ):
                try:
                    runtime_assets = materialize_auip_runtime_assets(
                        Path(str(export_plan["staging_root"]))
                    )
                except OSError:
                    if lease is not None:
                        self.store.release_writer_lease(
                            attempt.attempt_id,
                            status="released",
                        )
                    self.store.update_attempt(
                        attempt.attempt_id,
                        execution_status="cancelled",
                        error="failed to materialize AUIP runtime assets",
                        metadata={"start_rejected": "auip_runtime_asset_error"},
                    )
                    raise
                export_plan["host_materialized_files"] = sorted(runtime_assets)
                export_plan["host_materialized_assets"] = {
                    filename: {
                        "sha256": str(identity["sha256"]),
                        "size_bytes": Path(str(identity["path"])).stat().st_size,
                    }
                    for filename, identity in runtime_assets.items()
                }
                export_plan["host_validates_auip_bundle"] = True
            metadata["display_task"] = original_task
            metadata["export_plan"] = export_plan
            request.task = self.export_service.provider_prompt(original_task, export_plan)
            self.store.update_attempt(
                attempt.attempt_id,
                metadata={
                    "original_task": original_task,
                    "export_plan": export_plan,
                },
            )
        provider_task_kinds = {
            str(kind or "").strip().lower()
            for kind in (provider_capabilities.get("task_kinds") or [])
        }
        supports_workspace_authoring = (
            str(provider_capabilities.get("workspace_access") or "none") == "write"
            and "workspace_mutation" in provider_task_kinds
        )
        authoring_requirement = (
            metadata.get("host_outcome_requirement")
            if isinstance(metadata.get("host_outcome_requirement"), dict)
            else {}
        )
        requires_auip_authoring = (
            str(authoring_requirement.get("facet") or "").strip().lower()
            == "auip.application"
        )
        if supports_workspace_authoring and requires_auip_authoring:
            try:
                workspace_root = Path(item.workspace_path).resolve()
                host_manages_runtime_assets = bool(
                    export_plan is not None
                    and export_plan.get("host_validates_auip_bundle") is True
                )
                if not host_manages_runtime_assets:
                    runtime_assets = materialize_auip_runtime_assets(
                        workspace_root,
                        replace_existing=False,
                    )
                    metadata["auip_bundle_root"] = str(workspace_root)
                    metadata["auip_host_validates_bundle"] = True
                    metadata["auip_host_materialized_files"] = sorted(runtime_assets)
                    metadata["auip_host_materialized_assets"] = {
                        filename: {
                            "sha256": str(identity["sha256"]),
                            "size_bytes": Path(str(identity["path"])).stat().st_size,
                        }
                        for filename, identity in runtime_assets.items()
                    }
                    host_manages_runtime_assets = True
                    self.store.update_attempt(
                        attempt.attempt_id,
                        metadata={
                            "auip_bundle_root": metadata["auip_bundle_root"],
                            "auip_host_validates_bundle": True,
                            "auip_host_materialized_files": metadata[
                                "auip_host_materialized_files"
                            ],
                            "auip_host_materialized_assets": metadata[
                                "auip_host_materialized_assets"
                            ],
                        },
                    )
                authoring_root = self.export_service.ensure_private_workspace_child(
                    workspace_root,
                    "runtime",
                    "authoring_inputs",
                    attempt.attempt_id,
                )
                skill_path = stage_auip_authoring_bundle(
                    authoring_root,
                    include_opaque_dependencies=not host_manages_runtime_assets,
                )
                metadata["auip_authoring_skill_path"] = str(skill_path)
                metadata["auip_authoring_inputs"] = auip_authoring_bundle_metrics(
                    skill_path
                )
                metadata["auip_authoring_bundle_mode"] = (
                    "lean_host_managed"
                    if host_manages_runtime_assets
                    else "full"
                )
                self.store.update_attempt(
                    attempt.attempt_id,
                    metadata={
                        "auip_authoring_skill_path": metadata[
                            "auip_authoring_skill_path"
                        ],
                        "auip_authoring_inputs": metadata["auip_authoring_inputs"],
                        "auip_authoring_bundle_mode": metadata[
                            "auip_authoring_bundle_mode"
                        ],
                    },
                )
                logger.info(
                    "[AUIP-AUTHORING] mode=%s staged_files=%d staged_bytes=%d "
                    "required_read_files=%d required_read_bytes=%d",
                    str(metadata["auip_authoring_bundle_mode"]),
                    int(metadata["auip_authoring_inputs"]["staged_file_count"]),
                    int(metadata["auip_authoring_inputs"]["staged_bytes"]),
                    int(
                        metadata["auip_authoring_inputs"][
                            "required_read_file_count"
                        ]
                    ),
                    int(metadata["auip_authoring_inputs"]["required_read_bytes"]),
                )
            except (OSError, WorkLedgerConflict):
                if lease is not None:
                    self.store.release_writer_lease(
                        attempt.attempt_id,
                        status="released",
                    )
                self.store.update_attempt(
                    attempt.attempt_id,
                    execution_status="cancelled",
                    error="failed to stage the AUIP authoring contract",
                    metadata={"start_rejected": "auip_authoring_bundle_error"},
                )
                raise
        request.metadata = metadata

        focus_surfaces = [self.default_surface]
        request_surface = str(metadata.get("work_surface") or "").strip()
        if request_surface and request_surface not in focus_surfaces:
            focus_surfaces.append(request_surface)
        for surface in focus_surfaces:
            focus = self.store.get_focus(surface)
            if focus is None or focus.mode == "auto" or not focus.work_item_id:
                self.store.set_focus(surface, item.work_item_id, mode="auto")
        self._emit_snapshot_now(self.default_surface, reason="attempt.created")
        return request

    @staticmethod
    def _latest_host_outcome_requirement(
        attempts: list[RunAttemptRecord],
    ) -> dict[str, Any] | None:
        """Carry the latest Host-owned result contract across one WorkItem."""

        for candidate in reversed(attempts):
            requirement = (
                candidate.metadata.get("host_outcome_requirement")
                if isinstance(candidate.metadata.get("host_outcome_requirement"), dict)
                else None
            )
            if not requirement:
                continue
            operation = str(requirement.get("operation") or "").strip()
            facet = str(requirement.get("facet") or "").strip()
            if operation and facet:
                return dict(requirement)
        return None

    def begin_steer_replacement(
        self,
        work_item_id: str,
        *,
        provider_run_id: str,
        amendment_text: str,
    ) -> dict[str, Any]:
        """Record the intent to replace one active, non-steerable Attempt.

        This does not cancel anything.  The caller must first verify the live
        Runtime record and provider capability, then call Runtime.cancel.  A
        replacement request can therefore never manufacture a second writer
        merely because a ledger row looked active.
        """

        item = self.store.get_work_item(str(work_item_id or "").strip())
        if item is None:
            raise WorkLedgerNotFound(f"unknown work item: {work_item_id}")
        attempts = self.store.list_attempts(item.work_item_id)
        previous_attempt = attempts[-1] if attempts else None
        if previous_attempt is None or previous_attempt.execution_status not in _ACTIVE_EXECUTION:
            raise WorkLedgerConflict("the amendment target no longer has an active attempt")
        clean_run_id = str(provider_run_id or "").strip()
        if not clean_run_id or previous_attempt.provider_run_id != clean_run_id:
            raise WorkLedgerConflict("the active Runtime run does not match the ledger attempt")
        clean_amendment = str(amendment_text or "").strip()
        if not clean_amendment:
            raise WorkLedgerConflict("steer replacement requires a non-empty amendment")
        current_control = (
            previous_attempt.metadata.get("steer_replacement")
            if isinstance(previous_attempt.metadata.get("steer_replacement"), dict)
            else {}
        )
        if str(current_control.get("state") or "") == "cancel_pending":
            raise WorkLedgerConflict("a steer replacement is already waiting for cancellation")
        base_instruction, lineage = self.retry_instruction(
            item,
            previous_attempt,
            amendment_text=clean_amendment,
        )
        current_activity = (
            previous_attempt.metadata.get(ACTIVITY_METADATA_KEY)
            if isinstance(previous_attempt.metadata.get(ACTIVITY_METADATA_KEY), dict)
            else {}
        )
        current_steering = (
            current_activity.get("steering")
            if isinstance(current_activity.get("steering"), dict)
            else {}
        )
        revision = max(
            int(current_control.get("revision") or 0),
            int(current_steering.get("revision") or 0),
        ) + 1
        requested_at = float(self._clock())
        control = {
            "state": "cancel_pending",
            "revision": revision,
            "predecessor_attempt_id": previous_attempt.attempt_id,
            "predecessor_run_id": previous_attempt.provider_run_id,
            "requested_at": requested_at,
            "amendment": clean_amendment[:_MAX_AMENDMENT_TEXT],
        }
        activity = project_host_steering(
            current_activity,
            state="cancel_pending",
            revision=revision,
            observed_at=requested_at,
            safe_boundary="confirmed_cancel_then_restart",
            predecessor_attempt_id=previous_attempt.attempt_id,
        )
        self.store.update_attempt(
            previous_attempt.attempt_id,
            metadata={
                "steer_replacement": control,
                ACTIVITY_METADATA_KEY: activity,
            },
        )
        return {
            "work_item_id": item.work_item_id,
            "project_id": item.project_id,
            "workspace_path": item.workspace_path,
            "workspace_mode": item.workspace_mode,
            "provider": previous_attempt.provider,
            "mode": previous_attempt.mode,
            "predecessor_attempt_id": previous_attempt.attempt_id,
            "predecessor_run_id": previous_attempt.provider_run_id,
            "instruction": self._steer_replacement_prompt(
                base_instruction,
                previous_attempt.attempt_id,
            ),
            "lineage": lineage,
            "control": control,
        }

    def active_attempt_for_item(self, work_item_id: str) -> RunAttemptRecord | None:
        """Return only the latest attempt when it is still the active writer."""

        attempts = self.store.list_attempts(str(work_item_id or "").strip())
        latest = attempts[-1] if attempts else None
        if latest is None or latest.execution_status not in _ACTIVE_EXECUTION:
            return None
        return latest

    def continuation_routing_facts(self, work_item_id: str) -> dict[str, Any]:
        """Return bounded host facts used to compile a continuation route.

        This deliberately exposes neither native Provider Session ids nor
        task prose.  Selection only needs to know whether the target owns a
        filesystem workspace and which Provider most recently served it as a
        soft continuity preference.
        """

        clean_id = str(work_item_id or "").strip()
        item = self.store.get_work_item(clean_id) if clean_id else None
        if item is None:
            return {}
        attempts = self.store.list_attempts(item.work_item_id)
        latest = attempts[-1] if attempts else None
        return {
            "work_item_id": item.work_item_id,
            "workspace_mode": item.workspace_mode,
            "provider": str(latest.provider or "").strip().lower() if latest else "",
        }

    def reject_steer_replacement(
        self,
        attempt_id: str,
        *,
        reason: str,
    ) -> None:
        """Return an unconfirmed replacement to a truthful running state."""

        attempt = self.store.get_attempt(attempt_id)
        if attempt is None:
            return
        control = (
            attempt.metadata.get("steer_replacement")
            if isinstance(attempt.metadata.get("steer_replacement"), dict)
            else {}
        )
        revision = max(1, int(control.get("revision") or 0))
        observed_at = float(self._clock())
        activity = project_host_steering(
            (
                attempt.metadata.get(ACTIVITY_METADATA_KEY)
                if isinstance(attempt.metadata.get(ACTIVITY_METADATA_KEY), dict)
                else {}
            ),
            state="rejected",
            revision=revision,
            observed_at=observed_at,
            predecessor_attempt_id=attempt.attempt_id,
            reason=reason,
        )
        self.store.update_attempt(
            attempt.attempt_id,
            metadata={
                "steer_replacement": {
                    **control,
                    "state": "rejected",
                    "reason": str(reason or "")[:240],
                    "resolved_at": observed_at,
                },
                "provider_liveness": {
                    "state": "active",
                    "stage": "steer_rejected",
                    "reason": str(reason or "")[:240],
                    "observed_at": observed_at,
                },
                ACTIVITY_METADATA_KEY: activity,
            },
        )

    def retry_instruction(
        self,
        item: WorkItemRecord,
        previous_attempt: RunAttemptRecord,
        amendment_text: Any = "",
        authorization_permission_request_id: Any = "",
    ) -> tuple[str, dict[str, Any]]:
        """Build one host-authenticated Retry instruction and lineage payload."""

        if amendment_text is None:
            raw = ""
        elif isinstance(amendment_text, str):
            raw = amendment_text
        else:
            raise ValueError("amendment_text must be a string")
        if len(raw) > _MAX_AMENDMENT_TEXT:
            raise ValueError(f"amendment_text exceeds {_MAX_AMENDMENT_TEXT} characters")

        request_id = str(authorization_permission_request_id or "").strip()
        if len(request_id) > 240:
            raise WorkLedgerConflict("authorization permission identity is invalid")
        if request_id and raw.strip():
            raise WorkLedgerConflict(
                "a permission-authorized Retry cannot include a separate correction"
            )

        previous = self._amendment_history(previous_attempt.metadata.get("amendments"))
        authorization = None
        if request_id:
            authorization = self._retry_authorization_permission(
                item,
                previous_attempt,
                request_id,
            )
            raw = self._permission_retry_authorization_text(authorization)
        clean = raw.strip()
        if not clean:
            lineage: dict[str, Any] = {}
            if previous:
                lineage.update(
                    {
                        "amendments": previous,
                        "interventions": self._amendment_interventions(previous),
                    }
                )
            return previous_attempt.task, lineage

        created_at = datetime.fromtimestamp(float(self._clock()), tz=timezone.utc).isoformat(
            timespec="seconds"
        )
        entry = {
            "number": len(previous) + 1,
            "created_at": created_at,
            "text": clean,
            "amended_from": previous_attempt.attempt_id,
        }
        if authorization is not None:
            entry["authorization_permission_request_id"] = authorization.request_id
        amendments = [*previous, entry]
        instruction = self._compose_amended_instruction(item.goal, amendments)
        lineage = {
            "amended_from": previous_attempt.attempt_id,
            "amendments": amendments,
            "interventions": self._amendment_interventions(amendments),
        }
        if authorization is not None:
            lineage["authorization_permission_request_id"] = authorization.request_id
        return instruction, lineage

    def _validate_continuation_instruction(
        self,
        item: WorkItemRecord,
        previous_attempt: RunAttemptRecord,
        instruction: str,
        metadata: dict[str, Any],
        *,
        label: str,
    ) -> dict[str, Any]:
        previous = self._amendment_history(previous_attempt.metadata.get("amendments"))
        incoming = self._amendment_history(metadata.get("amendments"))
        if incoming == previous:
            if instruction != previous_attempt.task:
                raise WorkLedgerConflict(
                    f"{label} cannot change the instruction without a bounded amendment"
                )
            if not incoming:
                return {}
            return {
                "amendments": incoming,
                "interventions": self._amendment_interventions(incoming),
            }

        if len(incoming) != len(previous) + 1 or incoming[:-1] != previous:
            raise WorkLedgerConflict(
                f"{label} amendment history must extend the latest attempt"
            )
        added = incoming[-1]
        if str(added.get("amended_from") or "") != previous_attempt.attempt_id:
            raise WorkLedgerConflict(
                f"{label} amendment must reference the latest predecessor attempt"
            )
        if str(metadata.get("amended_from") or "") != previous_attempt.attempt_id:
            raise WorkLedgerConflict(f"{label} amendment lineage is missing amended_from")
        authorization_id = str(
            added.get("authorization_permission_request_id") or ""
        ).strip()
        supplied_authorization_id = str(
            metadata.get("authorization_permission_request_id") or ""
        ).strip()
        if authorization_id:
            authorization = self._retry_authorization_permission(
                item,
                previous_attempt,
                authorization_id,
            )
            if supplied_authorization_id != authorization_id:
                raise WorkLedgerConflict(
                    f"permission-authorized {label} identity is missing"
                )
            if str(added.get("text") or "") != self._permission_retry_authorization_text(
                authorization
            ):
                raise WorkLedgerConflict(
                    "permission-authorized Retry text does not match the ledger request"
                )
        elif supplied_authorization_id:
            raise WorkLedgerConflict("permission-authorized Retry lineage is invalid")
        expected = self._compose_amended_instruction(item.goal, incoming)
        if label == "steer replacement":
            expected = self._steer_replacement_prompt(
                expected,
                previous_attempt.attempt_id,
            )
        if instruction != expected:
            raise WorkLedgerConflict(
                f"{label} amendment instruction does not match its lineage"
            )
        lineage = {
            "amended_from": previous_attempt.attempt_id,
            "amendments": incoming,
            "interventions": self._amendment_interventions(incoming),
        }
        if authorization_id:
            lineage["authorization_permission_request_id"] = authorization_id
        return lineage

    @staticmethod
    def _amendment_history(value: Any) -> list[dict[str, Any]]:
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise WorkLedgerConflict("Retry amendment history must be a list")
        result: list[dict[str, Any]] = []
        for index, entry in enumerate(value, start=1):
            if not isinstance(entry, dict):
                raise WorkLedgerConflict("Retry amendment history contains an invalid entry")
            text = str(entry.get("text") or "").strip()
            created_at = str(entry.get("created_at") or "").strip()
            amended_from = str(entry.get("amended_from") or "").strip()
            authorization_id = str(
                entry.get("authorization_permission_request_id") or ""
            ).strip()
            try:
                number = int(entry.get("number") or 0)
            except (TypeError, ValueError) as exc:
                raise WorkLedgerConflict("Retry amendment number is invalid") from exc
            if number != index or not text or len(text) > _MAX_AMENDMENT_TEXT:
                raise WorkLedgerConflict("Retry amendment history is out of sequence or unbounded")
            if not created_at or len(created_at) > 64 or not amended_from or len(amended_from) > 160:
                raise WorkLedgerConflict("Retry amendment lineage is incomplete")
            if len(authorization_id) > 240:
                raise WorkLedgerConflict("Retry authorization identity is invalid")
            normalized = {
                "number": number,
                "created_at": created_at,
                "text": text,
                "amended_from": amended_from,
            }
            if authorization_id:
                normalized["authorization_permission_request_id"] = authorization_id
            result.append(normalized)
        return result

    def _retry_authorization_permission(
        self,
        item: WorkItemRecord,
        previous_attempt: RunAttemptRecord,
        request_id: str,
    ) -> PermissionRequestRecord:
        request = self.store.get_permission_request(request_id)
        if request is None:
            raise WorkLedgerNotFound(f"unknown permission request: {request_id}")
        if request.work_item_id != item.work_item_id:
            raise WorkLedgerConflict("permission request belongs to a different work item")
        if request.attempt_id != previous_attempt.attempt_id:
            raise WorkLedgerConflict("permission request does not belong to the latest attempt")
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        if (
            request.status not in {"denied", "expired"}
            or "allow_once" in request.options
            or metadata.get("kind") != "provider_permission"
            or str(metadata.get("provider") or "").strip().lower()
            != previous_attempt.provider.strip().lower()
            or metadata.get("diagnostic_only") is not True
            or metadata.get("retry_required") is not True
        ):
            raise WorkLedgerConflict(
                "permission request is not eligible for an authorized Retry"
            )
        return request

    @staticmethod
    def _permission_retry_authorization_text(request: PermissionRequestRecord) -> str:
        scopes = [str(path).strip()[:260] for path in request.scope_paths[:3] if str(path).strip()]
        scope_lines = "\n".join(f"- {path}" for path in scopes) or "- provider-declared scope"
        reason = str(request.reason or "Provider policy denied the operation.").strip()[:240]
        return (
            "[PER-REQUEST AUTHORIZATION]\n"
            "I explicitly authorize the single attempt immediately following "
            f"{request.attempt_id} to perform only the operation below.\n"
            f"Capability: {str(request.capability)[:120]}\n"
            f"Action: {str(request.action)[:120]}\n"
            f"Scope:\n{scope_lines}\n"
            f"Reason: {reason}\n"
            "This authorization expires when that immediately following attempt ends. "
            "Do not broaden its scope or persist it as an always-allow rule."
        )

    @staticmethod
    def _compose_amended_instruction(goal: str, amendments: list[dict[str, Any]]) -> str:
        blocks = [str(goal or "").strip()]
        for entry in amendments:
            blocks.append(
                f"[USER AMENDMENT {entry['number']} @ {entry['created_at']}]\n{entry['text']}"
            )
        return "\n\n".join(block for block in blocks if block)

    @staticmethod
    def _steer_replacement_prompt(instruction: str, predecessor_attempt_id: str) -> str:
        return (
            f"{str(instruction or '').strip()}\n\n"
            "[AMADEUS STEER REPLACEMENT]\n"
            "A previous run for this same task was cancelled after it may have written partial files. "
            "Inspect the current workspace and Git state before editing. Continue from valid existing work; "
            "do not assume the workspace is empty and do not recreate files merely because the original "
            "prompt did not list them. Validate the amended result before reporting it.\n"
            f"Predecessor attempt: {predecessor_attempt_id}"
        )

    @staticmethod
    def _amendment_interventions(amendments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "kind": (
                    "permission_authorization"
                    if entry.get("authorization_permission_request_id")
                    else "user_amendment"
                ),
                "source": "button" if entry.get("authorization_permission_request_id") else "text",
                "status": "injected",
                "number": int(entry["number"]),
                "created_at": str(entry["created_at"]),
                "amended_from": str(entry["amended_from"]),
                "summary": str(entry["text"])[:240],
                **(
                    {
                        "permission_request_id": str(
                            entry["authorization_permission_request_id"]
                        )
                    }
                    if entry.get("authorization_permission_request_id")
                    else {}
                ),
            }
            for entry in amendments
        ]

    @staticmethod
    def _request_has_write_intent(request: ProviderRunRequest, metadata: dict[str, Any]) -> bool:
        if metadata.get("read_only") is True:
            return False
        if metadata.get("write_intent") is True:
            return True
        if (
            request.requirements is not None
            and request.requirements.workspace_access == "write"
        ):
            return True
        if metadata.get("write_intent") is False or request.requirements is not None:
            return False
        mode = str(request.mode or "").strip().lower()
        if mode in {"plan", "observe", "open", "snapshot", "research", "read"}:
            return False
        return mode in {"agent", "delegate", "edit", "write", "execute"}

    @staticmethod
    def _workspace_policy(
        *,
        workspace_mode: str,
        write_intent: bool,
        worktree_requested: bool,
        ensured: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if workspace_mode == "none":
            return {
                "mode": "none",
                "write_intent": False,
                "worktree_requested": False,
                "decision": "not_applicable",
                "reason": (
                    "The selected provider contract requires no filesystem "
                    "workspace; Git discovery and writer leases are disabled."
                ),
                "automatic_worktree": False,
            }
        if ensured is not None:
            if workspace_mode == "worktree":
                decision = "ensured_worktree"
                reason = (
                    "The Host allocated an isolated Git worktree; concurrent "
                    "writers get their own roots regardless of Provider."
                )
                automatic = True
            else:
                decision = "ensured_local_degraded"
                reason = (
                    "Workspace ensure resolved to the project directory "
                    "(non-git project); isolation stays single-writer local."
                )
                automatic = False
            return {
                "mode": workspace_mode,
                "write_intent": write_intent,
                "worktree_requested": worktree_requested,
                "decision": decision,
                "reason": reason,
                "automatic_worktree": automatic,
            }
        if workspace_mode == "worktree":
            reason = "Caller supplied an already allocated worktree; Amadeus records but does not create it."
        elif worktree_requested:
            reason = "A worktree was requested, but no allocated path was supplied; P0 stays local with a single-writer lease."
        elif write_intent:
            reason = "Local workspace selected; P0 enforces a single active writer."
        else:
            reason = "Read-only attempt may share the selected workspace."
        return {
            "mode": workspace_mode,
            "write_intent": write_intent,
            "worktree_requested": worktree_requested,
            "decision": "reuse_supplied_workspace",
            "reason": reason,
            "automatic_worktree": False,
        }

    def _assert_workspace_available(self, workspace_path: str, *, write_intent: bool) -> None:
        """Fast friendly check; the SQLite partial unique index is authoritative."""
        if not write_intent:
            return
        identity = canonicalize_path(workspace_path).identity_key
        for lease in self.store.list_writer_leases(active_only=True):
            if lease.workspace_identity != identity:
                continue
            attempt = self.store.get_attempt(lease.attempt_id)
            if attempt is None or attempt.execution_status in _TERMINAL_EXECUTION:
                self.store.release_writer_lease(lease.attempt_id, status="released")
                continue
            item = self.store.get_work_item(lease.work_item_id)
            title = item.title if item is not None else lease.work_item_id
            raise WorkLedgerConflict(
                "workspace already has an active writer: "
                f"{title} ({lease.work_item_id}); wait, Retry after failure, or allocate a worktree"
            )

    def _workspace_less_project(self):
        """Return the hidden FK container for tasks that own no workspace.

        Projects remain place-backed in the public product model.  The v1
        ledger schema nevertheless requires every WorkItem to reference one,
        so workspace-less provider activity uses one retired internal row.
        The row is never a cwd and never appears in Project routing menus.
        """

        db_path = str(getattr(self.store, "db_path", "") or "")
        parent = (
            Path(db_path).resolve().parent
            if db_path and db_path != ":memory:"
            else Path.cwd() / "runtime"
        )
        project = self.store.create_or_get_project(
            parent / ".workspace-less-provider-activity",
            name="Provider activity",
            metadata={
                "virtual": True,
                "workspace_mode": "none",
                "routing_visible": False,
            },
        )
        if project.state != "retired":
            project = self.store.set_project_state(project.project_id, "retired")
        return project

    # -- Provider facts --------------------------------------------------

    async def _enqueue_provider_event(
        self,
        _method: str,
        params: dict[str, Any],
    ) -> None:
        event_type = str(params.get("type") or "").strip().lower()
        if event_type in {
            PARENT_CONTEXT_DELIVERED_EVENT,
            "run.created",
            "run.started",
            "run.status",
            "run.finished",
            "run.failed",
            "run.cancelled",
            "permission.requested",
            "permission.required",
            "permission.resolved",
            "permission.allowed",
            "permission.approved",
            "permission.granted",
            "permission.denied",
            "permission.rejected",
            "permission.expired",
            "input.requested",
            "question",
            "user.input.required",
        }:
            # Control/lifecycle edges must be visible before ProviderRuntime
            # acknowledges that edge. Drain earlier queued evidence first.
            await self.drain_provider_facts()
            await self._on_provider_event(_method, params)
            return
        await self._enqueue_provider_fact("event", params)

    async def _enqueue_provider_result(
        self,
        _method: str,
        params: dict[str, Any],
    ) -> None:
        # The terminal assessment consumes every earlier tool/artifact fact and
        # must be committed before ProviderRuntime reports completion.
        await self.drain_provider_facts()
        await self._on_provider_result(_method, params)

    async def _enqueue_provider_fact(
        self,
        kind: str,
        params: dict[str, Any],
    ) -> None:
        """Put one immutable Provider fact on the single ordered ingest lane."""

        if self._provider_fact_queue is None:
            self._provider_fact_queue = asyncio.Queue(maxsize=2048)
        if self._provider_fact_task is None or self._provider_fact_task.done():
            self._provider_fact_task = asyncio.create_task(
                self._run_provider_fact_queue(),
                name="work-provider-facts",
            )
        # Durable facts may backpressure only at this explicit bounded overload;
        # silently dropping one would corrupt attempt state.
        await self._provider_fact_queue.put((kind, copy.deepcopy(params)))

    async def _run_provider_fact_queue(self) -> None:
        queue = self._provider_fact_queue
        if queue is None:
            return
        while True:
            kind, params = await queue.get()
            try:
                if kind == "event":
                    await self._on_provider_event(Method.PROVIDER_EVENT, params)
                else:
                    await self._on_provider_result(Method.PROVIDER_RESULT, params)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ordered Provider fact ingest failed kind=%s", kind)
            finally:
                queue.task_done()

    async def drain_provider_facts(self) -> None:
        """Wait until every accepted Provider event/result has reached the ledger."""

        queue = self._provider_fact_queue
        if queue is not None:
            await queue.join()

    async def _on_provider_event(self, _method: str, params: dict[str, Any]) -> None:
        # SQLite is thread-safe behind WorkLedgerStore's RLock. Running the
        # durable ingest off the asyncio thread prevents one disk-heavy event
        # from freezing Chat, TTS, Provider IO, and every other bus subscriber.
        ingested = await asyncio.to_thread(self.event_ingestor.ingest_event, params)
        if ingested is None:
            return
        run_id = ingested.run_id
        event_type = ingested.event_type
        payload = ingested.payload
        attempt = ingested.attempt
        if not ingested.accepted:
            logger.debug(
                "ignored conflicting work-ledger event %s for %s",
                event_type,
                run_id,
            )
            if ingested.material:
                await self._publish_provider_snapshot(
                    reason=f"provider.event:{event_type}"
                )
            return

        try:
            if event_type == "run.created":
                self._heartbeat_lease(attempt.attempt_id)
                item = self.store.get_work_item(attempt.work_item_id)
                current_attempt = self.store.get_attempt(attempt.attempt_id) or attempt
                if (
                    item is not None
                    and item.workspace_mode != "none"
                    and not isinstance(current_attempt.metadata.get("git_baseline"), dict)
                ):
                    await self.artifact_registry.capture_baseline(current_attempt, item)
            elif event_type == "run.status":
                status = str(payload.get("status") or "").strip().lower()
                mapped = self._event_execution_status(status)
                liveness = str(payload.get("liveness") or "").strip().lower()
                if mapped in _ACTIVE_EXECUTION or liveness in {"active", "stalled", "cancel_pending"}:
                    self._heartbeat_lease(attempt.attempt_id)
            elif event_type in {"run.started"}:
                self._heartbeat_lease(attempt.attempt_id)
            elif event_type == "artifact.created":
                self._register_artifact_payload(attempt, payload)
            elif event_type == "tool.call":
                self._record_tool_artifact_hints(run_id, payload)
            elif event_type == "tool.result":
                self._record_tool_evidence(run_id, payload)
            elif event_type in {"permission.requested", "permission.required"}:
                event_metadata = (
                    params.get("metadata")
                    if isinstance(params.get("metadata"), dict)
                    else {}
                )
                permission = self._upsert_permission_event(
                    attempt,
                    run_id=run_id,
                    provider=str(params.get("provider") or attempt.provider),
                    payload=payload,
                    event_metadata=event_metadata,
                )
                facts = self._event_fact(run_id)
                if permission.metadata.get("resolved_automatically") is True:
                    # Retrospective provider-side policy denials arrive after
                    # the tool call was rejected. Keep them as audit evidence,
                    # but out of actionable Needs-you state.
                    diagnostic_ids = facts.setdefault("provider_permission_diagnostics", [])
                    if isinstance(diagnostic_ids, list) and permission.request_id not in diagnostic_ids:
                        diagnostic_ids.append(permission.request_id)
                    event_identity = self._provider_permission_event_identity(
                        payload,
                        event_metadata,
                    )
                    seen_events = facts.setdefault("provider_permission_events", [])
                    if isinstance(seen_events, list) and event_identity not in seen_events:
                        seen_events.append(event_identity)
                        facts["permission_failure_suppressions"] = (
                            int(facts.get("permission_failure_suppressions") or 0) + 1
                        )
                        tool_use_id = self._tool_use_id(payload)
                        if tool_use_id:
                            tool_ids = facts.setdefault(
                                "provider_permission_tool_ids", []
                            )
                            if isinstance(tool_ids, list) and tool_use_id not in tool_ids:
                                tool_ids.append(tool_use_id)
                    facts["pending_permissions"] = 0
                else:
                    pending = self.store.list_permission_requests(
                        attempt.work_item_id,
                        attempt_id=attempt.attempt_id,
                        status="pending",
                    )
                    facts["pending_permissions"] = max(1, len(pending))
                facts["permission_request_id"] = permission.request_id
            elif event_type in {
                "permission.resolved",
                "permission.allowed",
                "permission.approved",
                "permission.granted",
                "permission.denied",
                "permission.rejected",
                "permission.expired",
            }:
                # Normal user decisions are committed by work.permission.resolve.
                # Adapter-owned expiry is the only resolution that may arrive
                # without that Host command, so close that exact durable row.
                if payload.get("automatic") is True:
                    provider_request_id = str(
                        payload.get("request_id") or payload.get("requestId") or ""
                    ).strip()
                    status = (
                        "expired"
                        if event_type == "permission.expired"
                        else (
                            "allowed"
                            if event_type
                            in {
                                "permission.allowed",
                                "permission.approved",
                                "permission.granted",
                            }
                            else "denied"
                        )
                    )
                    for permission in self.store.list_permission_requests(
                        attempt.work_item_id,
                        attempt_id=attempt.attempt_id,
                        status="pending",
                    ):
                        if (
                            provider_request_id
                            and str(permission.metadata.get("provider_request_id") or "")
                            != provider_request_id
                        ):
                            continue
                        try:
                            self.store.resolve_permission_request(
                                permission.request_id,
                                status,
                                metadata={
                                    "resolution": "provider_interaction_expired",
                                    "provider_event": event_type,
                                },
                            )
                        except WorkLedgerConflict:
                            pass
                        break
                pending = self.store.list_permission_requests(
                    attempt.work_item_id,
                    attempt_id=attempt.attempt_id,
                    status="pending",
                )
                self._event_fact(run_id)["pending_permissions"] = len(pending)
            elif event_type in {"input.requested", "question", "user.input.required"}:
                self._event_fact(run_id)["pending_inputs"] = 1
            elif event_type in {"run.failed", "run.cancelled"}:
                if event_type == "run.cancelled":
                    for permission in self.store.list_permission_requests(
                        attempt.work_item_id,
                        attempt_id=attempt.attempt_id,
                        status="pending",
                    ):
                        try:
                            self.store.resolve_permission_request(
                                permission.request_id,
                                "expired",
                                metadata={
                                    "resolution": "attempt_cancelled",
                                    "cancel_reason": str(payload.get("reason") or ""),
                                },
                            )
                        except WorkLedgerConflict:
                            # A concurrent user decision is immutable and wins.
                            pass
                self.store.release_writer_lease(attempt.attempt_id, status="released")
        except WorkLedgerConflict:
            # ProviderRuntime emits both a terminal event and provider.result;
            # identical terminal facts are idempotent, stale contradictory
            # facts are retained in logs instead of corrupting the ledger.
            logger.debug("ignored conflicting work-ledger event %s for %s", event_type, run_id)

        if ingested.material:
            await self._publish_provider_snapshot(reason=f"provider.event:{event_type}")

    async def _on_provider_result(self, _method: str, params: dict[str, Any]) -> None:
        ingested = await asyncio.to_thread(self.event_ingestor.ingest_result, params)
        if ingested is None:
            return
        run_id = ingested.run_id
        attempt = ingested.attempt
        status = ingested.status
        result = ingested.result
        error = ingested.error
        metadata = ingested.metadata
        facts = ingested.evidence
        provider_completion = (
            metadata.get("provider_completion")
            if isinstance(metadata.get("provider_completion"), dict)
            else {}
        )
        if provider_completion.get("classification") == "progress_only_completion":
            top_level_activity = (
                metadata.get("activity_evidence")
                if isinstance(metadata.get("activity_evidence"), dict)
                else {}
            )
            if (
                not top_level_activity
                or provider_completion.get("activity_evidence")
                != top_level_activity
            ):
                logger.error(
                    "ignored inconsistent provider completion evidence attempt=%s",
                    attempt.attempt_id,
                )
                provider_completion = {}
        if provider_completion:
            # Runtime owns this transcript-shape classification. Persist the
            # exact protected fact once outside the provider-result envelope.
            # Replayed results still contain `recovery_state=unclaimed`; an
            # ordinary merge would roll a durable claimed/started state back.
            attempt, _initialized = self.store.compare_and_set_attempt_metadata(
                attempt.attempt_id,
                key="provider_completion",
                expected_present=False,
                value=dict(provider_completion),
            )
            persisted_completion = (
                attempt.metadata.get("provider_completion")
                if isinstance(attempt.metadata.get("provider_completion"), dict)
                else {}
            )
            immutable_keys = (
                "classification",
                "native_status",
                "contract_status",
                "activity_evidence",
            )
            if any(
                persisted_completion.get(key) != provider_completion.get(key)
                for key in immutable_keys
            ):
                logger.error(
                    "ignored contradictory provider completion replay attempt=%s",
                    attempt.attempt_id,
                )
        # Provider-native approvals are scoped to the live Attempt.  Expire
        # any unresolved checkpoint before completion assessment and UI
        # projection so a late/missing adapter callback cannot leave a dead
        # card that blocks the next Operation on this WorkItem.
        self._expire_terminal_provider_permissions(attempt)
        cancellation = (
            metadata.get("cancellation")
            if isinstance(metadata.get("cancellation"), dict)
            else {}
        )
        steer_replacement_transition = (
            str(cancellation.get("reason") or "").strip().lower()
            == "steer_replacement"
        )
        item = self.store.get_work_item(attempt.work_item_id)
        git_delta: dict[str, Any] = {}
        export_delta: dict[str, Any] = {}
        export_permission: PermissionRequestRecord | None = None
        export_plan: dict[str, Any] | None = None
        if item is not None and item.workspace_mode != "none":
            current_attempt = self.store.get_attempt(attempt.attempt_id) or attempt
            try:
                git_delta = await self.artifact_registry.finalize_attempt(current_attempt, item)
            except Exception as exc:
                logger.exception("failed to finalize Git artifacts for %s", attempt.attempt_id)
                git_delta = {
                    "available": False,
                    "reason": "artifact_registry_error",
                    "changed_files": [],
                    "ambiguous_paths": [],
                    "conflicts": [f"artifact registry failed: {exc.__class__.__name__}"],
                }
            current_attempt = self.store.get_attempt(attempt.attempt_id) or current_attempt
            export_plan = (
                current_attempt.metadata.get("export_plan")
                if isinstance(current_attempt.metadata.get("export_plan"), dict)
                else None
            )
            workspace_bundle_validation_required = bool(
                export_plan is None
                and current_attempt.metadata.get("auip_host_validates_bundle") is True
            )
            if status == "succeeded" and workspace_bundle_validation_required:
                from server.auip_bundle_validation import (
                    validate_staged_auip_web_bundle,
                )

                try:
                    bundle_validation = validate_staged_auip_web_bundle(
                        Path(
                            str(
                                current_attempt.metadata.get("auip_bundle_root")
                                or item.workspace_path
                            )
                        ),
                        materialized_files=tuple(
                            str(value)
                            for value in (
                                current_attempt.metadata.get(
                                    "auip_host_materialized_files"
                                )
                                or []
                            )
                        ),
                    )
                except Exception as exc:
                    bundle_validation = {
                        "verified": False,
                        "code": str(
                            getattr(exc, "code", exc.__class__.__name__)
                        ),
                        "detail": str(getattr(exc, "detail", "") or exc)[:600],
                    }
                self.store.update_attempt(
                    current_attempt.attempt_id,
                    metadata={"host_auip_bundle_validation": bundle_validation},
                )
                current_attempt = (
                    self.store.get_attempt(current_attempt.attempt_id)
                    or current_attempt
                )
            if export_plan is not None:
                if status != "succeeded":
                    export_delta = {
                        "available": False,
                        "reason": "staged_export_unverified",
                        "changed_files": [],
                        "untracked": [],
                        "patch": "",
                        "ambiguous_paths": [],
                        "conflicts": [
                            "provider attempt did not succeed; staged files were not approved for export"
                        ],
                        "pending_export": False,
                        "external_export_pending": False,
                        "recovery_required": False,
                        "permission_request_id": "",
                        "artifact_type": "business.proposed_export",
                        "baseline_head": git_delta.get("baseline_head"),
                        "current_head": git_delta.get("current_head"),
                    }
                else:
                    try:
                        if export_plan.get("host_validates_auip_bundle") is True:
                            from server.auip_bundle_validation import (
                                finalize_staged_auip_web_bundle,
                            )

                            staging_root, _staged_files = (
                                self.export_service.validated_staging_files(
                                    current_attempt,
                                    item,
                                    export_plan,
                                )
                            )
                            try:
                                bundle_validation = finalize_staged_auip_web_bundle(
                                    staging_root,
                                    entry_filename=str(
                                        export_plan.get("entry_filename") or ""
                                    ),
                                    materialized_files=tuple(
                                        str(value)
                                        for value in (
                                            export_plan.get("host_materialized_files")
                                            or []
                                        )
                                    ),
                                )
                            except Exception as exc:
                                self.store.update_attempt(
                                    current_attempt.attempt_id,
                                    metadata={
                                        "host_auip_bundle_validation": {
                                            "verified": False,
                                            "code": str(
                                                getattr(
                                                    exc,
                                                    "code",
                                                    exc.__class__.__name__,
                                                )
                                            ),
                                            "detail": str(
                                                getattr(exc, "detail", "") or exc
                                            )[:600],
                                        }
                                    },
                                )
                                raise
                            self.store.update_attempt(
                                current_attempt.attempt_id,
                                metadata={
                                    "host_auip_bundle_validation": bundle_validation
                                },
                            )
                            current_attempt = (
                                self.store.get_attempt(current_attempt.attempt_id)
                                or current_attempt
                            )
                        outcome = self.export_service.discover_staged_exports(
                            current_attempt,
                            item,
                            export_plan,
                        )
                        permission = outcome.get("permission")
                        if isinstance(permission, PermissionRequestRecord):
                            export_permission = permission
                        export_delta = {
                            "available": bool(outcome.get("available")),
                            "reason": str(outcome.get("reason") or "external_export_unavailable"),
                            "changed_files": [str(value) for value in outcome.get("changed_files") or []],
                            "untracked": [str(value) for value in outcome.get("changed_files") or []],
                            "patch": str(outcome.get("patch") or ""),
                            "ambiguous_paths": [],
                            "conflicts": [],
                            "pending_export": bool(outcome.get("pending_export")),
                            "external_export_pending": bool(outcome.get("pending_export")),
                            "recovery_required": bool(outcome.get("recovery_required")),
                            "permission_request_id": (
                                permission.request_id
                                if isinstance(permission, PermissionRequestRecord)
                                else ""
                            ),
                            "artifact_type": "business.proposed_export",
                            "baseline_head": git_delta.get("baseline_head"),
                            "current_head": git_delta.get("current_head"),
                        }
                    except Exception as exc:
                        logger.exception("failed to discover staged export for %s", attempt.attempt_id)
                        export_delta = {
                            "available": False,
                            "reason": "export_discovery_error",
                            "changed_files": [],
                            "untracked": [],
                            "patch": "",
                            "ambiguous_paths": [],
                            "conflicts": [f"export discovery failed: {exc.__class__.__name__}"],
                            "pending_export": False,
                            "external_export_pending": False,
                        }
                review_delta = dict(export_delta)
                self.store.update_attempt(
                    attempt.attempt_id,
                    metadata={
                        "export_delta": export_delta,
                        # View Diff is a review surface.  For an external
                        # deliverable, show its proposed Desktop path rather
                        # than the internal staging implementation detail.
                        "git_delta": review_delta,
                    },
                )
                git_delta = review_delta
        try:
            try:
                self._register_result_artifacts(attempt, metadata=metadata)
            except Exception:
                logger.exception("failed to register provider artifacts for %s", attempt.attempt_id)
            try:
                self._register_required_staged_outcome(
                    attempt=attempt,
                    item=item,
                    status=status,
                    export_plan=export_plan,
                )
            except Exception:
                # The structured outcome observer below owns the user-visible
                # failure.  Keep the provider result ingestible while refusing
                # to mint an application fact from an invalid staging tree.
                logger.exception(
                    "failed to materialize required Host outcome for %s",
                    attempt.attempt_id,
                )
        finally:
            self.store.release_writer_lease(attempt.attempt_id, status="released")
        tool_diagnostics = [
            dict(item)
            for item in facts.get("tool_diagnostics", [])
            if isinstance(item, dict)
        ][:16]
        if tool_diagnostics:
            self.store.update_attempt(
                attempt.attempt_id,
                metadata={
                    "tool_evidence": {
                        "unverified": tool_diagnostics,
                    }
                },
            )
        for hint in facts.get("artifact_hints", []):
            if isinstance(hint, dict):
                try:
                    self._register_path(
                        attempt,
                        str(hint.get("path") or ""),
                        kind="tool.output",
                        discovered_from="provider_tool_event",
                    )
                except Exception:
                    logger.exception("failed to register tool artifact hint for %s", attempt.attempt_id)
        assessment_metadata = dict(metadata)
        # Outcome verification answers whether a successful operation produced
        # its declared effect. Cancellation/failure already has a stronger
        # terminal truth and must not be rewritten as a domain-specific
        # "missing result" error (for example, a cancelled AUIP preparation
        # narrated as though it ran to completion but built an invalid app).
        host_outcome = None
        if status == "succeeded":
            host_outcome = observe_required_host_outcome(
                assessment_metadata,
                store=self.store,
                attempt=attempt,
            )
            if host_outcome is not None:
                assessment_metadata[OUTCOME_EVIDENCE_METADATA_KEY] = host_outcome.to_dict()
        compatibility = self._compatibility_blockers(result, assessment_metadata)
        outcome_verdict = (
            assess_provider_outcome(
                execution_status=status,
                provider_report=result,
                metadata=assessment_metadata,
                # Localization belongs to the actual Observer/output boundary.
                display_language="english",
            )
            if status == "succeeded"
            else None
        )
        if outcome_verdict is not None:
            self.store.update_attempt(
                attempt.attempt_id,
                metadata={OUTCOME_VERDICT_METADATA_KEY: outcome_verdict.to_dict()},
            )
        conflicts = tuple(str(item) for item in facts.get("conflicts", []) if str(item))
        conflicts += tuple(compatibility.get("conflicts", ()))
        conflicts += tuple(str(item) for item in git_delta.get("conflicts", []) if str(item))
        outcome_blocking_errors: tuple[str, ...] = ()
        if outcome_verdict is not None and outcome_verdict.attention == "conflict":
            conflicts += (outcome_verdict.rationale,)
        elif outcome_verdict is not None and outcome_verdict.attention == "error":
            outcome_blocking_errors = (outcome_verdict.rationale,)
        registered_artifacts = self.store.list_artifacts(
            attempt.work_item_id,
            attempt_id=attempt.attempt_id,
        )
        business_artifacts = [
            artifact
            for artifact in registered_artifacts
            if artifact.kind.startswith("business.")
            and not str(artifact.metadata.get("relative_path") or "")
            .replace("\\", "/")
            .startswith(".amadeus/proposed_exports/")
        ]
        verified_business_artifacts = [
            artifact for artifact in business_artifacts if artifact.status in {"registered", "approved"}
        ]
        missing_business_artifacts = [
            artifact for artifact in business_artifacts if artifact.status == "missing"
        ]
        missing_requirements = list(compatibility.get("missing_requirements", ()))
        if (
            outcome_verdict is not None
            and status == "succeeded"
            and outcome_verdict.completeness != "complete"
            and outcome_verdict.attention != "conflict"
        ):
            missing_requirements.append(
                f"provider outcome {outcome_verdict.facet} is not verified by host evidence"
            )
        if missing_business_artifacts:
            missing_requirements.append("a provider-declared artifact is missing")
        if export_delta and export_delta.get("reason") == "staged_export_missing":
            missing_requirements.append("the requested Desktop deliverable was not staged")
        pending_permissions = self.store.list_permission_requests(
            attempt.work_item_id,
            attempt_id=attempt.attempt_id,
            status="pending",
        )
        permission_records = self.store.list_permission_requests(
            attempt.work_item_id,
            attempt_id=attempt.attempt_id,
        )
        uncovered_external = []
        for artifact in registered_artifacts:
            if artifact.location != "external" or artifact.status != "pending":
                continue
            artifact_identity = (
                canonicalize_path(artifact.path).identity_key if artifact.path else ""
            )
            covered = bool(
                artifact_identity
                and any(
                    artifact_identity == canonicalize_path(scope).identity_key
                    for permission in pending_permissions
                    for scope in permission.scope_paths
                    if scope
                )
            )
            if not covered:
                uncovered_external.append(artifact)
        if uncovered_external:
            conflicts += ("external artifact has no matching explicit approval",)
        evidence = CompletionEvidence(
            execution_status=status,
            current_state=(self.store.get_work_item(attempt.work_item_id) or self._missing_item()).state,
            explicit_complete=self._explicit_completion(metadata),
            expected_artifact_count=self._expected_artifact_count(metadata),
            registered_artifact_count=len(verified_business_artifacts),
            validation_statuses=(
                *self._validation_statuses(metadata),
                *(
                    str(value)
                    for value in facts.get("validation_statuses", [])
                    if str(value)
                ),
            ),
            missing_requirements=tuple(missing_requirements),
            pending_permissions=max(
                len(pending_permissions),
                0
                if export_delta or permission_records
                else int(compatibility.get("pending_permissions") or 0),
            ),
            pending_inputs=max(
                int(facts.get("pending_inputs") or 0),
                int(compatibility.get("pending_inputs") or 0),
                1
                if outcome_verdict is not None and outcome_verdict.attention == "input"
                else 0,
            ),
            blocking_errors=outcome_blocking_errors,
            conflicts=conflicts,
        )
        decision = assess_completion(evidence)
        completion_history = self.store.list_completions(attempt.work_item_id)
        previous_completion = completion_history[-1] if completion_history else None
        if (
            status == "cancelled"
            and previous_completion is not None
            and previous_completion.work_item_state == "review_ready"
            and not git_delta.get("changed_files")
            and not git_delta.get("ambiguous_paths")
            and not git_delta.get("conflicts")
        ):
            decision = replace(
                decision,
                work_item_state="review_ready",
                rationale=(
                    "The provider attempt was cancelled before changing the workspace; "
                    "the previous review-ready result remains available."
                ),
            )
        already_assessed = any(
            item.attempt_id == attempt.attempt_id
            for item in completion_history
        )
        if not already_assessed:
            self.store.record_completion(
                attempt.work_item_id,
                decision,
                attempt_id=attempt.attempt_id,
                source="host",
                evidence={
                    **asdict(evidence),
                    "compatibility_fallback": compatibility,
                    "git_delta": {
                        "available": git_delta.get("available"),
                        "changed_files": list(git_delta.get("changed_files") or []),
                        "ambiguous_paths": list(git_delta.get("ambiguous_paths") or []),
                        "conflicts": list(git_delta.get("conflicts") or []),
                    },
                },
            )
        recovery_started = False
        if self._progress_only_recovery_admitted(
            attempt=self.store.get_attempt(attempt.attempt_id) or attempt,
            status=status,
            result=result,
            metadata=metadata,
            facts=facts,
            git_delta=git_delta,
            registered_artifacts=registered_artifacts,
            permission_records=permission_records,
            export_permission=export_permission,
            export_delta=export_delta,
            cancellation=cancellation,
        ):
            recovery_started = await self._start_progress_only_recovery(
                attempt=self.store.get_attempt(attempt.attempt_id) or attempt,
                item=item,
                metadata=metadata,
            )
        if recovery_started:
            # The predecessor Attempt is terminal, but the same Operation is
            # already continuing under one visible, bounded successor. Do not
            # narrate the predecessor as the task's final outcome.
            return
        latest_attempt = self.store.get_attempt(attempt.attempt_id) or attempt
        latest_completion = (
            latest_attempt.metadata.get("provider_completion")
            if isinstance(
                latest_attempt.metadata.get("provider_completion"), dict
            )
            else {}
        )
        if (
            latest_completion.get("classification")
            == "progress_only_completion"
            and latest_completion.get("recovery_state")
            in {"claimed", "started", "cancelled", "cancel_pending"}
        ):
            # A replayed Provider result cannot turn a continuing or retracted
            # recovery predecessor back into a terminal user-facing failure.
            return
        permission_note = self._claim_export_permission_notice(
            attempt,
            export_permission,
            run_id=run_id,
            provider_metadata=metadata,
        )
        terminal_note = None
        owns_terminal = export_plan is not None or _ledger_owns_terminal_narration()
        if owns_terminal and not steer_replacement_transition:
            # One assessment, one narration. WorkActivity defers to this note so
            # the character never announces an outcome from the process exit
            # code while the ledger is still cross-checking the tool evidence
            # against the git delta.
            spoken = (
                outcome_verdict.summary
                if outcome_verdict is not None
                and (
                    outcome_verdict.completeness != "complete"
                    or not outcome_verdict.provider_report_allowed
                )
                else self._terminal_narration_summary(decision, result, error)
            )
            notice = {
                "title": (
                    "Desktop deliverable task finished"
                    if export_plan is not None
                    else "Task finished"
                ),
                "summary": spoken or "The task finished without a result summary.",
                "session_id": self._attempt_session_id(attempt, metadata),
                "importance": (
                    "error"
                    if status != "succeeded" or decision.attention in {"conflict", "error"}
                    else "blocking" if decision.attention == "input" else "important"
                ),
                "metadata": {
                    "work_event": "work.attempt_finished",
                    "execution_status": status,
                    "attention": decision.attention,
                    "completeness": decision.completeness,
                    "work_item_state": decision.work_item_state,
                    # Carried so "did the assessment do the talking" is a
                    # checkable fact rather than a substring guess: when the
                    # evidence contradicts the provider, summary is this.
                    "rationale": str(decision.rationale or ""),
                    **(
                        {"outcome_verdict": outcome_verdict.to_dict()}
                        if outcome_verdict is not None
                        else {}
                    ),
                },
            }
            if export_permission is not None:
                # Approval owns the user-visible terminal boundary.  Retain
                # the evidence-bounded execution report now, then compose it
                # with the actual permission outcome after the copy commits.
                self._defer_terminal_work_notice(attempt, notice)
            else:
                terminal_note = self._claim_terminal_work_notice(
                    attempt,
                    delivery_id=f"attempt:{attempt.attempt_id}:provider_result",
                    title=str(notice["title"]),
                    summary=str(notice["summary"]),
                    session_id=str(notice["session_id"]),
                    importance=str(notice["importance"]),
                    metadata=dict(notice["metadata"]),
                )
        # The permission card must be in the canonical Slice snapshot before
        # Kurisu asks the user to act on it.  The notice claim above is durable
        # and happens before this publish so it cannot invalidate the revision
        # carried by the freshly rendered card.
        await self.publish_snapshot(reason="provider.result")
        if permission_note is not None:
            add_work_note(permission_note)
            await bus.emit(Method.CHAT_WORK_NOTE, permission_note)
        if terminal_note is not None:
            add_work_note(terminal_note)
            await bus.emit(Method.CHAT_WORK_NOTE, terminal_note)

    def _progress_only_recovery_admitted(
        self,
        *,
        attempt: RunAttemptRecord,
        status: str,
        result: str,
        metadata: dict[str, Any],
        facts: dict[str, Any],
        git_delta: dict[str, Any],
        registered_artifacts: list[Any],
        permission_records: list[PermissionRequestRecord],
        export_permission: PermissionRequestRecord | None,
        export_delta: dict[str, Any],
        cancellation: dict[str, Any],
    ) -> bool:
        """Admit one successor only after the Host proves zero execution effect."""

        if self._provider_start is None or status != "failed" or result.strip():
            return False
        completion = (
            attempt.metadata.get("provider_completion")
            if isinstance(attempt.metadata.get("provider_completion"), dict)
            else {}
        )
        if (
            completion.get("classification") != "progress_only_completion"
            or completion.get("recovery_state") != "unclaimed"
        ):
            return False
        prior_recovery = (
            attempt.metadata.get("provider_recovery")
            if isinstance(attempt.metadata.get("provider_recovery"), dict)
            else {}
        )
        try:
            prior_recovery_ordinal = int(prior_recovery.get("ordinal") or 0)
        except (TypeError, ValueError):
            return False
        if prior_recovery_ordinal >= 1:
            return False
        activity = (
            completion.get("activity_evidence")
            if isinstance(completion.get("activity_evidence"), dict)
            else {}
        )
        try:
            progress_milestones = int(activity.get("progress_milestones") or 0)
            execution_items = int(activity.get("execution_items") or 0)
        except (TypeError, ValueError):
            return False
        if not (
            activity.get("observation_authority") == "host"
            and activity.get("terminal_observed") is True
            and progress_milestones > 0
            and execution_items == 0
        ):
            return False
        manifest = attempt.metadata.get("provider_manifest")
        if not isinstance(manifest, dict):
            manifest = (
                metadata.get("provider_manifest")
                if isinstance(metadata.get("provider_manifest"), dict)
                else {}
            )
        capabilities = (
            manifest.get("capabilities")
            if isinstance(manifest.get("capabilities"), dict)
            else {}
        )
        session = attempt.metadata.get("provider_session")
        if not isinstance(session, dict):
            session = (
                metadata.get("provider_session")
                if isinstance(metadata.get("provider_session"), dict)
                else {}
            )
        if (
            str(capabilities.get("resume") or "").strip().lower() != "attach"
            or str(session.get("provider") or "").strip().lower()
            != attempt.provider.strip().lower()
            or str(session.get("scope") or "").strip().lower() != "work_item"
            or not str(session.get("session_id") or "").strip()
        ):
            return False
        if (
            git_delta.get("available") is not True
            or git_delta.get("changed_files")
            or git_delta.get("ambiguous_paths")
            or git_delta.get("conflicts")
        ):
            return False
        if (
            int(facts.get("pending_inputs") or 0) > 0
            or int(facts.get("pending_permissions") or 0) > 0
            or facts.get("conflicts")
            or facts.get("artifact_hints")
            or facts.get("tool_diagnostics")
            or permission_records
            or export_permission is not None
            or export_delta.get("pending_export") is True
            or export_delta.get("external_export_pending") is True
            or export_delta.get("recovery_required") is True
            or cancellation
        ):
            return False
        return not any(
            str(getattr(artifact, "status", "")) in {"registered", "approved", "pending"}
            for artifact in registered_artifacts
        )

    async def _start_progress_only_recovery(
        self,
        *,
        attempt: RunAttemptRecord,
        item: WorkItemRecord | None,
        metadata: dict[str, Any],
    ) -> bool:
        """Start one cancellable same-Operation Retry with typed Host lineage."""

        if self._provider_start is None or item is None:
            return False
        original_completion = dict(attempt.metadata.get("provider_completion") or {})
        if original_completion.get("recovery_state") != "unclaimed":
            return False
        claimed_completion = dict(original_completion)
        claimed_completion.update(
            {
                "recovery_state": "claimed",
                "recovery_root_attempt_id": attempt.attempt_id,
                "recovery_ordinal": 1,
                "recovery_claimed_at": float(self._clock()),
            }
        )
        claimed_attempt, claimed = self.store.compare_and_set_attempt_metadata(
            attempt.attempt_id,
            key="provider_completion",
            expected_present=True,
            expected_value=original_completion,
            value=claimed_completion,
        )
        if not claimed:
            current_completion = (
                claimed_attempt.metadata.get("provider_completion")
                if isinstance(
                    claimed_attempt.metadata.get("provider_completion"), dict
                )
                else {}
            )
            return current_completion.get("recovery_state") in {
                "claimed",
                "started",
                "cancelled",
            }
        self._pending_provider_recoveries[attempt.attempt_id] = {
            "attempt_id": attempt.attempt_id,
            "work_item_id": attempt.work_item_id,
            "provider": attempt.provider,
            "claimed_at": claimed_completion["recovery_claimed_at"],
            "cancelled": False,
        }

        instruction, lineage = self.retry_instruction(item, attempt)
        recovery = ProviderRecoveryContext(
            reason="progress_only_completion",
            root_attempt_id=attempt.attempt_id,
            predecessor_attempt_id=attempt.attempt_id,
            ordinal=1,
        )
        carry_keys = (
            "source",
            "session_id",
            "source_user_text",
            "source_user_context",
            SOURCE_CONTEXT_SCOPE_METADATA_KEY,
            MAIN_ROLE_NAME_METADATA_KEY,
            "presentation_locale",
            "host_outcome_requirement",
            "intent",
            "delegate_mode",
            "branch_intent",
            "work_surface",
        )
        retry_metadata = {
            key: metadata[key]
            for key in carry_keys
            if key in metadata
        }
        retry_metadata.update(
            {
                "continuation": "retry",
                "retry_of": attempt.attempt_id,
                "work": {"work_item_id": attempt.work_item_id},
                **lineage,
            }
        )
        requirements_source = (
            attempt.metadata.get("provider_requirements")
            if isinstance(attempt.metadata.get("provider_requirements"), dict)
            else metadata.get("provider_requirements")
            if isinstance(metadata.get("provider_requirements"), dict)
            else {}
        )
        ownership = str(
            attempt.metadata.get("provider_ownership")
            or metadata.get("provider_ownership")
            or "managed"
        ).strip().lower()
        if ownership not in {"managed", "attached"}:
            ownership = "managed"
        request = ProviderRunRequest(
            provider=attempt.provider,
            task=instruction,
            cwd=item.workspace_path or None,
            mode=attempt.mode,
            metadata=retry_metadata,
            requirements=ProviderRequirements.from_dict(dict(requirements_source)),
            ownership=ownership,  # type: ignore[arg-type]
            recovery=recovery,
        )
        successor = None
        successor_attempt_id = ""
        successor_run_id = ""
        try:
            successor = await self._provider_start(request)
            successor_metadata = (
                getattr(successor, "metadata", None)
                if isinstance(getattr(successor, "metadata", None), dict)
                else {}
            )
            successor_work = (
                successor_metadata.get("work")
                if isinstance(successor_metadata.get("work"), dict)
                else {}
            )
            successor_attempt_id = str(
                successor_work.get("attempt_id") or ""
            ).strip()
            successor_run_id = str(getattr(successor, "run_id", "") or "").strip()
            if not successor_attempt_id or not successor_run_id:
                raise WorkLedgerConflict(
                    "provider recovery started without durable successor identity"
                )
            pending = self._pending_provider_recoveries.get(attempt.attempt_id)
            if pending is not None:
                pending.update(
                    {
                        "successor_attempt_id": successor_attempt_id,
                        "successor_run_id": successor_run_id,
                    }
                )
        except Exception as exc:
            current_attempt = self.store.get_attempt(attempt.attempt_id) or attempt
            current_completion = (
                current_attempt.metadata.get("provider_completion")
                if isinstance(
                    current_attempt.metadata.get("provider_completion"), dict
                )
                else {}
            )
            if current_completion.get("recovery_state") == "cancelled":
                self._pending_provider_recoveries.pop(attempt.attempt_id, None)
                return True
            logger.exception(
                "failed to start progress-only provider recovery for %s",
                attempt.attempt_id,
            )
            failed_completion = dict(current_completion or claimed_completion)
            failed_completion.update(
                {
                    "recovery_state": "failed",
                    "recovery_failed_at": float(self._clock()),
                    "recovery_error": exc.__class__.__name__,
                }
            )
            self.store.compare_and_set_attempt_metadata(
                attempt.attempt_id,
                key="provider_completion",
                expected_present=True,
                expected_value=current_completion,
                value=failed_completion,
            )
            self._pending_provider_recoveries.pop(attempt.attempt_id, None)
            return False

        current_attempt = self.store.get_attempt(attempt.attempt_id) or attempt
        current_completion = (
            current_attempt.metadata.get("provider_completion")
            if isinstance(current_attempt.metadata.get("provider_completion"), dict)
            else {}
        )
        if current_completion.get("recovery_state") == "cancelled":
            cancelled_completion = dict(current_completion)
            cancelled_completion.update(
                {
                    "successor_attempt_id": successor_attempt_id,
                    "successor_run_id": successor_run_id,
                }
            )
            self.store.compare_and_set_attempt_metadata(
                attempt.attempt_id,
                key="provider_completion",
                expected_present=True,
                expected_value=current_completion,
                value=cancelled_completion,
            )
            cancel_confirmed = False
            cancel_reason = "provider_cancel_unavailable"
            if self._provider_cancel is not None:
                try:
                    cancel_outcome = await self._provider_cancel(successor_run_id)
                    cancel_run = (
                        cancel_outcome.get("run")
                        if isinstance(cancel_outcome.get("run"), dict)
                        else {}
                    )
                    cancel_confirmed = bool(
                        cancel_outcome.get("cancelled") is True
                        or str(cancel_run.get("status") or "").strip().lower()
                        == "cancelled"
                    )
                    cancel_reason = str(
                        cancel_outcome.get("reason") or "cancel_unconfirmed"
                    )
                except Exception as exc:
                    cancel_reason = exc.__class__.__name__
                    logger.exception(
                        "failed to cancel retracted recovery successor %s",
                        successor_run_id,
                    )
            elif successor is not None and getattr(successor, "task_handle", None):
                successor.task_handle.cancel()
                cancel_confirmed = True
            if not cancel_confirmed:
                latest_attempt = self.store.get_attempt(attempt.attempt_id) or attempt
                latest_completion = (
                    latest_attempt.metadata.get("provider_completion")
                    if isinstance(
                        latest_attempt.metadata.get("provider_completion"), dict
                    )
                    else cancelled_completion
                )
                cancel_failed = dict(latest_completion)
                cancel_failed.update(
                    {
                        "recovery_state": "cancel_pending",
                        "recovery_error": cancel_reason,
                    }
                )
                self.store.compare_and_set_attempt_metadata(
                    attempt.attempt_id,
                    key="provider_completion",
                    expected_present=True,
                    expected_value=latest_completion,
                    value=cancel_failed,
                )
            self._pending_provider_recoveries.pop(attempt.attempt_id, None)
            return True

        started_completion = dict(claimed_completion)
        started_completion.update(
            {
                "recovery_state": "started",
                "recovery_started_at": float(self._clock()),
                "successor_attempt_id": successor_attempt_id,
                "successor_run_id": successor_run_id,
            }
        )
        _started_attempt, started = self.store.compare_and_set_attempt_metadata(
            attempt.attempt_id,
            key="provider_completion",
            expected_present=True,
            expected_value=claimed_completion,
            value=started_completion,
        )
        self._pending_provider_recoveries.pop(attempt.attempt_id, None)
        if not started:
            if self._provider_cancel is not None:
                try:
                    await self._provider_cancel(successor_run_id)
                except Exception:
                    logger.exception(
                        "failed to cancel unauthorized recovery successor %s",
                        successor_run_id,
                    )
            return True

        # Speak only after a durable, cancellable successor exists. A note
        # delivery failure cannot undo or reclassify that successor.
        summary = (
            "The execution turn stopped after a progress update before changing the "
            "workspace, so Amadeus is continuing the same authorized task once."
        )
        note = work_note_payload(
            source="work_ledger",
            provider=attempt.provider,
            run_id=successor_run_id,
            session_id=self._attempt_session_id(attempt, metadata),
            phase="Progress",
            title="Task execution is continuing",
            summary=summary,
            signals=[
                work_signal(
                    label="recovery",
                    text=summary,
                    detail="work.provider_progress_only_recovery",
                    kind="status",
                    importance="important",
                    ref=attempt.work_item_id,
                )
            ],
            importance="important",
            observer_policy="auto",
            metadata={
                "work_event": "work.provider_progress_only_recovery",
                "work_item_id": attempt.work_item_id,
                "attempt_id": successor_attempt_id,
                "predecessor_attempt_id": attempt.attempt_id,
                "delivery_id": f"attempt:{attempt.attempt_id}:progress_only_recovery",
                "narration_keypoint": "semantic_progress",
            },
            speak=True,
        )
        try:
            await self.publish_snapshot(reason="provider.progress_only_recovery")
            add_work_note(note)
            await bus.emit(Method.CHAT_WORK_NOTE, note)
        except Exception:
            logger.exception(
                "failed to publish progress-only recovery note for %s",
                successor_run_id,
            )
        return True

    def _claim_export_permission_notice(
        self,
        attempt: RunAttemptRecord,
        permission: PermissionRequestRecord | None,
        *,
        run_id: str,
        provider_metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Claim and build one spoken notice for an actionable Desktop export.

        Retrospective provider permission diagnostics are deny-only and must never
        enter this path. The marker is written before the subsequent snapshot
        publish so recording it cannot make the permission card's revision
        stale immediately after rendering.
        """

        if (
            permission is None
            or permission.status != "pending"
            or not self._is_desktop_export_permission(permission)
            or "allow_once" not in permission.options
        ):
            return None
        current_attempt = self.store.get_attempt(attempt.attempt_id) or attempt
        if (
            str(current_attempt.metadata.get("desktop_export_permission_notice_id") or "")
            == permission.request_id
        ):
            return None
        entries = (
            permission.metadata.get("entries")
            if isinstance(permission.metadata.get("entries"), list)
            else []
        )
        targets = [
            str(entry.get("target_path") or "").strip()
            for entry in entries
            if isinstance(entry, dict) and str(entry.get("target_path") or "").strip()
        ]
        if not targets:
            return None
        filenames = [target.replace("\\", "/").rsplit("/", 1)[-1] or target for target in targets]
        label = ", ".join(filenames[:3])
        if len(filenames) > 3:
            label = f"{label} and {len(filenames) - 3} more files"
        summary = f"{label} is ready and needs approval before it can be copied to Desktop."
        session_id = str(
            provider_metadata.get("session_id")
            or provider_metadata.get("sessionId")
            or ""
        )
        note = work_note_payload(
            source="work_ledger",
            provider=attempt.provider,
            run_id=run_id or attempt.provider_run_id or attempt.attempt_id,
            session_id=session_id,
            phase="Checkpoint",
            title="Desktop export approval required",
            summary=summary,
            signals=[
                work_signal(
                    label="permission",
                    text=summary,
                    detail="; ".join(targets[:3]),
                    kind="permission",
                    importance="important",
                    ref=permission.request_id,
                )
            ],
            importance="important",
            observer_policy="auto",
            metadata={
                "attention": "permission",
                "permission_actionable": True,
                "permission_kind": "desktop_export",
                "permission_action": "allow_once",
                "permission_request_id": permission.request_id,
                "permission_targets": targets,
                "permission_filenames": filenames,
                "work_item_id": permission.work_item_id,
                "attempt_id": permission.attempt_id,
                "narration_keypoint": "export_staged",
            },
            speak=True,
        )
        self.store.update_attempt(
            attempt.attempt_id,
            metadata={"desktop_export_permission_notice_id": permission.request_id},
        )
        return note

    def _defer_terminal_work_notice(
        self,
        attempt: RunAttemptRecord,
        notice: dict[str, Any],
    ) -> None:
        """Persist an assessed execution report until export truth is known."""

        current = self.store.get_attempt(attempt.attempt_id) or attempt
        existing = (
            current.metadata.get("deferred_terminal_narration")
            if isinstance(current.metadata.get("deferred_terminal_narration"), dict)
            else {}
        )
        if str(existing.get("resolved_by") or "").strip():
            # A replayed provider.result must not reopen an export terminal
            # boundary that already committed or was declined.
            return
        summary = " ".join(str(notice.get("summary") or "").split()).strip()
        self.store.update_attempt(
            attempt.attempt_id,
            metadata={
                "deferred_terminal_narration": {
                    "title": str(notice.get("title") or "Task finished"),
                    "summary": summary[:2400],
                    "session_id": str(notice.get("session_id") or ""),
                    "importance": str(notice.get("importance") or "important"),
                    "metadata": dict(notice.get("metadata") or {}),
                    "deferred_at": float(self._clock()),
                    "reason": "desktop_export_pending",
                }
            },
        )

    @staticmethod
    def _terminal_narration_summary(decision, result: str, error: str) -> str:
        """What the character may say about how this attempt ended.

        The provider's own closing text is a claim, not a fact: on 2026-07-31 a
        run whose every tool call was denied still exited 0 and signed off with
        "chess game complete, saved to Desktop" while the disk held nothing. The
        ledger had already cross-checked the tool evidence against the git delta
        and written the honest sentence into `rationale` — nobody read it out.

        So the provider's wording is only repeated when the evidence agrees with
        it. Otherwise the assessment speaks, and the provider's claim stays in
        the Report panel, where it is shown as something that was reported
        rather than said in the character's own voice.
        """

        rationale = str(getattr(decision, "rationale", "") or "").strip()
        completeness = str(getattr(decision, "completeness", "") or "").strip()
        attention = str(getattr(decision, "attention", "") or "").strip()
        # `review` is where every finished attempt lands on its way to the
        # Accept gate, so it is a disposition rather than a disagreement.
        # Treating it as one would replace the report on every successful run
        # with a sentence about needing review, which is both duller and less
        # true. Only evidence that positively contradicts or blocks the claim
        # takes the telling away from the provider.
        contradicted = (
            attention in {"conflict", "error", "permission", "input"}
            or completeness == "incomplete"
        )
        if contradicted:
            return rationale or str(error or "").strip() or str(result or "").strip()
        return str(result or "").strip() or rationale or str(error or "").strip()

    def _supersede_pending_export_for_amend(
        self,
        item: WorkItemRecord,
        metadata: dict[str, Any],
    ) -> None:
        """Expire an obsolete uncommitted export when the user revises its Work.

        An export permission is approval for one immutable Attempt revision.
        A later explicit amendment of that same WorkItem makes the older bytes
        ineligible for delivery; asking the user to approve them first is both
        misleading and a deadlock. Other permission kinds remain blocking.
        """

        pending = self.store.list_permission_requests(
            item.work_item_id,
            status="pending",
        )
        export_permissions = [
            request for request in pending if self._is_desktop_export_permission(request)
        ]
        if not export_permissions:
            return
        now = float(self._clock())
        source_turn_id = str(metadata.get("turn_id") or "").strip()
        for permission in export_permissions:
            self.store.resolve_permission_request(
                permission.request_id,
                "expired",
                metadata={
                    "resolution": "superseded_by_work_amendment",
                    **({"superseding_turn_id": source_turn_id} if source_turn_id else {}),
                },
            )
            attempt = self.store.get_attempt(permission.attempt_id)
            if attempt is None:
                continue
            update: dict[str, Any] = {
                "export_superseded_by_amendment": {
                    "permission_request_id": permission.request_id,
                    "superseded_at": now,
                    **({"turn_id": source_turn_id} if source_turn_id else {}),
                }
            }
            deferred = (
                attempt.metadata.get("deferred_terminal_narration")
                if isinstance(
                    attempt.metadata.get("deferred_terminal_narration"),
                    dict,
                )
                else {}
            )
            if deferred and not str(deferred.get("resolved_by") or "").strip():
                update["deferred_terminal_narration"] = {
                    **deferred,
                    "resolved_by": "superseded_by_work_amendment",
                    "resolved_at": now,
                }
            outbox = self._terminal_notice_outbox(attempt)
            changed = False
            for record in outbox:
                if str(record.get("state") or "pending") != "pending":
                    continue
                record["state"] = "superseded"
                record["updated_at"] = now
                record["superseded_at"] = now
                record["reason"] = "successor_work_amendment"
                record.pop("note", None)
                changed = True
            if changed:
                update[_TERMINAL_NOTICE_OUTBOX_KEY] = self._bound_terminal_notice_outbox(
                    outbox
                )
            self.store.update_attempt(attempt.attempt_id, metadata=update)
            logger.info(
                "[WORK-EXPORT] superseded pending export permission=%s attempt=%s "
                "before Work amendment",
                permission.request_id,
                attempt.attempt_id,
            )

    def _claim_terminal_work_notice(
        self,
        attempt: RunAttemptRecord,
        *,
        delivery_id: str,
        title: str,
        summary: str,
        session_id: str = "",
        importance: str = "important",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Put one terminal narration note in the attempt-owned durable outbox.

        Claiming is not delivery.  Repeated provider results do not enqueue a
        duplicate; the explicit recovery pass replays pending records after
        startup.  Only the Observer's output-boundary receipt moves a note to
        ``delivered``.  This closes the old crash/stall gap where an id was
        persisted before the note had even reached the Observer and therefore
        suppressed every later replay.
        """

        clean_delivery_id = str(delivery_id or "").strip()
        if not clean_delivery_id:
            return None
        current_attempt = self.store.get_attempt(attempt.attempt_id) or attempt
        note_metadata = {
            "work_item_id": attempt.work_item_id,
            "attempt_id": attempt.attempt_id,
            "delivery_id": clean_delivery_id,
            "narration_keypoint": "terminal",
            **dict(metadata or {}),
        }
        note = work_note_payload(
            source="work_ledger",
            provider=attempt.provider,
            run_id=attempt.provider_run_id or attempt.attempt_id,
            session_id=str(session_id or self._attempt_session_id(current_attempt)),
            phase="Result",
            title=title,
            summary=summary,
            signals=[
                work_signal(
                    label="result",
                    text=summary,
                    detail=str(note_metadata.get("work_event") or "work.finished"),
                    kind="status",
                    importance=importance,
                    ref=attempt.work_item_id,
                )
            ],
            importance=importance,
            observer_policy="auto",
            metadata=note_metadata,
            speak=True,
        )
        outbox = self._terminal_notice_outbox(current_attempt)
        existing = next(
            (
                record
                for record in outbox
                if str(record.get("delivery_id") or "") == clean_delivery_id
            ),
            None,
        )
        if existing is not None:
            return None

        # Records written by older builds represented a completed claim even
        # though no delivery receipt existed.  Preserve their idempotence; new
        # records use the outbox state machine below.
        legacy_delivered = {
            str(value)
            for value in (current_attempt.metadata.get("terminal_work_notice_ids") or [])
            if str(value)
        }
        if clean_delivery_id in legacy_delivered:
            return None
        now = float(self._clock())
        outbox = self._bound_terminal_notice_outbox(
            [
                *outbox,
                {
                    "delivery_id": clean_delivery_id,
                    "state": "pending",
                    "note": note,
                    "created_at": now,
                    "updated_at": now,
                },
            ]
        )
        self.store.update_attempt(
            attempt.attempt_id,
            metadata={_TERMINAL_NOTICE_OUTBOX_KEY: outbox},
        )
        return note

    @staticmethod
    def _terminal_notice_outbox(attempt: RunAttemptRecord) -> list[dict[str, Any]]:
        value = attempt.metadata.get(_TERMINAL_NOTICE_OUTBOX_KEY)
        if not isinstance(value, list):
            return []
        return [dict(record) for record in value if isinstance(record, dict)]

    @staticmethod
    def _bound_terminal_notice_outbox(
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Bound delivered audit history without ever discarding work owed."""

        pending = [
            dict(record)
            for record in records
            if str(record.get("state") or "pending") == "pending"
        ]
        settled = [
            dict(record)
            for record in records
            if str(record.get("state") or "pending") != "pending"
        ]
        settled_budget = max(0, _MAX_TERMINAL_NOTICE_RECORDS - len(pending))
        return [*settled[-settled_budget:], *pending] if settled_budget else pending

    async def _on_terminal_work_notice_delivered(
        self,
        _method: str,
        params: dict[str, Any],
    ) -> None:
        attempt_id = str(params.get("attempt_id") or "").strip()
        delivery_id = str(params.get("delivery_id") or "").strip()
        if not attempt_id or not delivery_id:
            return
        attempt = self.store.get_attempt(attempt_id)
        if attempt is None:
            return
        outbox = self._terminal_notice_outbox(attempt)
        changed = False
        now = float(self._clock())
        for record in outbox:
            if str(record.get("delivery_id") or "") != delivery_id:
                continue
            if str(record.get("state") or "pending") == "delivered":
                return
            record["state"] = "delivered"
            record["updated_at"] = now
            record["delivered_at"] = now
            # The immutable attempt/result facts can reconstruct history; once
            # delivered, retaining a duplicate narration payload only bloats
            # every future Slice snapshot.
            record.pop("note", None)
            changed = True
            break
        if not changed:
            return
        delivered = [
            str(value)
            for value in (attempt.metadata.get("terminal_work_notice_ids") or [])
            if str(value)
        ]
        if delivery_id not in delivered:
            delivered = [*delivered[-15:], delivery_id]
        self.store.update_attempt(
            attempt_id,
            metadata={
                _TERMINAL_NOTICE_OUTBOX_KEY: self._bound_terminal_notice_outbox(outbox),
                "terminal_work_notice_ids": delivered,
            },
        )
        logger.info(
            "[WORK-NARRATION] terminal delivery acknowledged attempt=%s delivery=%s",
            attempt_id,
            delivery_id,
        )

    async def replay_pending_terminal_notices(self) -> int:
        """Replay pending terminal outbox entries after the Observer is ready."""

        pending: list[dict[str, Any]] = []
        for item in self.store.list_work_items(limit=1000):
            for attempt in self.store.list_attempts(item.work_item_id):
                for record in self._terminal_notice_outbox(attempt):
                    if str(record.get("state") or "pending") != "pending":
                        continue
                    note = record.get("note")
                    if isinstance(note, dict):
                        pending.append(dict(note))
        for note in pending:
            add_work_note(note)
            await bus.emit(Method.CHAT_WORK_NOTE, note)
        if pending:
            logger.warning(
                "[WORK-NARRATION] replayed %d pending terminal notice(s)",
                len(pending),
            )
        return len(pending)

    @staticmethod
    def _attempt_session_id(
        attempt: RunAttemptRecord,
        provider_metadata: dict[str, Any] | None = None,
    ) -> str:
        direct = dict(provider_metadata or {})
        stored_result = (
            attempt.metadata.get("provider_result")
            if isinstance(attempt.metadata.get("provider_result"), dict)
            else {}
        )
        return str(
            direct.get("session_id")
            or direct.get("sessionId")
            or attempt.metadata.get("session_id")
            or stored_result.get("session_id")
            or stored_result.get("sessionId")
            or ""
        ).strip()

    @staticmethod
    def _missing_item() -> WorkItemRecord:  # pragma: no cover - FK guard
        raise WorkLedgerNotFound("attempt references a missing work item")

    def _attempt_for_event(
        self,
        params: dict[str, Any],
        *,
        adopt: bool,
    ) -> RunAttemptRecord | None:
        return self.event_ingestor.attempt_for_event(params, adopt=adopt)

    def _adopt_runtime_run(self, params: dict[str, Any]) -> RunAttemptRecord | None:
        return self.event_ingestor.adopt_runtime_run(params)

    @staticmethod
    def _event_execution_status(value: str) -> str:
        return ProviderEventIngestor.execution_status(value)

    def _event_fact(self, run_id: str) -> dict[str, Any]:
        return self.event_ingestor.event_fact(run_id)

    @staticmethod
    def _provider_permission_event_identity(
        payload: dict[str, Any],
        event_metadata: dict[str, Any],
    ) -> str:
        """Identify one delivered denial event without conflating its audit group."""

        nested = next(
            (
                payload.get(key)
                for key in ("permissionRequest", "permission_request", "request", "permission")
                if isinstance(payload.get(key), dict)
            ),
            None,
        )
        source = nested if isinstance(nested, dict) else payload
        event_id = str(
            source.get("request_id")
            or source.get("requestId")
            or source.get("id")
            or source.get("toolUseId")
            or source.get("tool_use_id")
            or ""
        ).strip()
        if event_id:
            return event_id[:240]
        material = json.dumps(source, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

    def _upsert_permission_event(
        self,
        attempt: RunAttemptRecord,
        *,
        run_id: str,
        provider: str,
        payload: dict[str, Any],
        event_metadata: dict[str, Any],
    ) -> PermissionRequestRecord:
        """Persist a bounded provider contract; close retrospective denials."""

        nested = next(
            (
                payload.get(key)
                for key in ("permissionRequest", "permission_request", "request", "permission")
                if isinstance(payload.get(key), dict)
            ),
            None,
        )
        source = nested if isinstance(nested, dict) else payload
        scope_value = source.get("scope_paths", source.get("scope"))
        scope_paths: list[str] = []
        if isinstance(scope_value, dict):
            candidate = str(scope_value.get("path") or "").strip()
            if candidate:
                scope_paths.append(candidate[:2048])
            elif str(scope_value.get("kind") or "").strip().lower() in {
                "workspace",
                "project",
                "repository",
            }:
                item = self.store.get_work_item(attempt.work_item_id)
                if item is not None:
                    scope_paths.append(str(item.workspace_path)[:2048])
        elif isinstance(scope_value, list):
            for value in scope_value[:32]:
                candidate = str(value.get("path") if isinstance(value, dict) else value).strip()
                if candidate:
                    scope_paths.append(candidate[:2048])
        elif scope_value not in (None, ""):
            scope_paths.append(str(scope_value).strip()[:2048])

        diagnostic_only = source.get("diagnosticOnly") is True or source.get(
            "diagnostic_only"
        ) is True

        raw_options = source.get("options")
        if raw_options in (None, ""):
            raw_options = source.get("choices")
        option_values = raw_options if isinstance(raw_options, list) else [raw_options]
        normalized_options: list[str] = []
        for value in option_values[:8]:
            if isinstance(value, dict):
                value = value.get("kind") or value.get("id") or value.get("value")
            option = str(value or "").strip().lower()
            aliases = {
                "approve_once": "allow_once",
                "allow": "allow_once",
                "approved": "allow_once",
                "reject": "deny",
                "reject_once": "deny",
                "dismiss": "deny",
            }
            option = aliases.get(option, option)
            if option in {"allow_once", "deny"} and option not in normalized_options:
                normalized_options.append(option)
        options = ["deny"] if diagnostic_only else normalized_options or ["deny"]

        provider_request_id = str(
            source.get("request_id")
            or source.get("requestId")
            or source.get("id")
            or source.get("toolUseId")
            or source.get("tool_use_id")
            or ""
        ).strip()[:240]
        if not provider_request_id:
            material = json.dumps(
                {
                    "capability": source.get("capability"),
                    "action": source.get("action"),
                    "scope": scope_paths,
                    "reason": source.get("reason"),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            provider_request_id = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
        clean_provider = str(provider or attempt.provider or "provider").strip().lower()[:80]
        # Resumability is declared by the canonical event, not inferred from a
        # provider name.  Adapters whose permission arrives after a denial set
        # diagnosticOnly=true; bidirectional providers may leave it false and
        # retain their explicit checkpoint contract.
        retrospective = diagnostic_only
        raw_capability = str(source.get("capability") or "tool.execute").strip()[:120]
        raw_action = str(source.get("action") or "invoke_tool").strip()[:120]
        raw_reason_code = str(source.get("reasonCode") or source.get("reason_code") or "")[:120]
        capability = raw_capability.casefold() if retrospective else raw_capability
        action = raw_action.casefold() if retrospective else raw_action
        reason_code = raw_reason_code.casefold() if retrospective else raw_reason_code
        normalized_scope = sorted(
            {path.replace("\\", "/").casefold() for path in scope_paths}
        )
        diagnostic_material = json.dumps(
            {
                "capability": capability,
                "action": action,
                "scope": normalized_scope,
                "reason_code": reason_code,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        diagnostic_signature = hashlib.sha256(
            diagnostic_material.encode("utf-8")
        ).hexdigest()[:24]
        request = self.store.create_permission_request(
            attempt.work_item_id,
            attempt_id=attempt.attempt_id,
            capability=capability,
            action=action,
            scope_paths=scope_paths,
            reason=str(source.get("reason") or "Explicit user approval is required.").strip()[:1000],
            reversibility=str(source.get("reversibility") or "unknown").strip()[:240],
            options=options,
            idempotency_key=(
                f"provider:{clean_provider}:{attempt.attempt_id}:diagnostic:{diagnostic_signature}"
                if retrospective
                else f"provider:{clean_provider}:{run_id}:{provider_request_id}"
            ),
            metadata={
                "kind": "provider_permission",
                "provider": clean_provider,
                "provider_run_id": run_id,
                "provider_request_id": provider_request_id,
                "reason_code": reason_code,
                "retry_required": source.get("retryRequired") is True
                or source.get("retry_required") is True,
                "diagnostic_only": retrospective,
                "diagnostic_signature": diagnostic_signature if retrospective else "",
            },
        )
        if not retrospective or request.status != "pending":
            return request
        try:
            return self.store.resolve_permission_request(
                request.request_id,
                "denied",
                metadata={
                    "resolution": "provider_denied",
                    "resolved_automatically": True,
                },
            )
        except WorkLedgerConflict:
            # Duplicate delivery can race across surfaces/processes. The
            # permission store resolves pending rows atomically, so the first
            # denial remains authoritative.
            return self.store.get_permission_request(request.request_id) or request

    def _heartbeat_lease(self, attempt_id: str) -> None:
        if self.store.get_writer_lease(attempt_id) is None:
            return
        try:
            self.store.heartbeat_writer_lease(attempt_id)
        except (WorkLedgerConflict, WorkLedgerNotFound):
            logger.debug("writer lease heartbeat ignored for %s", attempt_id)

    def _record_tool_evidence(self, run_id: str, payload: dict[str, Any]) -> None:
        ok_value = payload.get("ok")
        success_value = payload.get("success")
        status = str(payload.get("status") or "").strip().lower()
        failed = ok_value is False or success_value is False or status in {"error", "failed", "failure"}
        if failed:
            # Tool failures are execution telemetry, not terminal business
            # truth. An agent may recover with another tool or validation
            # path. Completion is owned by the terminal provider result plus
            # Host-observed artifacts/outcomes, validation facts, permissions
            # and inputs. Preserve this failure for audit without minting a
            # conflict or a permanently pending validation.
            # Pair policy denial and failed invocation by canonical tool-use
            # identity.  A count is retained only for legacy events that did
            # not carry an id; a mismatched id never consumes that fallback.
            facts = self._event_fact(run_id)
            tool_use_id = self._tool_use_id(payload)
            permission_tool_ids = facts.setdefault(
                "provider_permission_tool_ids", []
            )
            suppressions = int(facts.get("permission_failure_suppressions") or 0)
            if (
                tool_use_id
                and isinstance(permission_tool_ids, list)
                and tool_use_id in permission_tool_ids
            ):
                permission_tool_ids.remove(tool_use_id)
                facts["permission_failure_suppressions"] = max(0, suppressions - 1)
                return
            if not tool_use_id and suppressions > 0:
                facts["permission_failure_suppressions"] = suppressions - 1
                if isinstance(permission_tool_ids, list) and permission_tool_ids:
                    permission_tool_ids.pop(0)
                return
            if int(facts.get("pending_permissions") or 0) > 0:
                return
            label = str(payload.get("tool") or payload.get("name") or "tool")
            unverified = self._is_unverified_shell_failure(payload)
            diagnostics = facts.setdefault("tool_diagnostics", [])
            if isinstance(diagnostics, list) and len(diagnostics) < 16:
                diagnostics.append(
                    {
                        "tool": label[:120],
                        "tool_use_id": tool_use_id,
                        "classification": "unverified" if unverified else "failed",
                        "reason": (
                            "tool reported failure without error evidence"
                            if unverified
                            else "tool invocation failed before terminal outcome assessment"
                        ),
                    }
                )

    @staticmethod
    def _tool_use_id(payload: dict[str, Any]) -> str:
        candidates = [payload]
        raw = payload.get("raw")
        if isinstance(raw, dict):
            candidates.append(raw)
        for key in ("permissionRequest", "permission_request", "request", "permission"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
        for candidate in candidates:
            value = str(
                candidate.get("tool_use_id")
                or candidate.get("toolUseId")
                or ""
            ).strip()
            if value:
                return value[:240]
        return ""

    @staticmethod
    def _is_unverified_shell_failure(payload: dict[str, Any]) -> bool:
        """Classify an evidence-free shell miss as unknown, not contradiction.

        A structured error is positive failure evidence and remains a conflict.
        A bare ``ok=false`` from a shell can equally mean a blocked preview or
        syntax check; it makes validation unavailable but does not contradict
        independently registered artifacts.
        """

        tool = str(payload.get("tool") or payload.get("name") or "").strip().casefold()
        if tool not in {"bash", "cmd", "command", "powershell", "shell", "terminal"}:
            return False
        candidates = [payload]
        raw = payload.get("raw")
        if isinstance(raw, dict):
            candidates.append(raw)
        for candidate in candidates:
            for key in (
                "error",
                "errorMessage",
                "error_message",
                "message",
                "reason",
                "stderr",
            ):
                if str(candidate.get(key) or "").strip():
                    return False
        return True

    def _record_tool_artifact_hints(self, run_id: str, payload: dict[str, Any]) -> None:
        tool = str(payload.get("tool") or payload.get("name") or "").strip().lower()
        if tool not in {"write", "edit", "multiedit", "notebookedit", "create_file", "write_file"}:
            return
        raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else payload
        hints: list[str] = []

        def visit(value: Any, key: str = "") -> None:
            if len(hints) >= 32:
                return
            if isinstance(value, dict):
                for child_key, child in value.items():
                    visit(child, str(child_key).strip().lower())
            elif isinstance(value, list):
                for child in value[:64]:
                    visit(child, key)
            elif key in {"path", "file", "file_path", "filepath", "notebook_path", "target_file"}:
                text = str(value or "").strip()
                if text:
                    hints.append(text)

        visit(raw)
        fact_hints = self._event_fact(run_id).setdefault("artifact_hints", [])
        if isinstance(fact_hints, list):
            known = {str(item.get("path") or "") for item in fact_hints if isinstance(item, dict)}
            for path in hints:
                if path not in known:
                    fact_hints.append({"path": path, "tool": tool})
                    known.add(path)

    # -- Artifact registry -----------------------------------------------

    def _register_artifact_payload(self, attempt: RunAttemptRecord, payload: dict[str, Any]) -> None:
        path = self._first_text(payload, ("path", "file", "file_path", "output_path"))
        uri = self._first_text(payload, ("uri", "url", "ref"))
        kind = self._first_text(payload, ("artifact_type", "kind", "type", "role")) or "provider.artifact"
        title = self._first_text(payload, ("title", "name", "label"))
        identity = ""
        if not path and not uri:
            diff_text = self._first_text(payload, ("diff", "patch", "unified_diff"))
            if diff_text:
                identity = "diff:" + hashlib.sha256(diff_text.encode("utf-8", errors="replace")).hexdigest()
                uri = "work-ledger://diff/" + identity.removeprefix("diff:")
            else:
                serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
                identity = "payload:" + hashlib.sha256(serialized.encode("utf-8", errors="replace")).hexdigest()
        if path and not Path(path).expanduser().is_absolute():
            item = self.store.get_work_item(attempt.work_item_id)
            if item is not None and item.workspace_mode != "none":
                path = str(Path(item.workspace_path) / path)
            else:
                # A relative filesystem path has no meaning for a provider
                # whose contract owns no cwd.  Keep URI/snapshot artifacts,
                # but never resolve this against the Amadeus process directory.
                path = ""
        artifact_status = None
        size_bytes = None
        modified_at = None
        if path:
            try:
                candidate = Path(path).expanduser()
                if not candidate.exists():
                    artifact_status = "missing"
                else:
                    stat = candidate.stat()
                    size_bytes = stat.st_size if candidate.is_file() else None
                    modified_at = stat.st_mtime
            except OSError:
                artifact_status = "missing"
        self.store.register_artifact(
            attempt.work_item_id,
            attempt_id=attempt.attempt_id,
            kind=kind,
            title=title or kind,
            path=path or None,
            uri=uri,
            identity=identity,
            status=artifact_status,
            size_bytes=size_bytes,
            modified_at=modified_at,
            metadata={"provider_payload": payload},
        )

    def _register_result_artifacts(
        self,
        attempt: RunAttemptRecord,
        *,
        metadata: dict[str, Any],
    ) -> None:
        artifacts = metadata.get("artifacts") if isinstance(metadata.get("artifacts"), list) else []
        for artifact in artifacts[:200]:
            if isinstance(artifact, dict):
                self._register_artifact_payload(attempt, artifact)
            elif isinstance(artifact, str):
                self._register_path(
                    attempt,
                    artifact,
                    kind="provider.artifact",
                    discovered_from="provider_artifact_result",
                )

    def _register_required_staged_outcome(
        self,
        *,
        attempt: RunAttemptRecord,
        item: WorkItemRecord | None,
        status: str,
        export_plan: dict[str, Any] | None,
    ) -> None:
        """Materialize a staged bundle only when a Host facet requires it.

        The Provider report cannot declare an application launchable.  The
        Host first snapshots the complete, bounded Attempt output through the
        ordinary artifact contract; the existing facet observer then decides
        whether those files form a valid AUIP application.  Desktop export
        selection remains independent and may still authorize only one file.
        """

        if item is None or status != "succeeded" or export_plan is None:
            return
        current = self.store.get_attempt(attempt.attempt_id) or attempt
        requirement = (
            current.metadata.get("host_outcome_requirement")
            if isinstance(current.metadata.get("host_outcome_requirement"), dict)
            else {}
        )
        if str(requirement.get("facet") or "").strip().lower() != "auip.application":
            return
        if (
            export_plan.get("host_validates_auip_bundle") is True
            and not (
                isinstance(current.metadata.get("host_auip_bundle_validation"), dict)
                and current.metadata["host_auip_bundle_validation"].get("verified")
                is True
            )
        ):
            return
        staging_root, files = self.export_service.validated_staging_files(
            current,
            item,
            export_plan,
        )
        artifact_ids = self.artifact_registry.register_attempt_files(
            current,
            item,
            root=staging_root,
            files=files,
            attribution="host_outcome:auip.application",
        )
        self.store.update_attempt(
            current.attempt_id,
            metadata={
                "host_outcome_materialization": {
                    "facet": "auip.application",
                    "artifact_ids": artifact_ids,
                    "file_count": len(artifact_ids),
                }
            },
        )

    def _register_path(
        self,
        attempt: RunAttemptRecord,
        raw_path: str,
        *,
        kind: str,
        discovered_from: str,
    ) -> None:
        path = str(raw_path or "").strip().strip("`'\".,;:()[]{}")
        if not path:
            return
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            item = self.store.get_work_item(attempt.work_item_id)
            if item is None or item.workspace_mode == "none":
                return
            candidate = Path(item.workspace_path) / candidate
        try:
            if not candidate.exists():
                return
            stat = candidate.stat()
        except OSError:
            return
        self.store.register_artifact(
            attempt.work_item_id,
            attempt_id=attempt.attempt_id,
            kind=kind,
            title=candidate.name or kind,
            path=str(candidate),
            size_bytes=stat.st_size if candidate.is_file() else None,
            modified_at=stat.st_mtime,
            metadata={"discovered_from": discovered_from},
        )

    @staticmethod
    def _first_text(payload: dict[str, Any], keys: Iterable[str]) -> str:
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    # -- Completion evidence --------------------------------------------

    @staticmethod
    def _explicit_completion(metadata: dict[str, Any]) -> bool:
        # Provider/request metadata is evidence, not a trusted host verdict.
        # P0 has no validation authority that can mint criteria_satisfied yet,
        # so successful execution remains partial/review_ready until the user
        # accepts it. This prevents a provider from self-certifying completion.
        return False

    @staticmethod
    def _expected_artifact_count(metadata: dict[str, Any]) -> int:
        completion = metadata.get("work_completion") if isinstance(metadata.get("work_completion"), dict) else {}
        try:
            return max(0, int(completion.get("expected_artifact_count") or 0))
        except Exception:
            return 0

    @staticmethod
    def _validation_statuses(metadata: dict[str, Any]) -> list[str]:
        completion = metadata.get("work_completion") if isinstance(metadata.get("work_completion"), dict) else {}
        values = completion.get("validations") if isinstance(completion.get("validations"), list) else []
        return [str(value.get("status") if isinstance(value, dict) else value) for value in values]

    @staticmethod
    def _compatibility_blockers(result: str, metadata: dict[str, Any]) -> dict[str, Any]:
        """Bounded fallback for legacy providers without structured blockers."""
        task_completion = (
            metadata.get("task_completion")
            if isinstance(metadata.get("task_completion"), dict)
            else {}
        )
        task_outcome = str(task_completion.get("taskOutcome") or "").strip().lower()
        if task_outcome in {"achieved", "blocked", "needs_input", "unverified"}:
            summary = str(task_completion.get("summary") or "").strip()
            blocker = str(task_completion.get("blocker") or "").strip()
            missing: list[str] = []
            if task_outcome == "blocked":
                missing.append(blocker or summary or "the provider reported a task blocker")
            elif task_outcome == "unverified":
                missing.append("the provider task outcome was not verified")
            return {
                "pending_permissions": 0,
                "pending_inputs": int(task_outcome == "needs_input"),
                "missing_requirements": missing,
                "conflicts": (),
                "used": True,
                "source": "task_completion",
                "task_outcome": task_outcome,
            }
        lowered = " ".join(str(result or "").lower().split())[:20_000]
        result_type = str(metadata.get("result_type") or "").strip().lower()
        pending_permissions = int(
            any(
                phrase in lowered
                for phrase in (
                    "please approve",
                    "once approved",
                    "approval is required",
                    "permission is required",
                    "需要批准",
                    "等待批准",
                    "需要权限",
                )
            )
        )
        pending_inputs = int(result_type == "question")
        missing: list[str] = []
        if result_type == "partial":
            missing.append("provider classified the result as partial")
        return {
            "pending_permissions": pending_permissions,
            "pending_inputs": pending_inputs,
            "missing_requirements": missing,
            "conflicts": (),
            "used": bool(pending_permissions or pending_inputs or missing),
        }

    # -- Durable task projection ----------------------------------------

    def workspace_routing_focus(self) -> dict[str, Any]:
        """Return the one global workspace lock used by chat/delegate routing."""

        return self.destination.workspace_routing_focus()

    def workspace_routing_context(self, *, limit: int = 8) -> dict[str, Any]:
        """Build a compact, trusted Project map for the main intent model."""

        return self.destination.workspace_routing_context(limit=limit)

    def project_catalog(
        self,
        *,
        limit: int = 100,
        projected_items: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the durable, routeable Project catalog shared by both UIs.

        This is presentation data, not prompt context: it may contain more than
        the model's deliberately small candidate set, but it applies the same
        trust and availability boundary.
        """

        projections = (
            list(projected_items)
            if projected_items is not None
            else [
                self._project_item(item)
                for item in self.store.list_work_items(limit=2000)
            ]
        )
        by_project: dict[str, list[dict[str, Any]]] = {}
        for item in projections:
            by_project.setdefault(str(item.get("projectId") or ""), []).append(item)
        catalog: list[dict[str, Any]] = []
        for project in self.store.list_projects():
            path = project.canonical_path
            if is_scratch_root(path):
                continue
            if not Path(path).is_dir() or not cwd_in_project_registry(path):
                continue
            projected = by_project.get(project.project_id, [])
            current = [
                item for item in projected
                if item.get("state") not in {"accepted", "archived"}
            ]
            needs_you = [
                item for item in current
                if item.get("attention") not in {"", "none"}
            ]
            running = [
                item for item in current
                if item.get("execution") in _ACTIVE_EXECUTION
            ]
            latest = projected[0] if projected else {}
            catalog.append(
                {
                    "projectId": project.project_id,
                    "name": project.name or Path(path).name,
                    "state": project.state,
                    "workspacePath": path,
                    "updatedAt": self._iso_time(project.updated_at),
                    "latestWorkItemId": str(latest.get("id") or ""),
                    "latestTaskTitle": str(latest.get("title") or ""),
                    "counts": {
                        "current": len(current),
                        "needsYou": len(needs_you),
                        "running": len(running),
                        "history": max(0, len(projected) - len(current)),
                    },
                }
            )
            if len(catalog) >= max(1, min(int(limit), 200)):
                break
        return catalog

    def create_project(self, workspace_path: str, *, name: str = "") -> dict[str, Any]:
        """Register one existing trusted directory as a durable Project.

        Directory creation and selection belong to the desktop host. This
        method only validates the selected path against the existing Project
        Registry and records its durable identity; it never expands trust.
        """

        raw_path = str(workspace_path or "").strip()
        if not raw_path:
            raise ValueError("project_directory_required")
        try:
            workspace = Path(raw_path).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("project_directory_invalid") from exc
        if not workspace.is_dir():
            raise ValueError("project_directory_not_found")
        if not cwd_in_project_registry(str(workspace)):
            raise WorkLedgerConflict("workspace_outside_project_registry")

        existing = self.store.get_project_by_path(workspace)
        if existing is not None:
            project = existing
            created = False
        else:
            clean_name = " ".join(str(name or "").split())[:240].strip()
            project = self.store.create_or_get_project(
                workspace,
                name=clean_name,
                metadata={
                    "identity_version": _PROJECT_IDENTITY_VERSION,
                    "name_source": "user:directory",
                    "semantic_aliases": [clean_name] if clean_name else [],
                },
            )
            created = True
        return {
            "projectId": project.project_id,
            "projectName": project.name,
            "workspacePath": project.canonical_path,
            "created": created,
        }

    def resolve_workspace_route(self, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
        """Resolve a Provider workspace with the global pin as hard authority."""

        return self.destination.resolve_workspace_route(attrs)

    def resolve_project_source_references(
        self,
        project_id: str,
        references: Iterable[str],
    ) -> dict[str, Any]:
        """Verify exact current-source paths inside one persistent Project.

        WorkItems are deliveries and history; a persistent Project's canonical
        tree is the authority for what source exists *now*.  This deliberately
        accepts only exact relative paths (a bare filename means the Project
        root) and never searches old WorkItems or guesses among duplicate
        basenames.  Semantic identity stays with ControlDecision while the host
        owns this filesystem fact.
        """

        clean_project_id = str(project_id or "").strip()
        clean_references = tuple(
            dict.fromkeys(
                str(reference or "").strip().replace("\\", "/")
                for reference in references
                if str(reference or "").strip()
            )
        )
        if not clean_project_id or not clean_references:
            return {
                "status": "invalid",
                "reason": "project_source_reference_missing",
                "projectId": clean_project_id,
                "workspacePath": "",
                "files": [],
            }
        try:
            project = self._available_session_project(clean_project_id)
            root = Path(project.canonical_path).resolve()
        except (OSError, RuntimeError, ValueError, WorkLedgerConflict, WorkLedgerNotFound):
            return {
                "status": "invalid",
                "reason": "project_source_unavailable",
                "projectId": clean_project_id,
                "workspacePath": "",
                "files": [],
            }

        resolved_files: list[str] = []
        for reference in clean_references:
            relative = Path(reference)
            if (
                relative.is_absolute()
                or bool(relative.drive)
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                return {
                    "status": "invalid",
                    "reason": "project_source_reference_unsafe",
                    "projectId": clean_project_id,
                    "workspacePath": str(root),
                    "files": [],
                }
            try:
                candidate = (root / relative).resolve()
            except (OSError, RuntimeError, ValueError):
                candidate = None
            if (
                candidate is None
                or not path_is_within(str(candidate), str(root))
                or not candidate.is_file()
            ):
                return {
                    "status": "missing",
                    "reason": "project_source_file_missing",
                    "projectId": clean_project_id,
                    "workspacePath": str(root),
                    "files": [],
                }
            resolved_files.append(str(candidate.relative_to(root)).replace("\\", "/"))

        return {
            "status": "resolved",
            "reason": "",
            "projectId": clean_project_id,
            "workspacePath": str(root),
            "files": resolved_files,
        }

    @staticmethod
    def _identity_aliases(values: Iterable[Any], *, primary: str = "") -> list[str]:

        return WorkDestinationService.identity_aliases(values, primary=primary)

    @staticmethod
    def _stored_project_aliases(project) -> list[str]:
        return WorkDestinationService.stored_project_aliases(project)

    def _work_items_in_workspace(self, workspace_path: str) -> list[WorkItemRecord]:
        identity = canonicalize_path(workspace_path).identity_key
        return [
            item
            for item in self.store.list_work_items(limit=2000)
            if str(item.workspace_path or "").strip()
            and canonicalize_path(item.workspace_path).identity_key == identity
        ]

    def _project_identity_from_workspace(
        self,
        selected: WorkItemRecord,
        items: Iterable[WorkItemRecord],
        *,
        previous_name: str = "",
    ) -> tuple[str, dict[str, Any]]:
        """Derive one concise generated name plus durable lookup aliases."""

        rows = list(items)
        if all(row.work_item_id != selected.work_item_id for row in rows):
            rows.append(selected)
        rows.sort(key=lambda row: (float(row.created_at), row.work_item_id))
        ordered = [selected, *(row for row in rows if row.work_item_id != selected.work_item_id)]

        artifact_names: list[str] = []
        selected_export_stems: list[str] = []
        all_export_stems: list[str] = []
        for row in ordered:
            for artifact in self.store.list_artifacts(row.work_item_id):
                if artifact.kind not in {
                    "business.export",
                    "business.proposed_export",
                }:
                    continue
                relative = str((artifact.metadata or {}).get("relative_path") or "").strip()
                filename = Path(relative or artifact.path or artifact.title).name
                if not filename:
                    continue
                artifact_names.append(filename)
                if artifact.kind != "business.export" or artifact.status != "approved":
                    continue
                stem = Path(filename).stem.strip()
                if not stem:
                    continue
                all_export_stems.append(stem)
                if row.work_item_id == selected.work_item_id:
                    selected_export_stems.append(stem)

        generated_name = next(
            iter(selected_export_stems or all_export_stems),
            "",
        )
        generated_name = generated_name or selected.title or Path(selected.workspace_path).name
        source_user_texts: list[str] = []
        for row in ordered:
            attempts = self.store.list_attempts(row.work_item_id)
            source_user_text = str(
                (attempts[-1].metadata if attempts else {}).get("source_user_text")
                or (row.metadata or {}).get("source_user_text")
                or ""
            ).strip()
            if source_user_text:
                source_user_texts.append(source_user_text)

        alias_values: list[Any] = [previous_name, selected.title]
        alias_values.extend(source_user_texts)
        alias_values.extend(artifact_names)
        for row in rows:
            alias_values.append(row.title)
        aliases = self._identity_aliases(alias_values, primary=generated_name)
        return generated_name, {
            "identity_version": _PROJECT_IDENTITY_VERSION,
            "name_source": "generated:business_artifact" if (selected_export_stems or all_export_stems) else "generated:work_item",
            "semantic_aliases": aliases,
        }

    def add_project_alias(self, project_id: str, alias: str) -> dict[str, Any]:
        """Persist one user-authored Project name without changing its path."""

        project = self.store.get_project(str(project_id or "").strip())
        if project is None:
            raise WorkLedgerNotFound(f"unknown project: {project_id}")
        clean_alias = " ".join(str(alias or "").split())[:240].strip()
        if not clean_alias:
            raise ValueError("project alias is required")
        aliases = self._identity_aliases(
            # An explicit user-authored name is stronger identity evidence than
            # aliases captured automatically when the Draft was promoted. Put
            # it first so a full bounded set evicts an old generated tail
            # instead of silently discarding what the user just named.
            [clean_alias, *self._stored_project_aliases(project)],
            primary=project.name,
        )
        updated = self.store.create_or_get_project(
            project.canonical_path,
            name=project.name,
            metadata={
                "identity_version": _PROJECT_IDENTITY_VERSION,
                "semantic_aliases": aliases,
            },
        )
        return {
            "projectId": updated.project_id,
            "projectName": updated.name,
            "projectAliases": aliases,
        }

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _materialize_approved_exports(
        self,
        workspace_path: str,
        items: Iterable[WorkItemRecord],
    ) -> dict[str, list[str]]:
        """Put approved deliverables in the Project root without overwriting.

        Export staging remains an approval transaction. Promotion is the later
        declaration that this directory is a durable editable Project, so an
        approved deliverable that exists only under ``.amadeus`` or on Desktop
        must acquire a source copy in that directory. Existing files always win;
        the ledger snapshot is never allowed to overwrite newer local work.
        """

        workspace = Path(workspace_path).resolve()
        latest: dict[str, Any] = {}
        for item in items:
            for artifact in self.store.list_artifacts(item.work_item_id):
                if artifact.kind != "business.export" or artifact.status != "approved":
                    continue
                relative_text = str((artifact.metadata or {}).get("relative_path") or "").strip()
                if not relative_text:
                    relative_text = Path(artifact.path).name
                relative = Path(relative_text)
                if (
                    not relative_text
                    or relative.is_absolute()
                    or any(part in {"", ".", ".."} for part in relative.parts)
                    or (relative.parts and relative.parts[0].casefold() == ".amadeus")
                ):
                    raise WorkLedgerConflict(
                        f"approved export has an unsafe project-relative path: {relative_text!r}"
                    )
                key = str(relative).replace("\\", "/").casefold()
                current = latest.get(key)
                if current is None or float(artifact.updated_at) > float(current.updated_at):
                    latest[key] = artifact

        plans: list[tuple[Path, Path, str]] = []
        existing: list[str] = []
        missing: list[str] = []
        for artifact in latest.values():
            relative_text = str((artifact.metadata or {}).get("relative_path") or "").strip()
            relative = Path(relative_text or Path(artifact.path).name)
            destination = workspace / relative
            if not path_is_within(str(destination), str(workspace)):
                raise WorkLedgerConflict(
                    f"approved export escapes the promoted project: {relative}"
                )
            if destination.exists():
                if not destination.is_file():
                    raise WorkLedgerConflict(
                        f"project source destination is not a file: {relative}"
                    )
                existing.append(str(relative))
                continue

            expected_hash = str(artifact.sha256 or "").strip().lower()
            source_values = [
                str((artifact.metadata or {}).get("source_path") or "").strip(),
                str(artifact.path or "").strip(),
            ]
            source = None
            for value in source_values:
                if not value:
                    continue
                candidate = Path(value)
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                if expected_hash and self._sha256_file(candidate).lower() != expected_hash:
                    continue
                source = candidate
                break
            if source is None:
                missing.append(str(relative))
                continue
            plans.append((source, destination, expected_hash))

        if missing:
            raise WorkLedgerConflict(
                "approved project source is unavailable: " + ", ".join(missing[:4])
            )

        materialized: list[str] = []
        for source, destination, expected_hash in plans:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not path_is_within(str(destination), str(workspace)):
                raise WorkLedgerConflict(
                    f"project source parent changed during promotion: {destination.name}"
                )
            created = False
            try:
                with source.open("rb") as input_handle, destination.open("xb") as output_handle:
                    shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
                created = True
                if expected_hash and self._sha256_file(destination).lower() != expected_hash:
                    destination.unlink(missing_ok=True)
                    raise WorkLedgerConflict(
                        f"materialized project source failed verification: {destination.name}"
                    )
                materialized.append(str(destination.relative_to(workspace)))
            except FileExistsError:
                existing.append(str(destination.relative_to(workspace)))
            except Exception:
                if created:
                    destination.unlink(missing_ok=True)
                raise
        return {"materialized": materialized, "existing": existing}

    def repair_project_identity(self, project_id: str) -> dict[str, Any]:
        """Backfill one legacy generated Project and its editable exports."""

        project = self.store.get_project(str(project_id or "").strip())
        if project is None:
            raise WorkLedgerNotFound(f"unknown project: {project_id}")
        items = self.store.list_work_items(project_id=project.project_id, limit=2000)
        if not items:
            return {
                "projectId": project.project_id,
                "projectName": project.name,
                "materializedFiles": [],
                "existingFiles": [],
            }
        selected = next((item for item in items if item.title == project.name), items[0])
        generated_name, metadata = self._project_identity_from_workspace(
            selected,
            items,
            previous_name=project.name,
        )
        existing_source = str((project.metadata or {}).get("name_source") or "")
        legacy_generated = project.name in {item.title for item in items}
        generated_identity = legacy_generated or existing_source.startswith("generated:")
        next_name = generated_name if generated_identity else project.name
        if not generated_identity:
            metadata = {
                "identity_version": _PROJECT_IDENTITY_VERSION,
                "name_source": existing_source or "registered",
                "semantic_aliases": self._stored_project_aliases(project),
            }
        materialization = self._materialize_approved_exports(project.canonical_path, items)
        updated = self.store.create_or_get_project(
            project.canonical_path,
            name=next_name,
            metadata=metadata,
        )
        return {
            "projectId": updated.project_id,
            "projectName": updated.name,
            "materializedFiles": materialization["materialized"],
            "existingFiles": materialization["existing"],
        }

    def promote_work_item_to_project(self, work_item_id: str) -> dict[str, Any]:
        """Turn one draft's workspace into a project the model can route to.

        Deciding at creation time whether a task will be worked on again means
        predicting the future, so nothing tries; the user decides afterwards, by
        selecting the row they can already see. That keeps the routing candidate
        list growing only by deliberate human acts rather than by accumulation
        (P1 work order section 11.4).

        The directory does not move: its path is recorded on the work item and
        indexed for lookup, so relocating it would separate the ledger from the
        disk.

        Everything that ran in that directory is re-filed under the new project,
        not just the task the user clicked. Which project a task belongs to
        follows from where it ran -- `_project_root_for` derives exactly that
        for subdirectories -- so this is not rewriting history, it is applying
        the same rule to a place that just acquired a name. Re-filing only one
        would leave its siblings, and usually the task that created the place,
        outside the project they are plainly part of.
        """

        item = self.store.get_work_item(work_item_id)
        if item is None:
            raise WorkLedgerNotFound(f"unknown work item: {work_item_id}")
        if not is_scratch_path(item.workspace_path):
            raise WorkLedgerConflict(
                f"work item {work_item_id} is not a scratch task; "
                "only a draft can be promoted to a project"
            )
        if not Path(item.workspace_path).is_dir():
            raise WorkLedgerConflict(
                f"workspace {item.workspace_path} no longer exists"
            )
        workspace_items = self._work_items_in_workspace(item.workspace_path)
        project_name, identity_metadata = self._project_identity_from_workspace(
            item,
            workspace_items,
        )
        materialization = self._materialize_approved_exports(
            item.workspace_path,
            workspace_items,
        )
        project = self.store.create_or_get_project(
            item.workspace_path,
            name=project_name,
            metadata=identity_metadata,
        )
        refiled = self.store.reassign_workspace_to_project(
            item.workspace_path,
            project.project_id,
        )
        logger.info(
            "[WORK-DESTINATION] promoted work_item=%s project=%s refiled=%d path=%s",
            item.work_item_id,
            project.project_id,
            refiled,
            item.workspace_path,
        )
        return {
            "workItemId": item.work_item_id,
            "projectId": project.project_id,
            "projectName": project.name,
            "workspacePath": item.workspace_path,
            "refiledTasks": refiled,
            "materializedFiles": materialization["materialized"],
            "existingFiles": materialization["existing"],
        }

    def _available_session_project(self, project_id: str):
        """Validate an explicit chat destination without mutating any state."""

        return self.destination.available_project(project_id)

    def bind_session_context(
        self,
        session_id: str,
        project_id: str,
        *,
        work_item_id: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        """Persist a Project default and/or foreground one exact WorkItem."""

        return self.destination.bind_session_context(
            session_id,
            project_id,
            work_item_id=work_item_id,
            source=source,
        )

    def conversation_binding(self, session_id: str) -> dict[str, Any] | None:
        return self.destination.conversation_binding(session_id)

    def set_session_project(self, session_id: str, project_id: str) -> dict[str, Any]:
        """Record which project this conversation said it is working in.

        This is durable conversation context, deliberately not a focus slot.
        The pin is a standing override that outlives and overrides even an
        explicitly named destination; this binding is only "what is this Chat
        working on". Persistence keeps the Electron history owner and Slice's
        ambient view from reconstructing different answers after a restart.
        """

        return self.destination.set_session_project(session_id, project_id)

    def session_project(self, session_id: str) -> str:
        """The project this conversation chose, if it still exists."""

        return self.destination.session_project(session_id)

    def clear_session_project(self, session_id: str) -> None:
        self.destination.clear_session_project(session_id)

    def set_session_project_feedback(
        self,
        session_id: str,
        *,
        status: str,
        message: str,
    ) -> None:
        self.destination.set_session_project_feedback(
            session_id,
            status=status,
            message=message,
        )

    def _destination_feedback(self) -> dict[str, str] | None:
        return self.destination.destination_feedback()

    def _destination_label(self) -> str:
        """The project the live conversation chose, or "" for drafts.

        Only the fact; the surface writes the sentence (R1). Empty is not
        missing information -- it is the answer, and it means the next unnamed
        instruction starts a draft.
        """

        return self.destination.destination_label()

    def _destination_project_id(self) -> str:
        return self.destination.destination_project_id()

    def set_project_retired(self, project_id: str, *, retired: bool) -> dict[str, Any]:
        """Take a place off the menu, or put it back. Nothing is deleted.

        Keeping a draft is a one-way ratchet without this: something worth two
        days of attention would sit among the choices forever, which is the
        list-only-grows shape this design keeps having to remove (P1 work order
        section 11.4). Retiring lowers the rate to zero rather than slowing it.

        Files, tasks and lookup are all untouched -- a retired project's past
        stays as answerable as before, exactly like an archived WorkItem stays
        readable (P1 R12: archive hides, it does not delete).
        """

        return self.destination.set_project_retired(project_id, retired=retired)

    def _scratch_route(self) -> dict[str, Any]:
        """The destination for work that belongs to no known project."""

        return self.destination.scratch_route()

    def _validated_workspace_route(
        self,
        workspace_path: str,
        *,
        project_id: str,
        source: str,
    ) -> dict[str, Any]:
        return self.destination.validated_workspace_route(
            workspace_path,
            project_id=project_id,
            source=source,
        )

    def snapshot(self, *, surface: str | None = None, limit: int = 200) -> dict[str, Any]:
        target_surface = str(surface or self.default_surface)
        current_session_id = str(self._current_session_id() or "").strip()
        records = self.store.list_work_items(limit=limit)
        items = [self._project_item(item) for item in records]
        focus = self.store.get_focus(target_surface)
        if (
            target_surface != WORKSPACE_ROUTING_SURFACE
            and focus is not None
            and focus.mode == "pinned"
        ):
            # Pre-routing-lock builds used the same flag to freeze a visual
            # history row.  Visual pinning no longer exists; normalize that
            # legacy state without creating a workspace lock implicitly.
            focus = self.store.set_focus(
                target_surface,
                focus.work_item_id,
                mode="auto",
            )
        known_ids = {item.work_item_id for item in records}
        auto_candidate = self._default_focus_id(items)
        if (
            focus is None
            or (focus.work_item_id and focus.work_item_id not in known_ids)
        ):
            selected_id = auto_candidate
            focus = self.store.set_focus(target_surface, selected_id or None, mode="auto")
        elif not focus.work_item_id and auto_candidate:
            focus = self.store.set_focus(target_surface, auto_candidate, mode=focus.mode)

        selected_id = str(focus.work_item_id if focus else "")
        running = sum(1 for item in items if item["execution"] in _ACTIVE_EXECUTION)
        needs_attention = sum(
            1
            for item in items
            if item["state"] not in {"accepted", "archived"}
            and item["attention"] not in {"", "none"}
        )
        active = sum(1 for item in items if item["state"] not in {"accepted", "archived"})
        workspace_focus = self.workspace_routing_focus()
        destination_label = self._destination_label()
        destination_project_id = self._destination_project_id()
        destination_feedback = self._destination_feedback()
        projects = self.project_catalog(projected_items=items)
        revision_material = {
            "selected": selected_id,
            "mode": focus.mode if focus else "auto",
            "workspace_focus": workspace_focus,
            "destination": destination_label,
            "destination_project_id": destination_project_id,
            "destination_feedback": destination_feedback,
            "current_session_id": current_session_id,
            "projects": projects,
            "items": [
                (
                    item["id"],
                    item["execution"],
                    item["completion"],
                    item["attention"],
                    item["state"],
                    item["attemptId"],
                    item.get("pendingPermissionRequestId"),
                    item.get("pendingPermissionCount"),
                    item.get("recoverableExportRequestId"),
                    item.get("liveness"),
                    item.get("livenessStage"),
                    item.get("probeStatus"),
                    item.get("silentForSeconds"),
                )
                for item in items
            ],
        }
        revision = hashlib.sha1(
            json.dumps(revision_material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        selected = next((item for item in items if item["id"] == selected_id), None)
        return {
            "surface": target_surface,
            "revision": revision,
            "currentSessionId": current_session_id,
            "selectedWorkItemId": selected_id,
            "focusMode": focus.mode if focus else "auto",
            "workspaceFocusMode": workspace_focus.get("mode") or "auto",
            "workspaceFocusWorkItemId": workspace_focus.get("workItemId") or "",
            "workspaceFocusProjectId": workspace_focus.get("projectId") or "",
            "workspaceFocusPath": workspace_focus.get("workspacePath") or "",
            # Where the next unnamed instruction will go. The user sets this by
            # saying it, so the surface only has to show it -- and it has to,
            # because the one way switching fails is the model saying it
            # switched without emitting the tag. Nothing else would reveal that.
            "destinationLabel": destination_label,
            "destinationProjectId": destination_project_id,
            "destinationFeedback": destination_feedback,
            "projects": projects,
            "counts": {
                "running": running,
                "needsAttention": needs_attention,
                "active": active,
            },
            "items": items,
            "selected": selected,
        }

    def conversation_work_items(
        self,
        session_id: str,
        *,
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        """Return a bounded task roster owned by one chat conversation.

        This is prompt context rather than execution authority. Workspace
        paths and raw provider output are intentionally excluded so task
        reference resolution cannot leak or jump across conversations.
        """

        clean_session_id = str(session_id or "").strip()
        if not clean_session_id:
            return []
        row_limit = max(1, min(int(limit), 8))
        return self._conversation_work_item_rows(clean_session_id, row_limit)

    def bound_work_item_status_row(
        self,
        session_id: str,
        work_item_id: str,
    ) -> dict[str, Any] | None:
        """Return the exact Ledger row named by a durable Chat binding."""

        item = self.store.get_work_item(str(work_item_id or "").strip())
        if item is None:
            return None
        return self._conversation_row_for_item(
            item,
            str(session_id or "").strip(),
            include_kept_projects=True,
        )

    def project_status_snapshot(self, project_id: str) -> dict[str, Any] | None:
        """Aggregate truthful, bounded status facts for one Project context."""
        return self.read_model.project_status_snapshot(project_id)

    def project_apps(self, project_id: str, *, limit: int = 100) -> dict[str, Any]:
        """Return a Project-scoped catalog of verified AUIP applications."""

        project = self.destination.available_project(str(project_id or "").strip())
        catalog = self.read_model.project_apps(project.project_id, limit=limit)
        return {
            "project": {
                "projectId": project.project_id,
                "name": project.name or Path(project.canonical_path).name,
                "updatedAt": self._iso_time(project.updated_at),
            },
            **catalog,
        }

    def draft_apps(self, *, limit: int = 5) -> dict[str, Any]:
        """Return a bounded recent catalog without making Drafts durable."""

        return self.read_model.draft_apps(limit=limit)

    def conversation_work_items_for_resolution(
        self,
        session_id: str,
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Return an execution-side roster for exhaustive exact-handle checks.

        Unlike prompt context, this view may scan the bounded ledger window.
        It still excludes paths and provider output. ``complete`` is false
        whenever either the global scan or the conversation result saturates
        its bound, so execution callers can fail closed.
        """

        clean_session_id = str(session_id or "").strip()
        if not clean_session_id:
            return {"items": [], "complete": True}
        row_limit = max(1, min(int(limit), 200))
        scan_limit = 2000
        candidates = self.store.list_work_items(limit=scan_limit)
        rows = self._conversation_work_item_rows(
            clean_session_id,
            row_limit,
            candidates=candidates,
        )
        return {
            "items": rows,
            "complete": len(candidates) < scan_limit and len(rows) < row_limit,
        }

    def _conversation_work_item_rows(
        self,
        clean_session_id: str,
        row_limit: int,
        *,
        candidates: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        source = list(
            candidates
            if candidates is not None
            else self.store.list_work_items(limit=200)
        )
        active_context = self.store.get_session_work_context(clean_session_id)
        binding = self.store.get_conversation_binding(clean_session_id)
        anchor_id = str(
            active_context.active_work_item_id
            if active_context is not None
            else binding.anchor_work_item_id
            if binding is not None
            else ""
        )
        if anchor_id:
            anchor = self.store.get_work_item(anchor_id)
            if anchor is not None:
                source = [anchor, *(item for item in source if item.work_item_id != anchor_id)]
        for item in source:
            row = self._conversation_row_for_item(
                item,
                clean_session_id,
                include_kept_projects=bool(anchor_id and item.work_item_id == anchor_id),
            )
            if row is None:
                continue
            rows.append(row)
            if len(rows) >= row_limit:
                break
        return rows

    def _conversation_row_for_item(
        self,
        item: Any,
        clean_session_id: str,
        *,
        include_kept_projects: bool = False,
    ) -> dict[str, Any] | None:
        """Project one work item into a conversation roster row, or None.

        None means the item does not belong to this conversation under the
        roster's ownership rule (the latest Provider attempt carries this
        session id). Callers that reach the ledger through an index still get
        exactly the rows the recency scan would have produced for these items.

        ``include_kept_projects`` admits work from other conversations as long
        as it did not run in an unkept draft. That is the whole difference
        between the two kinds of place: a draft reaches only as far as the
        conversation that made it, and a project is a place someone chose to
        keep, so its past is still answerable later. Only exact-index callers
        may set it -- see conversation_work_items_by_file.
        """

        attempts = self.store.list_attempts(item.work_item_id)
        latest_attempt = attempts[-1] if attempts else None
        if latest_attempt is None:
            return None
        if self._attempt_session_id(latest_attempt) != clean_session_id and not (
            include_kept_projects and not self._is_unkept_draft(item.workspace_path)
        ):
            return None
        projected = self._project_item(item)
        execution = str(projected.get("execution") or "idle")
        attention = str(projected.get("attention") or "none")
        if execution in _ACTIVE_EXECUTION:
            relation = "running"
        elif attention not in {"", "none"}:
            relation = "needs_attention"
        elif item.state in {"accepted", "archived"}:
            relation = "history"
        else:
            relation = "current"
        activity = projected.get("activity") if isinstance(projected.get("activity"), dict) else {}
        return {
            "work_item_id": item.work_item_id,
            "project_id": item.project_id,
            "related_work_item_id": str(
                (item.metadata or {}).get("related_work_item_id") or ""
            ),
            "attempt_id": latest_attempt.attempt_id,
            "operation_id": latest_attempt.operation_id,
            "title": item.title,
            "files": self._business_file_names(item.work_item_id),
            "source_user_text": str(
                latest_attempt.metadata.get("source_user_text")
                or item.metadata.get("source_user_text")
                or ""
            ),
            "state": item.state,
            "execution": execution,
            "completion": str(projected.get("completion") or "unknown"),
            "attention": attention,
            "relation": relation,
            "updated_at": str(projected.get("updatedAt") or ""),
            "completion_rationale": str(projected.get("completionRationale") or ""),
            **_activity_row_facts(activity),
        }

    async def enrich_report_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Refresh one already-resolved task with bounded runtime facts.

        Resolution owns which WorkItem the user meant.  This method only
        refreshes that item's latest Attempt and reads its already-authorised
        workspace; it never asks a Provider to generate a status response.
        """

        work_item_id = str(row.get("work_item_id") or "").strip()
        item = self.store.get_work_item(work_item_id)
        if item is None:
            return dict(row)
        attempts = self.store.list_attempts(work_item_id)
        latest_attempt = attempts[-1] if attempts else None
        if latest_attempt is None:
            return dict(row)
        projected = self._project_item(item)
        activity = projected.get("activity") if isinstance(projected.get("activity"), dict) else {}
        latest_completion = self.store.latest_completion(work_item_id)
        terminal_summary = ""
        if (
            latest_attempt.execution_status in _TERMINAL_EXECUTION
            and latest_completion is not None
            and latest_completion.attempt_id == latest_attempt.attempt_id
        ):
            terminal_summary = self._terminal_narration_summary(
                latest_completion,
                latest_attempt.result,
                latest_attempt.error,
            )[:4000]
        stored_verdict = latest_attempt.metadata.get(OUTCOME_VERDICT_METADATA_KEY)
        enriched = {
            **dict(row),
            "attempt_id": latest_attempt.attempt_id,
            "execution": str(projected.get("execution") or "idle"),
            "completion": str(projected.get("completion") or "unknown"),
            "attention": str(projected.get("attention") or "none"),
            "completion_rationale": str(projected.get("completionRationale") or ""),
            "terminal_summary": terminal_summary,
            "outcome_verdict": (
                dict(stored_verdict) if isinstance(stored_verdict, dict) else {}
            ),
            **_activity_row_facts(activity),
        }
        enriched["workspace_observation"] = await self._observe_report_workspace(
            item,
            latest_attempt,
        )
        return enriched

    async def _observe_report_workspace(
        self,
        item: WorkItemRecord,
        attempt: RunAttemptRecord,
    ) -> dict[str, Any]:
        """Collect only Git-baseline and attempt-owned staging facts."""

        metadata = attempt.metadata if isinstance(attempt.metadata, dict) else {}
        baseline = metadata.get("git_baseline") if isinstance(metadata.get("git_baseline"), dict) else {}
        observation: dict[str, Any] = {
            "available": False,
            "reason": "git_baseline_unavailable",
            "changed_files": [],
            "staged_files": [],
            "ambiguous_paths": [],
        }
        try:
            git_delta = await collect_git_delta(
                item.workspace_path,
                baseline,
                include_patch=False,
                verify_baseline_dirty=False,
            )
            observation.update(
                {
                    "available": bool(git_delta.get("available")),
                    "reason": str(git_delta.get("reason") or "observed"),
                    "changed_files": [
                        str(path) for path in (git_delta.get("changed_files") or [])[:12]
                    ],
                    "ambiguous_paths": [
                        str(path) for path in (git_delta.get("ambiguous_paths") or [])[:12]
                    ],
                    "truncated_paths": int(git_delta.get("truncated_paths") or 0),
                }
            )
        except Exception as exc:
            logger.warning(
                "report Git observation failed for %s: %s",
                attempt.attempt_id,
                exc.__class__.__name__,
            )
            observation["reason"] = "git_observation_failed"
        export_plan = metadata.get("export_plan") if isinstance(metadata.get("export_plan"), dict) else None
        if export_plan is not None:
            try:
                staged = self.export_service.observe_staged_files(attempt, item, export_plan)
                observation["staged_files"] = [
                    str(path) for path in (staged.get("changed_files") or [])[:12]
                ]
                observation["staging_reason"] = str(staged.get("reason") or "")
            except Exception as exc:
                logger.warning(
                    "report staging observation failed for %s: %s",
                    attempt.attempt_id,
                    exc.__class__.__name__,
                )
                observation["staging_reason"] = "staging_observation_failed"
        return observation

    def conversation_work_items_by_file(
        self,
        session_id: str,
        name: str,
        *,
        limit: int = 32,
        include_kept_projects: bool = False,
    ) -> list[dict[str, Any]]:
        """Resolve one spoken filename against everything this conversation did.

        The resolution roster walks a recency window (2000 scanned items, 200
        conversation rows) and fails closed when it saturates, which is the
        silent-fallback-to-new-task failure the lookup work order exists to
        close. This asks the ledger the exact question through the artifact
        and title indexes instead, so the answer is complete by construction:
        zero rows means the task does not exist, not that it fell out of a
        window. Precision (exact reference matching) stays with the caller;
        recall lives here.

        With ``include_kept_projects``, "does not exist" widens from this
        conversation to this conversation plus every kept project. That is the
        cheap half of remembering: this path is an index query, so it costs no
        model call, needs nothing injected into a prompt, and answers a project
        that has run for a year exactly as fast as one that ran yesterday. It
        also carries no completeness burden -- an exact match either hits or
        does not -- which is why the expensive rung above it, whose pick never
        refuses and so demands a complete candidate set, stays session-scoped.
        """

        clean_session_id = str(session_id or "").strip()
        clean_name = str(name or "").strip()
        if not clean_name or (not clean_session_id and not include_kept_projects):
            return []
        ordered_ids: list[str] = []
        seen: set[str] = set()
        artifact_ids = [
            work_item_id
            for kind in (
                "business.file",
                "business.export",
                "business.proposed_export",
            )
            for work_item_id in self.store.find_work_item_ids_by_artifact_name(
                clean_name,
                kind=kind,
            )
        ]
        for work_item_id in (
            *artifact_ids,
            *self.store.find_work_item_ids_by_title_match(clean_name),
        ):
            if work_item_id in seen:
                continue
            seen.add(work_item_id)
            ordered_ids.append(work_item_id)
        rows: list[dict[str, Any]] = []
        for work_item_id in ordered_ids:
            item = self.store.get_work_item(work_item_id)
            if item is None:
                continue
            row = self._conversation_row_for_item(
                item,
                clean_session_id,
                include_kept_projects=include_kept_projects,
            )
            if row is None:
                continue
            rows.append(row)
            if len(rows) >= max(1, int(limit)):
                break
        return rows

    def approved_desktop_export_work_items_by_file(
        self,
        session_id: str,
        name: str,
        *,
        limit: int = 32,
    ) -> list[dict[str, Any]]:
        """Resolve an exact Desktop deliverable to the WorkItem that owns it.

        ``target=desktop`` is an authority boundary, not another Project
        workspace.  A later amendment must therefore continue the WorkItem
        whose approved export established the target/hash transaction.  This
        query is exact and index-backed, so persistent Project history remains
        reachable without putting every historical WorkItem in the per-turn
        reference catalog.  Draft history still stays Session-scoped.
        """

        clean_session_id = str(session_id or "").strip()
        raw_name = Path(str(name or "").strip()).name
        clean_name = raw_name.casefold()
        if not raw_name:
            return []
        try:
            desktop_root = Path(self.export_service.desktop_path).resolve()
        except (OSError, RuntimeError, ValueError):
            return []
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        target_path = desktop_root / raw_name
        for work_item_id in self.store.find_work_item_ids_by_artifact_path(
            target_path,
            kind="business.export",
            status="approved",
            limit=max(1, int(limit)),
        ):
            if work_item_id in seen:
                continue
            seen.add(work_item_id)
            item = self.store.get_work_item(work_item_id)
            if item is None:
                continue
            owns_approved_target = False
            for artifact in self.store.list_artifacts(work_item_id):
                if (
                    artifact.kind != "business.export"
                    or artifact.status != "approved"
                    or Path(str(artifact.path or "")).name.casefold() != clean_name
                ):
                    continue
                try:
                    target = Path(str(artifact.path)).resolve()
                except (OSError, RuntimeError, ValueError):
                    continue
                if target.parent == desktop_root:
                    owns_approved_target = True
                    break
            if not owns_approved_target:
                continue
            row = self._conversation_row_for_item(
                item,
                clean_session_id,
                include_kept_projects=True,
            )
            if row is not None:
                rows.append(row)
            if len(rows) >= max(1, int(limit)):
                break
        return rows

    def _project_root_for(self, path: str) -> str:
        """Which project a workspace belongs to, derived from where it is.

        An unkept draft belongs to the scratch container, never to itself.
        Without that, continuing a draft in the same session -- which arrives
        here with the draft's own directory as cwd -- would register that
        directory as a project, quietly granting it the permanence that is
        supposed to require someone deciding to keep it, and removing the offer
        to keep it before the user ever saw it.
        """

        if self._is_unkept_draft(path):
            return str(ensure_scratch_root())
        return _project_registry_root_for(path)

    def _is_unkept_draft(self, workspace_path: str) -> bool:
        """A scratch workspace the user has not turned into a project.

        Both the offer to keep it and the sentence explaining why it cannot be
        picked up read this, so neither can start describing a different set of
        tasks than the other.
        """

        return self.destination.is_unkept_draft(workspace_path)

    def drafts_in_other_conversations(self, name: str, *, limit: int = 4) -> list[dict[str, Any]]:
        """Drafts elsewhere that match a spoken reference. Wording only.

        A draft reaches only as far as the conversation that made it, so asking
        about one in a later session finds nothing -- while the thing itself is
        sitting in the task list on screen. Saying "no such task" there would
        contradict what the user can see, which is the most expensive kind of
        wrong sentence this system can produce (scenarios G1/G2).

        This never feeds routing. Its only job is to let the answer be true:
        the draft exists, it was not kept as a project, and there is a button
        that changes that.
        """

        clean_name = str(name or "").strip()
        if not clean_name:
            return []
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for work_item_id in (
            *self.store.find_work_item_ids_by_artifact_name(clean_name),
            *self.store.find_work_item_ids_by_title_match(clean_name),
        ):
            if work_item_id in seen:
                continue
            seen.add(work_item_id)
            item = self.store.get_work_item(work_item_id)
            if item is None or not self._is_unkept_draft(item.workspace_path):
                continue
            found.append({"work_item_id": item.work_item_id, "title": item.title})
            if len(found) >= max(1, int(limit)):
                break
        return found

    def conversation_work_item_index(
        self,
        session_id: str,
        *,
        limit: int = 64,
        scan_limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Lean per-turn rows for literal-overlap prefiltering.

        Same scan bound as the per-turn roster render (200 recent items), but
        without ``_project_item`` -- the prefilter ranks by title and produced
        files only, and execution state is fetched later for the one row that
        wins. Exact filename references do not come through here at all; they
        take the unbounded artifact/title index.
        """

        clean_session_id = str(session_id or "").strip()
        if not clean_session_id:
            return []
        rows: list[dict[str, Any]] = []
        for item in self.store.list_work_items(limit=max(1, int(scan_limit))):
            attempts = self.store.list_attempts(item.work_item_id)
            latest_attempt = attempts[-1] if attempts else None
            if latest_attempt is None:
                continue
            if self._attempt_session_id(latest_attempt) != clean_session_id:
                continue
            rows.append(
                {
                    "work_item_id": item.work_item_id,
                    "title": item.title,
                    "files": self._business_file_names(item.work_item_id),
                    "updated_at": self._iso_time(item.last_activity_at),
                }
            )
            if len(rows) >= max(1, int(limit)):
                break
        return rows

    def _business_file_names(self, work_item_id: str) -> list[str]:
        """Base names of the deliverables this task actually produced.

        Resolving "add a line to amend.txt" used to match the filename against
        the work item's *title*, which only works when the title happens to
        contain it. On 2026-08-01 a real run showed what that costs: the first
        delegate had been synthesised by the repair net from the raw utterance,
        so the title held the harness preamble and not the filename, the
        amendment resolved against nothing, and the follow-up became a second
        task in its own worktree.

        The ledger knew all along -- it had `amend.txt` registered as a
        business.file for that very item. Base names only: the roster still
        keeps paths out, and the base name is the word the user actually says.
        """

        names: set[str] = set()
        try:
            for artifact in self.store.list_artifacts(work_item_id):
                if str(getattr(artifact, "kind", "") or "") not in {
                    "business.file",
                    "business.export",
                    "business.proposed_export",
                }:
                    continue
                raw = str(getattr(artifact, "path", "") or "").strip()
                if not raw:
                    continue
                base = PurePath(raw.replace("\\", "/")).name.strip().lower()
                if base:
                    names.add(base)
        except Exception:
            logger.debug("artifact names unavailable for %s", work_item_id, exc_info=True)
        return sorted(names)

    @staticmethod
    def _default_focus_id(items: list[dict[str, Any]]) -> str:
        return WorkReadModel.default_focus_id(items)

    def detail(self, work_item_id: str) -> dict[str, Any]:
        return self.read_model.detail(work_item_id)

    def _project_item(self, item: WorkItemRecord) -> dict[str, Any]:
        """Compatibility facade for callers that still project one ledger item."""
        return self.read_model.project_item(item)

    @staticmethod
    def _iso_time(timestamp: float) -> str:
        return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat(timespec="seconds")

    def select(self, work_item_id: str, *, surface: str | None = None) -> dict[str, Any]:
        target_surface = str(surface or self.default_surface)
        if self.store.get_work_item(work_item_id) is None:
            raise WorkLedgerNotFound(f"unknown work item: {work_item_id}")
        # History selection is view state only.  It must never silently change
        # the workspace used by the next spoken/chat instruction.
        self.store.set_focus(target_surface, work_item_id, mode="auto")
        return self.snapshot(surface=target_surface)

    async def dispose_work_item(
        self,
        work_item_id: str,
        *,
        action: str,
        rationale: str,
        surface: str | None = None,
    ) -> dict[str, Any]:
        """Apply a user disposition through one idempotent control-plane path."""

        clean_action = str(action or "").strip().lower()
        if clean_action not in {"accept", "archive"}:
            raise ValueError(f"unsupported WorkItem disposition: {action!r}")
        item = self.store.get_work_item(work_item_id)
        if item is None:
            raise WorkLedgerNotFound(f"unknown work item: {work_item_id}")
        attempts = self.store.list_attempts(work_item_id)
        if any(attempt.execution_status in _ACTIVE_EXECUTION for attempt in attempts):
            raise WorkLedgerConflict(f"work item {work_item_id} still has an active attempt")

        desired_state = "accepted" if clean_action == "accept" else "archived"
        if item.state == desired_state:
            return await self.publish_snapshot(
                reason=f"work.{clean_action}.noop",
                surface=surface,
            )

        latest = self.store.latest_completion(work_item_id)
        latest_attempt = attempts[-1] if attempts else None
        if clean_action == "accept":
            if item.state == "archived":
                raise WorkLedgerConflict("an archived work item must be reopened before acceptance")
            if self.store.list_permission_requests(work_item_id, status="pending"):
                raise WorkLedgerConflict("pending permissions must be resolved before acceptance")
            if latest is None:
                raise WorkLedgerConflict("work item has no completion assessment to accept")
            decision = CompletionDecision(
                execution_status=latest.execution_status,
                completeness=latest.completeness,
                attention="none",
                work_item_state="accepted",
                rationale=str(rationale or "User accepted the reviewed work item."),
                terminal=True,
            )
            self.store.record_completion(
                work_item_id,
                decision,
                attempt_id=latest.attempt_id,
                source="user",
                evidence={"accepted_from_assessment": latest.assessment_id},
            )
        else:
            execution_status = (
                latest.execution_status
                if latest is not None
                else latest_attempt.execution_status
                if latest_attempt is not None
                else "cancelled"
            )
            completeness = latest.completeness if latest is not None else "unknown"
            self.store.record_completion(
                work_item_id,
                CompletionDecision(
                    execution_status=execution_status,
                    completeness=completeness,
                    attention="none",
                    work_item_state="archived",
                    rationale=str(rationale or "User archived the work item."),
                    terminal=True,
                ),
                attempt_id=(
                    latest.attempt_id
                    if latest is not None
                    else latest_attempt.attempt_id
                    if latest_attempt is not None
                    else ""
                ),
                source="user",
                evidence={
                    "archived_from_state": item.state,
                    **(
                        {"archived_from_assessment": latest.assessment_id}
                        if latest is not None
                        else {}
                    ),
                },
            )
            # record_completion deliberately preserves an already accepted
            # disposition. Explicit Archive is the one user action allowed to
            # move accepted history into archived history.
            if item.state == "accepted":
                self.store.set_work_item_state(work_item_id, "archived")

        return await self.publish_snapshot(
            reason=f"work.{clean_action}",
            surface=surface,
        )

    def set_focus(
        self,
        *,
        mode: str,
        work_item_id: str = "",
        surface: str | None = None,
    ) -> dict[str, Any]:
        target_surface = str(surface or self.default_surface)
        focus_mode = "pinned" if str(mode).strip().lower() == "pinned" else "auto"
        current = self.store.get_focus(target_surface)
        selected_id = str(work_item_id or (current.work_item_id if current else ""))
        if focus_mode == "pinned":
            if not selected_id:
                raise WorkLedgerConflict("a WorkItem is required to lock its workspace")
            item = self.store.get_work_item(selected_id)
            if item is None:
                raise WorkLedgerNotFound(f"unknown work item: {selected_id}")
            route = self._validated_workspace_route(
                item.workspace_path,
                project_id=item.project_id,
                source="workspace_pin",
            )
            if route.get("status") != "resolved":
                raise WorkLedgerConflict(
                    f"cannot lock unavailable workspace: {route.get('reason') or item.workspace_path}"
                )
            self.store.set_focus(
                WORKSPACE_ROUTING_SURFACE,
                item.work_item_id,
                mode="pinned",
            )
        else:
            # Unlock is global: future Provider instructions return to intent
            # routing.  The selected history item remains a visual selection.
            self.store.clear_focus(WORKSPACE_ROUTING_SURFACE)
        if focus_mode == "auto" and not selected_id:
            items = self.store.list_work_items(states=["open", "review_ready", "accepted"], limit=1)
            selected_id = items[0].work_item_id if items else ""
        # Visual selection stays in auto-follow mode.  The durable pin above
        # owns only workspace routing, so a newly created WorkItem can still
        # become visible instead of leaving Slice stuck on an old history row.
        self.store.set_focus(target_surface, selected_id or None, mode="auto")
        return self.snapshot(surface=target_surface)

    def record_presentation(self, work_item_id: str, payload: dict[str, Any]) -> None:
        clean = dict(payload or {})
        clean.pop("taskDock", None)
        clean.pop("workContext", None)
        clean.pop("clear", None)
        if not clean:
            return
        self.store.update_work_item_metadata(
            work_item_id,
            {"presentation": clean},
            touch_activity=False,
        )

    async def resolve_permission(
        self,
        request_id: str,
        *,
        allow: bool,
        work_item_id: str = "",
        attempt_id: str = "",
    ) -> dict[str, Any]:
        """Resolve one exact ledger request; renderer-supplied paths are ignored."""

        context = self.permission_service.context(
            request_id,
            work_item_id=work_item_id,
            attempt_id=attempt_id,
        )
        request = context.request
        attempt = context.attempt
        desktop_export = context.desktop_export
        try:
            resolution = self.permission_service.resolve(context, allow=allow)
            resolved = resolution.permission
            exported_paths = resolution.exported_paths
        except Exception as exc:
            refreshed = self.store.get_permission_request(request.request_id)
            if desktop_export and refreshed is not None and refreshed.status == "allowed":
                self._mark_export_delta_resolved(
                    attempt.attempt_id,
                    status="failed",
                    reason="external_export_failed",
                )
                self.store.record_completion(
                    request.work_item_id,
                    CompletionDecision(
                        execution_status=attempt.execution_status,
                        completeness="partial",
                        attention="conflict",
                        work_item_state="open",
                        rationale="The export was authorized, but Amadeus could not complete the exact Desktop copy.",
                        terminal=True,
                    ),
                    attempt_id=attempt.attempt_id,
                    source="host",
                    evidence={
                        "permission_request_id": request.request_id,
                        "resolution": "allowed_export_failed",
                        "error": exc.__class__.__name__,
                    },
                )
                failure_note = self._claim_terminal_work_notice(
                    attempt,
                    delivery_id=f"permission:{request.request_id}:export_failed",
                    title="Desktop export failed",
                    summary=(
                        "The Desktop export was approved, but the exact copy could not be "
                        "completed safely. The task needs attention."
                    ),
                    importance="error",
                    metadata={
                        "work_event": "work.export_failed",
                        "permission_request_id": request.request_id,
                        "attention": "conflict",
                    },
                )
                await self.publish_snapshot(reason="permission.export_failed")
                if failure_note is not None:
                    add_work_note(failure_note)
                    await bus.emit(Method.CHAT_WORK_NOTE, failure_note)
            raise

        if desktop_export:
            self._mark_export_delta_resolved(
                attempt.attempt_id,
                status=resolved.status,
                reason=(
                    "external_export_complete"
                    if resolved.status == "allowed"
                    else "external_export_denied"
                ),
            )

        auto_accepted = bool(
            allow
            and desktop_export
            and self._auto_accept_approved_export(
                request=request,
                resolved=resolved,
                attempt=attempt,
                exported_paths=exported_paths,
            )
        )

        if attempt.execution_status in _ACTIVE_EXECUTION:
            snapshot = await self.publish_snapshot(reason=f"permission.{resolved.status}")
            return {
                "permission": resolved.to_dict(),
                "exportedPaths": list(exported_paths),
                "work": snapshot,
            }

        if auto_accepted:
            terminal_note = self._claim_export_resolution_notice(
                request=request,
                resolved=resolved,
                attempt=attempt,
                exported_paths=exported_paths,
                work_item_state="accepted",
                attention="none",
            )
            snapshot = await self.publish_snapshot(reason="permission.allowed.auto_accepted")
            if terminal_note is not None:
                add_work_note(terminal_note)
                await bus.emit(Method.CHAT_WORK_NOTE, terminal_note)
            return {
                "permission": resolved.to_dict(),
                "exportedPaths": list(exported_paths),
                "work": snapshot,
            }
        if allow and desktop_export:
            decision = CompletionDecision(
                execution_status=attempt.execution_status,
                completeness="partial",
                attention="review",
                work_item_state="review_ready",
                rationale="The validated staged deliverable was exported to Desktop; user review is still required.",
                terminal=True,
            )
        elif allow:
            decision = CompletionDecision(
                execution_status=attempt.execution_status,
                completeness="partial",
                attention="review",
                work_item_state="open",
                rationale="Permission was approved. Retry the same instruction to rerun the provider operation.",
                terminal=True,
            )
        else:
            decision = CompletionDecision(
                execution_status=attempt.execution_status,
                completeness="partial",
                attention="review",
                work_item_state="open",
                rationale="The permission request was declined; no external action was performed.",
                terminal=True,
            )
        self.store.record_completion(
            request.work_item_id,
            decision,
            attempt_id=attempt.attempt_id,
            source="user",
            evidence={
                "permission_request_id": request.request_id,
                "permission_status": resolved.status,
                "exported_paths": list(exported_paths),
            },
        )
        terminal_note = (
            self._claim_export_resolution_notice(
                request=request,
                resolved=resolved,
                attempt=attempt,
                exported_paths=exported_paths,
                work_item_state=decision.work_item_state,
                attention=decision.attention,
            )
            if desktop_export
            else None
        )
        snapshot = await self.publish_snapshot(reason=f"permission.{resolved.status}")
        if terminal_note is not None:
            add_work_note(terminal_note)
            await bus.emit(Method.CHAT_WORK_NOTE, terminal_note)
        return {
            "permission": resolved.to_dict(),
            "exportedPaths": list(exported_paths),
            "work": snapshot,
        }

    def _claim_export_resolution_notice(
        self,
        *,
        request: PermissionRequestRecord,
        resolved: PermissionRequestRecord,
        attempt: RunAttemptRecord,
        exported_paths: Iterable[str],
        work_item_state: str,
        attention: str,
    ) -> dict[str, Any] | None:
        exported = [str(path) for path in exported_paths if str(path)]
        filenames = [Path(path).name or path for path in exported]
        current_attempt = self.store.get_attempt(attempt.attempt_id) or attempt
        deferred = (
            current_attempt.metadata.get("deferred_terminal_narration")
            if isinstance(
                current_attempt.metadata.get("deferred_terminal_narration"),
                dict,
            )
            else {}
        )
        execution_summary = self._resolved_deferred_execution_summary(deferred)
        if resolved.status == "allowed":
            label = ", ".join(filenames[:3]) or "The approved deliverable"
            if len(filenames) > 3:
                label = f"{label} and {len(filenames) - 3} more files"
            title = "Desktop export complete"
            destinations = ", ".join(exported[:3])
            if len(exported) > 3:
                destinations = f"{destinations}, and {len(exported) - 3} more"
            export_summary = (
                f"Export confirmed: {label} was copied successfully"
                + (f" to {destinations}." if destinations else " to Desktop.")
            )
            summary = (
                f"{export_summary} Work result: {execution_summary}"
                if execution_summary
                else export_summary
            )
            work_event = (
                "work.accepted" if work_item_state == "accepted" else "work.review_ready"
            )
        else:
            title = "Desktop export declined"
            # Do not replay a provider sentence that may itself have claimed
            # the external copy happened.  The declined permission is the
            # authoritative outcome at this boundary.
            summary = (
                "The provider run finished, but the Desktop export was declined, "
                "so no file was copied."
            )
            work_event = "work.export_declined"
        note = self._claim_terminal_work_notice(
            attempt,
            delivery_id=f"permission:{request.request_id}:{resolved.status}",
            title=title,
            summary=summary,
            importance="important",
            metadata={
                "work_event": work_event,
                "permission_request_id": request.request_id,
                "permission_status": resolved.status,
                "exported_paths": exported,
                "work_item_state": work_item_state,
                "attention": attention,
                "execution_summary_included": bool(
                    resolved.status == "allowed" and execution_summary
                ),
            },
        )
        if note is not None and deferred:
            self.store.update_attempt(
                attempt.attempt_id,
                metadata={
                    "deferred_terminal_narration": {
                        **deferred,
                        "resolved_by": f"permission:{request.request_id}:{resolved.status}",
                        "resolved_at": float(self._clock()),
                    }
                },
            )
        return note

    @staticmethod
    def _resolved_deferred_execution_summary(deferred: dict[str, Any]) -> str:
        """Remove permission-only wording once that permission is resolved.

        A deferred terminal may contain either substantive execution evidence
        or merely the then-current fact that export approval is pending.  The
        latter becomes false at this boundary and must not be appended to the
        approved/declined terminal sentence.  A verified structured outcome,
        when present, remains useful independent evidence and replaces that
        obsolete permission rationale.
        """

        summary = str(deferred.get("summary") or "").strip()
        metadata = (
            deferred.get("metadata")
            if isinstance(deferred.get("metadata"), dict)
            else {}
        )
        permission_only = bool(
            str(deferred.get("reason") or "") == "desktop_export_pending"
            and str(metadata.get("attention") or "") == "permission"
            and summary
            and summary == str(metadata.get("rationale") or "").strip()
        )
        if not permission_only:
            return summary
        outcome = (
            metadata.get("outcome_verdict")
            if isinstance(metadata.get("outcome_verdict"), dict)
            else {}
        )
        if outcome.get("verified") is True:
            return str(outcome.get("summary") or "").strip()
        return ""

    def _auto_accept_approved_export(
        self,
        *,
        request: PermissionRequestRecord,
        resolved: PermissionRequestRecord,
        attempt: RunAttemptRecord,
        exported_paths: Iterable[str],
    ) -> bool:
        return self.permission_service.auto_accept_approved_export(
            request=request,
            resolved=resolved,
            attempt=attempt,
            exported_paths=exported_paths,
        )

    async def resume_export(
        self,
        request_id: str,
        *,
        work_item_id: str = "",
        attempt_id: str = "",
    ) -> dict[str, Any]:
        """Retry publication for an already-authorized, uncommitted export.

        This is recovery of the exact durable contract, not a second user
        decision.  A committed allow-once receipt is therefore never eligible.
        """

        context = self.permission_service.context(
            request_id,
            work_item_id=work_item_id,
            attempt_id=attempt_id,
        )
        request = context.request
        attempt = context.attempt
        try:
            permission_resolution = self.permission_service.resume_export(context)
        except Exception as exc:
            self._mark_export_delta_resolved(
                attempt.attempt_id,
                status="failed",
                reason="external_export_recovery_required",
            )
            self.store.record_completion(
                request.work_item_id,
                CompletionDecision(
                    execution_status=attempt.execution_status,
                    completeness="partial",
                    attention="conflict",
                    work_item_state="open",
                    rationale=(
                        "The already-authorized Desktop export still could not be "
                        "published safely; its exact scope remains available for recovery."
                    ),
                    terminal=True,
                ),
                attempt_id=attempt.attempt_id,
                source="host",
                evidence={
                    "permission_request_id": request.request_id,
                    "resolution": "export_recovery_failed",
                    "error": exc.__class__.__name__,
                },
            )
            failure_note = self._claim_terminal_work_notice(
                attempt,
                delivery_id=f"permission:{request.request_id}:export_recovery_failed",
                title="Desktop export recovery failed",
                summary=(
                    "The previously approved Desktop export still could not be "
                    "completed safely. The task remains available for recovery."
                ),
                importance="error",
                metadata={
                    "work_event": "work.export_recovery_failed",
                    "permission_request_id": request.request_id,
                    "attention": "conflict",
                },
            )
            await self.publish_snapshot(reason="permission.export_recovery_failed")
            if failure_note is not None:
                add_work_note(failure_note)
                await bus.emit(Method.CHAT_WORK_NOTE, failure_note)
            raise

        self._mark_export_delta_resolved(
            attempt.attempt_id,
            status="allowed",
            reason="external_export_complete",
        )
        if self._auto_accept_approved_export(
            request=request,
            resolved=permission_resolution.permission,
            attempt=attempt,
            exported_paths=permission_resolution.exported_paths,
        ):
            terminal_note = self._claim_export_resolution_notice(
                request=request,
                resolved=permission_resolution.permission,
                attempt=attempt,
                exported_paths=permission_resolution.exported_paths,
                work_item_state="accepted",
                attention="none",
            )
            snapshot = await self.publish_snapshot(
                reason="permission.export_recovered.auto_accepted"
            )
            if terminal_note is not None:
                add_work_note(terminal_note)
                await bus.emit(Method.CHAT_WORK_NOTE, terminal_note)
            return {
                "permission": permission_resolution.permission.to_dict(),
                "exportedPaths": list(permission_resolution.exported_paths),
                "work": snapshot,
            }
        self.store.record_completion(
            request.work_item_id,
            CompletionDecision(
                execution_status=attempt.execution_status,
                completeness="partial",
                attention="review",
                work_item_state="review_ready",
                rationale=(
                    "The previously authorized staged deliverable was recovered and "
                    "exported to Desktop; user review is still required."
                ),
                terminal=True,
            ),
            attempt_id=attempt.attempt_id,
            source="host",
            evidence={
                "permission_request_id": request.request_id,
                "permission_status": request.status,
                "resolution": "authorized_export_recovered",
                "exported_paths": list(permission_resolution.exported_paths),
            },
        )
        terminal_note = self._claim_export_resolution_notice(
            request=request,
            resolved=permission_resolution.permission,
            attempt=attempt,
            exported_paths=permission_resolution.exported_paths,
            work_item_state="review_ready",
            attention="review",
        )
        snapshot = await self.publish_snapshot(reason="permission.export_recovered")
        if terminal_note is not None:
            add_work_note(terminal_note)
            await bus.emit(Method.CHAT_WORK_NOTE, terminal_note)
        return {
            "permission": permission_resolution.permission.to_dict(),
            "exportedPaths": list(permission_resolution.exported_paths),
            "work": snapshot,
        }

    async def abandon_export(
        self,
        request_id: str,
        *,
        work_item_id: str = "",
        attempt_id: str = "",
    ) -> dict[str, Any]:
        """Close an unrecoverable approved export without touching its files."""

        context = self.permission_service.context(
            request_id,
            work_item_id=work_item_id,
            attempt_id=attempt_id,
        )
        request = context.request
        attempt = context.attempt
        permission = self.permission_service.abandon_export(context).permission
        self._mark_export_delta_resolved(
            attempt.attempt_id,
            status="abandoned",
            reason="external_export_abandoned",
        )
        self.store.record_completion(
            request.work_item_id,
            CompletionDecision(
                execution_status=attempt.execution_status,
                completeness="partial",
                attention="review",
                work_item_state="open",
                rationale=(
                    "The interrupted Desktop export recovery was abandoned. "
                    "Any already-published partial targets were left untouched; "
                    "a new user instruction can now start a separate WorkItem."
                ),
                terminal=True,
            ),
            attempt_id=attempt.attempt_id,
            source="user",
            evidence={
                "permission_request_id": request.request_id,
                "permission_status": permission.status,
                "resolution": "authorized_export_abandoned",
            },
        )
        snapshot = await self.publish_snapshot(reason="permission.export_abandoned")
        return {
            "permission": permission.to_dict(),
            "exportedPaths": [],
            "work": snapshot,
        }

    @staticmethod
    def _is_desktop_export_permission(request: PermissionRequestRecord) -> bool:
        return WorkPermissionService.is_desktop_export_permission(request)

    def _mark_export_delta_resolved(
        self,
        attempt_id: str,
        *,
        status: str,
        reason: str,
    ) -> None:
        self.permission_service.mark_export_delta_resolved(
            attempt_id,
            status=status,
            reason=reason,
        )

    def _refresh_committed_export_integrity(self, attempt: RunAttemptRecord) -> None:
        """Refresh one selected attempt's committed Desktop integrity lazily."""

        permissions = self.store.list_permission_requests(
            attempt.work_item_id,
            attempt_id=attempt.attempt_id,
            status="allowed",
        )
        committed = next(
            (
                request
                for request in reversed(permissions)
                if self._is_desktop_export_permission(request)
                and self.export_service.is_committed_export(
                    request,
                    request.metadata.get("entries")
                    if isinstance(request.metadata.get("entries"), list)
                    else [],
                )
            ),
            None,
        )
        if committed is None:
            return
        entries = committed.metadata.get("entries")
        targets_intact = bool(
            isinstance(entries, list)
            and entries
            and self.export_service.committed_targets_match(committed, entries)
        )
        self._mark_export_delta_resolved(
            attempt.attempt_id,
            status="allowed" if targets_intact else "missing",
            reason=(
                "external_export_complete"
                if targets_intact
                else "external_export_drift"
            ),
        )

    async def route_provider_inspection(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Serve historical View Diff from the attempt boundary, not cwd HEAD."""
        action = str(payload.get("action") or "").strip().lower()
        if action != "view_diff":
            return {"handled": False}
        run_id = str(payload.get("run_id") or payload.get("runId") or "").strip()
        attempt_id = str(payload.get("attempt_id") or payload.get("attemptId") or "").strip()
        attempt = self.store.get_attempt(attempt_id) if attempt_id else None
        if attempt is None and run_id:
            attempt = self.store.get_attempt_by_provider_run(run_id)
        if attempt is None:
            return {"handled": False}
        item = self.store.get_work_item(attempt.work_item_id)
        if item is None:
            return {"handled": True, "ok": False, "error": "work_item_not_found"}
        self._refresh_committed_export_integrity(attempt)
        delta = self.artifact_registry.delta_for_attempt(attempt.attempt_id)
        if delta is None:
            reason_code = "attempt_diff_unavailable"
            reason_text = "This attempt predates the persistent Git baseline, so no attributed diff is available."
            structured = parse_unified_diff("", changed_files=[], untracked=[])
            structured.update(
                {
                    "available": False,
                    "reasonCode": reason_code,
                    "reason": reason_text,
                    "message": reason_text,
                }
            )
            presentation = (
                item.metadata.get("presentation")
                if isinstance(item.metadata.get("presentation"), dict)
                else {}
            )
            report_markdown = str(
                presentation.get("reportMarkdown")
                or presentation.get("markdown")
                or ""
            )
            canvas = diff_canvas_payload(
                phase="Preview",
                title=f"Attempt {attempt.attempt_number} diff",
                lead="Historical diff unavailable",
                diff=structured,
                report_markdown=report_markdown,
                signals=[
                    work_signal(
                        label="diff",
                        text="No persistent attempt baseline",
                        detail="historical attribution unavailable",
                        kind="diff",
                        importance="important",
                        presentation={
                            "text": presentation_message("diff.no_persistent_baseline"),
                            "detail": presentation_message("diff.historical_attribution_unavailable"),
                        },
                    )
                ],
                progress=100,
                size_preset="wide",
                open=True,
                metadata={
                    "provider": attempt.provider,
                    "run_id": attempt.provider_run_id,
                    "cwd": item.workspace_path,
                    "artifact_type": "git.delta",
                    "work": {
                        "project_id": item.project_id,
                        "work_item_id": item.work_item_id,
                        "attempt_id": attempt.attempt_id,
                        "attempt_number": attempt.attempt_number,
                    },
                },
                presentation={
                    "title": presentation_message("attempt.diff", number=attempt.attempt_number),
                    "lead": presentation_message("diff.historical_unavailable"),
                    "diff.message": presentation_message("diff.historical_unavailable"),
                },
            )
            canvas["reason"] = reason_text
            canvas["reasonCode"] = reason_code
            await bus.emit(Method.WALLPAPER_CANVAS, canvas)
            return {
                "handled": True,
                "ok": True,
                "work_item_id": item.work_item_id,
                "attempt_id": attempt.attempt_id,
                "changed_files": [],
            }
        patch = str(delta.get("patch") or "")
        changed_files = [str(path) for path in delta.get("changed_files") or []]
        untracked = [str(path) for path in delta.get("untracked") or []]
        ambiguous_paths = [
            str(path) for path in delta.get("ambiguous_paths") or []
        ]
        structured = parse_unified_diff(patch, changed_files=changed_files, untracked=untracked)
        reason_code = str(delta.get("reason") or "")
        reason_text = {
            "external_export_pending": "The proposed Desktop export is ready and waiting for explicit approval.",
            "external_export_complete": "The approved files were exported to Desktop.",
            "external_export_denied": "The Desktop export was declined; no target file was created.",
            "external_export_expired": "The Desktop export approval expired.",
            "external_export_failed": "The export was authorized, but the exact Desktop copy failed safely without overwriting a target.",
            "external_export_recovery_required": "The export was authorized but interrupted before its Desktop files were fully verified; Amadeus will retry the exact approved copy safely.",
            "external_export_drift": "The export transaction committed, but a Desktop artifact is now missing or changed; allow-once was not replayed.",
            "external_export_abandoned": "The interrupted export recovery was abandoned; any already-published partial targets were left untouched.",
            "staged_export_missing": "The workspace provider did not create the requested staged deliverable, so there is no export diff to review.",
            "export_discovery_error": "Amadeus could not safely inspect the staged deliverable.",
        }.get(reason_code, reason_code)
        ambiguous_without_patch = bool(ambiguous_paths and not patch.strip())
        if ambiguous_without_patch:
            # Git can prove that a pre-existing dirty path changed without
            # owning its before-image.  That is useful conflict evidence, but
            # it is not a renderable attempt diff.  Keep the raw git.delta in
            # the Ledger while making the review surface tell the narrower
            # truth.
            reason_code = "attempt_diff_ambiguous"
            reason_text = (
                "Changes were detected, but this attempt did not own a "
                "before-image for the affected paths, so no trustworthy diff "
                "can be rendered."
            )
            structured["message"] = reason_text
        structured.update(
            {
                "available": bool(delta.get("available", True))
                and not ambiguous_without_patch,
                "reasonCode": reason_code,
                "reason": reason_text,
                "ambiguousPaths": ambiguous_paths,
                "conflicts": [str(value) for value in delta.get("conflicts") or []],
                "pendingExport": bool(
                    delta.get("pending_export") or delta.get("external_export_pending")
                ),
            }
        )
        if not changed_files and not patch:
            structured.update(
                {
                    "clean": not ambiguous_without_patch and reason_code not in {
                        "staged_export_missing",
                        "export_discovery_error",
                    },
                    "message": reason_text or "No changes were attributed to this attempt.",
                }
            )
        presentation = item.metadata.get("presentation") if isinstance(item.metadata.get("presentation"), dict) else {}
        report_markdown = str(
            presentation.get("reportMarkdown")
            or presentation.get("markdown")
            or ""
        )
        canvas = diff_canvas_payload(
            phase="Preview",
            title=f"Attempt {attempt.attempt_number} diff",
            lead=(
                "Diff unavailable: baseline ownership is ambiguous"
                if ambiguous_without_patch
                else f"{len(changed_files)} proposed Desktop file(s)"
                if reason_code.startswith("external_export") and changed_files
                else f"{len(changed_files)} attributed file(s)"
                if changed_files
                else "No attributed Git changes"
            ),
            diff=structured,
            report_markdown=report_markdown,
            signals=[
                work_signal(
                    label="diff",
                    text=f"Baseline {str(delta.get('baseline_head') or 'none')[:12]} to {str(delta.get('current_head') or 'none')[:12]}",
                    detail="ambiguous origin" if ambiguous_paths else "attempt baseline",
                    kind="diff",
                    importance="important" if changed_files or ambiguous_paths else "normal",
                    presentation={
                        "text": presentation_message(
                            "diff.baseline_range",
                            start=str(delta.get("baseline_head") or "none")[:12],
                            end=str(delta.get("current_head") or "none")[:12],
                        ),
                        "detail": presentation_message(
                            "diff.ambiguous_origin" if ambiguous_paths else "diff.attempt_baseline"
                        ),
                    },
                )
            ],
            progress=100,
            size_preset="wide",
            open=True,
            metadata={
                "provider": attempt.provider,
                "run_id": attempt.provider_run_id,
                "cwd": item.workspace_path,
                "artifact_type": "git.delta",
                "work": {
                    "project_id": item.project_id,
                    "work_item_id": item.work_item_id,
                    "attempt_id": attempt.attempt_id,
                    "attempt_number": attempt.attempt_number,
                },
            },
            presentation={
                "title": presentation_message("attempt.diff", number=attempt.attempt_number),
                "lead": presentation_message(
                    "diff.ambiguous"
                    if ambiguous_without_patch
                    else "diff.proposed_desktop_files"
                    if reason_code.startswith("external_export") and changed_files
                    else "diff.attributed_files"
                    if changed_files
                    else "diff.no_attributed_changes",
                    count=len(changed_files),
                ),
                **(
                    {
                        "diff.message": presentation_message(
                            "diff.ambiguous"
                            if ambiguous_without_patch
                            else "diff.no_attributed_changes"
                        )
                    }
                    if ambiguous_without_patch or not changed_files
                    else {}
                ),
            },
        )
        canvas["reason"] = reason_text
        canvas["reasonCode"] = reason_code
        canvas["pendingExport"] = bool(
            delta.get("pending_export") or delta.get("external_export_pending")
        )
        await bus.emit(Method.WALLPAPER_CANVAS, canvas)
        return {
            "handled": True,
            "ok": True,
            "work_item_id": item.work_item_id,
            "attempt_id": attempt.attempt_id,
            "changed_files": changed_files,
        }

    def project_canvas(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return one complete canonical payload for the selected Slice task."""
        incoming = dict(payload or {})
        context = self._context_from_canvas(incoming)
        if context and context.get("workItemId"):
            try:
                self.record_presentation(str(context["workItemId"]), incoming)
            except WorkLedgerNotFound:
                context = None
        snapshot = self.snapshot(surface=self.default_surface)
        selected_id = str(snapshot.get("selectedWorkItemId") or "")
        if selected_id and context and context.get("workItemId") != selected_id:
            selected_item = self.store.get_work_item(selected_id)
            presentation = (
                selected_item.metadata.get("presentation")
                if selected_item is not None and isinstance(selected_item.metadata.get("presentation"), dict)
                else None
            )
            if isinstance(presentation, dict):
                incoming = dict(presentation)
            else:
                selected_projection = snapshot.get("selected") if isinstance(snapshot.get("selected"), dict) else {}
                incoming = self._placeholder_canvas(selected_projection)
            context = self._context_for_selected(snapshot)
        elif selected_id and not context and not incoming:
            selected_item = self.store.get_work_item(selected_id)
            presentation = (
                selected_item.metadata.get("presentation")
                if selected_item is not None and isinstance(selected_item.metadata.get("presentation"), dict)
                else None
            )
            incoming = dict(presentation) if isinstance(presentation, dict) else self._placeholder_canvas(snapshot.get("selected") or {})
            context = self._context_for_selected(snapshot)
        elif selected_id:
            # Unbound legacy canvases remain visible. They receive task counts
            # but are not falsely attributed to the selected WorkItem.
            context = context or None
        if context:
            incoming["workContext"] = context
        if snapshot.get("items"):
            incoming["taskDock"] = self._task_dock(snapshot)
        return self._with_pending_permission(incoming, selected_id)

    def selected_canvas(self, *, surface: str | None = None) -> dict[str, Any] | None:
        target_surface = str(surface or self.default_surface)
        snapshot = self.snapshot(surface=target_surface)
        selected_id = str(snapshot.get("selectedWorkItemId") or "")
        if not selected_id:
            return None
        item = self.store.get_work_item(selected_id)
        presentation = (
            item.metadata.get("presentation")
            if item is not None and isinstance(item.metadata.get("presentation"), dict)
            else None
        )
        payload = dict(presentation) if isinstance(presentation, dict) else self._placeholder_canvas(snapshot.get("selected") or {})
        payload["workContext"] = self._context_for_selected(snapshot)
        payload["taskDock"] = self._task_dock(snapshot)
        return self._with_pending_permission(payload, selected_id)

    def _with_pending_permission(
        self,
        payload: dict[str, Any],
        work_item_id: str,
    ) -> dict[str, Any]:
        output = dict(payload or {})
        item = self.store.get_work_item(work_item_id) if work_item_id else None
        attempts = self.store.list_attempts(work_item_id) if item is not None else []
        attempt = attempts[-1] if attempts else None
        pending = (
            self.store.list_permission_requests(
                work_item_id,
                attempt_id=attempt.attempt_id,
                status="pending",
            )
            if attempt is not None
            else []
        )
        recoverable = None
        if attempt is not None and not pending:
            allowed = self.store.list_permission_requests(
                work_item_id,
                attempt_id=attempt.attempt_id,
                status="allowed",
            )
            recoverable = next(
                (
                    request
                    for request in reversed(allowed)
                    if self._is_desktop_export_permission(request)
                    and self.export_service.can_resume_authorized(request)
                ),
                None,
            )
        if recoverable is not None:
            request_payload = {
                "id": recoverable.request_id,
                "requestId": recoverable.request_id,
                "request_id": recoverable.request_id,
                "workItemId": recoverable.work_item_id,
                "attemptId": recoverable.attempt_id,
                "capability": recoverable.capability,
                "action": "resume_authorized_export",
                "scope": list(recoverable.scope_paths),
                "scope_paths": list(recoverable.scope_paths),
                "reason": (
                    "The exact export was already allowed once, but publication did not "
                    "commit. Retry only the previously authorized targets."
                ),
                "reversibility": recoverable.reversibility,
                "status": recoverable.status,
                "options": ["retry_export", "abandon_export"],
                "retryRequired": False,
            }
            current_signals = output.get("signals") if isinstance(output.get("signals"), list) else []
            output.update(
                {
                    "phase": "Recovery",
                    "title": "Export recovery required",
                    "lead": request_payload["reason"],
                    "blocking": True,
                    "permissionVisible": True,
                    "permissionRequest": request_payload,
                    "signals": [
                        work_signal(
                            label="recovery",
                            text="Authorized Desktop export interrupted",
                            detail="No additional permission is requested",
                            kind="status",
                            importance="blocking",
                            presentation={
                                "text": presentation_message("permission.recovery_signal"),
                                "detail": presentation_message("permission.no_additional"),
                            },
                        ),
                        *current_signals[:3],
                    ],
                    "open": True,
                }
            )
            recovery_metadata = dict(output.get("metadata") or {})
            recovery_metadata["presentation"] = {
                "title": presentation_message("permission.recovery_required"),
                "lead": presentation_message("permission.recovery_lead"),
                "permissionRequest.reason": presentation_message("permission.recovery_lead"),
            }
            output["metadata"] = recovery_metadata
            return output
        if not pending:
            if item is not None and self._is_permission_shell(output):
                # WorkActivity permission checkpoints can be the last durable
                # presentation for an attempt.  Once the ledger request is no
                # longer pending, keeping that shell would leave Slice saying
                # "approval required" even though there is nothing to approve.
                # Replace only an explicit permission presentation; normal
                # markdown/diff canvases may have received a transient card and
                # must keep their report content.
                work_context = output.get("workContext")
                task_dock = output.get("taskDock")
                projected = self._project_item(item)
                neutral = self._placeholder_canvas(projected)
                rationale = str(projected.get("completionRationale") or "").strip()
                if rationale:
                    neutral["lead"] = rationale
                neutral["blocking"] = False
                neutral["permissionVisible"] = False
                if isinstance(work_context, dict):
                    neutral["workContext"] = work_context
                if isinstance(task_dock, dict):
                    neutral["taskDock"] = task_dock
                return neutral
            if output.get("permissionVisible") is True or "permissionRequest" in output:
                output["permissionVisible"] = False
                output.pop("permissionRequest", None)
            return output

        request = pending[-1]
        retry_required = bool(request.metadata.get("retry_required"))
        provider_diagnostic = bool(
            request.metadata.get("kind") == "provider_permission"
            and "allow_once" not in request.options
        )
        request_payload = {
            "id": request.request_id,
            "requestId": request.request_id,
            "request_id": request.request_id,
            "workItemId": request.work_item_id,
            "attemptId": request.attempt_id,
            "capability": request.capability,
            "action": request.action,
            "scope": list(request.scope_paths),
            "scope_paths": list(request.scope_paths),
            "reason": request.reason,
            "reversibility": request.reversibility,
            "status": request.status,
            "options": list(request.options),
            "retryRequired": retry_required,
            "diagnosticOnly": provider_diagnostic,
            **self._binary_export_preview_projection(request),
        }
        current_signals = output.get("signals") if isinstance(output.get("signals"), list) else []
        scope_detail = ", ".join(Path(path).name or path for path in request.scope_paths[:3])
        diagnostic_lead = (
            f"{request.reason} This provider run cannot be approved in place. "
            "Dismiss the checkpoint, then Retry the same instruction or give a new instruction."
            if provider_diagnostic
            else request.reason
        )
        output.update(
            {
                "phase": "Blocked" if provider_diagnostic else "Check",
                "title": "Provider action blocked" if provider_diagnostic else "Approval required",
                "lead": diagnostic_lead,
                "blocking": True,
                "permissionVisible": True,
                "permissionRequest": request_payload,
                "signals": [
                    work_signal(
                        label="permission",
                        text=f"{request.capability}: {request.action}",
                        detail=scope_detail or "scoped operation",
                        kind="permission",
                        importance="blocking",
                        presentation={
                            "detail": presentation_message("permission.scoped_operation"),
                        },
                    ),
                    *current_signals[:3],
                ],
                "open": True,
            }
        )
        permission_metadata = dict(output.get("metadata") or {})
        permission_metadata["presentation"] = {
            "title": presentation_message(
                "permission.action_blocked" if provider_diagnostic else "permission.approval_required"
            ),
            **(
                {
                    "lead": presentation_message("permission.provider_blocked"),
                    "permissionRequest.reason": presentation_message("permission.provider_blocked"),
                }
                if provider_diagnostic
                else {
                    "lead": presentation_message("permission.approval_lead"),
                    "permissionRequest.reason": presentation_message("permission.approval_lead"),
                }
            ),
        }
        output["metadata"] = permission_metadata
        return output

    @staticmethod
    def _binary_export_preview_projection(
        request: PermissionRequestRecord,
    ) -> dict[str, Any]:
        """Expose binary identity evidence without leaking staging authority."""

        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        if (
            metadata.get("kind") != "desktop_export"
            or metadata.get("preview_complete") is not True
        ):
            return {}
        entries = metadata.get("entries") if isinstance(metadata.get("entries"), list) else []
        previews = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("preview_status") != "binary_identity":
                continue
            relative = str(entry.get("relative_path") or "").replace("\\", "/").strip("/")
            if not relative:
                continue
            previews.append(
                {
                    "path": f"Desktop/{relative}",
                    "status": "binary_identity",
                    "mediaType": str(
                        entry.get("media_type_hint") or "application/octet-stream"
                    ),
                    "sizeBytes": int(entry.get("size_bytes") or 0),
                    "sha256": str(entry.get("sha256") or ""),
                }
            )
        if not previews:
            return {}
        return {
            "previewComplete": True,
            "previewVersion": int(metadata.get("preview_version") or 1),
            "previews": previews,
        }

    @staticmethod
    def _is_permission_shell(payload: dict[str, Any]) -> bool:
        """Recognize a durable permission checkpoint without matching reports."""

        mode = str(payload.get("mode") or "").strip().lower()
        if mode == "permission":
            return True

        # Conservative compatibility for permission canvases persisted before
        # the dedicated mode existed.  Requiring every marker keeps workflow,
        # markdown, and diff presentations out of this cleanup path.
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        phase = str(payload.get("phase") or "").strip().lower()
        title = str(payload.get("title") or "").strip().lower()
        has_request = isinstance(payload.get("permissionRequest"), dict)
        has_permission_title = "permission" in title or "approval" in title
        return bool(
            mode in {"", "workflow"}
            and phase in {"check", "checkpoint"}
            and metadata.get("attention") == "permission"
            and payload.get("permissionVisible") is True
            and has_request
            and has_permission_title
        )

    async def _publish_provider_snapshot(self, *, reason: str) -> None:
        """Schedule one coalesced Work projection without stalling Provider IO.

        Provider events are durable facts and are ingested before this method is
        called.  The Work/Canvas projection is presentation: awaiting its event
        subscribers here used to serialize every Provider tool event behind UI
        work.  Terminal and control paths still call ``publish_snapshot``
        directly, so their ordering remains explicit.
        """

        interval = self._provider_snapshot_min_interval_s
        now = time.monotonic()
        elapsed = now - self._provider_snapshot_last_at
        self._provider_snapshot_reason = str(reason or "provider.event")
        if self._provider_snapshot_task is not None and not self._provider_snapshot_task.done():
            return
        delay = (
            0.0
            if interval <= 0.0
            or self._provider_snapshot_last_at <= 0.0
            or elapsed >= interval
            else max(0.0, interval - elapsed)
        )

        async def publish_later() -> None:
            try:
                if delay > 0.0:
                    await asyncio.sleep(delay)
                self._provider_snapshot_last_at = time.monotonic()
                await self.publish_snapshot(reason=self._provider_snapshot_reason)
            except asyncio.CancelledError:
                raise
            finally:
                if self._provider_snapshot_task is asyncio.current_task():
                    self._provider_snapshot_task = None

        self._provider_snapshot_task = asyncio.create_task(
            publish_later(),
            name="work-provider-snapshot",
        )

    async def publish_snapshot(self, *, reason: str, surface: str | None = None) -> dict[str, Any]:
        # Any explicit control/result publication supersedes a delayed activity
        # projection.  The latest state is included in this snapshot already.
        if not str(reason or "").startswith("provider.event:"):
            pending = self._provider_snapshot_task
            if (
                pending is not None
                and pending is not asyncio.current_task()
                and not pending.done()
            ):
                pending.cancel()
                self._provider_snapshot_task = None
        if surface:
            surfaces = [str(surface)]
        else:
            surfaces = [self.default_surface]
            surfaces.extend(
                focus.surface
                for focus in self.store.list_focus()
                if (
                    focus.surface
                    and focus.surface != WORKSPACE_ROUTING_SURFACE
                    and focus.surface not in surfaces
                )
            )
        default_snapshot: dict[str, Any] | None = None
        for target_surface in surfaces:
            snapshot = self.snapshot(surface=target_surface)
            if target_surface == self.default_surface:
                default_snapshot = snapshot
            await bus.emit(Method.WORK_UPDATED, {"work": snapshot, "reason": reason})
        if self.default_surface in surfaces:
            canvas = self.selected_canvas(surface=self.default_surface)
            if canvas is not None:
                await bus.emit(Method.WALLPAPER_CANVAS, canvas)
        return default_snapshot or self.snapshot(surface=surfaces[0])

    def _emit_snapshot_now(self, surface: str, *, reason: str) -> None:
        surfaces = [surface]
        surfaces.extend(
            focus.surface
            for focus in self.store.list_focus()
            if (
                focus.surface
                and focus.surface != WORKSPACE_ROUTING_SURFACE
                and focus.surface not in surfaces
            )
        )
        for target_surface in surfaces:
            snapshot = self.snapshot(surface=target_surface)
            bus.emit_now(Method.WORK_UPDATED, {"work": snapshot, "reason": reason})

    @staticmethod
    def _task_dock(snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "revision": snapshot.get("revision") or "",
            "currentSessionId": snapshot.get("currentSessionId") or "",
            "selectedWorkItemId": snapshot.get("selectedWorkItemId") or "",
            "focusMode": snapshot.get("focusMode") or "auto",
            "workspaceFocusMode": snapshot.get("workspaceFocusMode") or "auto",
            "workspaceFocusWorkItemId": snapshot.get("workspaceFocusWorkItemId") or "",
            "workspaceFocusProjectId": snapshot.get("workspaceFocusProjectId") or "",
            "workspaceFocusPath": snapshot.get("workspaceFocusPath") or "",
            "destinationLabel": snapshot.get("destinationLabel") or "",
            "destinationProjectId": snapshot.get("destinationProjectId") or "",
            "destinationFeedback": snapshot.get("destinationFeedback"),
            "counts": dict(snapshot.get("counts") or {}),
            "projects": list(snapshot.get("projects") or [])[:100],
            "items": list(snapshot.get("items") or [])[:24],
        }

    @staticmethod
    def _context_for_selected(snapshot: dict[str, Any]) -> dict[str, Any]:
        selected = snapshot.get("selected") if isinstance(snapshot.get("selected"), dict) else {}
        return {
            "projectId": str(selected.get("projectId") or ""),
            "workItemId": str(selected.get("id") or snapshot.get("selectedWorkItemId") or ""),
            "runId": str(selected.get("currentRunId") or ""),
            "attemptId": str(selected.get("attemptId") or ""),
        }

    def _context_from_canvas(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        explicit = payload.get("workContext") if isinstance(payload.get("workContext"), dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        work = metadata.get("work") if isinstance(metadata.get("work"), dict) else {}
        run_id = str(
            explicit.get("runId")
            or explicit.get("run_id")
            or metadata.get("run_id")
            or metadata.get("runId")
            or ""
        ).strip()
        work_item_id = str(
            explicit.get("workItemId")
            or explicit.get("work_item_id")
            or work.get("work_item_id")
            or work.get("workItemId")
            or ""
        ).strip()
        attempt: RunAttemptRecord | None = None
        if run_id:
            attempt = self.store.get_attempt_by_provider_run(run_id)
        if not work_item_id and attempt is not None:
            work_item_id = attempt.work_item_id
        if not work_item_id:
            return None
        item = self.store.get_work_item(work_item_id)
        if item is None:
            return None
        if attempt is None:
            attempt_id = str(work.get("attempt_id") or explicit.get("attemptId") or "")
            attempt = self.store.get_attempt(attempt_id) if attempt_id else None
        return {
            "projectId": item.project_id,
            "workItemId": item.work_item_id,
            "runId": run_id or (attempt.provider_run_id if attempt else ""),
            "attemptId": attempt.attempt_id if attempt else str(work.get("attempt_id") or ""),
        }

    @staticmethod
    def _placeholder_canvas(item: dict[str, Any]) -> dict[str, Any]:
        title = str(item.get("title") or "Selected task")
        goal = str(item.get("goal") or "No presentation has been recorded for this task yet.")
        return {
            "schema_id": "amadeus.ai_os.v1",
            "mode": "workflow",
            "phase": "Archive" if item.get("state") == "archived" else "Review",
            "title": title,
            "lead": goal,
            "progress": 100 if item.get("execution") == "succeeded" else 0,
            "signals": [
                {
                    "schema_id": "amadeus.ai_os.v1",
                    "kind": "status",
                    "label": "task",
                    "text": str(item.get("execution") or "idle"),
                    "detail": str(item.get("completion") or "unknown"),
                    "importance": "normal",
                }
            ],
            "sizePreset": "compact",
            "open": True,
        }

    @staticmethod
    def _task_title(task: str) -> str:
        return ProviderEventIngestor.task_title(task)

    # -- Restart compatibility ------------------------------------------

    def adopt_runtime_records(self, records: Iterable[dict[str, Any]]) -> None:
        live_ids: set[str] = set()
        for record in records:
            run_id = str(record.get("run_id") or "").strip()
            if not run_id:
                continue
            mapped = (
                self._event_execution_status(str(record.get("status") or "orphaned"))
                or "orphaned"
            )
            # Only records backed by a task in this process are live. A
            # recovered runtime snapshot may be orphaned and must use the
            # bounded Resume path instead of keeping its old writer lease.
            if mapped in _ACTIVE_EXECUTION:
                live_ids.add(run_id)
            existing = self.store.get_attempt_by_provider_run(run_id)
            if existing is not None:
                if (
                    mapped == "orphaned"
                    and existing.execution_status not in _TERMINAL_EXECUTION
                ):
                    self.store.update_attempt(
                        existing.attempt_id,
                        execution_status=(
                            "orphaned"
                            if existing.execution_status in _ACTIVE_EXECUTION
                            else None
                        ),
                        metadata={
                            "runtime_resumable": True,
                            "startup_reconciliation": "runtime_record_orphaned",
                        },
                    )
                elif (
                    mapped in _TERMINAL_EXECUTION
                    and existing.execution_status in (_ACTIVE_EXECUTION | {"orphaned"})
                ):
                    self.store.update_attempt(
                        existing.attempt_id,
                        execution_status=mapped,
                        result=str(record.get("result") or ""),
                        error=str(record.get("error") or ""),
                        metadata={
                            "runtime_resumable": False,
                            "startup_reconciliation": "runtime_record_terminal",
                        },
                    )
                continue
            attempt = self._adopt_runtime_run(record)
            if attempt is None:
                continue
            self.store.update_attempt(
                attempt.attempt_id,
                execution_status=mapped,
                result=str(record.get("result") or ""),
                error=str(record.get("error") or ""),
                metadata={"runtime_resumable": mapped == "orphaned"},
            )
        for item in self.store.list_work_items(limit=2000):
            for attempt in self.store.list_attempts(item.work_item_id):
                if (
                    attempt.execution_status in _ACTIVE_EXECUTION
                    and (
                        not attempt.provider_run_id
                        or attempt.provider_run_id not in live_ids
                    )
                ):
                    self.store.update_attempt(
                        attempt.attempt_id,
                        execution_status="orphaned",
                        metadata={
                            "runtime_resumable": False,
                            "startup_reconciliation": "provider_run_not_live",
                        },
                    )
        for lease in self.store.list_writer_leases(active_only=True):
            attempt = self.store.get_attempt(lease.attempt_id)
            if attempt is None or attempt.execution_status in _TERMINAL_EXECUTION:
                self.store.release_writer_lease(lease.attempt_id, status="released")
            elif not attempt.provider_run_id or attempt.provider_run_id not in live_ids:
                self.store.release_writer_lease(
                    lease.attempt_id,
                    status="stale",
                    metadata={"startup_reconciliation": "provider_run_not_live"},
                )
