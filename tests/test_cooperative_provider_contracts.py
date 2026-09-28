"""Cooperative Host boundaries use existing Provider contracts, not Codex traits."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_host.provider_contract import ProviderCapabilities, ProviderManifest, ProviderRequirements
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import (ProviderNativeExecutionHandle, ProviderRunResult,
    ProviderSessionHandle, ProviderSubmissionReconciliationResult)
from server.control_ledger import ControlLedgerStore, ControlLedgerConflict
from server.cooperative_context_store import CooperativeContextStore
from server.cooperative_provider_loop import CooperativeProviderLoop, LoopConflict
from server.turn_admission import admission_transcript_hash


def assemble(database, adapter, requirements, allocate):
    ledger = ControlLedgerStore(database)
    runtime = ProviderRuntime()
    runtime.register(adapter)
    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        return (json.dumps({"say":"OK", "action":{"op":"send"}})
            if frame["source_kind"] == "user" else "OK")
    loop = CooperativeProviderLoop(runtime, query, allocate, provider=adapter.provider_id,
        context_requirements={adapter.provider_id:requirements}, publish=lambda event: True)
    loop.attach_state(CooperativeContextStore(ledger, "contracts"))
    async def send(text, key):
        ledger.open_admission(root_id="root-" + key, source_scope="chat:contracts", fence_scope="foreground",
            utterance_id=key, authority_mode="legacy", transcript_hash=admission_transcript_hash(text))
        receipt = await loop.submit(text, input_id=key)
        await loop.wait()
        return receipt
    async def close():
        try:
            await loop.close()
        finally:
            ledger.close()
    return SimpleNamespace(loop=loop, runtime=runtime, ledger=ledger, send=send, close=close)


def no_allocation(*args):
    raise AssertionError("this Provider does not use a Host filesystem workspace")


class AgentFixture:
    def __init__(self, ownership, access, resume):
        self.provider_id = "contract-agent"
        self.manifest = ProviderManifest(provider_id=self.provider_id, display_name="Contract agent",
            capabilities=ProviderCapabilities(workspace_access=access, workspace_ownership=ownership, resume=resume))
        self.requests = []
    async def run(self, request, run_id, emit):
        self.requests.append(request)
        handle = (request.session or ProviderSessionHandle(provider=self.provider_id,
            session_id="native-" + run_id, scope="interaction")) if self.manifest.capabilities.resume == "attach" else None
        return ProviderRunResult(status="done", result="OK", session=handle)


class StatelessFixture:
    def __init__(self, *, task_kinds=("general",), runtime_kind="agent", per_run_handles=False):
        self.provider_id = "stateless-agent"
        self.manifest = ProviderManifest(provider_id=self.provider_id, display_name="Stateless agent",
            runtime_kind=runtime_kind, capabilities=ProviderCapabilities(task_kinds=task_kinds,
                workspace_access="none", workspace_ownership="none", resume="none",
                submission_reconciliation="query"))
        self.per_run_handles = per_run_handles
        self.requests, self.inspections = [], []
        self.observation = ProviderSubmissionReconciliationResult(state="unavailable")

    async def run(self, request, run_id, emit):
        self.requests.append(request)
        handle = (ProviderSessionHandle(provider=self.provider_id,
            session_id="attempt-" + run_id, scope="attempt") if self.per_run_handles else None)
        return ProviderRunResult(status="done", result="OK", session=handle)

    async def reconcile_submission(self, request):
        self.inspections.append(request)
        return self.observation


@pytest.mark.parametrize("ownership,access,resume", [
    ("none", "none", "attach"), ("none", "none", "none"), ("provider", "write", "attach")])
async def test_non_host_workspace_and_declared_continuity_survive_reconstruction(tmp_path, ownership, access, resume):
    policy = ProviderRequirements(workspace_access=access, workspace_ownership=ownership, resume=resume)
    first_adapter = AgentFixture(ownership, access, resume)
    first = assemble(tmp_path/"host.sqlite3", first_adapter, policy, no_allocation)
    try:
        await first.send("First request", "first")
        child = first.loop.children[first.loop.bound_context_id]
        original_id, original_session = child.child_id, child.native_session
        assert child.workspace == "" and first_adapter.requests[0].cwd is None
        assert first_adapter.requests[0].metadata["workspace_binding"]["status"] == (
            "provider_pending" if ownership == "provider" else "not_required")
    finally:
        await first.close()
    second_adapter = AgentFixture(ownership, access, resume)
    second = assemble(tmp_path/"host.sqlite3", second_adapter, policy, no_allocation)
    try:
        await second.send("Follow-up", "second")
        assert second.loop.bound_context_id == original_id
        request, = second_adapter.requests
        assert request.cwd is None and request.requirements == policy
        assert request.session == original_session
        assert (request.session is None) == (resume == "none")
    finally:
        await second.close()


async def test_read_only_context_does_not_inherit_a_later_write_default(tmp_path):
    policy = ProviderRequirements(workspace_access="read", workspace_ownership="caller", resume="attach")
    first = assemble(tmp_path/"host.sqlite3", AgentFixture("caller", "write", "attach"), policy, lambda *_:tmp_path)
    try:
        await first.send("Inspect", "first")
    finally:
        await first.close()
    second_adapter = AgentFixture("caller", "write", "attach")
    second = assemble(tmp_path/"host.sqlite3", second_adapter, replace(policy, workspace_access="write"), no_allocation)
    try:
        await second.send("Inspect again", "second")
        assert second_adapter.requests[0].requirements.workspace_access == "read"
        child = second.loop.children[second.loop.bound_context_id]
        with pytest.raises(ControlLedgerConflict, match="checkpoint changed"):
            second.loop._state.checkpoint(replace(child, requirements=replace(policy, workspace_access="write")))
    finally:
        await second.close()


async def test_real_openclaw_adapter_uses_gateway_session_without_scratch(tmp_path):
    from agent_host.adapters.openclaw import OpenClawAdapter
    from test_openclaw_adapter import _FakeGatewayClient
    _FakeGatewayClient.configure("Gateway response")
    policy = ProviderRequirements(workspace_access="none", workspace_ownership="none", resume="attach")
    first = assemble(tmp_path/"host.sqlite3", OpenClawAdapter(gateway_client_factory=_FakeGatewayClient), policy, no_allocation)
    try:
        await first.send("First request", "first")
        child = first.loop.children[first.loop.bound_context_id]
        handle = child.native_session
        assert handle.provider == "openclaw" and handle.scope == "interaction"
    finally:
        await first.close()
    second = assemble(tmp_path/"host.sqlite3", OpenClawAdapter(gateway_client_factory=_FakeGatewayClient), policy, no_allocation)
    try:
        await second.send("Follow-up", "second")
        assert second.loop.children[child.child_id].native_session == handle
        requests = [row for client in _FakeGatewayClient.instances for row in client.requests]
        assert sum(method == "sessions.create" for method, _ in requests) == 1
        assert sum(method == "sessions.send" for method, _ in requests) == 2
        assert all(params["key"] == handle.session_id for method, params in requests
            if method in {"sessions.create", "sessions.send", "sessions.get"})
    finally:
        await second.close()


async def test_stateless_provider_reconciles_an_unresolved_run_without_a_session_handle(tmp_path):
    policy = ProviderRequirements(workspace_access="none", workspace_ownership="none", resume="none")
    first = assemble(tmp_path/"host.sqlite3", StatelessFixture(), policy, no_allocation)
    try:
        await first.send("First request", "first")
        child = first.loop.children[first.loop.bound_context_id]
        first.loop._save_child(child, run_id="stateless-host-run", run_status="orphaned",
            native_session=None)
        child_id = child.child_id
    finally:
        await first.close()

    adapter = StatelessFixture()
    adapter.observation = ProviderSubmissionReconciliationResult(state="matched_terminal",
        execution=ProviderNativeExecutionHandle(provider=adapter.provider_id,
            execution_id="stateless-native-result"),
        terminal_result=ProviderRunResult(status="done", result="Recovered by run id"))
    second = assemble(tmp_path/"host.sqlite3", adapter, policy, no_allocation)
    try:
        receipt = await second.loop.reconcile_restored_context(child_id)
        assert receipt == {"state":"matched_terminal", "promoted":True, "reason":""}
        inspection, = adapter.inspections
        assert inspection.run_id == "stateless-host-run" and inspection.session is None
        restored = second.loop.children[child_id]
        assert restored.run_status == "done" and restored.native_session is None
        assert not adapter.requests and not second.runtime.list_runs()
    finally:
        await second.close()


async def test_non_attach_provider_keeps_only_the_latest_run_handle(tmp_path):
    policy = ProviderRequirements(workspace_access="none", workspace_ownership="none", resume="none")
    adapter = StatelessFixture(per_run_handles=True)
    host = assemble(tmp_path/"host.sqlite3", adapter, policy, no_allocation)
    try:
        await host.send("First request", "first")
        child = host.loop.children[host.loop.bound_context_id]
        first_handle = child.native_session
        await host.send("Second request", "second")
        assert [request.session for request in adapter.requests] == [None, None]
        assert child.native_session is not None and child.native_session != first_handle
        assert child.native_session.scope == "attempt"
    finally:
        await host.close()


async def test_restored_context_rejects_a_changed_provider_capability_before_dispatch(tmp_path):
    policy = ProviderRequirements(task_kind="general", workspace_access="none",
        workspace_ownership="none", resume="none")
    first = assemble(tmp_path/"host.sqlite3", StatelessFixture(), policy, no_allocation)
    try:
        await first.send("First request", "first")
        child = first.loop.children[first.loop.bound_context_id]
        original = (child.run_id, child.run_status, child.revision)
    finally:
        await first.close()

    changed = StatelessFixture(task_kinds=("research",))
    second = assemble(tmp_path/"host.sqlite3", changed, policy, no_allocation)
    try:
        receipt = await second.send("Follow-up", "second")
        assert receipt["state"] == "rejected"
        assert receipt["reason"] == "provider_context_contract_unavailable"
        assert receipt["details"] == ["task_kind:general"]
        restored = second.loop.children[child.child_id]
        assert (restored.run_id, restored.run_status, restored.revision) == original
        assert not changed.requests and not second.runtime.list_runs()
    finally:
        await second.close()


async def test_stateful_tool_requires_its_existing_operation_compiler(tmp_path):
    policy = ProviderRequirements(task_kind="browser", workspace_access="none",
        workspace_ownership="none", resume="none")
    tool = StatelessFixture(task_kinds=("browser",), runtime_kind="stateful_tool")
    host = assemble(tmp_path/"host.sqlite3", tool, policy, no_allocation)
    try:
        with pytest.raises(LoopConflict, match="runtime_kind:natural_language_conversation"):
            await host.send("Open the page", "first")
        assert not host.loop.children and not tool.requests
    finally:
        await host.close()
