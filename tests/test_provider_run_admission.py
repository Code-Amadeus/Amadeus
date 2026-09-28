"""A configured Work intake binds Runtime identity before external execution.

Real Store/Coordinator/Runtime/EventBus, fake external adapter only. A bound
outer run ID is not proof of native submission, outcome or user acceptance.
"""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_host.provider_contract import ProviderCapabilities, ProviderManifest
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderRunRequest, ProviderRunResult
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.event_bus import bus
from server.protocol import Method
from server.work_ledger_coordinator import WorkLedgerCoordinator


class _Adapter:
    provider_id = "admission_test"
    manifest = ProviderManifest(
        provider_id=provider_id, display_name="Admission test adapter",
        capabilities=ProviderCapabilities(task_kinds=("general",),
            workspace_access="none", workspace_ownership="none"),
    )

    def __init__(self, store):
        self.store = store
        self.entries = []
        self.crash = False
        self.result_status = "done"

    async def run(self, request, run_id, emit):
        attempt = self.store.get_attempt(request.metadata["work"]["attempt_id"])
        self.entries.append(dict(run_id=run_id, attempt_id=attempt.attempt_id,
                                 durable_run_id=attempt.provider_run_id))
        if self.crash:
            print(json.dumps(self.entries[-1]), flush=True)
            os._exit(77)
        return ProviderRunResult(status=self.result_status, result="Fake adapter finished",
                                 error="Fake failure" if self.result_status == "failed" else "")

    async def cancel(self, run_id):
        return dict(confirmed=True, cancelled=True)


@asynccontextmanager
async def _host(path, *, subscribe=True):
    with WorkLedgerStore(path / "work.sqlite3") as store:
        coordinator = WorkLedgerCoordinator(store)
        runtime = ProviderRuntime()
        adapter = _Adapter(store)
        runtime.register(adapter)
        runtime.set_request_preparer(coordinator.prepare_request)
        if subscribe:
            coordinator.configure()
        try:
            with patch("config.settings.WORK_WORKTREE_ISOLATION", False):
                yield SimpleNamespace(store=store, coordinator=coordinator, runtime=runtime, adapter=adapter)
        finally:
            await asyncio.gather(*(record.task_handle for record in runtime._runs.values()
                                   if record.task_handle is not None), return_exceptions=True)
            await runtime.close()
            await coordinator.drain_provider_facts()
            coordinator.close()


def _request(**metadata):
    return ProviderRunRequest(provider=_Adapter.provider_id, task="Check durable run identity",
                              metadata={"source": "admission-test", "session_id": "admission-session", **metadata})


async def _finish(host, request=None):
    record = await host.runtime.start(request or _request())
    assert record.task_handle is not None
    await record.task_handle
    return record


async def test_identity_is_prepared_without_event_subscription(tmp_path):
    async with _host(tmp_path, subscribe=False) as host:
        record = await _finish(host)
        assert host.adapter.entries[0]["durable_run_id"] == record.run_id
        attempt = host.store.get_attempt(host.adapter.entries[0]["attempt_id"])
        # Without lifecycle ingestion only preparation is known to the Store.
        assert attempt.execution_status == "queued"


async def test_late_binding_update_is_not_the_execution_gate(tmp_path):
    async with _host(tmp_path) as host:
        host.store._connection.execute("""CREATE TEMP TRIGGER reject_late_binding
            BEFORE UPDATE OF provider_run_id ON run_attempts
            WHEN NEW.provider_run_id<>'' AND OLD.provider_run_id=''
            BEGIN SELECT RAISE(ABORT,'late binding unavailable'); END""")
        record = await _finish(host)
        assert host.adapter.entries[0]["durable_run_id"] == record.run_id


async def test_initial_binding_failure_aborts_before_adapter(tmp_path):
    async with _host(tmp_path) as host:
        host.store._connection.execute("""CREATE TEMP TRIGGER reject_initial_binding
            BEFORE INSERT ON run_attempts WHEN NEW.provider_run_id<>''
            BEGIN SELECT RAISE(ABORT,'initial binding unavailable'); END""")
        with pytest.raises(WorkLedgerConflict):
            await host.runtime.start(_request())
        assert host.adapter.entries == []
        assert host.runtime.list_runs() == []
        for table in ("work_items", "work_operations", "run_attempts"):
            assert host.store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


async def test_presentation_failure_does_not_refuse_a_bound_run(tmp_path):
    async def fail(_method, params):
        if params.get("provider") == _Adapter.provider_id and params.get("type") == "run.created":
            raise RuntimeError("presentation failed")

    async with _host(tmp_path) as host:
        bus.on(Method.PROVIDER_EVENT, fail)
        try:
            record = await _finish(host)
            assert record.status == "done"
            assert host.adapter.entries[0]["durable_run_id"] == record.run_id
        finally:
            bus.off(Method.PROVIDER_EVENT, fail)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_preparer_receives_exact_runtime_identity_before_record_exists(tmp_path, asynchronous):
    async with _host(tmp_path) as host:
        seen = []

        def prepare(request, run_id):
            assert host.runtime.get_run(run_id) is None
            prepared = host.coordinator.prepare_request(request, run_id)
            attempt = host.store.get_attempt(prepared.metadata["work"]["attempt_id"])
            seen.append((run_id, attempt.provider_run_id))
            return prepared

        async def prepare_async(request, run_id):
            return prepare(request, run_id)

        host.runtime.set_request_preparer(prepare_async if asynchronous else prepare)
        record = await _finish(host)
        assert seen == [(record.run_id, record.run_id)]


