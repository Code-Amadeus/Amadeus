from __future__ import annotations

import asyncio
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
from agent_host.provider_types import (
    ACTIVITY_EVIDENCE_METADATA_KEY,
    ProviderAdapter,
    ProviderActivityEvidence,
    ProviderEvent,
    ProviderPermissionResponse,
    ProviderRunRequest,
    ProviderRunResult,
    ProviderRecoveryContext,
    ProviderSessionHandle,
    ProviderSteerRequest,
    ProviderStatus,
)
from server.event_bus import bus
from server.protocol import Method

logger = logging.getLogger(__name__)

_CONTROL_PLANE_METADATA_KEYS = frozenset(
    {
        "work",
        "provider_manifest",
        "provider_operation",
        "provider_ownership",
        "provider_requirements",
        "provider_selection",
        "provider_session",
        "host_outcome_requirement",
        "session_id",
        "turn_id",
        "workspace_binding",
        "continuation",
        "replaces_attempt_id",
        "steer_replacement",
        "cancellation",
        "provider_completion",
        "provider_recovery",
    }
)


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

    def to_dict(self) -> dict[str, Any]:
        identity = _identity_from_metadata(self.metadata)
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
            "metadata": self.metadata,
            "events": self.events[-200:],
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
        self._steer_locks: dict[str, asyncio.Lock] = {}
        self._request_preparer: Callable[[ProviderRunRequest], Any] | None = None
        # The sync intake hook is a check-then-write sequence (active-attempt
        # guard -> create WorkItem -> create attempt -> acquire writer lease).
        # It used to get mutual exclusion for free by running inline on the
        # loop; running it in a worker thread takes that away, so serialize it.
        self._intake_lock = asyncio.Lock()

    def set_request_preparer(self, callback: Callable[[ProviderRunRequest], Any] | None) -> None:
        """Install the provider-neutral control-plane intake hook.

        Every new provider attempt, including runs started outside the
        WebSocket ProviderHandler, crosses this boundary. Resume deliberately
        bypasses it because it continues the same provider attempt.
        """
        self._request_preparer = callback

    def register(self, adapter: ProviderAdapter) -> None:
        provider_id = str(adapter.provider_id or "").strip().lower()
        manifest = manifest_for_adapter(adapter)
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

    async def close(self) -> None:
        """Release Provider-owned runtime resources during Host shutdown.

        Provider execution lifecycles stay behind their adapters.  The Host
        only invokes an optional close hook; it does not know whether that
        releases a subprocess, socket, browser, or remote client.  Active-run
        cancellation remains an explicit control operation and is therefore
        deliberately not fabricated here.
        """

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
        )

    async def start(self, request: ProviderRunRequest) -> ProviderRunRecord:
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
        preparer = self._request_preparer
        if preparer is not None:
            async with self._intake_lock:
                if inspect.iscoroutinefunction(preparer):
                    prepared = await preparer(request)
                else:
                    # A synchronous intake may block on subprocess IO while
                    # the Host provisions a Git worktree. Keep it off the
                    # event loop so chat streaming and TTS do not stall.
                    prepared = await asyncio.to_thread(preparer, request)
                    if inspect.isawaitable(prepared):
                        prepared = await prepared
            if isinstance(prepared, ProviderRunRequest):
                request = prepared
            self._apply_request_contract(request, manifest=manifest)
            self._assert_request_compatible(manifest, request)

        workspace_binding = prepare_workspace_binding(request, manifest)
        request.metadata["workspace_binding"] = workspace_binding.to_dict()

        display_task = str(request.metadata.get("display_task") or request.task)
        run_id = f"{request.provider}_{uuid.uuid4().hex[:12]}"
        now = time.time()
        record = ProviderRunRecord(
            run_id=run_id,
            provider=request.provider,
            task=display_task,
            cwd=request.cwd,
            status="queued",
            created_at=now,
            updated_at=now,
            metadata=dict(request.metadata),
        )
        self._runs[run_id] = record

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

        if record.status in ("done", "error", "cancelled"):
            return {"cancelled": False, "reason": "already_finished", "run": record.to_dict()}

        if record.status == "queued":
            clean_reason = str(reason or "user_cancelled").strip() or "user_cancelled"
            record.metadata["cancellation"] = {
                **dict(metadata or {}),
                "reason": clean_reason,
                "requested_at": time.time(),
                "in_flight": False,
            }
            record.metadata["liveness"] = {
                "state": "terminal",
                "reason": clean_reason,
                "observed_at": time.time(),
            }
            if record.task_handle is not None and not record.task_handle.done():
                record.task_handle.cancel()
            record.status = "cancelled"
            record.updated_at = time.time()
            await self._emit(
                record,
                ProviderEvent(
                    provider=record.provider,
                    run_id=record.run_id,
                    type="run.cancelled",
                    payload={"reason": clean_reason, "before_execution": True},
                ),
            )
            await bus.emit(Method.PROVIDER_RESULT, record.to_dict())
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

        clean_reason = str(reason or "user_cancelled").strip() or "user_cancelled"
        record.metadata["cancellation"] = {
            **dict(metadata or {}),
            "reason": clean_reason,
            "requested_at": time.time(),
            "in_flight": True,
        }
        record.metadata["liveness"] = {
            "state": "cancel_pending",
            "reason": clean_reason,
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
                    "status": "running",
                    "liveness": "cancel_pending",
                    "reason": clean_reason,
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
            return {
                "cancelled": record.status == "cancelled",
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

        if record.task_handle and not record.task_handle.done():
            record.task_handle.cancel()

        record.metadata["cancellation"]["in_flight"] = False
        record.status = "cancelled"
        record.updated_at = time.time()
        await self._emit(
            record,
            ProviderEvent(
                provider=record.provider,
                run_id=record.run_id,
                type="run.cancelled",
                payload={"reason": clean_reason},
            ),
        )
        await bus.emit(Method.PROVIDER_RESULT, record.to_dict())
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
                    "automatic": False,
                },
            ),
        )
        return {"accepted": True, "run": record.to_dict()}

    async def steer(
        self,
        run_id: str,
        request: ProviderSteerRequest,
    ) -> dict[str, Any]:
        """Serialize and queue a latest-wins instruction for an active run."""

        clean_run_id = str(run_id or "").strip()
        lock = self._steer_locks.setdefault(clean_run_id, asyncio.Lock())
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
        if str(request.metadata.get("source_user_text") or "").strip():
            record.metadata["source_user_text"] = str(
                request.metadata["source_user_text"]
            )[:4000]
        if str(request.metadata.get("source_user_context") or "").strip():
            record.metadata["source_user_context"] = str(
                request.metadata["source_user_context"]
            )[:2000]
        else:
            record.metadata.pop("source_user_context", None)
        record.metadata["source_context_mode"] = str(
            request.metadata.get("source_context_mode") or "none"
        )
        source_turn_id = str(request.metadata.get("turn_id") or "").strip()
        if source_turn_id:
            record.metadata["source_context_cursor_turn_id"] = source_turn_id[:200]
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
            result = await adapter.run(request, record.run_id, emit)
            self._validate_adapter_result_contract(result, request, record.provider)
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
            adapter_metadata = dict(result.metadata)
            # Generic/native provider metadata cannot mint a host observation
            # receipt. Only the typed adapter result contract can cross this
            # boundary; Runtime serializes it into canonical run metadata.
            adapter_metadata.pop(OUTCOME_EVIDENCE_METADATA_KEY, None)
            adapter_metadata.pop(ACTIVITY_EVIDENCE_METADATA_KEY, None)
            adapter_metadata.pop("provider_completion", None)
            adapter_metadata.pop("provider_recovery", None)
            # Native metadata is informational and cannot mint or redirect an
            # attachable Provider Session. Only the typed result field may
            # establish one. Providers may rotate an opaque capability after
            # a successful attachment, but cannot change its provider-owned
            # scope or contract version.
            adapter_metadata.pop("provider_session", None)
            record.metadata.update(adapter_metadata)
            record.metadata.update(protected_metadata)
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
            await self._emit(
                record,
                ProviderEvent(
                    provider=record.provider,
                    run_id=record.run_id,
                    type=terminal_type,
                    payload={
                        "status": record.status,
                        "result": record.result,
                        "error": record.error,
                    },
                    metadata=record.metadata,
                ),
            )
            await bus.emit(Method.PROVIDER_RESULT, record.to_dict())

    async def _emit(self, record: ProviderRunRecord, event: ProviderEvent) -> None:
        event.provider = record.provider
        event.run_id = record.run_id
        event.metadata = dict(event.metadata or {})
        for key in _CONTROL_PLANE_METADATA_KEYS:
            if key in record.metadata:
                event.metadata[key] = record.metadata[key]
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
                except (TypeError, ValueError):
                    revision = 0
                try:
                    previous_revision = max(
                        0, int(previous.get("revision") or 0)
                    )
                except (TypeError, ValueError):
                    previous_revision = 0
                if revision >= previous_revision:
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
        data = event.to_dict()
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
        request.metadata = dict(request.metadata or {})
        request.metadata.pop(OUTCOME_EVIDENCE_METADATA_KEY, None)
        request.metadata.pop(ACTIVITY_EVIDENCE_METADATA_KEY, None)
        request.metadata.pop("provider_completion", None)
        request.metadata.pop("provider_recovery", None)
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
    def _validate_adapter_result_contract(
        result: ProviderRunResult,
        request: ProviderRunRequest,
        provider: str,
    ) -> None:
        if not isinstance(result, ProviderRunResult):
            raise TypeError("provider adapter must return ProviderRunResult")
        if result.session is not None:
            if not isinstance(result.session, ProviderSessionHandle):
                raise TypeError("provider session must use the typed contract")
            if result.session.provider != provider:
                raise ValueError("provider session does not match the provider run")
            if request.session is not None and (
                result.session.scope != request.session.scope
                or result.session.version != request.session.version
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
