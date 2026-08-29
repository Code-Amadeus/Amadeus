from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_contract import (
    ProviderCapabilities,
    ProviderManifest,
    ProviderRequirements,
)
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import (
    ProviderActivityEvidence,
    ProviderEvent,
    ProviderRunRequest,
    ProviderRunResult,
    ProviderSessionHandle,
)
from agent_host.work_ledger_store import WorkLedgerStore
from config import settings
from server.event_bus import bus
from server.protocol import Method
from server.work_context import run_work_notes
from server.work_ledger_coordinator import WorkLedgerCoordinator


class _ProgressOnlyThenExecuteAdapter:
    provider_id = "progress_recovery_test"
    manifest = ProviderManifest(
        provider_id=provider_id,
        display_name="progress recovery test",
        capabilities=ProviderCapabilities(
            task_kinds=("general", "workspace_mutation"),
            workspace_access="write",
            workspace_ownership="caller",
            durability="process",
            resume="attach",
            event_model="canonical+native",
        ),
    )

    def __init__(self, *, execute_on_second: bool) -> None:
        self.execute_on_second = execute_on_second
        self.requests: list[ProviderRunRequest] = []
        self.run_ids: list[str] = []
        self.second_returned = asyncio.Event()

    async def run(self, request, run_id, emit):
        self.requests.append(request)
        self.run_ids.append(run_id)
        call_number = len(self.requests)
        if call_number == 1 or not self.execute_on_second:
            await emit(
                ProviderEvent(
                    provider=self.provider_id,
                    run_id=run_id,
                    type="semantic.progress",
                    payload={
                        "milestone": "design",
                        "summary": "The execution direction is selected.",
                        "source": "test_provider",
                        "explicit": True,
                        "verified": False,
                        "status": "reported",
                    },
                )
            )
            result = ProviderRunResult(
                status="done",
                result="",
                activity_evidence=ProviderActivityEvidence(
                    terminal_observed=True,
                    progress_milestones=1,
                    execution_items=0,
                ),
                session=ProviderSessionHandle(
                    provider=self.provider_id,
                    session_id="native-thread-one",
                ),
            )
            if call_number == 2:
                self.second_returned.set()
            return result

        assert request.recovery is not None
        assert request.recovery.reason == "progress_only_completion"
        assert request.recovery.ordinal == 1
        assert request.session == ProviderSessionHandle(
            provider=self.provider_id,
            session_id="native-thread-one",
        )
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.call",
                payload={"name": "file_change", "item_id": "change-one"},
            )
        )
        target = Path(str(request.cwd)) / "recovered.txt"
        target.write_text("recovered\n", encoding="utf-8")
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="tool.result",
                payload={
                    "name": "file_change",
                    "item_id": "change-one",
                    "success": True,
                    "changes": [{"path": "recovered.txt", "kind": "add"}],
                },
            )
        )
        self.second_returned.set()
        return ProviderRunResult(
            status="done",
            result="Recovered the same authorized task.",
            activity_evidence=ProviderActivityEvidence(
                terminal_observed=True,
                progress_milestones=0,
                execution_items=1,
            ),
            session=ProviderSessionHandle(
                provider=self.provider_id,
                session_id="native-thread-one",
            ),
        )

    async def cancel(self, run_id):
        return {"confirmed": True, "cancelled": True}


def _init_repository(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "progress-recovery@example.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Progress Recovery Test"],
        cwd=root,
        check=True,
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)


