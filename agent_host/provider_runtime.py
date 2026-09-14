from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_host.provider_contract import (
    ProviderManifest,
    compatibility_errors,
    manifest_for_adapter,
)
from agent_host.provider_workspace import prepare_workspace_binding
from agent_host.provider_outcome import (
    OUTCOME_EVIDENCE_METADATA_KEY,
    ProviderOutcomeEvidence,
)
from agent_host.provider_progress import is_progress_only_workspace_completion
from agent_host.provider_identity import (
    PARENT_CONTEXT_DELIVERED_EVENT,
    PARENT_CONTEXT_DELIVERY_METADATA_KEY,
    SOURCE_CONTEXT_SCOPE_METADATA_KEY,
    SOURCE_UTTERANCE_ID_METADATA_KEY,
    project_parent_context_delivery,
)
from agent_host.provider_types import (
    ACTIVITY_EVIDENCE_METADATA_KEY,
    COOPERATIVE_CONTEXT_ACCEPTED_METADATA_KEY,
    ProviderAdapter,
    ProviderActivityEvidence,
    ProviderEvent,
    ProviderInputDelivery,
    ProviderPermissionResponse,
    PreparedProviderRun,
    ProviderRunRequest,
    ProviderRunResult,
    ProviderRecoveryContext,
    ProviderRunIntakeAuthority,
    ProviderRunIntakeReceipt,
    ProviderSessionHandle,
    ProviderSteerRequest,
    ProviderStatus,
    ProviderSubmissionReconciliationRequest,
    ProviderSubmissionReconciliationResult,
    ProviderTerminalResultProjection,
)
from server.event_bus import bus
from server.protocol import Method

logger = logging.getLogger(__name__)


class ProviderStartAdmissionRejected(RuntimeError):
    """The Host's frozen routing scope no longer permits adapter execution."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "provider_start_admission_rejected")
        super().__init__(self.reason)

_CONTROL_PLANE_METADATA_KEYS = frozenset(
    {
        "work",
        "provider_manifest",
        "provider_operation",
        "provider_ownership",
        "provider_requirements",
        "provider_selection",
        "provider_session",
        "cooperative_context_id",
        "host_outcome_requirement",
        "session_id",
        "sessionId",
        "chat_session_id",
        "turn_id",
        SOURCE_UTTERANCE_ID_METADATA_KEY,
        "source_user_text",
        "source_user_operation_text",
        "source_user_context",
        "payload_continuity",
        "workspace_binding",
        "interaction_branch_routing_scope",
        "interaction_branch_admission_id",
        "interaction_branch_id",
        "branch_id",
        "branch_instruction_revision",
        "branch_intent",
        "continuation",
        "replaces_attempt_id",
        "steer_replacement",
        "cancellation",
        "provider_completion",
        "provider_terminal_pipeline",
        "provider_recovery",
        "runtime_resumable",
        PARENT_CONTEXT_DELIVERY_METADATA_KEY,
        SOURCE_CONTEXT_SCOPE_METADATA_KEY,
        SOURCE_UTTERANCE_ID_METADATA_KEY,
    }
)

_INTERNAL_CONTEXT_METADATA_KEYS = frozenset(
    {
        PARENT_CONTEXT_DELIVERY_METADATA_KEY,
        SOURCE_CONTEXT_SCOPE_METADATA_KEY,
        "source_context_cursor_turn_id",
        "source_context_base_turn_id",
        "interaction_branch_routing_scope",
        "interaction_branch_admission_id",
    }
)

# Accepted-effect authority crosses Runtime beside the Provider request. These
# exact names are reserved at the Host-owned request/work boundary so a custom
# intake preparer cannot accidentally serialize Control authority to an
# adapter. Assignment context remains adapter-visible execution evidence.
_HOST_AUTHORITY_METADATA_KEYS = frozenset(
    {
        COOPERATIVE_CONTEXT_ACCEPTED_METADATA_KEY,
        "effect_id",
        "origin_effect_id",
        "claim_token",
        "intake_authority",
        "intake_receipt",
        "source_proof",
    }
)


def _detach_provider_request(request: ProviderRunRequest) -> ProviderRunRequest:
    """Give Runtime an owned request graph before any durable or async boundary."""

    if not isinstance(request, ProviderRunRequest):
        raise TypeError("provider start requires ProviderRunRequest")
    try:
        return ProviderRunRequest(
            provider=copy.deepcopy(request.provider),
            task=copy.deepcopy(request.task),
            cwd=copy.deepcopy(request.cwd),
            mode=copy.deepcopy(request.mode),
            metadata=copy.deepcopy(
                request.metadata if isinstance(request.metadata, dict) else {}
            ),
            requirements=copy.deepcopy(request.requirements),
            ownership=copy.deepcopy(request.ownership),
            session=copy.deepcopy(request.session),
            recovery=copy.deepcopy(request.recovery),
        )
    except Exception as exc:
        raise TypeError("provider request must be safely detachable") from exc


def _public_provider_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Remove private delivery-cursor authority from public run surfaces."""

    source = metadata if isinstance(metadata, dict) else {}
    return {
        key: value
        for key, value in source.items()
        if key not in _INTERNAL_CONTEXT_METADATA_KEYS
        and key not in _HOST_AUTHORITY_METADATA_KEYS
    }


def _public_provider_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project only displayable Provider events into list/result payloads."""

    projected: list[dict[str, Any]] = []
    for source in events:
        if str(source.get("type") or "").strip().lower() == PARENT_CONTEXT_DELIVERED_EVENT:
            continue
        event = dict(source)
        event["metadata"] = _public_provider_metadata(
            source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
        )
        projected.append(event)
    return projected


def scrub_untrusted_provider_metadata(
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    """Remove fields that only an assembled Host control path may author."""

    source = metadata if isinstance(metadata, dict) else {}
    blocked = {
        *_CONTROL_PLANE_METADATA_KEYS,
        *_HOST_AUTHORITY_METADATA_KEYS,
        OUTCOME_EVIDENCE_METADATA_KEY,
        ACTIVITY_EVIDENCE_METADATA_KEY,
    }
    return {key: value for key, value in source.items() if key not in blocked}


def _scrub_request_authority_metadata(
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep accepted-effect authority out of the Provider request envelope."""

    scrubbed = {
        key: value
        for key, value in dict(metadata or {}).items()
        if key not in _HOST_AUTHORITY_METADATA_KEYS
    }
    work = scrubbed.get("work")
    if isinstance(work, dict):
        scrubbed["work"] = {
            key: value
            for key, value in work.items()
            if key not in _HOST_AUTHORITY_METADATA_KEYS
        }
    return scrubbed


def _identity_from_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    source = metadata if isinstance(metadata, dict) else {}
    work = source.get("work") if isinstance(source.get("work"), dict) else {}
    attempt_epoch_raw = (
        work.get("attempt_epoch")
        or work.get("attemptEpoch")
        or work.get("attempt_number")
        or work.get("attemptNumber")
        or source.get("attempt_epoch")
        or 0
    )
    try:
        attempt_epoch = max(0, int(attempt_epoch_raw))
    except (TypeError, ValueError):
        attempt_epoch = 0
    ownership = str(source.get("provider_ownership") or source.get("ownership") or "managed")
    if ownership not in {"managed", "attached"}:
        ownership = "managed"
    return {
        "task_id": str(
            work.get("work_item_id")
            or work.get("workItemId")
            or source.get("task_id")
            or ""
        ).strip(),
        "attempt_id": str(
            work.get("attempt_id")
            or work.get("attemptId")
            or source.get("attempt_id")
            or ""
        ).strip(),
        "attempt_epoch": attempt_epoch,
        "ownership": ownership,
    }


@dataclass(slots=True)
class ProviderRunRecord:
    run_id: str
    provider: str
    task: str
    cwd: str | None
    status: ProviderStatus
    created_at: float
    updated_at: float
    result: str = ""
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    task_handle: asyncio.Task | None = None
    event_sequence: int = 0
    terminal_publication_task: asyncio.Task[None] | None = field(
        default=None,
        repr=False,
    )

    def to_dict(self) -> dict[str, Any]:
        identity = _identity_from_metadata(self.metadata)
        public_events = _public_provider_events(self.events)
        return {
            "run_id": self.run_id,
            "provider": self.provider,
            "task": self.task,
            "cwd": self.cwd,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
            "error": self.error,
            "metadata": _public_provider_metadata(self.metadata),
            "events": public_events[-200:],
            "task_id": identity["task_id"],
            "attempt_id": identity["attempt_id"],
            "attempt_epoch": identity["attempt_epoch"],
            "ownership": identity["ownership"],
            "event_sequence": self.event_sequence,
        }


