"""Executor for one accepted Control Work effect."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from agent_host.provider_runtime import ProviderRunRecord, ProviderRuntime
from agent_host.provider_types import ProviderRunIntakeAuthority
from agent_host.work_ledger_store import WorkLedgerConflict
from server.control_ledger import ControlLedgerConflict
from server.work_control import WorkControl
from server.work_ledger_coordinator import WorkLedgerCoordinator


@dataclass(slots=True)
class WorkEffectDispatch:
    effect_id: str
    binding: dict[str, str]
    record: ProviderRunRecord | None
    status: str
    replayed: bool
    receipt: dict[str, Any] | None = None


class WorkEffectExecutor:
    """Enter the existing Runtime/Work pipeline exactly once per effect."""

    def __init__(
        self,
        control: WorkControl,
        runtime: ProviderRuntime,
        coordinator: WorkLedgerCoordinator,
    ) -> None:
        self.control = control
        self.runtime = runtime
        self.coordinator = coordinator

    async def execute(self, effect_id: str) -> dict[str, Any]:
        """Run or replay one accepted effect without submitting a sibling."""

        dispatch = await self.dispatch(effect_id)
        return await self.finish(dispatch)

    async def dispatch(self, effect_id: str) -> WorkEffectDispatch:
        """Stop once Runtime/Work own the exact run; do not wait for terminal."""

        existing_receipt = self.control.ledger.get_receipt(effect_id)
        if existing_receipt is not None:
            binding = self.control.binding(effect_id)
            if binding is None:
                raise ControlLedgerConflict(
                    "terminal Control receipt has no exact Work binding"
                )
            return WorkEffectDispatch(effect_id, binding, None,
                "rejected" if existing_receipt.get("details", {}).get("authority")
                    == "work_intake_rejection" else "terminal", True, existing_receipt)

        binding = self.control.binding(effect_id)
        was_bound = binding is not None
        record = None
        if binding is not None:
            record = self.runtime.get_run(binding["provider_run_id"])
            if record is None:
                rejected = self._rejected_intake(effect_id, binding, replayed=True)
                if rejected is not None:
                    return rejected
                return WorkEffectDispatch(effect_id, binding, None,
                    "bound_without_runtime", True)
        else:
            request = self.control.provider_request(effect_id)
            authority = ProviderRunIntakeAuthority(effect_id)
            try:
                record = await self.runtime.start_accepted(request, authority)
            except Exception as exc:
                # A competing Runtime may have allocated another provisional
                # id and won the one C1 transaction. Only a complete durable
                # binding converts that race into an idempotent replay.
                binding = self.control.binding(effect_id)
                if binding is None:
                    raise
                record = self.runtime.get_run(binding["provider_run_id"])
                if record is None:
                    rejected = self._rejected_intake(effect_id, binding, replayed=False)
                    if rejected is not None:
                        return rejected
                if not isinstance(exc, (ControlLedgerConflict, WorkLedgerConflict)):
                    raise
                return WorkEffectDispatch(effect_id, binding, record,
                    "already_dispatched" if record is not None else "bound_without_runtime", True)
            binding = self.control.binding(effect_id)
            if binding is None or binding["provider_run_id"] != record.run_id:
                raise WorkLedgerConflict(
                    "accepted Runtime run lost its exact Work effect binding"
                )

        return WorkEffectDispatch(effect_id, binding, record,
            "already_dispatched" if was_bound else "dispatched", was_bound)

    async def finish(self, dispatch: WorkEffectDispatch) -> dict[str, Any]:
        """Wait for one already-owned dispatch and project its domain receipt."""

        if dispatch.status in {"terminal", "rejected"}:
            return {"status":"terminal", "replayed":dispatch.replayed,
                "binding":dispatch.binding, "receipt":dispatch.receipt}
        record = dispatch.record
        binding = dispatch.binding

        if record is not None and record.task_handle is not None:
            try:
                await asyncio.shield(record.task_handle)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                latest = self.runtime.get_run(record.run_id)
                if ((current is not None and current.cancelling())
                        or latest is None or latest.status != "cancelled"):
                    raise
        await self.coordinator.drain_provider_facts()
        return await self._project_existing(dispatch.effect_id, binding)

    def _rejected_intake(self, effect_id, binding, *, replayed):
        attempt = self.control.work.get_attempt(binding["attempt_id"])
        if (attempt is None or attempt.execution_status != "cancelled"
                or not attempt.metadata.get("start_rejected")):
            return None
        recorded = self.control.record_intake_rejection(effect_id)
        return WorkEffectDispatch(effect_id, binding, None, "rejected", replayed,
            recorded["receipt"])

    async def _project_existing(
        self,
        effect_id: str,
        binding: dict[str, str],
    ) -> dict[str, Any]:
        await self.coordinator.drain_provider_facts()
        attempt = self.control.work.get_attempt(binding["attempt_id"])
        if attempt is None:
            raise WorkLedgerConflict("accepted Work effect Attempt disappeared")
        if attempt.execution_status in {"succeeded", "failed", "cancelled"}:
            recorded = self.control.record_terminal_receipt(effect_id)
            return {
                "status": "terminal",
                "replayed": bool(recorded["replayed"]),
                "binding": binding,
                "receipt": recorded["receipt"],
            }
        return {
            "status": (
                "unknown"
                if attempt.execution_status == "orphaned"
                    or self.runtime.get_run(binding["provider_run_id"]) is None
                else "already_dispatched"
            ),
            "replayed": True,
            "binding": binding,
            "receipt": None,
        }


__all__ = ["WorkEffectDispatch", "WorkEffectExecutor"]
