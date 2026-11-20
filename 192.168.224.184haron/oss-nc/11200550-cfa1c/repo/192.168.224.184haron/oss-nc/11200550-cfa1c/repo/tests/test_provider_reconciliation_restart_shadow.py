from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from agent_host.provider_contract import (
    ProviderCapabilities,
    ProviderManifest,
    ProviderRequirements,
)
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import (
    ProviderNativeExecutionHandle,
    ProviderRunRequest,
    ProviderRunResult,
    ProviderSessionHandle,
    ProviderSubmissionReconciliationRequest,
    ProviderSubmissionReconciliationResult,
)
from agent_host.work_ledger_store import WorkLedgerStore
from config import settings
from server.work_ledger_coordinator import WorkLedgerCoordinator


class _RestartQueryableAdapter:
    provider_id = "restart_query_test"
    manifest = ProviderManifest(
        provider_id=provider_id,
        display_name="Restart reconciliation test",
        capabilities=ProviderCapabilities(
            task_kinds=("workspace_mutation",),
            workspace_access="write",
            workspace_ownership="caller",
            durability="host_restart",
            resume="attach",
            cancellation="confirmed",
            submission_reconciliation="query",
        ),
    )

    def __init__(self, observation: Any = None) -> None:
        self.observation = observation or ProviderSubmissionReconciliationResult(
            state="unavailable",
            reason="not_configured",
        )
        self.queries: list[ProviderSubmissionReconciliationRequest] = []

    async def run(self, request, run_id, emit) -> ProviderRunResult:
        del request, run_id, emit
        return ProviderRunResult(
            status="orphaned",
            error="native submission acknowledgement is unknown",
            session=ProviderSessionHandle(
                provider=self.provider_id,
                session_id="opaque-native-session",
                scope="work_item",
            ),
        )

    async def cancel(self, run_id: str) -> dict[str, Any]:
        del run_id
        return {
            "confirmed": False,
            "cancelled": False,
            "reason": "native_outcome_unknown",
        }

    async def reconcile_submission(
        self,
        request: ProviderSubmissionReconciliationRequest,
    ) -> ProviderSubmissionReconciliationResult:
        self.queries.append(request)
        if isinstance(self.observation, Exception):
            raise self.observation
        return self.observation


def _initialize_repository(workspace: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "restart-shadow@example.invalid"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Restart Shadow"],
        cwd=workspace,
        check=True,
    )
    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "seed"], cwd=workspace, check=True)


async def _create_persisted_unknown(database: Path, workspace: Path) -> tuple[str, str, str]:
    runtime = ProviderRuntime()
    adapter = _RestartQueryableAdapter()
    runtime.register(adapter)
    store = WorkLedgerStore(database)
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
        record = await runtime.start(
            ProviderRunRequest(
                provider=adapter.provider_id,
                task="Apply one mutation whose native acceptance becomes unknown.",
                cwd=str(workspace),
                requirements=ProviderRequirements(
                    task_kind="workspace_mutation",
                    workspace_access="write",
                    workspace_ownership="caller",
                    durability="host_restart",
                    resume="attach",
                    preferred_provider=adapter.provider_id,
                    preference_policy="require",
                ),
                metadata={"session_id": "restart-shadow-session"},
            )
        )
        assert record.task_handle is not None
        await asyncio.wait_for(record.task_handle, timeout=10.0)
        await coordinator.drain_provider_facts()
        work = record.metadata.get("work")
        assert isinstance(work, dict)
        work_item_id = str(work["work_item_id"])
        attempt_id = str(work["attempt_id"])
        attempt = store.get_attempt(attempt_id)
        assert attempt is not None
        assert record.status == "orphaned"
        assert attempt.execution_status == "orphaned"
        assert attempt.provider_run_id == record.run_id
        assert attempt.metadata["provider_session"] == {
            "provider": adapter.provider_id,
            "session_id": "opaque-native-session",
            "scope": "work_item",
            "version": 1,
        }
        assert store.latest_completion(work_item_id) is None
        lease = store.get_writer_lease(attempt_id)
        assert lease is not None and lease.status == "active"
        return record.run_id, work_item_id, attempt_id
    finally:
        settings.WORK_WORKTREE_ISOLATION = previous_isolation
        await runtime.close()
        coordinator.close()
        store.close()


