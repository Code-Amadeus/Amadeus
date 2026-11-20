import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.backend_cleanup import BackendCleanup, LAUNCH_ENV, SCHEMA, TrackingJob, begin_from_environment
from agent_host.provider_runtime import ProviderRuntime
from agent_host.adapters.pi import PiAdapter


class FakeKernel:
    def __init__(self, *, assign=True, active=1, query=True):
        self.assign, self.active, self.query = assign, active, query
        self.closed = []

    def CreateJobObjectW(self, security, name):
        assert security is None
        self.name = name
        return 81

    def GetCurrentProcess(self):
        return 17

    def AssignProcessToJobObject(self, handle, process):
        assert (handle, process) == (81, 17)
        return self.assign

    def QueryInformationJobObject(self, handle, kind, output, size, length):
        assert (handle, kind, length) == (81, 1, None)
        output._obj.ActiveProcesses = self.active
        return self.query

    def CloseHandle(self, handle):
        self.closed.append(handle)


def test_job_only_tracks_without_termination_or_breakaway_flags():
    kernel = FakeKernel()
    job = TrackingJob("Global\\fixture", kernel=kernel)
    assert job.only_backend_remains()
    kernel.active = 2  # Includes grandchildren in nested Provider/MCP jobs.
    assert not job.only_backend_remains()
    kernel.query = False
    assert not job.only_backend_remains()
    job.close()
    assert kernel.closed == [81]
    assert not job.only_backend_remains()


def test_job_assignment_failure_is_not_accepted_as_empty():
    kernel = FakeKernel(assign=False)
    with pytest.raises(OSError, match="assign"):
        TrackingJob("Global\\fixture", kernel=kernel)
    assert kernel.closed == [81]


def launch_fixture(tmp_path):
    owner = {"id": "a" * 32, "nonce": "synthetic-owner-nonce",
             "project_root": str(Path(__file__).resolve().parents[1]), "python_command": sys.executable}
    index = tmp_path / "backend-cleanup.json"
    index.write_text(json.dumps({"schema": SCHEMA, "unknown": False, "owners": [owner]}))
    Path(str(index) + ".receipts").mkdir()
    return {"schema": SCHEMA, "state_path": str(index), "owner": owner}


def proof_fixture(tmp_path, *, active=1, identity="133000000000000000"):
    launch = launch_fixture(tmp_path)
    job = TrackingJob("Global\\fixture", kernel=FakeKernel(active=active))
    proof = BackendCleanup(launch, job_factory=lambda name: job, identity_factory=lambda: identity,
                           observer_factory=lambda proof: None)
    assert proof.failure is None
    return proof


@pytest.mark.parametrize("providers,work,teardown", [(False, True, True), (True, False, True), (True, True, False)])
def test_unknown_or_failed_cleanup_cannot_publish(tmp_path, providers, work, teardown):
    proof = proof_fixture(tmp_path)
    proof.record_runtime_cleanup(providers_settled=providers, work_settled=work, teardown_succeeded=teardown)
    assert not proof.finish()
    assert not proof.approval.exists()


def test_surviving_process_blocks_receipt_without_being_killed(tmp_path):
    proof = proof_fixture(tmp_path, active=2)
    proof.record_runtime_cleanup(providers_settled=True, work_settled=True, teardown_succeeded=True)
    assert not proof.finish()
    assert proof.job.kernel.closed == []
    assert not proof.approval.exists()


def test_success_receipt_binds_launch_and_root_creation_identity(tmp_path):
    proof = proof_fixture(tmp_path)
    proof.record_runtime_cleanup(providers_settled=True, work_settled=True, teardown_succeeded=True)
    assert proof.finish()
    receipt = json.loads(proof.approval.read_text())
    assert receipt["owner"] == proof.launch["owner"]
    assert receipt["process"] == {"pid": os.getpid(), "created": "133000000000000000"}
    assert receipt["job_name"] == "Global\\AmadeusBackendCleanup-" + "a" * 32
    tracking = json.loads(proof.receipt.with_suffix(".tracking.json").read_text())
    assert tracking == {"schema": SCHEMA, "owner": proof.launch["owner"],
                        "process": receipt["process"], "job_name": proof.job_name}
    assert proof.job.handle is not None  # Lifetime handle stays open until exit.
    with pytest.raises(OSError):
        proof.finish()  # An existing receipt is never overwritten.


