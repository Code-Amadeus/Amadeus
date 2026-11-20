"""Provider terminal facts settle once across process gaps and concurrent replay."""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_host.work_ledger_store import WorkLedgerStore
from server.provider_event_ingestion import PROVIDER_TERMINAL_PIPELINE_METADATA_KEY
from server.work_ledger_coordinator import WorkLedgerCoordinator


@pytest.mark.parametrize("failure", ["none", "replay", "startup_adoption", "release_writer_lease", "record_completion", "publish_snapshot"])
def test_terminal_pipeline_recovers_each_commit_boundary_once(tmp_path, monkeypatch, failure):
    from config import settings
    from server.event_bus import bus
    from server.protocol import Method

    monkeypatch.setattr(settings, "WORK_LEDGER_OWNS_TERMINAL_NARRATION", True)

    async def run():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        database = tmp_path / "pipeline.sqlite3"
        store = WorkLedgerStore(database)
        project = store.create_or_get_project(workspace)
        item = store.create_work_item(project.project_id, title="Recover one terminal result")
        attempt = store.create_attempt(item.work_item_id, provider="fixture",
            provider_run_id="known-run", task="Finish the accepted work")
        store.update_attempt(attempt.attempt_id, execution_status="orphaned")
        store.acquire_writer_lease(item.work_item_id, attempt.attempt_id, workspace_path=workspace)
        owner = WorkLedgerCoordinator(store)
        payload = {"provider":"fixture", "run_id":"known-run", "status":"done",
                   "result":"Finished", "error":"", "metadata":{}}
        try:
            if failure == "startup_adoption":
                owner.adopt_runtime_records([payload])
            elif failure in {"release_writer_lease", "record_completion", "publish_snapshot"}:
                target = owner if failure == "publish_snapshot" else store
                fake = AsyncMock(side_effect=RuntimeError("injected failure")) if failure == "publish_snapshot" else None
                with patch.object(target, failure, **({"new":fake} if fake else {"side_effect":RuntimeError("injected failure")})):
                    with pytest.raises(RuntimeError, match="injected failure"):
                        await owner._on_provider_result("provider.result", payload)
                assert store.get_attempt(attempt.attempt_id).metadata[
                    PROVIDER_TERMINAL_PIPELINE_METADATA_KEY]["state"] == "pending"
            else:
                await owner._on_provider_result("provider.result", payload)
                if failure == "replay":
                    await owner._on_provider_result("provider.result", payload)
        finally:
            owner.close()

        reopened = WorkLedgerStore(database)
        restarted = WorkLedgerCoordinator(reopened)
        try:
            restarted.adopt_runtime_records([])
            emitted = AsyncMock()
            with patch.object(bus, "emit", new=emitted):
                recovered = await restarted.recover_pending_terminal_results()
                await restarted.replay_pending_terminal_notices()
            assert recovered == (0 if failure in {"none", "replay"} else 1)
            terminal = reopened.get_attempt(attempt.attempt_id)
            assert terminal.execution_status == "succeeded"
            assert terminal.metadata[PROVIDER_TERMINAL_PIPELINE_METADATA_KEY]["state"] == "completed"
            assert reopened.get_writer_lease(attempt.attempt_id).status == "released"
            assert len(reopened.list_completions(item.work_item_id)) == 1
            assert sum(call.args[0] == Method.CHAT_WORK_NOTE for call in emitted.await_args_list) == 1
            assert await restarted.recover_pending_terminal_results() == 0
            # The transport alone does not acknowledge presentation. Settle the
            # emitted notice through its existing delivery owner before checking
            # that it will not be narrated again on another recovery pass.
            note = next(call.args[1] for call in emitted.await_args_list if call.args[0] == Method.CHAT_WORK_NOTE)
            await restarted._on_terminal_work_notice_delivered(Method.CHAT_WORK_NOTE_DELIVERED, note["metadata"])
            assert await restarted.replay_pending_terminal_notices() == 0
        finally:
            restarted.close()

    asyncio.run(run())