async def _run_scenario(
    root: Path,
    ledger_path: Path,
    *,
    execute_on_second: bool,
):
    adapter = _ProgressOnlyThenExecuteAdapter(
        execute_on_second=execute_on_second
    )
    runtime = ProviderRuntime()
    runtime.register(adapter)
    store = WorkLedgerStore(ledger_path)
    coordinator = WorkLedgerCoordinator(
        store,
        provider_start=runtime.start,
        provider_cancel=runtime.cancel,
    )
    runtime.set_request_preparer(coordinator.prepare_request)
    coordinator.configure()
    previous_isolation = settings.WORK_WORKTREE_ISOLATION
    settings.WORK_WORKTREE_ISOLATION = False
    try:
        first = await runtime.start(
            ProviderRunRequest(
                provider=adapter.provider_id,
                task="Create recovered.txt in the existing project.",
                cwd=str(root),
                requirements=ProviderRequirements(
                    task_kind="workspace_mutation",
                    workspace_access="write",
                    workspace_ownership="caller",
                    preferred_provider=adapter.provider_id,
                    preference_policy="require",
                ),
                metadata={
                    "source": "test_progress_recovery",
                    "session_id": "progress-recovery-session",
                },
            )
        )
        assert first.task_handle is not None
        await asyncio.wait_for(first.task_handle, timeout=10)
        await asyncio.wait_for(adapter.second_returned.wait(), timeout=10)
        runs = runtime.list_runs()
        assert len(runs) == 2
        successor = next(
            runtime.get_run(str(run["run_id"]))
            for run in runs
            if str(run["run_id"]) != first.run_id
        )
        assert successor is not None and successor.task_handle is not None
        await asyncio.wait_for(successor.task_handle, timeout=10)
        work_item_id = str(first.metadata["work"]["work_item_id"])
        attempts = store.list_attempts(work_item_id)
        operations = store.list_operations(work_item_id)
        return adapter, runtime, coordinator, store, first, successor, attempts, operations
    except Exception:
        await runtime.close()
        coordinator.close()
        raise
    finally:
        settings.WORK_WORKTREE_ISOLATION = previous_isolation


def test_progress_only_write_continues_once_on_same_operation_and_session() -> None:
    async def scenario(root: Path) -> None:
        (
            adapter,
            runtime,
            coordinator,
            _store,
            first,
            successor,
            attempts,
            operations,
        ) = await _run_scenario(
            root,
            root.parent / "work-ledger.sqlite3",
            execute_on_second=True,
        )
        try:
            assert len(adapter.requests) == 2
            assert len(set(adapter.run_ids)) == 2
            assert first.status == "error"
            assert successor.status == "done"
            assert len(operations) == 1
            assert len(attempts) == 2
            assert attempts[0].operation_id == attempts[1].operation_id
            assert attempts[0].execution_status == "failed"
            assert attempts[1].execution_status == "succeeded"
            completion = attempts[0].metadata["provider_completion"]
            assert completion["classification"] == "progress_only_completion"
            assert completion["recovery_state"] == "started"
            assert completion["successor_attempt_id"] == attempts[1].attempt_id
            recovery = attempts[1].metadata["provider_recovery"]
            assert recovery == {
                "reason": "progress_only_completion",
                "root_attempt_id": attempts[0].attempt_id,
                "predecessor_attempt_id": attempts[0].attempt_id,
                "ordinal": 1,
            }
            attach = attempts[1].metadata["provider_session_attach"]
            assert attach["previous_attempt_id"] == attempts[0].attempt_id
            assert attach["recovery_reason"] == "progress_only_completion"
            assert (root / "recovered.txt").read_text(encoding="utf-8") == "recovered\n"
        finally:
            await runtime.close()
            coordinator.close()

    with tempfile.TemporaryDirectory(prefix="amadeus-progress-recovery-") as temp_dir:
        root = Path(temp_dir) / "workspace"
        root.mkdir()
        _init_repository(root)
        asyncio.run(scenario(root))