async def _inspect_after_restart(
    database: Path,
    workspace: Path,
    *,
    run_id: str,
    work_item_id: str,
    attempt_id: str,
    observation: Any,
) -> tuple[
    ProviderSubmissionReconciliationResult,
    _RestartQueryableAdapter,
]:
    runtime = ProviderRuntime()
    adapter = _RestartQueryableAdapter(observation)
    runtime.register(adapter)
    store = WorkLedgerStore(database)
    coordinator = WorkLedgerCoordinator(store)
    try:
        coordinator.adopt_runtime_records([])
        attempt = store.get_attempt(attempt_id)
        assert attempt is not None
        assert attempt.execution_status == "orphaned"
        runtime.add_orphaned_run(
            provider=attempt.provider,
            run={
                "provider": attempt.provider,
                "run_id": attempt.provider_run_id,
                "task": attempt.task,
                "cwd": str(workspace),
                "status": "orphaned",
                "updated_at": attempt.updated_at,
                "metadata": dict(attempt.metadata),
            },
        )
        runtime_record = runtime.get_run(run_id)
        assert runtime_record is not None
        runtime_before = runtime_record.to_dict()
        attempt_before = attempt.to_dict()
        lease_before = store.get_writer_lease(attempt_id)
        assert lease_before is not None and lease_before.status == "active"
        lease_before_payload = lease_before.to_dict()

        first = await runtime.inspect_orphaned_submission(run_id)
        second = await runtime.inspect_orphaned_submission(run_id)

        after = store.get_attempt(attempt_id)
        assert after is not None
        assert first == second
        assert after.to_dict() == attempt_before
        assert after.execution_status == "orphaned"
        assert store.latest_completion(work_item_id) is None
        lease_after = store.get_writer_lease(attempt_id)
        assert lease_after is not None
        assert lease_after.to_dict() == lease_before_payload
        assert runtime_record.to_dict() == runtime_before
        assert runtime_record.status == "orphaned"
        assert runtime_record.events == []
        return first, adapter
    finally:
        await runtime.close()
        coordinator.close()
        store.close()


def test_restart_shadow_observes_exact_active_without_changing_work() -> None:
    async def scenario(root: Path) -> None:
        workspace = root / "workspace"
        workspace.mkdir()
        _initialize_repository(workspace)
        database = root / "ledger.sqlite3"
        run_id, work_item_id, attempt_id = await _create_persisted_unknown(
            database,
            workspace,
        )
        expected = ProviderSubmissionReconciliationResult(
            state="matched_active",
            execution=ProviderNativeExecutionHandle(
                provider=_RestartQueryableAdapter.provider_id,
                execution_id="opaque-native-execution-active",
            ),
        )

        observed, adapter = await _inspect_after_restart(
            database,
            workspace,
            run_id=run_id,
            work_item_id=work_item_id,
            attempt_id=attempt_id,
            observation=expected,
        )

        assert observed is expected
        assert len(adapter.queries) == 2
        assert all(query.run_id == run_id for query in adapter.queries)
        assert all(
            query.session
            == ProviderSessionHandle(
                provider=_RestartQueryableAdapter.provider_id,
                session_id="opaque-native-session",
            )
            for query in adapter.queries
        )

    with tempfile.TemporaryDirectory(prefix="provider-reconcile-restart-active-") as temp:
        asyncio.run(scenario(Path(temp)))


def test_restart_shadow_observes_terminal_without_completing_work() -> None:
    async def scenario(root: Path) -> None:
        workspace = root / "workspace"
        workspace.mkdir()
        _initialize_repository(workspace)
        database = root / "ledger.sqlite3"
        run_id, work_item_id, attempt_id = await _create_persisted_unknown(
            database,
            workspace,
        )
        expected = ProviderSubmissionReconciliationResult(
            state="matched_terminal",
            execution=ProviderNativeExecutionHandle(
                provider=_RestartQueryableAdapter.provider_id,
                execution_id="opaque-native-execution-terminal",
            ),
            terminal_result=ProviderRunResult(
                status="done",
                result="native terminal evidence",
                session=ProviderSessionHandle(
                    provider=_RestartQueryableAdapter.provider_id,
                    session_id="opaque-native-session",
                ),
            ),
        )

        observed, _adapter = await _inspect_after_restart(
            database,
            workspace,
            run_id=run_id,
            work_item_id=work_item_id,
            attempt_id=attempt_id,
            observation=expected,
        )

        assert observed is expected
        assert observed.terminal_result is not None
        assert observed.terminal_result.status == "done"

    with tempfile.TemporaryDirectory(prefix="provider-reconcile-restart-terminal-") as temp:
        asyncio.run(scenario(Path(temp)))


def test_restart_shadow_keeps_query_failure_unknown_and_fenced() -> None:
    async def scenario(root: Path) -> None:
        workspace = root / "workspace"
        workspace.mkdir()
        _initialize_repository(workspace)
        database = root / "ledger.sqlite3"
        run_id, work_item_id, attempt_id = await _create_persisted_unknown(
            database,
            workspace,
        )

        observed, adapter = await _inspect_after_restart(
            database,
            workspace,
            run_id=run_id,
            work_item_id=work_item_id,
            attempt_id=attempt_id,
            observation=RuntimeError("provider query unavailable"),
        )

        assert observed == ProviderSubmissionReconciliationResult(
            state="unavailable",
            reason="provider_query_failed:RuntimeError",
        )
        assert len(adapter.queries) == 2

    with tempfile.TemporaryDirectory(prefix="provider-reconcile-restart-failure-") as temp:
        asyncio.run(scenario(Path(temp)))