def test_terminal_event_process_gap_restarts_as_orphan_not_partial_terminal() -> None:
    async def scenario(root: Path) -> None:
        workspace = root / "workspace"
        workspace.mkdir()
        database = root / "ledger.sqlite3"
        store = WorkLedgerStore(database)
        project = store.create_or_get_project(workspace)
        item = store.create_work_item(
            project.project_id,
            title="Terminal event process gap",
        )
        attempt = store.create_attempt(
            item.work_item_id,
            provider="generic_event_gap",
            provider_run_id="host-run-event-gap",
            task="Exercise the terminal event/result process gap.",
        )
        store.update_attempt(attempt.attempt_id, execution_status="running")
        store.acquire_writer_lease(
            item.work_item_id,
            attempt.attempt_id,
            workspace_path=workspace,
        )
        coordinator = WorkLedgerCoordinator(store)
        await coordinator._on_provider_event(
            "provider.event",
            {
                "provider": "generic_event_gap",
                "run_id": "host-run-event-gap",
                "type": "run.failed",
                "payload": {"status": "error", "error": "event-only failure"},
            },
        )

        event_only = store.get_attempt(attempt.attempt_id)
        lease = store.get_writer_lease(attempt.attempt_id)
        assert event_only is not None and event_only.execution_status == "running"
        assert lease is not None and lease.status == "active"
        assert PROVIDER_TERMINAL_PIPELINE_METADATA_KEY not in event_only.metadata
        assert store.latest_completion(item.work_item_id) is None
        coordinator.close()

        reopened = WorkLedgerStore(database)
        restarted = WorkLedgerCoordinator(reopened)
        restarted.adopt_runtime_records([])
        try:
            recovered = reopened.get_attempt(attempt.attempt_id)
            lease = reopened.get_writer_lease(attempt.attempt_id)
            assert recovered is not None and recovered.execution_status == "orphaned"
            assert lease is not None and lease.status == "active"
            assert PROVIDER_TERMINAL_PIPELINE_METADATA_KEY not in recovered.metadata
            assert reopened.latest_completion(item.work_item_id) is None
        finally:
            restarted.close()

    with tempfile.TemporaryDirectory(prefix="provider-terminal-event-gap-") as temp:
        asyncio.run(scenario(Path(temp)))

def test_concurrent_exact_terminal_results_run_downstream_once() -> None:
    async def scenario(root: Path) -> None:
        store = WorkLedgerStore(root / "ledger.sqlite3")
        project = store.create_or_get_project(root / "project")
        item = store.create_work_item(
            project.project_id,
            title="Concurrent terminal replay",
            workspace_mode="none",
        )
        attempt = store.create_attempt(
            item.work_item_id,
            provider="generic_concurrent_result",
            provider_run_id="host-run-concurrent-result",
            task="Process one terminal result exactly once.",
        )
        store.update_attempt(attempt.attempt_id, execution_status="orphaned")
        coordinator = WorkLedgerCoordinator(store)
        payload = {
            "provider": "generic_concurrent_result",
            "run_id": "host-run-concurrent-result",
            "status": "done",
            "result": "generic concurrent terminal result",
            "error": "",
            "metadata": {},
        }
        publish_started = asyncio.Event()
        release_publish = asyncio.Event()
        publish_calls = 0

        async def blocking_publish(*, reason: str) -> dict:
            nonlocal publish_calls
            del reason
            publish_calls += 1
            publish_started.set()
            await release_publish.wait()
            return {}

        with patch.object(coordinator, "publish_snapshot", side_effect=blocking_publish):
            first = asyncio.create_task(
                coordinator._on_provider_result("provider.result", payload)
            )
            await asyncio.wait_for(publish_started.wait(), timeout=1.0)
            second = asyncio.create_task(
                coordinator._on_provider_result("provider.result", payload)
            )
            await asyncio.sleep(0)
            assert publish_calls == 1
            assert second.done() is False
            release_publish.set()
            await asyncio.wait_for(asyncio.gather(first, second), timeout=2.0)

        stored = store.get_attempt(attempt.attempt_id)
        assert stored is not None
        assert publish_calls == 1
        assert len(store.list_completions(item.work_item_id)) == 1
        assert stored.metadata[PROVIDER_TERMINAL_PIPELINE_METADATA_KEY]["state"] == (
            "completed"
        )
        coordinator.close()

    with tempfile.TemporaryDirectory(prefix="provider-terminal-concurrent-") as temp:
        asyncio.run(scenario(Path(temp)))