def test_failed_containment_preserves_registration_and_blocks_receipt(tmp_path):
    launch = launch_fixture(tmp_path)
    def unavailable(name):
        raise OSError("nested Job assignment unavailable")
    proof = BackendCleanup(launch, job_factory=unavailable)
    assert proof.failure == "OSError"
    proof.record_runtime_cleanup(providers_settled=True, work_settled=True, teardown_succeeded=True)
    assert not proof.finish()
    assert Path(launch["state_path"]).exists()
    assert not Path(str(launch["state_path"]) + ".receipts", "a" * 32 + ".tracking.json").exists()


def test_failed_assignment_never_claims_that_an_empty_job_contained_the_backend(tmp_path):
    launch = launch_fixture(tmp_path)
    job = TrackingJob("Global\\fixture", kernel=FakeKernel(assign=False), assign=False)
    proof = BackendCleanup(launch, job_factory=lambda name: job,
                           identity_factory=lambda: "133000000000000000", observer_factory=lambda proof: None)
    assert proof.failure == "OSError"
    assert not proof.receipt.with_suffix(".tracking.json").exists()
    assert not proof.finish()


def test_environment_is_consumed_before_any_child_can_inherit_it(tmp_path, monkeypatch):
    launch = launch_fixture(tmp_path)
    monkeypatch.setenv(LAUNCH_ENV, json.dumps(launch))
    monkeypatch.setattr("server.backend_cleanup.BackendCleanup", lambda item: item)
    assert begin_from_environment() == launch
    assert LAUNCH_ENV not in os.environ


@pytest.mark.asyncio
async def test_provider_close_reports_adapter_failure_without_skipping_other_owners():
    runtime = ProviderRuntime()
    closed = []
    async def broken():
        raise RuntimeError("synthetic close failure")
    async def healthy():
        closed.append(True)
    runtime._adapters = {"broken": SimpleNamespace(close=broken), "healthy": SimpleNamespace(close=healthy)}
    result = await runtime.close()
    assert not result["settled"]
    assert result["errors"] == ["adapter_close_failed:broken"]
    assert closed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running", "orphaned"])
async def test_provider_close_does_not_invent_terminal_work(status):
    runtime = ProviderRuntime()
    runtime._runs["run"] = SimpleNamespace(run_id="run", status=status, task_handle=None, terminal_publication_task=None)
    result = await runtime.close()
    assert not result["settled"]
    assert result["pending_runs"] == ["run"]


@pytest.mark.asyncio
async def test_provider_terminal_status_is_insufficient_while_execution_finally_is_running():
    runtime = ProviderRuntime()
    wait = asyncio.Event()
    task = asyncio.create_task(wait.wait())
    runtime._runs["run"] = SimpleNamespace(run_id="run", status="cancelled", task_handle=task, terminal_publication_task=None)
    try:
        assert not (await runtime.close())["settled"]
        wait.set()
        await task
        assert (await runtime.close())["settled"]
    finally:
        wait.set()
        await task


@pytest.mark.asyncio
async def test_pi_close_does_not_swallow_cancel_or_transport_failure():
    adapter = PiAdapter.__new__(PiAdapter)
    async def failed_close():
        raise OSError("synthetic transport close failure")
    async def unconfirmed(key):
        return {"confirmed": False}
    adapter._runs = {"run": SimpleNamespace(client=SimpleNamespace(close=failed_close))}
    adapter.cancel = unconfirmed
    assert await adapter.close() == {"settled": False, "errors": ["pi_cancel_unconfirmed", "pi_close_failed"]}
