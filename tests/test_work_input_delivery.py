from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3
from threading import Barrier
from unittest.mock import patch

import pytest

from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderInputDelivery, ProviderRunRequest
from agent_host.work_ledger_store import SCHEMA_VERSION, WorkLedgerConflict, WorkLedgerStore
from server.handlers.work_ledger_handler import WorkLedgerHandler
from server.protocol import Method
from server.work_ledger_coordinator import WorkLedgerCoordinator
from test_provider_input_delivery import _InputAdapter


def _seed(store, root, *, run_id="run-first"):
    root.mkdir(exist_ok=True)
    project = store.create_or_get_project(root)
    item = store.create_work_item(project.project_id, title="Current goal", goal="Original goal")
    attempt = store.create_attempt(item.work_item_id, provider="input-test", task="Original instruction", provider_run_id=run_id)
    store.update_attempt(attempt.attempt_id, execution_status="running")
    return {"input_id": "message-1", "work_item_id": item.work_item_id, "run_id": run_id, "text": "CPU only"}


def test_input_acceptance_is_durable_idempotent_and_does_not_amend_work(tmp_path):
    database = tmp_path / "ledger.sqlite3"
    with WorkLedgerStore(database) as store:
        params = _seed(store, tmp_path / "project")
        original = store.get_work_item(params["work_item_id"]).to_dict()
        receipt, created = store.accept_provider_input(**params)
        assert created and receipt["state"] == "unknown"
        attempt_id = receipt["attempt_id"]
        assert len(store.list_operations(params["work_item_id"])) == 1
        assert store.get_work_item(params["work_item_id"]).to_dict() == original
    with WorkLedgerStore(database) as store:
        replay, created = store.accept_provider_input(**params)
        assert not created and replay == receipt
        with pytest.raises(WorkLedgerConflict):
            store.accept_provider_input(**{**params, "text": "Different fact"})
        delivered = store.finish_provider_input(params["input_id"], state="delivered")
        assert store.finish_provider_input(params["input_id"], state="rejected")["state"] == "delivered"
        store.update_attempt(attempt_id, execution_status="succeeded")
        assert store.accept_provider_input(**params) == (delivered, False)
        with pytest.raises(WorkLedgerConflict):
            store.accept_provider_input(**{**params, "input_id": "late-message"})
        assert store.list_provider_inputs(params["work_item_id"]) == [delivered]


def test_competing_connections_accept_only_one_delivery_attempt(tmp_path):
    database = tmp_path / "ledger.sqlite3"
    with WorkLedgerStore(database) as store:
        params = _seed(store, tmp_path / "project")
    gate = Barrier(2)
    def accept():
        with WorkLedgerStore(database) as store:
            gate.wait(timeout=5)
            return store.accept_provider_input(**params)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: accept(), range(2)))
    assert sorted(created for _, created in results) == [False, True]
    assert results[0][0] == results[1][0]