def test_queued_provider_run_can_be_cancelled_before_adapter_scheduling() -> None:
    async def scenario(root: Path) -> None:
        adapter = _ProgressOnlyThenExecuteAdapter(execute_on_second=True)
        runtime = ProviderRuntime()
        runtime.register(adapter)

        async def cancel_created(_method: str, params: dict) -> None:
            if str(params.get("type") or "") == "run.created":
                outcome = await runtime.cancel(str(params.get("run_id") or ""))
                assert outcome["cancelled"] is True

        bus.on(Method.PROVIDER_EVENT, cancel_created)
        try:
            record = await runtime.start(
                ProviderRunRequest(
                    provider=adapter.provider_id,
                    task="Do not start native execution after retraction.",
                    cwd=str(root),
                )
            )
            assert record.status == "cancelled"
            assert record.task_handle is None
            assert adapter.requests == []
        finally:
            bus.off(Method.PROVIDER_EVENT, cancel_created)
            await runtime.close()

    with tempfile.TemporaryDirectory(prefix="amadeus-provider-queued-cancel-") as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_progress_only_recovery_is_never_repeated() -> None:
    async def scenario(root: Path) -> None:
        (
            adapter,
            runtime,
            coordinator,
            _store,
            first,
            successor,
            attempts,
            operations,
        ) = await _run_scenario(
            root,
            root.parent / "work-ledger.sqlite3",
            execute_on_second=False,
        )
        try:
            assert len(adapter.requests) == 2
            assert first.status == "error"
            assert successor.status == "error"
            assert len(operations) == 1
            assert len(attempts) == 2
            assert all(attempt.execution_status == "failed" for attempt in attempts)
            assert attempts[1].metadata["provider_recovery"]["ordinal"] == 1
            assert attempts[1].metadata["provider_completion"]["recovery_state"] == "unclaimed"
            assert len(runtime.list_runs()) == 2
            assert not (root / "recovered.txt").exists()
        finally:
            await runtime.close()
            coordinator.close()

    with tempfile.TemporaryDirectory(prefix="amadeus-progress-exhausted-") as temp_dir:
        root = Path(temp_dir) / "workspace"
        root.mkdir()
        _init_repository(root)
        asyncio.run(scenario(root))


def test_replayed_predecessor_result_cannot_rollback_or_narrate_recovery() -> None:
    async def scenario(root: Path) -> None:
        (
            _adapter,
            runtime,
            coordinator,
            store,
            first,
            successor,
            attempts,
            _operations,
        ) = await _run_scenario(
            root,
            root.parent / "work-ledger.sqlite3",
            execute_on_second=True,
        )
        try:
            predecessor = attempts[0]
            before_completion = dict(
                predecessor.metadata["provider_completion"]
            )
            before_notes = len(run_work_notes(first.run_id, limit=100))

            await coordinator._on_provider_result("provider.result", first.to_dict())

            after = store.get_attempt(predecessor.attempt_id)
            assert after is not None
            assert after.metadata["provider_completion"] == before_completion
            assert after.metadata["provider_completion"]["recovery_state"] == "started"
            assert after.metadata["provider_completion"]["successor_run_id"] == (
                successor.run_id
            )
            assert len(run_work_notes(first.run_id, limit=100)) == before_notes
            assert len(runtime.list_runs()) == 2
        finally:
            await runtime.close()
            coordinator.close()

    with tempfile.TemporaryDirectory(prefix="amadeus-progress-replay-") as temp_dir:
        root = Path(temp_dir) / "workspace"
        root.mkdir()
        _init_repository(root)
        asyncio.run(scenario(root))


