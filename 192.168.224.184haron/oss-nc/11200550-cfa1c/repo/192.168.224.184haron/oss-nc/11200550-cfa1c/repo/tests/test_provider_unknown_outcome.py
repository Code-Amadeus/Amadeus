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
    ProviderRunRequest,
    ProviderRunResult,
    ProviderSessionHandle,
)
from agent_host.work_ledger_store import WorkLedgerStore
from config import settings
from server.work_ledger_coordinator import WorkLedgerCoordinator


class _UnknownOutcomeAdapter:
    provider_id = "unknown_outcome_test"
    manifest = ProviderManifest(
        provider_id=provider_id,
        display_name="unknown outcome test",
        capabilities=ProviderCapabilities(
            task_kinds=("general", "workspace_mutation"),
            workspace_access="write",
            workspace_ownership="caller",
            durability="host_restart",
            resume="attach",
            event_model="canonical+native",
        ),
    )

    async def run(self, request, run_id, emit):
        return ProviderRunResult(
            status="orphaned",
            error="native submission acknowledgement is unknown",
            metadata={
                "result_type": "transport_outcome_unknown",
                # Untyped adapter metadata cannot grant Resume authority.
                "runtime_resumable": True,
                "outcome_uncertainty": "native_run_may_still_be_active",
            },
            session=ProviderSessionHandle(
                provider=self.provider_id,
                session_id="native-thread-unknown",
                scope="work_item",
            ),
        )

    async def cancel(self, run_id):
        return {"confirmed": False, "cancelled": False, "reason": "outcome_unknown"}


def _init_repository(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "unknown-outcome@example.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Unknown Outcome Test"],
        cwd=root,
        check=True,
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=root, check=True)


def test_unknown_provider_outcome_keeps_one_nonterminal_attempt_and_writer_fence() -> None:
    async def scenario(root: Path) -> None:
        runtime = ProviderRuntime()
        runtime.register(_UnknownOutcomeAdapter())
        store = WorkLedgerStore(root.parent / "work-ledger.sqlite3")
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
                    provider=_UnknownOutcomeAdapter.provider_id,
                    task="Apply one workspace mutation.",
                    cwd=str(root),
                    requirements=ProviderRequirements(
                        task_kind="workspace_mutation",
                        workspace_access="write",
                        workspace_ownership="caller",
                        preferred_provider=_UnknownOutcomeAdapter.provider_id,
                        preference_policy="require",
                    ),
                    metadata={
                        "source": "test_unknown_outcome",
                        "session_id": "unknown-outcome-session",
                    },
                )
            )
            assert record.task_handle is not None
            await asyncio.wait_for(record.task_handle, timeout=10)
            await coordinator.drain_provider_facts()

            work_item_id = str(record.metadata["work"]["work_item_id"])
            attempts = store.list_attempts(work_item_id)
            assert record.status == "orphaned"
            assert record.metadata["runtime_resumable"] is False
            assert len(attempts) == 1
            assert attempts[0].provider_run_id == record.run_id
            assert attempts[0].execution_status == "orphaned"
            assert attempts[0].metadata["runtime_resumable"] is False
            assert attempts[0].metadata["provider_session"] == {
                "provider": _UnknownOutcomeAdapter.provider_id,
                "session_id": "native-thread-unknown",
                "scope": "work_item",
                "version": 1,
            }
            assert store.latest_completion(work_item_id) is None
            lease = store.get_writer_lease(attempts[0].attempt_id)
            assert lease is not None and lease.status == "active"
            detail = coordinator.detail(work_item_id)
            assert detail["execution"] == "orphaned"
            assert detail["canResume"] is False

            cancellation = await runtime.cancel(
                record.run_id,
                reason="user_requested_stop_while_outcome_unknown",
            )
            await coordinator.drain_provider_facts()

            assert cancellation["cancelled"] is False
            assert cancellation["reason"] == "cancel_unconfirmed"
            assert record.status == "orphaned"
            after_cancel = store.get_attempt(attempts[0].attempt_id)
            assert after_cancel is not None
            assert after_cancel.execution_status == "orphaned"
            assert after_cancel.metadata["provider_liveness"]["state"] == "cancel_pending"
            assert store.latest_completion(work_item_id) is None
            lease = store.get_writer_lease(attempts[0].attempt_id)
            assert lease is not None and lease.status == "active"

            reconciled_payload = {
                "provider": record.provider,
                "run_id": record.run_id,
                "status": "done",
                "result": "",
                "error": "",
                "metadata": dict(record.metadata),
            }
            await coordinator._on_provider_result(
                "provider.result",
                reconciled_payload,
            )
            await coordinator._on_provider_result(
                "provider.result",
                reconciled_payload,
            )

            reconciled = store.get_attempt(attempts[0].attempt_id)
            assert reconciled is not None and reconciled.execution_status == "succeeded"
            assert reconciled.error == ""
            completions = store.list_completions(work_item_id)
            assert len(completions) == 1
            assert completions[0].execution_status == "succeeded"
            assert completions[0].terminal is True
            lease = store.get_writer_lease(attempts[0].attempt_id)
            assert lease is not None and lease.status == "released"
        finally:
            settings.WORK_WORKTREE_ISOLATION = previous_isolation
            await runtime.close()
            coordinator.close()
            store.close()

    with tempfile.TemporaryDirectory(prefix="amadeus-provider-unknown-") as temp_dir:
        workspace = Path(temp_dir) / "workspace"
        workspace.mkdir()
        _init_repository(workspace)
        asyncio.run(scenario(workspace))


if __name__ == "__main__":
    test_unknown_provider_outcome_keeps_one_nonterminal_attempt_and_writer_fence()
    print("Provider unknown-outcome tests passed")
