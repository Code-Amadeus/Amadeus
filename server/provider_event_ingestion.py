"""Canonical Provider event ownership at the Work Ledger boundary.

Provider adapters report runtime facts; they do not own durable Work identity.
This module resolves an event to exactly one Attempt and applies only the
generic lifecycle/activity transitions.  Artifact, permission, export,
completion, narration, and UI services consume the returned ingestion result
without re-interpreting Provider identity or terminal status.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading
from typing import Any, Callable

from agent_host.work_ledger_store import (
    WorkLedgerConflict,
    WorkLedgerStore,
)
from agent_host.work_ledger_types import RunAttemptRecord
from agent_host.provider_identity import (
    PARENT_CONTEXT_DELIVERED_EVENT,
    PARENT_CONTEXT_DELIVERY_METADATA_KEY,
    project_parent_context_delivery,
)
from server.work_activity_snapshot import (
    ACTIVITY_METADATA_KEY,
    is_material_activity_event,
    project_activity_event,
    project_activity_result,
)
from server.work_completion import normalize_execution_status


PROVIDER_TERMINAL_PIPELINE_METADATA_KEY = "provider_terminal_pipeline"
_TERMINAL_EXECUTION = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class IngestedProviderEvent:
    run_id: str
    event_type: str
    payload: dict[str, Any]
    attempt: RunAttemptRecord
    material: bool
    accepted: bool


@dataclass(frozen=True, slots=True)
class IngestedProviderResult:
    run_id: str
    attempt: RunAttemptRecord
    status: str
    result: str
    error: str
    metadata: dict[str, Any]
    evidence: dict[str, Any]
    pipeline_required: bool
    pipeline_receipt: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OrphanedProviderResultPrecondition:
    """Exact Host snapshot required for one reconciled terminal admission.

    This type carries only generic Work/Provider identities and an opaque
    Work-Ledger token.  It gives a reconciled result no authority to adopt a
    missing run or to attach itself to a different Attempt.
    """

    attempt_id: str
    work_item_id: str
    provider: str
    run_id: str
    snapshot_token: str

    def __post_init__(self) -> None:
        values = {
            "attempt_id": str(self.attempt_id or "").strip(),
            "work_item_id": str(self.work_item_id or "").strip(),
            "provider": str(self.provider or "").strip().lower(),
            "run_id": str(self.run_id or "").strip(),
            "snapshot_token": str(self.snapshot_token or "").strip().lower(),
        }
        if any(not value for value in values.values()):
            raise ValueError("orphaned Provider result precondition is incomplete")
        if len(values["snapshot_token"]) != 64 or any(
            character not in "0123456789abcdef"
            for character in values["snapshot_token"]
        ):
            raise ValueError("orphaned Provider result snapshot token is invalid")
        for key, value in values.items():
            object.__setattr__(self, key, value)


class ProviderEventIngestor:
    """Resolve canonical runtime facts and persist one Attempt lifecycle."""

    def __init__(
        self,
        store: WorkLedgerStore,
        *,
        clock: Callable[[], float],
        default_surface: str,
    ) -> None:
        self.store = store
        self._clock = clock
        self.default_surface = str(default_surface)
        self._evidence: dict[str, dict[str, Any]] = {}
        self._ingest_lock = threading.RLock()

    def ingest_event(self, params: dict[str, Any]) -> IngestedProviderEvent | None:
        with self._ingest_lock:
            return self._ingest_event(params)

    def _ingest_event(self, params: dict[str, Any]) -> IngestedProviderEvent | None:
        run_id = str(params.get("run_id") or "").strip()
        if not run_id:
            return None
        event_type = str(params.get("type") or "").strip().lower()
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        attempt = self.attempt_for_event(params)
        if attempt is None:
            return None
        material = is_material_activity_event(event_type)
        accepted = True

        if material:
            activity = (
                attempt.metadata.get(ACTIVITY_METADATA_KEY)
                if isinstance(attempt.metadata.get(ACTIVITY_METADATA_KEY), dict)
                else {}
            )
            sequence = self._event_sequence(params.get("sequence"))
            previous_sequence = self._event_sequence(activity.get("eventSequence"))
            if sequence > 0 and previous_sequence > 0 and sequence <= previous_sequence:
                return IngestedProviderEvent(
                    run_id=run_id,
                    event_type=event_type,
                    payload=payload,
                    attempt=attempt,
                    material=True,
                    accepted=False,
                )
        if (
            event_type in {"run.finished", "run.failed", "run.cancelled"}
            and attempt.execution_status in _TERMINAL_EXECUTION
        ):
            return IngestedProviderEvent(
                run_id=run_id,
                event_type=event_type,
                payload=payload,
                attempt=attempt,
                material=material,
                accepted=False,
            )

        try:
            if event_type == "run.created":
                if not attempt.provider_run_id:
                    attempt = self.store.bind_provider_run(attempt.attempt_id, run_id)
                self.store.update_attempt(attempt.attempt_id, execution_status="queued")
            elif event_type == PARENT_CONTEXT_DELIVERED_EVENT:
                event_metadata = (
                    params.get("metadata")
                    if isinstance(params.get("metadata"), dict)
                    else {}
                )
                receipt = event_metadata.get(PARENT_CONTEXT_DELIVERY_METADATA_KEY)
                if isinstance(receipt, dict):
                    projection = project_parent_context_delivery(
                        event_metadata,
                        receipt=receipt,
                    )
                    if projection:
                        self.store.merge_attempt_control_metadata(
                            attempt.attempt_id,
                            projection,
                        )
            elif event_type == "run.status":
                status = str(payload.get("status") or "").strip().lower()
                mapped = self.execution_status(status)
                liveness = str(payload.get("liveness") or "").strip().lower()
                status_metadata: dict[str, Any] = {}
                if liveness:
                    status_metadata["provider_liveness"] = {
                        "state": liveness,
                        **{
                            key: payload[key]
                            for key in (
                                "stage",
                                "silence_s",
                                "elapsed_s",
                                "probe_status",
                                "probe_reachable",
                                "observed_at",
                                "last_provider_event_at",
                                "recovered",
                                "stall_duration_s",
                                "reason",
                            )
                            if key in payload
                        },
                    }
                if mapped or status_metadata:
                    self.store.update_attempt(
                        attempt.attempt_id,
                        execution_status=mapped or None,
                        metadata=status_metadata or None,
                    )
            elif event_type == "run.started":
                self.store.update_attempt(attempt.attempt_id, execution_status="running")
            # Terminal Provider events remain observable activity. The one
            # canonical Provider result owns durable terminal status and its
            # pending pipeline receipt. If the process stops between Runtime's
            # event and result publication, startup must see an unresolved
            # Attempt rather than a terminal row with no completion path.

            if material:
                current = self.store.get_attempt(attempt.attempt_id) or attempt
                previous = (
                    current.metadata.get(ACTIVITY_METADATA_KEY)
                    if isinstance(current.metadata.get(ACTIVITY_METADATA_KEY), dict)
                    else {}
                )
                projected = project_activity_event(
                    previous,
                    params,
                    execution_status=current.execution_status,
                    now=float(self._clock()),
                )
                if projected != previous:
                    self.store.update_attempt(
                        attempt.attempt_id,
                        metadata={ACTIVITY_METADATA_KEY: projected},
                    )
        except WorkLedgerConflict:
            # Runtime emits both terminal events and a result.  Repeated facts
            # are idempotent; contradictory late facts cannot reopen an
            # Attempt or replace its terminal status.
            accepted = False

        current = self.store.get_attempt(attempt.attempt_id) or attempt
        return IngestedProviderEvent(
            run_id=run_id,
            event_type=event_type,
            payload=payload,
            attempt=current,
            material=material,
            accepted=accepted,
        )

    def ingest_result(
        self,
        params: dict[str, Any],
        *,
        precondition: OrphanedProviderResultPrecondition | None = None,
    ) -> IngestedProviderResult | None:
        with self._ingest_lock:
            return self._ingest_result(params, precondition=precondition)

    def _ingest_result(
        self,
        params: dict[str, Any],
        *,
        precondition: OrphanedProviderResultPrecondition | None = None,
    ) -> IngestedProviderResult | None:
        run_id = str(params.get("run_id") or "").strip()
        if precondition is None:
            attempt = self.attempt_for_event(params)
        else:
            if not isinstance(precondition, OrphanedProviderResultPrecondition):
                raise TypeError(
                    "reconciled Provider result precondition must use the typed contract"
                )
            provider = str(params.get("provider") or "").strip().lower()
            attempt_id = str(
                params.get("attempt_id") or params.get("attemptId") or ""
            ).strip()
            work_item_id = str(
                params.get("task_id") or params.get("taskId") or ""
            ).strip()
            if (
                run_id != precondition.run_id
                or provider != precondition.provider
                or attempt_id != precondition.attempt_id
                or work_item_id != precondition.work_item_id
            ):
                return None
            attempt = self.store.get_attempt(precondition.attempt_id)
            if (
                attempt is None
                or attempt.execution_status != "orphaned"
                or attempt.work_item_id != precondition.work_item_id
                or attempt.provider != precondition.provider
                or attempt.provider_run_id != precondition.run_id
            ):
                return None
        if attempt is None:
            return None
        status = normalize_execution_status(str(params.get("status") or "failed"))
        if precondition is not None and status not in _TERMINAL_EXECUTION:
            return None
        result = str(params.get("result") or "")
        error = str(params.get("error") or "")
        metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        activity = project_activity_result(
            (
                attempt.metadata.get(ACTIVITY_METADATA_KEY)
                if isinstance(attempt.metadata.get(ACTIVITY_METADATA_KEY), dict)
                else {}
            ),
            status=status,
            observed_at=float(self._clock()),
        )
        result_metadata: dict[str, Any] = {
            "provider_result": metadata,
            ACTIVITY_METADATA_KEY: activity,
        }
        if status == "orphaned":
            # False is a restriction, not Provider-granted execution authority.
            # Resume stays closed unless the Host later verifies a checkpoint
            # and explicitly promotes this durable Attempt fact.
            result_metadata["runtime_resumable"] = False
        if isinstance(metadata.get("provider_session"), dict):
            result_metadata["provider_session"] = dict(metadata["provider_session"])

        if status == "orphaned":
            try:
                attempt = self.store.update_attempt(
                    attempt.attempt_id,
                    execution_status=status,
                    result=result,
                    error=error,
                    metadata=result_metadata,
                )
            except WorkLedgerConflict:
                attempt = self.store.get_attempt(attempt.attempt_id) or attempt
                status = attempt.execution_status
            return IngestedProviderResult(
                run_id=run_id,
                attempt=attempt,
                status=status,
                result=result,
                error=error,
                metadata=metadata,
                # Orphaned is not a terminal receipt. Keep accumulated facts
                # for a later reconciled result instead of consuming them at
                # uncertainty.
                evidence={},
                pipeline_required=False,
                pipeline_receipt={},
            )

        receipt_hash = self._terminal_receipt_sha256(
            provider=attempt.provider,
            run_id=run_id,
            status=status,
            result=result,
            error=error,
            metadata=metadata,
        )
        existing_receipt = attempt.metadata.get(PROVIDER_TERMINAL_PIPELINE_METADATA_KEY)
        if isinstance(existing_receipt, dict):
            exact = bool(
                existing_receipt.get("version") == 1
                and existing_receipt.get("provider") == attempt.provider
                and existing_receipt.get("run_id") == run_id
                and existing_receipt.get("status") == status
                and existing_receipt.get("receipt_sha256") == receipt_hash
                and attempt.execution_status == status
            )
            state = str(existing_receipt.get("state") or "")
            if exact and state in {"pending", "completed"}:
                persisted_metadata = attempt.metadata.get("provider_result")
                persisted_facts = existing_receipt.get("facts")
                return IngestedProviderResult(
                    run_id=run_id,
                    attempt=attempt,
                    status=attempt.execution_status,
                    result=attempt.result,
                    error=attempt.error,
                    metadata=(
                        dict(persisted_metadata)
                        if isinstance(persisted_metadata, dict)
                        else {}
                    ),
                    evidence=(
                        self._json_object_copy(persisted_facts)
                        if isinstance(persisted_facts, dict)
                        else {}
                    ),
                    pipeline_required=state == "pending",
                    pipeline_receipt=dict(existing_receipt),
                )
            # Once the Host has a terminal receipt, a malformed or
            # contradictory replay is evidence only. Preserve the durable
            # winner and never run downstream effects from the new payload.
            return IngestedProviderResult(
                run_id=run_id,
                attempt=attempt,
                status=attempt.execution_status,
                result=attempt.result,
                error=attempt.error,
                metadata={},
                evidence={},
                pipeline_required=False,
                pipeline_receipt={},
            )

        if (
            attempt.execution_status in _TERMINAL_EXECUTION
            and attempt.execution_status != status
        ):
            return IngestedProviderResult(
                run_id=run_id,
                attempt=attempt,
                status=attempt.execution_status,
                result=attempt.result,
                error=attempt.error,
                metadata={},
                evidence={},
                pipeline_required=False,
                pipeline_receipt={},
            )

        facts = self._json_object_copy(self._evidence.get(run_id, {}))
        receipt = {
            "version": 1,
            "state": "pending",
            "provider": attempt.provider,
            "run_id": run_id,
            "status": status,
            "receipt_sha256": receipt_hash,
            "facts": facts,
            "received_at": float(self._clock()),
        }
        result_metadata[PROVIDER_TERMINAL_PIPELINE_METADATA_KEY] = receipt
        try:
            attempt = self.store.update_attempt(
                attempt.attempt_id,
                execution_status=status,
                result=result,
                error=error,
                metadata=result_metadata,
                expected_snapshot_token=(
                    precondition.snapshot_token
                    if precondition is not None
                    else None
                ),
            )
        except WorkLedgerConflict:
            if precondition is not None:
                return None
            attempt = self.store.get_attempt(attempt.attempt_id) or attempt
            # A late contradictory result is evidence, not authority to
            # reinterpret an already-terminal Attempt.  Downstream completion
            # and narration must use the durable status that actually won.
            status = attempt.execution_status
            return IngestedProviderResult(
                run_id=run_id,
                attempt=attempt,
                status=status,
                result=attempt.result,
                error=attempt.error,
                metadata={},
                evidence={},
                pipeline_required=False,
                pipeline_receipt={},
            )
        self._evidence.pop(run_id, None)
        return IngestedProviderResult(
            run_id=run_id,
            attempt=attempt,
            status=status,
            result=result,
            error=error,
            metadata=metadata,
            evidence=facts,
            pipeline_required=True,
            pipeline_receipt=receipt,
        )

    def complete_terminal_pipeline(
        self,
        attempt_id: str,
        receipt: dict[str, Any],
    ) -> bool:
        """Atomically retire the exact pending Host terminal receipt."""

        if not isinstance(receipt, dict) or receipt.get("state") != "pending":
            return False
        completed = dict(receipt)
        completed["state"] = "completed"
        completed["completed_at"] = float(self._clock())
        current, swapped = self.store.compare_and_set_attempt_metadata(
            attempt_id,
            key=PROVIDER_TERMINAL_PIPELINE_METADATA_KEY,
            expected_present=True,
            expected_value=receipt,
            value=completed,
        )
        if swapped:
            return True
        existing = current.metadata.get(PROVIDER_TERMINAL_PIPELINE_METADATA_KEY)
        return bool(
            isinstance(existing, dict)
            and existing.get("state") == "completed"
            and existing.get("receipt_sha256") == receipt.get("receipt_sha256")
            and existing.get("run_id") == receipt.get("run_id")
        )

    @staticmethod
    def terminal_replay_payload(attempt: RunAttemptRecord) -> dict[str, Any] | None:
        """Reconstruct only a Host-created pending terminal receipt."""

        receipt = attempt.metadata.get(PROVIDER_TERMINAL_PIPELINE_METADATA_KEY)
        metadata = attempt.metadata.get("provider_result")
        if not isinstance(receipt, dict) or not isinstance(metadata, dict):
            return None
        if not (
            receipt.get("version") == 1
            and receipt.get("state") == "pending"
            and receipt.get("provider") == attempt.provider
            and receipt.get("run_id") == attempt.provider_run_id
            and receipt.get("status") == attempt.execution_status
            and attempt.execution_status in _TERMINAL_EXECUTION
        ):
            return None
        receipt_hash = ProviderEventIngestor._terminal_receipt_sha256(
            provider=attempt.provider,
            run_id=attempt.provider_run_id,
            status=attempt.execution_status,
            result=attempt.result,
            error=attempt.error,
            metadata=metadata,
        )
        if receipt.get("receipt_sha256") != receipt_hash:
            return None
        return {
            "provider": attempt.provider,
            "run_id": attempt.provider_run_id,
            "status": attempt.execution_status,
            "result": attempt.result,
            "error": attempt.error,
            "metadata": dict(metadata),
        }

    @staticmethod
    def _terminal_receipt_sha256(
        *,
        provider: str,
        run_id: str,
        status: str,
        result: str,
        error: str,
        metadata: dict[str, Any],
    ) -> str:
        canonical = json.dumps(
            {
                "provider": provider,
                "run_id": run_id,
                "status": status,
                "result": result,
                "error": error,
                "metadata": metadata,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()

    @staticmethod
    def _json_object_copy(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        copied = json.loads(json.dumps(value, ensure_ascii=False, default=str))
        return copied if isinstance(copied, dict) else {}

    @staticmethod
    def _event_sequence(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def attempt_for_event(
        self,
        params: dict[str, Any],
    ) -> RunAttemptRecord | None:
        """Resolve accepted Work ownership; live events never create that ownership."""
        run_id = str(params.get("run_id") or "").strip()
        provider = str(params.get("provider") or "").strip().lower()
        attempt = self.store.get_attempt_by_provider_run(run_id)
        if attempt is not None:
            return attempt if self._identity_matches(attempt, run_id, provider) else None
        attempt_id = str(params.get("attempt_id") or params.get("attemptId") or "").strip()
        if attempt_id:
            attempt = self.store.get_attempt(attempt_id)
            return (
                attempt
                if attempt is not None
                and self._identity_matches(attempt, run_id, provider)
                else None
            )
        metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        work = metadata.get("work") if isinstance(metadata.get("work"), dict) else {}
        attempt_id = str(work.get("attempt_id") or work.get("attemptId") or "").strip()
        if attempt_id:
            attempt = self.store.get_attempt(attempt_id)
            return (
                attempt
                if attempt is not None
                and self._identity_matches(attempt, run_id, provider)
                else None
            )
        return None

    @staticmethod
    def _identity_matches(
        attempt: RunAttemptRecord,
        run_id: str,
        provider: str,
    ) -> bool:
        bound_run = str(attempt.provider_run_id or "").strip()
        if bound_run and run_id and bound_run != run_id:
            return False
        bound_provider = str(attempt.provider or "").strip().lower()
        return not provider or not bound_provider or provider == bound_provider

    def adopt_runtime_run(self, params: dict[str, Any]) -> RunAttemptRecord | None:
        """Explicit startup recovery of a Host-selected legacy Work runtime record.

        Live event/result ingestion must not call this: native activity is not
        authority to create a WorkItem, Project or foreground focus.
        """

        run_id = str(params.get("run_id") or "").strip()
        if not run_id:
            return None
        existing = self.store.get_attempt_by_provider_run(run_id)
        if existing is not None:
            return existing
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        cwd = str(params.get("cwd") or payload.get("cwd") or Path.cwd())
        task = str(params.get("task") or payload.get("task") or "Recovered provider task").strip()
        provider = str(params.get("provider") or "provider").strip().lower()
        mode = str(params.get("mode") or payload.get("mode") or "agent")
        project = self.store.get_project_by_path(cwd)
        if project is None:
            project = self.store.create_or_get_project(
                cwd,
                metadata={"runtime_recovery": True},
            )
        item = self.store.create_work_item(
            project.project_id,
            title=self.task_title(task),
            goal=task,
            workspace_path=cwd,
            metadata={
                "source_run_id": run_id,
                "runtime_recovery": True,
            },
        )
        attempt = self.store.create_attempt(
            item.work_item_id,
            provider=provider,
            task=task,
            mode=mode,
            provider_run_id=run_id,
            metadata={"runtime_recovery": True},
        )
        focus = self.store.get_focus(self.default_surface)
        if focus is None or focus.mode == "auto":
            self.store.set_focus(self.default_surface, item.work_item_id, mode="auto")
        return attempt

    def event_fact(self, run_id: str) -> dict[str, Any]:
        return self._evidence.setdefault(
            run_id,
            {
                "pending_permissions": 0,
                "pending_inputs": 0,
                "conflicts": [],
                "artifact_hints": [],
                "provider_permission_diagnostics": [],
                "provider_permission_events": [],
                "provider_permission_tool_ids": [],
                "permission_failure_suppressions": 0,
                "validation_statuses": [],
                "tool_diagnostics": [],
            },
        )

    @staticmethod
    def execution_status(value: str) -> str:
        aliases = {
            "queued": "queued",
            "pending": "queued",
            "running": "running",
            "active": "running",
            "working": "running",
            "done": "succeeded",
            "completed": "succeeded",
            "succeeded": "succeeded",
            "error": "failed",
            "failed": "failed",
            "interrupted": "failed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
            "orphaned": "orphaned",
        }
        return aliases.get(str(value or "").strip().lower(), "")

    @staticmethod
    def task_title(task: str) -> str:
        text = " ".join(str(task or "").split())
        if len(text) <= 96:
            return text or "Untitled work item"
        return text[:93].rstrip() + "..."

    @classmethod
    def work_item_title(cls, task: str, display_title: str = "") -> str:
        """Canonicalize a presentation label while retaining task as fallback."""

        suggested = " ".join(str(display_title or "").split())
        try:
            suggested.encode("utf-8")
        except UnicodeEncodeError:
            suggested = ""
        return cls.task_title(suggested or task)