def test_v8_upgrade_keeps_existing_attempts_and_rolls_back_failed_input_ddl(tmp_path):
    database = tmp_path / "ledger.sqlite3"
    with WorkLedgerStore(database) as store:
        params = _seed(store, tmp_path / "project")
        before = store.list_attempts(params["work_item_id"])[0].to_dict()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE provider_inputs")
        connection.execute("PRAGMA user_version=8")
    connect = sqlite3.connect
    opened = []
    def fail_index(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_authorizer(lambda action, first, *_: sqlite3.SQLITE_DENY
                                 if action == sqlite3.SQLITE_CREATE_INDEX and first == "idx_provider_inputs_attempt"
                                 else sqlite3.SQLITE_OK)
        opened.append(connection)
        return connection
    try:
        with patch("agent_host.work_ledger_store.sqlite3.connect", fail_index):
            with pytest.raises(sqlite3.DatabaseError):
                WorkLedgerStore(database)
    finally:
        for connection in opened:
            connection.close()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='provider_inputs'").fetchone() is None
    with WorkLedgerStore(database) as store:
        assert store.schema_version == SCHEMA_VERSION
        assert store.list_attempts(params["work_item_id"])[0].to_dict() == before
        assert store.list_provider_inputs(params["work_item_id"]) == []


def test_real_host_runtime_path_keeps_goal_and_delivers_each_message(tmp_path):
    async def scenario():
        store = WorkLedgerStore(tmp_path / "ledger.sqlite3")
        coordinator = WorkLedgerCoordinator(store)
        coordinator.configure()
        runtime = ProviderRuntime()
        runtime.set_request_preparer(coordinator.prepare_request)
        adapter = _InputAdapter()
        adapter.acknowledge.set()
        runtime.register(adapter)
        handler = WorkLedgerHandler(coordinator, provider_input=runtime.append_input)
        run = None
        try:
            run = await runtime.start(ProviderRunRequest(provider=adapter.provider_id, task="Original goal", cwd=str(tmp_path)))
            await asyncio.wait_for(adapter.started.wait(), 2)
            await coordinator.drain_provider_facts()
            attempt = store.get_attempt_by_provider_run(run.run_id)
            work_id = attempt.work_item_id
            before = deepcopy((store.get_work_item(work_id).to_dict(), [o.to_dict() for o in store.list_operations(work_id)]))
            for index, text in enumerate(("Only CPU is available.", "No internet is available.")):
                params = {"work_item_id": work_id, "run_id": run.run_id, "input_id": f"input-{index}", "text": text}
                result = await handler.handle(Method.WORK_INPUT, params)
                assert result["input"]["state"] == "unknown"
                await handler.drain_inputs()
                assert store.list_provider_inputs(work_id)[-1]["state"] == "delivered"
                assert (await handler.handle(Method.WORK_INPUT, params))["replayed"] is True
            assert [text for _, text in adapter.inputs] == ["Only CPU is available.", "No internet is available."]
            assert (store.get_work_item(work_id).to_dict(), [o.to_dict() for o in store.list_operations(work_id)]) == before
            assert len(store.list_attempts(work_id)) == len(runtime.list_runs()) == 1
            assert len(coordinator.detail(work_id)["providerInputs"]) == 2
            with pytest.raises(WorkLedgerConflict):
                await handler.handle(Method.WORK_INPUT, {**params, "input_id": "wrong-target", "work_item_id": "other-work"})
            assert len(adapter.inputs) == 2
        finally:
            adapter.finish.set()
            if run is not None:
                await asyncio.wait_for(run.task_handle, 5)
            await coordinator.drain_provider_facts()
            coordinator.close()
    with patch("config.settings.WORK_WORKTREE_ISOLATION", False):
        asyncio.run(scenario())


def test_acceptance_releases_fifo_and_host_delivery_survives_the_request(tmp_path):
    async def scenario():
        store = WorkLedgerStore(tmp_path / "ledger.sqlite3")
        params = _seed(store, tmp_path / "project")
        coordinator = WorkLedgerCoordinator(store)
        entered = asyncio.Event()
        approval = asyncio.Event()
        calls = []
        async def interrupted(run_id, text):
            calls.append((run_id, text))
            assert store.list_provider_inputs(params["work_item_id"])[0]["state"] == "unknown"
            entered.set()
            await approval.wait()
            return ProviderInputDelivery("delivered")
        handler = WorkLedgerHandler(coordinator, provider_input=interrupted)
        accepted = await asyncio.wait_for(handler.handle(Method.WORK_INPUT, params), 2)
        assert accepted["input"]["state"] == "unknown"
        await asyncio.wait_for(entered.wait(), 2)
        # The next ordinary request can now release the Provider's approval.
        replay = await handler.handle(Method.WORK_INPUT, params)
        assert replay["replayed"] and replay["input"]["state"] == "unknown"
        approval.set()
        await asyncio.wait_for(handler.drain_inputs(), 2)
        assert store.list_provider_inputs(params["work_item_id"])[0]["state"] == "delivered"
        assert len(calls) == 1
        coordinator.close()
    asyncio.run(scenario())


def test_unsupported_delivery_is_a_saved_refusal_not_a_replacement(tmp_path):
    async def scenario():
        with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
            params = _seed(store, tmp_path / "project")
            async def unsupported(run_id, text):
                return ProviderInputDelivery("rejected", "append_input_not_supported")
            handler = WorkLedgerHandler(WorkLedgerCoordinator(store), provider_input=unsupported)
            result = await handler.handle(Method.WORK_INPUT, params)
            assert result["input"]["state"] == "unknown"
            await handler.drain_inputs()
            assert store.list_provider_inputs(params["work_item_id"])[0]["state"] == "rejected"
            assert len(store.list_attempts(params["work_item_id"])) == 1
    asyncio.run(scenario())