def test_pending_progress_recovery_can_be_retracted_before_successor_start() -> None:
    async def scenario(root: Path) -> None:
        adapter = _ProgressOnlyThenExecuteAdapter(execute_on_second=True)
        runtime = ProviderRuntime()
        runtime.register(adapter)
        store = WorkLedgerStore(root.parent / "work-ledger.sqlite3")
        recovery_entered = asyncio.Event()
        release_recovery = asyncio.Event()

        async def delayed_start(request: ProviderRunRequest):
            recovery_entered.set()
            await release_recovery.wait()
            return await runtime.start(request)

        coordinator = WorkLedgerCoordinator(
            store,
            provider_start=delayed_start,
            provider_cancel=runtime.cancel,
        )
        runtime.set_request_preparer(coordinator.prepare_request)
        coordinator.configure()
        previous_isolation = settings.WORK_WORKTREE_ISOLATION
        settings.WORK_WORKTREE_ISOLATION = False
        try:
            first = await runtime.start(
                ProviderRunRequest(
                    provider=adapter.provider_id,
                    task="Create recovered.txt in the existing project.",
                    cwd=str(root),
                    requirements=ProviderRequirements(
                        task_kind="workspace_mutation",
                        workspace_access="write",
                        workspace_ownership="caller",
                        preferred_provider=adapter.provider_id,
                        preference_policy="require",
                    ),
                    metadata={
                        "source": "test_progress_recovery_cancel",
                        "session_id": "progress-recovery-cancel-session",
                    },
                )
            )
            await asyncio.wait_for(recovery_entered.wait(), timeout=10)
            pending = coordinator.pending_provider_recoveries()
            assert len(pending) == 1
            assert coordinator.cancel_pending_provider_recovery(
                pending[0]["attempt_id"]
            )
            release_recovery.set()
            assert first.task_handle is not None
            await asyncio.wait_for(first.task_handle, timeout=10)
            runs = runtime.list_runs()
            assert len(runs) == 1
            work_item_id = str(first.metadata["work"]["work_item_id"])
            attempts = store.list_attempts(work_item_id)
            assert len(attempts) == 1
            assert attempts[0].metadata["provider_completion"]["recovery_state"] == (
                "cancelled"
            )
            assert not (root / "recovered.txt").exists()
            assert not coordinator.pending_provider_recoveries()
        finally:
            settings.WORK_WORKTREE_ISOLATION = previous_isolation
            await runtime.close()
            coordinator.close()

    with tempfile.TemporaryDirectory(prefix="amadeus-progress-cancel-") as temp_dir:
        root = Path(temp_dir) / "workspace"
        root.mkdir()
        _init_repository(root)
        asyncio.run(scenario(root))


def test_startup_reconciles_claimed_recovery_without_replaying_execution() -> None:
    def seed(root: Path, *, with_successor: bool, successor_run_id: str = "run-successor"):
        store = WorkLedgerStore(root / "work-ledger.sqlite3")
        workspace = root / "workspace"
        workspace.mkdir()
        project = store.create_or_get_project(workspace)
        item = store.create_work_item(
            project.project_id,
            title="Recover a bounded successor",
            goal="Keep recovery lineage durable across a Host restart.",
        )
        first = store.create_attempt(
            item.work_item_id,
            provider="codex",
            task="Continue the task.",
            provider_run_id="run-predecessor",
            metadata={
                "provider_completion": {
                    "classification": "progress_only_completion",
                    "recovery_state": "claimed",
                    "recovery_root_attempt_id": "attempt-predecessor",
                    "recovery_ordinal": 1,
                }
            },
            attempt_id="attempt-predecessor",
        )
        store.update_attempt(first.attempt_id, execution_status="failed")
        successor = None
        if with_successor:
            successor = store.create_attempt(
                item.work_item_id,
                provider="codex",
                task="Continue the same task once.",
                provider_run_id=successor_run_id,
                operation_id=first.operation_id,
                metadata={
                    "provider_recovery": {
                        "reason": "progress_only_completion",
                        "root_attempt_id": first.attempt_id,
                        "predecessor_attempt_id": first.attempt_id,
                        "ordinal": 1,
                    }
                },
            )
        return store, first, successor

    with tempfile.TemporaryDirectory(prefix="amadeus-progress-boot-") as temp_dir:
        root = Path(temp_dir)
        for case_name, with_successor, successor_run_id, expected_started in (
            ("unlinked", False, "", False),
            ("linked", True, "run-successor", True),
            ("half-built", True, "", False),
        ):
            case_root = root / case_name
            case_root.mkdir()
            store, first, successor = seed(
                case_root,
                with_successor=with_successor,
                successor_run_id=successor_run_id,
            )
            coordinator = WorkLedgerCoordinator(store)
            coordinator.configure()
            try:
                recovered = store.get_attempt(first.attempt_id)
                assert recovered is not None
                completion = recovered.metadata["provider_completion"]
                if not expected_started:
                    assert completion["recovery_state"] == "failed"
                    assert completion["recovery_error"] == (
                        "host_restarted_before_successor_intake"
                    )
                else:
                    assert successor is not None
                    assert completion["recovery_state"] == "started"
                    assert completion["successor_attempt_id"] == successor.attempt_id
                    assert completion["successor_run_id"] == "run-successor"
                assert coordinator.pending_provider_recoveries() == []
            finally:
                coordinator.close()