async def test_request_metadata_cannot_choose_the_runtime_binding(tmp_path):
    async with _host(tmp_path) as host:
        request = _request(run_id="forged-outer", provider_run_id="forged-top",
                           work={"provider_run_id": "forged-work", "attempt_id": "forged-attempt"})
        record = await _finish(host, request)
        entry = host.adapter.entries[0]
        assert entry["durable_run_id"] == record.run_id
        assert entry["attempt_id"] != "forged-attempt"
        assert all(host.store.get_attempt_by_provider_run(value) is None
                   for value in ("forged-outer", "forged-top", "forged-work"))


@pytest.mark.parametrize("continuation", ["amend", "retry", "steer_replacement"])
async def test_later_instruction_and_retry_bind_their_own_runs(tmp_path, continuation):
    async with _host(tmp_path) as host:
        if continuation == "retry":
            host.adapter.result_status = "failed"
        elif continuation == "steer_replacement":
            host.adapter.result_status = "cancelled"
        first = await _finish(host)
        host.adapter.result_status = "done"
        previous = host.store.get_attempt(host.adapter.entries[0]["attempt_id"])
        request = _request(continuation=continuation, work={"work_item_id": previous.work_item_id})
        request.task = "A new instruction" if continuation == "amend" else previous.task
        if continuation != "amend":
            key = "retry_of" if continuation == "retry" else "replaces_attempt_id"
            request.metadata[key] = previous.attempt_id
        second = await _finish(host, request)
        current = host.store.get_attempt(host.adapter.entries[-1]["attempt_id"])
        assert previous.provider_run_id == first.run_id
        assert current.provider_run_id == second.run_id != first.run_id
        assert current.work_item_id == previous.work_item_id
        assert (current.operation_id == previous.operation_id) == (continuation != "amend")


async def test_binding_collision_rolls_back_new_work_instead_of_starting_sibling(tmp_path):
    async with _host(tmp_path) as host:
        first = await _finish(host)
        original = host.coordinator.prepare_request

        def corrupt_identity(request, _run_id):
            # Host integration defect injection, not accepted user authority.
            return original(request, first.run_id)

        host.runtime.set_request_preparer(corrupt_identity)
        with pytest.raises(WorkLedgerConflict):
            await host.runtime.start(_request())
        assert len(host.adapter.entries) == 1
        assert len(host.store.list_work_items()) == 1
        assert len(host.runtime.list_runs()) == 1


async def test_cancellation_after_durable_intake_preserves_exact_run_identity(tmp_path):
    async with _host(tmp_path) as host:
        entered, release = Event(), Event()
        prepared_ids = []

        def prepare(request, run_id):
            result = host.coordinator.prepare_request(request, run_id)
            prepared_ids.append((run_id, result.metadata["work"]["attempt_id"]))
            entered.set()
            assert release.wait(timeout=5)
            return result

        host.runtime.set_request_preparer(prepare)
        start = asyncio.create_task(host.runtime.start(_request()))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            start.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await start
        run_id, attempt_id = prepared_ids[0]
        attempt = host.store.get_attempt(attempt_id)
        assert attempt.provider_run_id == run_id
        assert attempt.execution_status == "cancelled"
        assert host.runtime.get_run(run_id).status == "cancelled"
        assert host.adapter.entries == []


def test_standalone_preparation_retains_unknown_run_identity(tmp_path):
    with WorkLedgerStore(tmp_path / "work.sqlite3") as store:
        coordinator = WorkLedgerCoordinator(store)
        request = _request(provider_manifest=_Adapter.manifest.to_dict())
        prepared = coordinator.prepare_request(request)
        attempt = store.get_attempt(prepared.metadata["work"]["attempt_id"])
        assert attempt.provider_run_id == ""


_CRASH_CHILD = r'''
import asyncio, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_provider_run_admission import _host, _request
root, stage = sys.argv[1:]
async def main():
    async with _host(Path(root)) as host:
        if stage == "after_preparer":
            def prepare(request, run_id):
                result = host.coordinator.prepare_request(request, run_id)
                attempt = host.store.get_attempt(result.metadata["work"]["attempt_id"])
                print(json.dumps(dict(run_id=run_id, attempt_id=attempt.attempt_id,
                    durable_run_id=attempt.provider_run_id)), flush=True)
                os._exit(77)
            host.runtime.set_request_preparer(prepare)
        else:
            host.adapter.crash = True
            if stage == "late_update_abort":
                host.store._connection.execute("CREATE TEMP TRIGGER reject_update BEFORE UPDATE OF provider_run_id ON run_attempts WHEN NEW.provider_run_id<>'' AND OLD.provider_run_id='' BEGIN SELECT RAISE(ABORT,'late failure'); END")
        record = await host.runtime.start(_request())
        await record.task_handle
asyncio.run(main())
raise RuntimeError("crash point not reached")
'''


@pytest.mark.parametrize("stage", ["after_preparer", "adapter_entry", "late_update_abort"])
def test_real_process_exit_keeps_the_binding_before_and_after_adapter_entry(tmp_path, stage):
    result = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-c", _CRASH_CHILD, str(tmp_path), stage],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf-8", timeout=30,
        env=dict(os.environ, PYTHONUTF8="1", AMADEUS_SESSION_DIR=str(tmp_path / "child-session")),
    )
    assert result.returncode == 77, result.stdout + result.stderr
    entry = next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"run_id"'))
    assert entry["run_id"] == entry["durable_run_id"]
    with WorkLedgerStore(tmp_path / "work.sqlite3") as store:
        attempt = store.get_attempt(entry["attempt_id"])
        assert attempt.provider_run_id == entry["run_id"]
        assert attempt.execution_status == ("queued" if stage == "after_preparer" else "running")
