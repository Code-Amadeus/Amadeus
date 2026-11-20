"""Automatic approval preserves the Cooperative owner's manual permission boundary."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from config import settings
from core import session_manager as sm
from agent_host.provider_types import ProviderEvent
from agent_host.work_ledger_store import WorkLedgerStore
from server.cooperative_chat_ingress import CooperativeChatManager
from server.event_bus import bus, EventBus
from server.protocol import Method
import test_cooperative_context_recovery as fixture_module


@pytest.fixture
async def approval_host(tmp_path, monkeypatch):
    monkeypatch.setattr(fixture_module, "NativeFixture", fixture_module.InteractiveNativeFixture)
    factory = fixture_module.host_factory.__wrapped__(tmp_path, monkeypatch)
    host = factory()
    host.adapter.release.clear()
    monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", False)
    manager = object.__new__(CooperativeChatManager)
    manager._closed = False
    manager.runtime = host.runtime
    manager.ingresses = {"conversation-A": host.ingress}
    manager.permission_policy = "ask"
    manager.permission_store = WorkLedgerStore(host.ledger.path)
    manager.permission_receipts = []

    async def start(policy, access, automatic):
        manager.permission_policy = policy
        host.loop.context_requirements[host.adapter.provider_id] = replace(
            host.loop.context_requirements[host.adapter.provider_id], workspace_access=access)
        await host.send("Inspect synthetic status", "approval-start")
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        child = host.loop.get_context(host.loop.bound_context_id)
        assert child.requirements.workspace_access == access
        record = host.runtime.get_run(child.run_id)
        monkeypatch.setattr(bus, "emit", EventBus.emit.__get__(bus, EventBus))
        bus.on(Method.PROVIDER_EVENT, manager._handle_provider_event)
        host.runtime.set_automatic_permission_authorizer(manager.authorize_automatic_permission)
        monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", automatic)
        return record

    try:
        yield SimpleNamespace(host=host, manager=manager, start=start)
    finally:
        bus.off(Method.PROVIDER_EVENT, manager._handle_provider_event)
        host.adapter.release.set()
        await host.close()
        manager.permission_store.close()


def permission_event(record, capability):
    return ProviderEvent(provider=record.provider, run_id=record.run_id,
        type="permission.requested", payload={"permissionRequest": {
            "request_id": "synthetic-call", "capability": capability,
            "action": "synthetic_action", "options": ["allow_once", "deny"]}})


@pytest.mark.parametrize("policy,access,capability,eligible", [
    ("deny", "write", "shell.execute", False),
    ("deny", "write", "mcp.tool.call", False),
    ("ask", "read", "shell.execute", False),
    ("ask", "read", "file.write", False),
    ("ask", "read", "tool.execute", False),
    ("ask", "read", "mcp.tool.call", True),
    ("ask", "write", "shell.execute", True),
])
@pytest.mark.parametrize("automatic", [False, True])
async def test_runtime_preserves_cooperative_permission_boundary(
        approval_host, monkeypatch, policy, access, capability, eligible, automatic):
    ctx = approval_host
    record = await ctx.start(policy, access, automatic)
    await ctx.host.runtime._emit(record, permission_event(record, capability))
    permission, = ctx.manager.permission_store.list_cooperative_permission_requests("conversation-A")
    expected = "allowed" if eligible and automatic else "pending" if eligible else "denied"
    assert permission.status == expected
    assert [response.allow for _, response in ctx.host.adapter.permission_responses] == (
        [True] if eligible and automatic else [] if eligible else [False])
    assert permission.metadata["resolution_policy"] == ("user_prompt" if eligible else "automatic_deny")
    if not eligible:
        assert permission.options == ["deny"]
        assert permission.metadata["resolution"] == "policy_denied"
        # Turning the switch on later must not revive a settled denial.
        monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", True)
        await ctx.host.runtime.apply_permission_policy()
        assert len(ctx.host.adapter.permission_responses) == 1
    elif automatic:
        assert permission.metadata["resolution"] == "policy_allowed"


@pytest.mark.parametrize("policy,access,capability,error", [
    ("deny", "write", "shell.execute", "cooperative_permission_interaction_unavailable"),
    ("deny", "write", "mcp.tool.call", "cooperative_permission_interaction_unavailable"),
    ("ask", "read", "shell.execute", "cooperative_permission_read_only_context"),
    ("ask", "read", "file.write", "cooperative_permission_read_only_context"),
])
async def test_existing_allow_option_cannot_override_current_owner_limits(
        approval_host, policy, access, capability, error):
    ctx = approval_host
    record = await ctx.start("ask", "write", False)
    await ctx.host.runtime._emit(record, permission_event(record, capability))
    permission, = ctx.manager.permission_store.list_cooperative_permission_requests("conversation-A")
    assert permission.status == "pending" and "allow_once" in permission.options
    # A previously offered option must not remain authority after owner limits change.
    ctx.manager.permission_policy = policy
    child = ctx.host.loop.get_context(permission.context_id)
    child.requirements = replace(child.requirements, workspace_access=access)
    manual = await ctx.manager.resolve_permission({"session_id": "conversation-A",
        "run_id": record.run_id, "provider_request_id": "synthetic-call",
        "permission_request_id": permission.request_id, "decision": "allow_once"})
    assert manual == {"ok": False, "error": error}
    assert ctx.manager.authorize_automatic_permission(record.run_id, "synthetic-call") is False
    assert ctx.manager.permission_store.get_permission_request(permission.request_id).status == "pending"
    assert ctx.host.adapter.permission_responses == []


async def test_hot_enable_approves_eligible_background_chat_once(approval_host, monkeypatch):
    ctx = approval_host
    record = await ctx.start("ask", "read", False)
    await ctx.host.runtime._emit(record, permission_event(record, "mcp.tool.call"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", "another-conversation")
    monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", True)
    await ctx.host.runtime.apply_permission_policy()
    await ctx.host.runtime.apply_permission_policy()
    permission, = ctx.manager.permission_store.list_cooperative_permission_requests("conversation-A")
    assert permission.status == "allowed"
    response, = ctx.host.adapter.permission_responses
    assert response[1].allow is True and response[1].automatic is True