class ProviderRuntime:
    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}
        self._manifests: dict[str, ProviderManifest] = {}
        self._runs: dict[str, ProviderRunRecord] = {}
        self._input_locks: dict[str, asyncio.Lock] = {}
        self._cancel_tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._request_preparer: Callable[..., Any] | None = None
        self._native_session_checkpoint: Callable[[str, ProviderSessionHandle], Any] | None = None
        self._start_admission_validator: Callable[
            [ProviderRunRequest, str, str], Any
        ] | None = None
        self._admission_cleanup_tasks: set[asyncio.Task[Any]] = set()
        # The sync intake hook is a check-then-write sequence (active-attempt
        # guard -> create Work/Operation/Attempt -> acquire writer lease).
        # It used to get mutual exclusion for free by running inline on the
        # loop; running it in a worker thread takes that away, so serialize it.
        self._intake_lock = asyncio.Lock()

    def set_request_preparer(self, callback: Callable[..., Any] | None) -> None:
        """Install the provider-neutral control-plane intake hook.

        Every new provider attempt, including runs started outside the
        WebSocket ProviderHandler, crosses this boundary. Resume deliberately
        bypasses it because it continues the same provider attempt.
        The second argument is this Runtime's allocated outer run ID, so a
        durable intake can persist identity in its original write transaction.
        It is not a native submit receipt or a caller-supplied metadata value.
        This callback is trusted Host configuration: accepted-effect intake
        must revalidate the side-band authority against its durable owner.
        """
        self._request_preparer = callback

    def set_native_session_checkpoint(
        self, callback: Callable[[str, ProviderSessionHandle], Any] | None,
    ) -> None:
        """Install the Host's awaited native-address checkpoint.

        An adapter awaits session.opened before submitting native user execution.
        Unlike presentation subscribers, checkpoint failure propagates to it.
        The callback returns normally only after its required persistence succeeds.
        """
        self._native_session_checkpoint = callback

    def set_start_admission_validator(
        self,
        callback: Callable[[ProviderRunRequest, str, str], Any] | None,
    ) -> None:
        """Install the final Host authority check for a newly created run.

        The callback reserves before intake, binds the exact queued run after
        intake, and commits after ``run.created`` immediately before adapter
        scheduling. A post-intake rejection therefore has a terminal run
        receipt that can settle any durable intake records.
        """

        self._start_admission_validator = callback

    async def _validate_start_admission(
        self,
        request: ProviderRunRequest,
        run_id: str,
        phase: str,
    ) -> None:
        validator = self._start_admission_validator
        if validator is None:
            return
        outcome = validator(_detach_provider_request(request), run_id, phase)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        accepted = (
            outcome.get("accepted")
            if isinstance(outcome, dict)
            else outcome
        )
        if accepted is not True:
            reason = (
                str(outcome.get("reason") or "provider_start_admission_rejected")
                if isinstance(outcome, dict)
                else "provider_start_admission_rejected"
            )
            raise ProviderStartAdmissionRejected(reason)

    async def _release_start_admission(
        self,
        request: ProviderRunRequest,
        run_id: str = "",
    ) -> None:
        validator = self._start_admission_validator
        if validator is None:
            return
        release_request = _detach_provider_request(request)

        async def release() -> None:
            try:
                outcome = validator(release_request, run_id, "release")
                if inspect.isawaitable(outcome):
                    await outcome
            except BaseException:
                logger.exception("provider start admission release failed run=%s", run_id)

        cleanup = asyncio.create_task(
            release(),
            name=f"provider-admission-release:{run_id or 'prestart'}",
        )
        self._admission_cleanup_tasks.add(cleanup)
        cleanup.add_done_callback(self._admission_cleanup_tasks.discard)
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # The retained task owns eventual release even if this caller is
            # cancelled again while unwinding another cancellation.
            return

    async def reserve_start_admission(self, request: ProviderRunRequest) -> None:
        """Reserve the request's routing scope before any dispatch side effect."""

        await self._validate_start_admission(request, "", "reserve")

    async def release_start_admission(self, request: ProviderRunRequest) -> None:
        """Release a reservation when dispatch completes without Runtime commit."""

        await self._release_start_admission(request)

    def register(self, adapter: ProviderAdapter) -> None:
        provider_id = str(adapter.provider_id or "").strip().lower()
        manifest = manifest_for_adapter(adapter)
        if (
            manifest.capabilities.submission_reconciliation == "query"
            and not callable(getattr(adapter, "reconcile_submission", None))
        ):
            raise ValueError(
                f"provider {provider_id} advertises submission reconciliation "
                "without reconcile_submission"
            )
        self._adapters[provider_id] = adapter
        self._manifests[provider_id] = manifest
        logger.info(
            "registered provider adapter: %s (contract=%s declared=%s)",
            provider_id,
            manifest.contract_version,
            manifest.declared,
        )

    def list_providers(self) -> list[str]:
        return sorted(self._adapters.keys())

    def list_provider_manifests(self) -> list[dict[str, Any]]:
        return [self._manifests[key].to_dict() for key in sorted(self._manifests)]

    def provider_manifests(self) -> tuple[ProviderManifest, ...]:
        return tuple(self._manifests[key] for key in sorted(self._manifests))

    def get_manifest(self, provider: str) -> ProviderManifest | None:
        return self._manifests.get(str(provider or "").strip().lower())

    def list_runs(self) -> list[dict[str, Any]]:
        return [
            record.to_dict()
            for record in sorted(
                self._runs.values(),
                key=lambda run: run.created_at,
                reverse=True,
            )
        ]

    def get_run(self, run_id: str) -> ProviderRunRecord | None:
        return self._runs.get(run_id)

    def get_adapter(self, provider: str) -> ProviderAdapter | None:
        return self._adapters.get(provider)

    async def inspect_orphaned_submission(
        self,
        run_id: str,
    ) -> ProviderSubmissionReconciliationResult:
        """Query one unresolved Provider run without changing Host state.

        This is the observe-only contract slice. It deliberately emits no
        Provider event, changes no Runtime record, grants no Resume, and does
        not enter Work completion. A later authority decision may consume the
        typed observation through the ordinary lifecycle pipeline.
        """

        clean_run_id = str(run_id or "").strip()
        record = self._runs.get(clean_run_id)
        if record is None:
            raise ValueError(f"unknown provider run: {clean_run_id}")
        if record.status != "orphaned":
            return ProviderSubmissionReconciliationResult(
                state="unavailable",
                reason="provider_run_is_not_orphaned",
            )
        session: ProviderSessionHandle | None = None
        raw_session = record.metadata.get("provider_session")
        if raw_session is not None:
            try:
                session = ProviderSessionHandle.from_dict(raw_session)
            except (TypeError, ValueError) as exc:
                return ProviderSubmissionReconciliationResult(
                    state="unavailable",
                    reason=f"invalid_provider_session:{type(exc).__name__}",
                )
        request = ProviderSubmissionReconciliationRequest(
            provider=record.provider,
            run_id=record.run_id,
            session=session,
        )
        return await self.inspect_submission(request)

    async def inspect_submission(
        self,
        request: ProviderSubmissionReconciliationRequest,
    ) -> ProviderSubmissionReconciliationResult:
        """Invoke one Provider-neutral, observe-only submission query.

        The caller owns proof that the Host submission is unresolved. This
        lower-level entry point exists so durable Work recovery can query an
        orphan after process restart without fabricating an in-memory Runtime
        record. Native lookup identities and query mechanics stay inside the
        adapter. This method emits no event and mutates no Runtime state.
        """

        if not isinstance(request, ProviderSubmissionReconciliationRequest):
            raise TypeError("provider reconciliation request must use the typed contract")
        manifest = self._manifests.get(request.provider)
        adapter = self._adapters.get(request.provider)
        if (
            manifest is None
            or adapter is None
            or manifest.capabilities.submission_reconciliation != "query"
        ):
            return ProviderSubmissionReconciliationResult(
                state="unavailable",
                reason="provider_does_not_support_submission_reconciliation",
            )
        reconcile = getattr(adapter, "reconcile_submission", None)
        if not callable(reconcile):
            raise RuntimeError(
                f"provider {request.provider} reconciliation contract is unavailable"
            )
        try:
            outcome = reconcile(request)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        except Exception as exc:
            logger.exception("provider submission query failed: %s", request.run_id)
            return ProviderSubmissionReconciliationResult(
                state="unavailable",
                reason=f"provider_query_failed:{type(exc).__name__}",
            )
        if not isinstance(outcome, ProviderSubmissionReconciliationResult):
            raise TypeError(
                "provider reconcile_submission must return "
                "ProviderSubmissionReconciliationResult"
            )
        if outcome.execution is not None and outcome.execution.provider != request.provider:
            raise ValueError("reconciled native execution belongs to another provider")
        terminal = outcome.terminal_result
        if terminal is not None:
            self._validate_provider_result_contract(
                terminal,
                request.provider,
                request.session,
            )
        return outcome

    def project_reconciled_terminal_result(
        self,
        request: ProviderRunRequest,
        run_id: str,
        result: ProviderRunResult,
    ) -> ProviderTerminalResultProjection:
        """Project one typed terminal observation through the live Runtime rules.

        This is a pure, Provider-neutral projection. It neither looks up a
        native execution nor mutates/publishes a Runtime record. The caller
        must separately prove durable orphan identity before Work accepts the
        returned payload.
        """

        if not isinstance(request, ProviderRunRequest):
            raise TypeError("provider run request must use the typed contract")
        if not isinstance(result, ProviderRunResult):
            raise TypeError("provider result must use the typed contract")
        clean_run_id = str(run_id or "").strip()
        clean_provider = str(request.provider or "").strip().lower()
        if not clean_run_id or not clean_provider:
            raise ValueError("reconciled Provider projection identity is required")
        if result.status not in {"done", "error", "cancelled"}:
            raise ValueError("reconciled Provider projection requires a terminal result")
        now = time.time()
        record = ProviderRunRecord(
            run_id=clean_run_id,
            provider=clean_provider,
            task=str(request.task or ""),
            cwd=request.cwd,
            status="orphaned",
            created_at=now,
            updated_at=now,
            metadata=dict(request.metadata or {}),
        )
        self._apply_provider_result(record, request, result)
        if record.status not in {"done", "error", "cancelled"}:
            raise ValueError("reconciled Provider projection did not remain terminal")
        record.updated_at = time.time()
        record.metadata["liveness"] = {
            "state": "terminal",
            "observed_at": record.updated_at,
        }
        identity = _identity_from_metadata(record.metadata)
        return ProviderTerminalResultProjection(
            provider=record.provider,
            run_id=record.run_id,
            work_item_id=identity["task_id"],
            attempt_id=identity["attempt_id"],
            status=record.status,
            result=record.result,
            error=record.error,
            metadata=_public_provider_metadata(record.metadata),
        )

    async def close(self) -> None:
        """Release Provider-owned runtime resources during Host shutdown.

        Provider execution lifecycles stay behind their adapters.  The Host
        only invokes an optional close hook; it does not know whether that
        releases a subprocess, socket, browser, or remote client.  Active-run
        cancellation remains an explicit control operation and is therefore
        deliberately not fabricated here.
        """

        if self._admission_cleanup_tasks:
            await asyncio.gather(
                *tuple(self._admission_cleanup_tasks),
                return_exceptions=True,
            )
        retained_control_tasks = {
            task
            for task in (
                *tuple(self._cancel_tasks.values()),
                *tuple(
                    record.terminal_publication_task
                    for record in self._runs.values()
                    if record.terminal_publication_task is not None
                ),
            )
            if not task.done()
        }
        for task in retained_control_tasks:
            try:
                await self._wait_for_retained_task(task)
            except BaseException:
                logger.exception("provider retained control transaction failed during close")
        for provider_id, adapter in tuple(self._adapters.items()):
            close = getattr(adapter, "close", None)
            if not callable(close):
                continue
            try:
                outcome = close()
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                logger.exception("provider adapter close failed: %s", provider_id)

    def add_orphaned_run(self, *, provider: str, run: dict[str, Any]) -> None:
        """Restore one adapter-owned resumable checkpoint without interpreting it."""

        run_id = str(run.get("run_id") or "").strip()
        if not run_id or run_id in self._runs:
            return
        updated_at_raw = run.get("updated_at")
        updated_at = (
            float(updated_at_raw)
            if isinstance(updated_at_raw, (int, float))
            else time.time()
        )
        persisted_task = str(run.get("task") or "").strip()
        try:
            event_sequence = max(0, int(run.get("event_sequence") or 0))
        except (TypeError, ValueError, OverflowError):
            event_sequence = 0
        metadata = (
            dict(run.get("metadata"))
            if isinstance(run.get("metadata"), dict)
            else {}
        )
        metadata.update(
            {
                "orphaned": True,
                "resume_task_authoritative": bool(persisted_task),
            }
        )
        self._runs[run_id] = ProviderRunRecord(
            run_id=run_id,
            provider=str(provider or "").strip().lower(),
            task=persisted_task or "Provider run resume available",
            cwd=str(run.get("cwd") or "") or None,
            status="orphaned",
            created_at=updated_at,
            updated_at=updated_at,
            metadata=metadata,
            event_sequence=event_sequence,
        )

    async def start(self, request: ProviderRunRequest) -> ProviderRunRecord:
        """Start an ordinary legacy/current Provider request."""

        return await self._start(request, intake_authority=None)

    async def start_accepted(
        self,
        request: ProviderRunRequest,
        intake_authority: ProviderRunIntakeAuthority,
    ) -> ProviderRunRecord:
        """Start one accepted effect without exposing its authority to an adapter."""

        if not isinstance(intake_authority, ProviderRunIntakeAuthority):
            raise TypeError("accepted Provider start requires typed intake authority")
        return await self._start(request, intake_authority=intake_authority)

    async def _start(
        self,
        request: ProviderRunRequest,
        *,
        intake_authority: ProviderRunIntakeAuthority | None,
    ) -> ProviderRunRecord:
        request = _detach_provider_request(request)
        self._apply_request_contract(request)
        adapter = self._adapters.get(request.provider)
        if adapter is None:
            raise ValueError(f"unknown provider: {request.provider}")
        manifest = self._manifests[request.provider]
        if request.ownership not in manifest.ownership_modes:
            raise ValueError(
                f"provider {request.provider} does not support {request.ownership} ownership"
            )
        if (
            request.requirements is not None
            and request.requirements.ownership != request.ownership
        ):
            raise ValueError("request ownership disagrees with provider requirements")
        self._assert_request_compatible(manifest, request)
        self._apply_request_contract(request, manifest=manifest)
        await self._validate_start_admission(request, "", "reserve")
        run_id = f"{request.provider}_{uuid.uuid4().hex[:12]}"
        intake_cancelled = False
        intake_prepared = False
        admission_metadata: dict[str, Any] = {
            key: copy.deepcopy(request.metadata[key])
            for key in (
                "session_id",
                "interaction_branch_routing_scope",
                "interaction_branch_admission_id",
            )
            if key in request.metadata
        }

        async def await_intake(awaitable: Any) -> Any:
            nonlocal intake_cancelled
            task = asyncio.ensure_future(awaitable)
            while True:
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    if task.cancelled():
                        raise
                    # Intake may own a worker thread and durable ledger writes.
                    # Keep the admission reservation and intake lock until that
                    # exact operation returns, then publish a cancelled run
                    # receipt instead of detaching an orphan attempt.
                    intake_cancelled = True

        try:
            preparer = self._request_preparer
            if intake_authority is not None and preparer is None:
                raise ProviderStartAdmissionRejected(
                    "accepted_effect_intake_owner_unavailable"
                )
            if preparer is not None:
                preparer_args = (
                    (request, run_id, intake_authority)
                    if intake_authority is not None
                    else (request, run_id)
                )
                async with self._intake_lock:
                    if inspect.iscoroutinefunction(preparer):
                        prepared = await await_intake(preparer(*preparer_args))
                    else:
                        # A synchronous intake may block on subprocess IO while
                        # the Host provisions a Git worktree. Keep it off the
                        # event loop so chat streaming and TTS do not stall.
                        prepared = await await_intake(
                            asyncio.to_thread(preparer, *preparer_args)
                        )
                        if inspect.isawaitable(prepared):
                            prepared = await await_intake(prepared)
                intake_prepared = True
                if intake_authority is not None:
                    if not isinstance(prepared, PreparedProviderRun):
                        raise ProviderStartAdmissionRejected(
                            "accepted_effect_intake_receipt_missing"
                        )
                    receipt = prepared.intake_receipt
                    prepared_work = (
                        prepared.request.metadata.get("work")
                        if isinstance(prepared.request.metadata, dict)
                        and isinstance(prepared.request.metadata.get("work"), dict)
                        else {}
                    )
                    receipt_has_work = bool(
                        receipt.work_item_id
                        or receipt.operation_id
                        or receipt.attempt_id
                    ) if isinstance(receipt, ProviderRunIntakeReceipt) else False
                    if (
                        not isinstance(receipt, ProviderRunIntakeReceipt)
                        or receipt.effect_id != intake_authority.effect_id
                        or receipt.run_id != run_id
                        or (
                            intake_authority.kind == "control_work_effect"
                            and (
                                not receipt_has_work
                                or receipt.work_item_id
                                != str(prepared_work.get("work_item_id") or "")
                                or receipt.operation_id
                                != str(prepared_work.get("operation_id") or "")
                                or receipt.attempt_id
                                != str(prepared_work.get("attempt_id") or "")
                            )
                        )
                        or (
                            intake_authority.kind == "cooperative_provider_effect"
                            and (receipt_has_work or bool(prepared_work))
                        )
                    ):
                        raise ProviderStartAdmissionRejected(
                            "accepted_effect_intake_receipt_mismatch"
                        )
                    request = _detach_provider_request(prepared.request)
                elif isinstance(prepared, PreparedProviderRun):
                    raise ProviderStartAdmissionRejected(
                        "ordinary_start_cannot_consume_effect_receipt"
                    )
                elif isinstance(prepared, ProviderRunRequest):
                    request = _detach_provider_request(prepared)
                request.metadata = {
                    **dict(request.metadata or {}),
                    **admission_metadata,
                }
                self._apply_request_contract(request, manifest=manifest)
                self._assert_request_compatible(manifest, request)

            workspace_binding = prepare_workspace_binding(request, manifest)
            request.metadata["workspace_binding"] = workspace_binding.to_dict()
            if (
                preparer is not None
                and str(request.metadata.get("cooperative_context_id") or "").strip()
                and "work" not in request.metadata
            ):
                # The configured Host preparer has just revalidated the exact
                # cooperative dispatch slot. Raw caller metadata cannot mint
                # this fact because _apply_request_contract strips it both
                # before and after preparation.
                request.metadata[COOPERATIVE_CONTEXT_ACCEPTED_METADATA_KEY] = True
        except BaseException as exc:
            await self._release_start_admission(request)
            if (intake_authority is not None and intake_prepared
                    and isinstance(exc, Exception)
                    and not isinstance(exc, ProviderStartAdmissionRejected)):
                raise ProviderStartAdmissionRejected(
                    "accepted_effect_pre_execution_failed:" + type(exc).__name__
                ) from exc
            raise

        display_task = str(request.metadata.get("display_task") or request.task)
        now = time.time()
        record = ProviderRunRecord(
            run_id=run_id,
            provider=request.provider,
            task=display_task,
            cwd=request.cwd,
            status="queued",
            created_at=now,
            updated_at=now,
            metadata=copy.deepcopy(request.metadata),
        )
        self._runs[run_id] = record

        try:
            from server.turn_decision_shadow import (
                get_enabled_turn_decision_shadow_observer,
            )

            shadow = get_enabled_turn_decision_shadow_observer()
            if shadow is not None:
                work = (
                    record.metadata.get("work")
                    if isinstance(record.metadata.get("work"), dict)
                    else {}
                )
                shadow.record_event(
                    str(record.metadata.get("turn_id") or ""),
                    stage="provider_run_created",
                    origin_kind="provider_runtime",
                    origin_id=run_id,
                    payload={
                        "provider": request.provider,
                        "run_id": run_id,
                        "work_item_id": str(work.get("work_item_id") or ""),
                        "operation_id": str(work.get("operation_id") or ""),
                        "attempt_id": str(work.get("attempt_id") or ""),
                    },
                )
        except Exception:
            logger.debug("turn decision Provider lineage observation failed", exc_info=True)

        try:
            await self._validate_start_admission(request, run_id, "created")
            await self._emit(
                record,
                ProviderEvent(
                    provider=request.provider,
                    run_id=run_id,
                    type="run.created",
                    payload={
                        "task": display_task,
                        "cwd": request.cwd,
                        "mode": request.mode,
                    },
                    metadata=dict(request.metadata),
                ),
            )
            if intake_cancelled:
                await self.cancel(
                    run_id,
                    reason="start_cancelled_after_intake",
                    metadata={
                        "source": "provider_runtime",
                        "before_execution": True,
                    },
                )
                raise asyncio.CancelledError
            await self._validate_start_admission(request, run_id, "commit")

            if record.status == "cancelled":
                # A Host retraction may arrive while run.created subscribers are
                # still observing this queued record. Queued cancellation is local
                # authority, so the adapter must never be scheduled afterwards.
                return record

            record.task_handle = asyncio.create_task(
                self._run_adapter(adapter, request, record),
                name=f"provider:{run_id}",
            )
            return record
        except BaseException:
            # There is an intentional await between publishing run.created and
            # installing the adapter task. Chat turn supersession can cancel the
            # caller in that window. Convert the orphaned queued record into an
            # exact terminal fact before propagating the original interruption.
            if record.task_handle is None:
                cleanup: asyncio.Task[Any] | None = self._cancel_tasks.get(run_id)
                if cleanup is None and record.status == "queued":
                    cleanup = asyncio.create_task(
                        self.cancel(
                            run_id,
                            reason="start_interrupted_before_execution",
                            metadata={
                                "source": "provider_runtime",
                                "before_execution": True,
                            },
                        ),
                        name=f"provider-start-cleanup:{run_id}",
                    )
                if cleanup is not None:
                    try:
                        await self._wait_for_retained_task(cleanup)
                    except BaseException:
                        logger.exception(
                            "provider queued-start cleanup failed run=%s",
                            run_id,
                        )
                publication = record.terminal_publication_task
                if publication is not None and not publication.done():
                    try:
                        await self._wait_for_retained_task(publication)
                    except BaseException:
                        logger.exception(
                            "provider queued-start terminal publication failed run=%s",
                            run_id,
                        )
            await self._release_start_admission(request, run_id)
            raise

    async def resume(self, run_id: str, request: ProviderRunRequest) -> ProviderRunRecord:
        self._apply_request_contract(request)
        record = self._runs.get(run_id)
        if record is None:
            raise ValueError(f"unknown provider run: {run_id}")
        adapter = self._adapters.get(record.provider)
        if adapter is None:
            raise ValueError(f"unknown provider: {record.provider}")
        manifest = self._manifests[record.provider]
        if request.ownership not in manifest.ownership_modes:
            raise ValueError(
                f"provider {record.provider} does not support {request.ownership} ownership"
            )
        if (
            request.requirements is not None
            and request.requirements.ownership != request.ownership
        ):
            raise ValueError("request ownership disagrees with provider requirements")
        self._assert_request_compatible(manifest, request)
        self._apply_request_contract(request, manifest=manifest)
        if record.status != "orphaned":
            raise ValueError(f"provider run is not orphaned/resumable: {run_id}")
        if record.metadata.get("runtime_resumable") is not True:
            raise ValueError(
                f"provider run has no Host-verified resumable checkpoint: {run_id}"
            )
        cancellation = (
            record.metadata.get("cancellation")
            if isinstance(record.metadata.get("cancellation"), dict)
            else {}
        )
        liveness = (
            record.metadata.get("liveness")
            if isinstance(record.metadata.get("liveness"), dict)
            else {}
        )
        cancel_task = self._cancel_tasks.get(run_id)
        if (
            (cancel_task is not None and not cancel_task.done())
            or cancellation.get("in_flight") is True
            or str(liveness.get("state") or "").strip().lower() == "cancel_pending"
        ):
            raise ValueError(
                f"provider run cancellation requires reconciliation before Resume: {run_id}"
            )
        if record.task_handle is not None and not record.task_handle.done():
            raise ValueError(f"provider run already has an active task: {run_id}")
        if request.provider and request.provider != record.provider:
            raise ValueError("Resume cannot change provider")
        task_authoritative = record.metadata.get("resume_task_authoritative") is True
        if task_authoritative and request.task and request.task != record.task:
            raise ValueError("Resume cannot change the original task")
        if record.cwd and request.cwd and not self._same_workspace(record.cwd, request.cwd):
            raise ValueError("Resume cannot change the original workspace")
        if not task_authoritative and request.task:
            # A host-restored record may lack task text. Only the Work Ledger
            # resume path can reach this API, so bind its durable attempt task
            # once instead of treating a placeholder as authority.
            record.task = request.task
            record.metadata["resume_task_authoritative"] = True
        request.task = record.task
        request.cwd = record.cwd or request.cwd
        if record.cwd is None and request.cwd:
            record.cwd = request.cwd
        record.metadata.update(dict(request.metadata))
        record.metadata["resume_task_authoritative"] = True
        workspace_binding = prepare_workspace_binding(request, manifest)
        request.metadata["workspace_binding"] = workspace_binding.to_dict()
        record.metadata["workspace_binding"] = workspace_binding.to_dict()
        record.status = "queued"
        record.updated_at = time.time()
        try:
            await self._emit(
                record,
                ProviderEvent(
                    provider=record.provider,
                    run_id=record.run_id,
                    type="run.status",
                    payload={"status": "queued", "resumed": True},
                    metadata=dict(record.metadata),
                ),
            )
        except Exception:
            record.status = "orphaned"
            record.updated_at = time.time()
            raise
        record.task_handle = asyncio.create_task(
            self._run_adapter(adapter, request, record),
            name=f"provider:{run_id}",
        )
        return record

    @staticmethod
    def _same_workspace(left: str, right: str) -> bool:
        try:
            return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
                os.path.realpath(right)
            )
        except (OSError, TypeError, ValueError):
            return str(left) == str(right)

    async def cancel(
        self,
        run_id: str,
        *,
        reason: str = "user_cancelled",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = self._runs.get(run_id)
        if record is None:
            return {"cancelled": False, "reason": "not_found"}

        existing = self._cancel_tasks.get(run_id)
        if existing is not None:
            if existing.done():
                return existing.result()
            return {
                "cancelled": False,
                "reason": "cancel_pending",
                "run": record.to_dict(),
            }

        if record.status in ("done", "error", "cancelled"):
            return {"cancelled": False, "reason": "already_finished", "run": record.to_dict()}

        clean_reason = str(reason or "user_cancelled").strip() or "user_cancelled"
        transaction = asyncio.create_task(
            self._cancel_record(
                record,
                reason=clean_reason,
                metadata=dict(metadata or {}),
            ),
            name=f"provider-cancel-transaction:{run_id}",
        )
        self._cancel_tasks[run_id] = transaction

        def finish_cancel_transaction(
            done: asyncio.Task[dict[str, Any]],
        ) -> None:
            self._finish_cancel_transaction(run_id, done)

        transaction.add_done_callback(finish_cancel_transaction)
        return await asyncio.shield(transaction)

    def _finish_cancel_transaction(
        self,
        run_id: str,
        task: asyncio.Task[dict[str, Any]],
    ) -> None:
        if self._cancel_tasks.get(run_id) is task:
            self._cancel_tasks.pop(run_id, None)
        try:
            error = task.exception()
        except asyncio.CancelledError:
            logger.error("provider cancel transaction was cancelled run=%s", run_id)
            return
        if error is not None:
            logger.error(
                "provider cancel transaction failed run=%s: %s",
                run_id,
                error,
            )

    @staticmethod
    async def _wait_for_retained_task(task: asyncio.Task[Any]) -> Any:
        """Wait through repeated caller cancellation for a retained Host commit."""

        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        return task.result()

    async def _cancel_record(
        self,
        record: ProviderRunRecord,
        *,
        reason: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = record.run_id
        if record.status in ("done", "error", "cancelled"):
            return {
                "cancelled": False,
                "reason": "already_finished",
                "run": record.to_dict(),
            }

        if record.status == "queued":
            record.metadata["cancellation"] = {
                **metadata,
                "reason": reason,
                "requested_at": time.time(),
                "in_flight": False,
            }
            record.metadata["liveness"] = {
                "state": "terminal",
                "reason": reason,
                "observed_at": time.time(),
            }
            if record.task_handle is not None and not record.task_handle.done():
                record.task_handle.cancel()
            record.status = "cancelled"
            record.updated_at = time.time()
            await self._publish_terminal_once(
                record,
                ProviderEvent(
                    provider=record.provider,
                    run_id=record.run_id,
                    type="run.cancelled",
                    payload={"reason": reason, "before_execution": True},
                ),
            )
            return {"cancelled": True, "run": record.to_dict()}

        liveness = (
            record.metadata.get("liveness")
            if isinstance(record.metadata.get("liveness"), dict)
            else {}
        )
        cancellation = (
            record.metadata.get("cancellation")
            if isinstance(record.metadata.get("cancellation"), dict)
            else {}
        )
        if (
            str(liveness.get("state") or "").strip().lower() == "cancel_pending"
            and cancellation.get("in_flight") is True
        ):
            return {
                "cancelled": False,
                "reason": "cancel_pending",
                "run": record.to_dict(),
            }

        record.metadata["cancellation"] = {
            **metadata,
            "reason": reason,
            "requested_at": time.time(),
            "in_flight": True,
        }
        record.metadata["liveness"] = {
            "state": "cancel_pending",
            "reason": reason,
            "observed_at": time.time(),
        }

        adapter = self._adapters.get(record.provider)
        cancel_outcome: dict[str, Any] | None = None
        cancel_task: asyncio.Task[Any] | None = None
        if adapter is not None:
            # Start the native interrupt before publishing status. Provider
            # event consumers may be slow, but UI projection latency must not
            # give the provider extra execution time after a user cancellation.
            cancel_task = asyncio.create_task(
                adapter.cancel(run_id),
                name=f"provider-cancel:{run_id}",
            )
        await self._emit(
            record,
            ProviderEvent(
                provider=record.provider,
                run_id=record.run_id,
                type="run.status",
                payload={
                    # Cancellation is a liveness transition, not evidence that
                    # an already-unknown native outcome has become running
                    # again. Preserve the execution fact while the interrupt
                    # itself remains unconfirmed.
                    "status": record.status,
                    "liveness": "cancel_pending",
                    "reason": reason,
                    "observed_at": time.time(),
                },
            ),
        )
        if cancel_task is not None:
            try:
                raw_outcome = await cancel_task
                if isinstance(raw_outcome, dict):
                    cancel_outcome = raw_outcome
            except Exception as exc:
                logger.exception("provider cancel failed: %s", run_id)
                cancel_outcome = {
                    "confirmed": False,
                    "cancelled": False,
                    "reason": str(exc) or exc.__class__.__name__,
                }

        if record.status in ("done", "error", "cancelled"):
            record.metadata["cancellation"]["in_flight"] = False
            if (record.status == "cancelled" and cancel_outcome is not None
                    and cancel_outcome.get("confirmed") is True
                    and cancel_outcome.get("cancelled") is True):
                return {
                    "cancelled": True,
                    "reason": str(cancel_outcome.get("reason") or "cancelled"),
                    "run": record.to_dict(),
                }
            return {
                "cancelled": False,
                "reason": "terminal_while_cancelling",
                "run": record.to_dict(),
            }

        if cancel_outcome is not None and cancel_outcome.get("confirmed") is not True:
            reason = str(cancel_outcome.get("reason") or "provider did not confirm cancellation")
            record.metadata["cancellation"]["in_flight"] = False
            record.metadata["liveness"] = {
                "state": "cancel_pending",
                "reason": reason,
                "observed_at": time.time(),
            }
            return {
                "cancelled": False,
                "reason": "cancel_unconfirmed",
                "run": record.to_dict(),
            }

        if cancel_outcome is not None and cancel_outcome.get("cancelled") is not True:
            record.metadata["cancellation"]["in_flight"] = False
            return {
                "cancelled": False,
                "reason": str(cancel_outcome.get("reason") or "provider_not_cancelled"),
                "run": record.to_dict(),
            }

        # A confirmed native stop can win before run() returns its typed
        # context. Retain that same typed fact before cancelling the producer;
        # never infer a session from native ids or untrusted metadata.
        if cancel_outcome is not None and cancel_outcome.get("session") is not None:
            try:
                attached = record.metadata.get("provider_session")
                session = cancel_outcome["session"]
                self._validate_provider_result_contract(
                    ProviderRunResult(status="cancelled", session=session), record.provider,
                    ProviderSessionHandle.from_dict(attached) if attached is not None else None,
                )
                record.metadata["provider_session"] = session.to_dict()
            except (TypeError, ValueError) as exc:
                logger.warning("invalid cancellation context for %s: %s", run_id, exc)
        if record.task_handle and not record.task_handle.done():
            record.task_handle.cancel()

        record.metadata["cancellation"]["in_flight"] = False
        record.status = "cancelled"
        record.updated_at = time.time()
        record.metadata["liveness"] = {
            "state": "terminal",
            "reason": reason,
            "observed_at": record.updated_at,
        }
        await self._publish_terminal_once(
            record,
            ProviderEvent(
                provider=record.provider,
                run_id=record.run_id,
                type="run.cancelled",
                payload={"reason": reason},
            ),
        )
        return {"cancelled": True, "run": record.to_dict()}

    async def resolve_permission(
        self,
        run_id: str,
        response: ProviderPermissionResponse,
    ) -> dict[str, Any]:
        """Deliver one validated Host decision to a bidirectional Provider.

        Runtime owns active-run and capability checks plus the canonical audit
        event. The adapter owns translation to its native callback protocol.
        """

        record = self._runs.get(str(run_id or "").strip())
        if record is None:
            return {"accepted": False, "reason": "not_found"}
        if record.status not in {"queued", "running"}:
            return {
                "accepted": False,
                "reason": "already_finished",
                "run": record.to_dict(),
            }
        manifest = self._manifests.get(record.provider)
        if manifest is None or manifest.capabilities.interaction != "bidirectional":
            return {
                "accepted": False,
                "reason": "bidirectional_interaction_not_supported",
                "run": record.to_dict(),
            }
        adapter = self._adapters.get(record.provider)
        resolve = getattr(adapter, "resolve_permission", None) if adapter is not None else None
        if not callable(resolve):
            return {
                "accepted": False,
                "reason": "adapter_permission_resolution_unavailable",
                "run": record.to_dict(),
            }
        raw_outcome = resolve(record.run_id, response)
        outcome = await raw_outcome if inspect.isawaitable(raw_outcome) else raw_outcome
        outcome = dict(outcome) if isinstance(outcome, dict) else {}
        if outcome.get("accepted") is not True:
            return {
                "accepted": False,
                "reason": str(outcome.get("reason") or "adapter_rejected_permission_response"),
                "run": record.to_dict(),
            }
        await self._emit(
            record,
            ProviderEvent(
                provider=record.provider,
                run_id=record.run_id,
                type="permission.allowed" if response.allow else "permission.denied",
                payload={
                    "request_id": response.request_id,
                    "decision": "allow_once" if response.allow else "deny",
                    "automatic": response.automatic,
                    **({"reason":response.reason} if response.reason else {}),
                },
            ),
        )
        return {"accepted": True, "run": record.to_dict()}

    async def append_input(self, run_id: str, text: str) -> ProviderInputDelivery:
        """Deliver one Host-accepted input without replacing earlier inputs.

        This internal transport does not accept a user goal or bind a new
        workspace. The Host caller must establish those facts before calling,
        and retain an unknown disposition if its await is cancelled. There is
        no retry, resume, replacement-run or latest-wins fallback here.
        """

        clean_run_id = str(run_id or "").strip()
        if not isinstance(text, str) or not text.strip():
            return ProviderInputDelivery("rejected", "input_text_required")
        if clean_run_id not in self._runs:
            return ProviderInputDelivery("rejected", "not_found")
        lock = self._input_locks.setdefault(clean_run_id, asyncio.Lock())
        async with lock:
            record = self._runs[clean_run_id]
            if record.status != "running":
                return ProviderInputDelivery("rejected", "run_not_active")
            cancel = self._cancel_tasks.get(clean_run_id)
            if cancel is not None and not cancel.done():
                return ProviderInputDelivery("rejected", "run_stopping")
            manifest = self._manifests.get(record.provider)
            if manifest is None or not manifest.capabilities.append_input:
                return ProviderInputDelivery("rejected", "append_input_not_supported")
            adapter = self._adapters.get(record.provider)
            append = getattr(adapter, "append_input", None)
            if not callable(append):
                return ProviderInputDelivery("rejected", "adapter_append_input_unavailable")
            try:
                raw = append(clean_run_id, text)
                result = await raw if inspect.isawaitable(raw) else raw
            except Exception as exc:
                return ProviderInputDelivery("unknown", str(exc) or type(exc).__name__)
            if not isinstance(result, ProviderInputDelivery):
                return ProviderInputDelivery("unknown", "invalid_input_delivery_result")
            return result

    async def steer(
        self,
        run_id: str,
        request: ProviderSteerRequest,
    ) -> dict[str, Any]:
        """Serialize and queue a latest-wins instruction for an active run."""

        clean_run_id = str(run_id or "").strip()
        lock = self._input_locks.setdefault(clean_run_id, asyncio.Lock())
        async with lock:
            return await self._steer_locked(clean_run_id, request)

    async def _steer_locked(
        self,
        run_id: str,
        request: ProviderSteerRequest,
    ) -> dict[str, Any]:
        """Queue a latest-wins instruction for an active immediate-steer run.

        The adapter decides its safe boundary.  Runtime owns capability
        enforcement and the canonical audit envelope, while the original run
        and WorkItem identity remain unchanged.
        """

        record = self._runs.get(run_id)
        if record is None:
            return {"accepted": False, "reason": "not_found"}
        if record.status not in {"queued", "running"}:
            return {
                "accepted": False,
                "reason": "already_finished",
                "run": record.to_dict(),
            }
        manifest = self._manifests.get(record.provider)
        if manifest is None or manifest.capabilities.steering != "immediate":
            return {
                "accepted": False,
                "reason": "immediate_steering_not_supported",
                "run": record.to_dict(),
            }
        adapter = self._adapters.get(record.provider)
        steer = getattr(adapter, "steer", None) if adapter is not None else None
        if not callable(steer):
            return {
                "accepted": False,
                "reason": "adapter_steering_unavailable",
                "run": record.to_dict(),
            }

        steering = (
            record.metadata.get("steering")
            if isinstance(record.metadata.get("steering"), dict)
            else {}
        )
        current_revision = max(
            0,
            int(steering.get("revision") or 0),
            int(record.metadata.get("branch_instruction_revision") or 0),
        )
        revision = int(request.revision or 0)
        if revision <= current_revision:
            return {
                "accepted": False,
                "reason": "stale_revision",
                "current_revision": current_revision,
                "run": record.to_dict(),
            }
        request.revision = revision
        raw_outcome = steer(record.run_id, request)
        outcome = await raw_outcome if inspect.isawaitable(raw_outcome) else raw_outcome
        outcome = dict(outcome) if isinstance(outcome, dict) else {}
        if outcome.get("accepted") is not True:
            return {
                "accepted": False,
                "reason": str(outcome.get("reason") or "adapter_rejected_steer"),
                "run": record.to_dict(),
            }

        record.metadata["steering"] = {
            "state": "queued",
            "revision": revision,
            "replaces_revision": current_revision,
            "safe_boundary": str(outcome.get("safe_boundary") or "next_atomic_boundary"),
            "requested_at": time.time(),
            "turn_id": str(request.metadata.get("turn_id") or ""),
        }
        record.updated_at = time.time()
        await self._emit(
            record,
            ProviderEvent(
                provider=record.provider,
                run_id=record.run_id,
                type="run.status",
                payload={
                    "status": "running",
                    "stage": "steer_queued",
                    "revision": revision,
                    "replaces_revision": current_revision,
                    "safe_boundary": record.metadata["steering"]["safe_boundary"],
                },
                metadata=dict(record.metadata),
            ),
        )
        return {
            "accepted": True,
            "revision": revision,
            "disposition": "queued_at_safe_boundary",
            "run": record.to_dict(),
        }

    async def _run_adapter(
        self,
        adapter: ProviderAdapter,
        request: ProviderRunRequest,
        record: ProviderRunRecord,
    ) -> None:
        record.status = "running"
        record.updated_at = time.time()
        await self._emit(
            record,
            ProviderEvent(
                provider=record.provider,
                run_id=record.run_id,
                type="run.status",
                payload={"status": "running"},
            ),
        )

        started = time.monotonic()

        async def emit(event: ProviderEvent) -> None:
            event.time_ms = int((time.monotonic() - started) * 1000)
            await self._emit(record, event)

        try:
            self._observe_execution_stage(record, "provider_adapter_started")
            result = await adapter.run(request, record.run_id, emit)
            self._apply_provider_result(record, request, result)
        except asyncio.CancelledError:
            record.status = "cancelled"
            record.error = None
            raise
        except Exception as exc:
            logger.exception("provider run failed: %s", record.run_id)
            record.status = "error"
            record.error = str(exc)
        finally:
            record.updated_at = time.time()
            record.metadata["liveness"] = {
                "state": "orphaned" if record.status == "orphaned" else "terminal",
                "observed_at": record.updated_at,
            }
            terminal_type = {
                "done": "run.finished",
                "error": "run.failed",
                "cancelled": "run.cancelled",
                # Orphaned is deliberately non-terminal: the accepted native
                # run may still finish and must be reconciled rather than
                # described as failed or replayed.
                "orphaned": "run.status",
            }.get(record.status, "run.finished")
            try:
                from server.turn_decision_shadow import (
                    get_enabled_turn_decision_shadow_observer,
                )

                shadow = get_enabled_turn_decision_shadow_observer()
                if shadow is not None:
                    work = (
                        record.metadata.get("work")
                        if isinstance(record.metadata.get("work"), dict)
                        else {}
                    )
                    shadow.record_event(
                        str(record.metadata.get("turn_id") or ""),
                        stage="provider_run_terminal",
                        origin_kind="provider_runtime",
                        origin_id=record.run_id,
                        payload={
                            "provider": record.provider,
                            "run_id": record.run_id,
                            "status": record.status,
                            "work_item_id": str(work.get("work_item_id") or ""),
                            "operation_id": str(work.get("operation_id") or ""),
                            "attempt_id": str(work.get("attempt_id") or ""),
                        },
                    )
            except Exception:
                logger.debug(
                    "turn decision Provider terminal observation failed",
                    exc_info=True,
                )
            terminal_event = ProviderEvent(
                provider=record.provider,
                run_id=record.run_id,
                type=terminal_type,
                payload={
                    "status": record.status,
                    "result": record.result,
                    "error": record.error,
                },
                metadata=record.metadata,
            )
            if record.status == "orphaned":
                await self._emit(record, terminal_event)
                await bus.emit(Method.PROVIDER_RESULT, record.to_dict())
            else:
                await self._publish_terminal_once(record, terminal_event)

    async def _publish_terminal_once(
        self,
        record: ProviderRunRecord,
        event: ProviderEvent,
    ) -> None:
        """Retain one indivisible terminal event/result publication per run."""

        publication = record.terminal_publication_task
        if publication is None:

            async def publish() -> None:
                await self._emit(record, event)
                await bus.emit(Method.PROVIDER_RESULT, record.to_dict())
                self._observe_execution_stage(record, "provider_terminal_published")

            publication = asyncio.create_task(
                publish(),
                name=f"provider-terminal-publication:{record.run_id}",
            )
            record.terminal_publication_task = publication
        await asyncio.shield(publication)

    @staticmethod
    def _observe_execution_stage(record: ProviderRunRecord, stage: str) -> None:
        try:
            from server.turn_decision_shadow import get_enabled_turn_decision_shadow_observer

            observer = get_enabled_turn_decision_shadow_observer()
            if observer is not None:
                observer.record_event(
                    str(record.metadata.get("turn_id") or ""),
                    stage=stage,
                    origin_kind="provider_runtime",
                    origin_id=record.run_id,
                    payload={"provider": record.provider, "status": record.status},
                )
        except Exception:
            logger.debug("provider execution timing observation failed", exc_info=True)

    async def _emit(self, record: ProviderRunRecord, event: ProviderEvent) -> None:
        if event.type == "session.opened" or event.session is not None:
            session = event.session
            if event.type != "session.opened" or not isinstance(session, ProviderSessionHandle):
                raise TypeError("session.opened requires a typed native session")
            if session.provider != record.provider:
                raise ValueError("opened native session belongs to another provider")
            existing = record.metadata.get("provider_session")
            if existing is not None and ProviderSessionHandle.from_dict(existing) != session:
                raise ValueError("opened native session changed the attached context")
            # Retain the observed address even if persistence fails, so local
            # cleanup/result handling can still identify the native resource.
            record.metadata["provider_session"] = session.to_dict()
            checkpoint = self._native_session_checkpoint
            if checkpoint is not None:
                outcome = checkpoint(record.run_id, session)
                if inspect.isawaitable(outcome):
                    await outcome
        event.provider = record.provider
        event.run_id = record.run_id
        event.metadata = dict(event.metadata or {})
        reported_metadata = dict(event.metadata)
        event.metadata.pop("steering", None)
        for key in {*_CONTROL_PLANE_METADATA_KEYS, *_HOST_AUTHORITY_METADATA_KEYS}:
            event.metadata.pop(key, None)
        for key in _CONTROL_PLANE_METADATA_KEYS:
            if key in record.metadata:
                event.metadata[key] = record.metadata[key]
        if isinstance(record.metadata.get("steering"), dict):
            event.metadata["steering"] = dict(record.metadata["steering"])
        if event.type == PARENT_CONTEXT_DELIVERED_EVENT:
            # The receipt is an internal control-plane fact, so it must not
            # create a gap in the public event cursor exposed by to_dict().
            event.sequence = 0
        else:
            record.event_sequence += 1
            event.sequence = record.event_sequence
        event.observed_at = time.time()
        event_identity = _identity_from_metadata(event.metadata)
        record_identity = _identity_from_metadata(record.metadata)
        # The run record was bound by the Amadeus control plane before the
        # adapter started.  Provider metadata may describe native resources,
        # but it cannot redirect an event into a different durable Task.
        event.task_id = record_identity["task_id"] or event.task_id or event_identity["task_id"]
        event.attempt_id = (
            record_identity["attempt_id"] or event.attempt_id or event_identity["attempt_id"]
        )
        event.attempt_epoch = (
            record_identity["attempt_epoch"]
            or event.attempt_epoch
            or event_identity["attempt_epoch"]
        )
        event.ownership = record_identity["ownership"]
        event.replay = bool(
            event.replay
            or event.metadata.get("replay")
        )
        if event.type == PARENT_CONTEXT_DELIVERED_EVENT:
            projection = project_parent_context_delivery(reported_metadata)
            if projection:
                if "source_user_context" not in projection:
                    record.metadata.pop("source_user_context", None)
                record.metadata.update(projection)
                event.metadata.update(projection)
        if event.type == "run.status":
            stage = str(event.payload.get("stage") or "").strip().lower()
            if stage in {"steer_queued", "steer_applied"}:
                previous = (
                    dict(record.metadata.get("steering") or {})
                    if isinstance(record.metadata.get("steering"), dict)
                    else {}
                )
                try:
                    revision = max(0, int(event.payload.get("revision") or 0))
                except (TypeError, ValueError, OverflowError):
                    revision = 0
                try:
                    previous_revision = max(
                        0, int(previous.get("revision") or 0)
                    )
                except (TypeError, ValueError, OverflowError):
                    previous_revision = 0
                previous_state = str(previous.get("state") or "").strip().lower()
                receipt_matches_host = bool(
                    revision > 0
                    and revision == previous_revision
                    and (
                        stage == "steer_queued"
                        and previous_state == "queued"
                        or stage == "steer_applied"
                        and previous_state in {"queued", "applied"}
                    )
                )
                if receipt_matches_host:
                    record.metadata["steering"] = {
                        **previous,
                        "state": "queued" if stage == "steer_queued" else "applied",
                        "revision": revision,
                        "safe_boundary": str(
                            event.payload.get("safe_boundary")
                            or previous.get("safe_boundary")
                            or ""
                        ),
                        "observed_at": time.time(),
                    }
                    event.metadata["steering"] = dict(
                        record.metadata["steering"]
                    )
                else:
                    # Adapter events may acknowledge an exact Host-issued steer;
                    # they cannot mint a queued/applied revision of their own.
                    event.payload = {
                        key: value
                        for key, value in event.payload.items()
                        if key not in {"revision", "safe_boundary"}
                    }
                    event.payload["stage"] = "steer_receipt_rejected"
                    event.payload["reason"] = "unmatched_host_steering_revision"
            liveness = str(event.payload.get("liveness") or "").strip().lower()
            if liveness:
                record.metadata["liveness"] = {
                    "state": liveness,
                    **{
                        key: event.payload[key]
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
                        if key in event.payload
                    },
                }
        if event.type != PARENT_CONTEXT_DELIVERED_EVENT:
            event.metadata = _public_provider_metadata(event.metadata)
        data = event.to_dict()
        if event.type != PARENT_CONTEXT_DELIVERED_EVENT:
            record.events.append(data)
            cap = self._event_cap()
            if cap > 0 and len(record.events) > cap:
                dropped = len(record.events) - cap
                del record.events[:dropped]
                record.metadata["events_dropped"] = int(record.metadata.get("events_dropped") or 0) + dropped
            record.updated_at = time.time()
        await bus.emit(Method.PROVIDER_EVENT, data)

    @staticmethod
    def _apply_request_contract(
        request: ProviderRunRequest,
        *,
        manifest: ProviderManifest | None = None,
    ) -> None:
        request.provider = str(request.provider or "").strip().lower()
        request.metadata = _scrub_request_authority_metadata(request.metadata)
        request.metadata.pop(OUTCOME_EVIDENCE_METADATA_KEY, None)
        request.metadata.pop(ACTIVITY_EVIDENCE_METADATA_KEY, None)
        request.metadata.pop("provider_completion", None)
        request.metadata.pop("provider_recovery", None)
        request.metadata.pop("runtime_resumable", None)
        request.metadata.pop(PARENT_CONTEXT_DELIVERY_METADATA_KEY, None)
        request.metadata.pop("source_context_cursor_turn_id", None)
        # Callers cannot smuggle a native session through generic metadata.
        # The typed request field is populated by the host-owned ledger after
        # it validates WorkItem continuity and Provider capability.
        request.metadata.pop("provider_session", None)
        request.metadata["provider_ownership"] = request.ownership
        if request.recovery is not None:
            if not isinstance(request.recovery, ProviderRecoveryContext):
                raise TypeError("provider recovery must use the typed contract")
            request.metadata["provider_recovery"] = request.recovery.to_dict()
        if request.session is not None:
            if not isinstance(request.session, ProviderSessionHandle):
                raise TypeError("provider session must use the typed contract")
            if request.session.provider != request.provider:
                raise ValueError("provider session does not match the request provider")
            request.metadata["provider_session"] = request.session.to_dict()
        if request.requirements is not None:
            request.metadata["provider_requirements"] = request.requirements.to_dict()
        if manifest is not None:
            request.metadata["provider_manifest"] = manifest.to_dict()
            operation = manifest.capabilities.operation(request.mode)
            if operation is None:
                request.metadata.pop("provider_operation", None)
            else:
                request.metadata["provider_operation"] = operation.operation_id

    @staticmethod
    def _apply_provider_result(
        record: ProviderRunRecord,
        request: ProviderRunRequest,
        result: ProviderRunResult,
    ) -> None:
        """Apply the one generic adapter-result contract to a Runtime record."""

        ProviderRuntime._validate_adapter_result_contract(
            result,
            request,
            record.provider,
        )
        requirements = request.requirements
        if is_progress_only_workspace_completion(
            status=result.status,
            result_text=result.result,
            task_kind=requirements.task_kind if requirements is not None else "",
            workspace_access=(
                requirements.workspace_access if requirements is not None else ""
            ),
            activity_evidence=result.activity_evidence,
        ):
            record.metadata["provider_completion"] = {
                "classification": "progress_only_completion",
                "native_status": result.status,
                "contract_status": "error",
                "recovery_state": "unclaimed",
                "activity_evidence": result.activity_evidence.to_dict(),
            }
            result = ProviderRunResult(
                status="error",
                result="",
                error=(
                    "Provider stopped after reporting progress and before any "
                    "observable execution"
                ),
                metadata=dict(result.metadata),
                outcome_evidence=result.outcome_evidence,
                activity_evidence=result.activity_evidence,
                session=result.session,
            )
        record.status = result.status
        record.result = result.result
        record.error = result.error
        protected_metadata = {
            key: record.metadata[key]
            for key in _CONTROL_PLANE_METADATA_KEYS
            if key in record.metadata
        }
        # Generic/native Provider metadata cannot mint Host control state.
        # Preserve the live contract's explicit object coercion: malformed
        # metadata must fail the result boundary rather than being silently
        # treated as an empty object by the reusable scrub helper.
        adapter_metadata = scrub_untrusted_provider_metadata(dict(result.metadata))
        adapter_steering = (
            dict(adapter_metadata.get("steering"))
            if isinstance(adapter_metadata.get("steering"), dict)
            else {}
        )
        host_steering = (
            dict(record.metadata.get("steering"))
            if isinstance(record.metadata.get("steering"), dict)
            else {}
        )
        if adapter_steering or host_steering:
            for key in (
                "state",
                "revision",
                "run_id",
                "turn_id",
                "safe_boundary",
                "observed_at",
            ):
                adapter_steering.pop(key, None)
            merged_steering = {
                **adapter_steering,
                **host_steering,
            }
            if merged_steering:
                adapter_metadata["steering"] = merged_steering
            else:
                adapter_metadata.pop("steering", None)
        # Native metadata is informational and cannot mint or redirect an
        # attachable Provider Session. Only a typed result/cancellation field
        # may establish one. Providers may rotate an opaque capability after a
        # successful attachment, but cannot change its Provider-owned scope
        # or contract version.
        adapter_metadata.pop("provider_session", None)
        record.metadata.update(adapter_metadata)
        record.metadata.update(protected_metadata)
        if result.status == "orphaned":
            # A live adapter can report uncertainty and a typed native
            # identity, but cannot grant replay authority through metadata.
            record.metadata["runtime_resumable"] = False
        record.metadata.pop(OUTCOME_EVIDENCE_METADATA_KEY, None)
        record.metadata.pop(ACTIVITY_EVIDENCE_METADATA_KEY, None)
        if result.session is not None:
            record.metadata["provider_session"] = result.session.to_dict()
        if result.outcome_evidence is not None:
            record.metadata[OUTCOME_EVIDENCE_METADATA_KEY] = (
                result.outcome_evidence.to_dict()
            )
        if result.activity_evidence is not None:
            record.metadata[ACTIVITY_EVIDENCE_METADATA_KEY] = (
                result.activity_evidence.to_dict()
            )

    @staticmethod
    def _validate_adapter_result_contract(
        result: ProviderRunResult,
        request: ProviderRunRequest,
        provider: str,
    ) -> None:
        ProviderRuntime._validate_provider_result_contract(
            result,
            provider,
            request.session,
        )

    @staticmethod
    def _validate_provider_result_contract(
        result: ProviderRunResult,
        provider: str,
        attached_session: ProviderSessionHandle | None,
    ) -> None:
        if not isinstance(result, ProviderRunResult):
            raise TypeError("provider adapter must return ProviderRunResult")
        if result.session is not None:
            if not isinstance(result.session, ProviderSessionHandle):
                raise TypeError("provider session must use the typed contract")
            if result.session.provider != provider:
                raise ValueError("provider session does not match the provider run")
            if attached_session is not None and (
                result.session.scope != attached_session.scope
                or result.session.version != attached_session.version
            ):
                raise ValueError("provider changed an attached session boundary")
        if result.outcome_evidence is not None:
            if not isinstance(result.outcome_evidence, ProviderOutcomeEvidence):
                raise TypeError("provider outcome_evidence must use the typed contract")
            if result.outcome_evidence.observation_authority != "host":
                raise ValueError(
                    "typed provider outcome_evidence must carry host observation authority"
                )
        if result.activity_evidence is not None:
            if not isinstance(result.activity_evidence, ProviderActivityEvidence):
                raise TypeError("provider activity_evidence must use the typed contract")
            if result.activity_evidence.observation_authority != "host":
                raise ValueError(
                    "typed provider activity_evidence must carry host observation authority"
                )

    @staticmethod
    def _assert_request_compatible(
        manifest: ProviderManifest,
        request: ProviderRunRequest,
    ) -> None:
        requirements = request.requirements
        if requirements is None or requirements.preference_policy == "force":
            return
        errors = compatibility_errors(manifest, requirements)
        if errors:
            raise ValueError(
                f"provider {manifest.provider_id} does not satisfy request: "
                + ", ".join(errors)
            )

    @staticmethod
    def _event_cap() -> int:
        try:
            from config import settings

            return max(0, int(getattr(settings, "PROVIDER_RUN_EVENT_CAP", 500)))
        except Exception:
            return 500


runtime = ProviderRuntime()
