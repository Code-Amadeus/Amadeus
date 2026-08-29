"""Canonical Provider event ownership at the Work Ledger boundary.

Provider adapters report runtime facts; they do not own durable Work identity.
This module resolves an event to exactly one Attempt and applies only the
generic lifecycle/activity transitions.  Artifact, permission, export,
completion, narration, and UI services consume the returned ingestion result
without re-interpreting Provider identity or terminal status.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_host.work_ledger_store import (
    WorkLedgerConflict,
    WorkLedgerStore,
)
from agent_host.work_ledger_types import RunAttemptRecord
from server.work_activity_snapshot import (
    ACTIVITY_METADATA_KEY,
    is_material_activity_event,
    project_activity_event,
    project_activity_result,
)
from server.work_completion import normalize_execution_status


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

    def ingest_event(self, params: dict[str, Any]) -> IngestedProviderEvent | None:
        run_id = str(params.get("run_id") or "").strip()
        if not run_id:
            return None
        event_type = str(params.get("type") or "").strip().lower()
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        attempt = self.attempt_for_event(params, adopt=event_type == "run.created")
        if attempt is None:
            return None
        material = is_material_activity_event(event_type)
        accepted = True

        try:
            if event_type == "run.created":
                if not attempt.provider_run_id:
                    attempt = self.store.bind_provider_run(attempt.attempt_id, run_id)
                self.store.update_attempt(attempt.attempt_id, execution_status="queued")
            elif event_type == "run.status":
                status = str(payload.get("status") or "").strip().lower()
                mapped = self.execution_status(status)
                liveness = str(payload.get("liveness") or "").strip().lower()
                liveness_metadata: dict[str, Any] = {}
                if liveness:
                    liveness_metadata["provider_liveness"] = {
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
                if mapped or liveness_metadata:
                    self.store.update_attempt(
                        attempt.attempt_id,
                        execution_status=mapped or None,
                        metadata=liveness_metadata or None,
                    )
            elif event_type == "run.started":
                self.store.update_attempt(attempt.attempt_id, execution_status="running")
            elif event_type in {"run.failed", "run.cancelled"}:
                self.store.update_attempt(
                    attempt.attempt_id,
                    execution_status=("failed" if event_type == "run.failed" else "cancelled"),
                    result=str(payload.get("result") or ""),
                    error=str(payload.get("error") or ""),
                )

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

    def ingest_result(self, params: dict[str, Any]) -> IngestedProviderResult | None:
        run_id = str(params.get("run_id") or "").strip()
        attempt = self.attempt_for_event(params, adopt=True)
        if attempt is None:
            return None
        status = normalize_execution_status(str(params.get("status") or "failed"))
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
        if isinstance(metadata.get("provider_session"), dict):
            result_metadata["provider_session"] = dict(metadata["provider_session"])
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
            # A late contradictory result is evidence, not authority to
            # reinterpret an already-terminal Attempt.  Downstream completion
            # and narration must use the durable status that actually won.
            status = attempt.execution_status
        return IngestedProviderResult(
            run_id=run_id,
            attempt=attempt,
            status=status,
            result=result,
            error=error,
            metadata=metadata,
            evidence=self._evidence.pop(run_id, {}),
        )

    def attempt_for_event(
        self,
        params: dict[str, Any],
        *,
        adopt: bool,
    ) -> RunAttemptRecord | None:
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
        return self.adopt_runtime_run(params) if adopt else None

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
        """Recover one current ProviderRuntime record not yet bound to the ledger."""

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
