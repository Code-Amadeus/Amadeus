"""Cooperative durable approvals preserve exact ownership and policy audit."""

import asyncio
from unittest.mock import AsyncMock

from config import settings
import pytest
from agent_host.work_ledger_store import WorkLedgerStore
from server.cooperative_chat_ingress import CooperativeChatManager
from server.protocol import Method
from test_cooperative_context_recovery import host_factory as _host_factory


@pytest.fixture
def host_factory(tmp_path, monkeypatch):
    return _host_factory.__wrapped__(tmp_path, monkeypatch)


async def _manager(host_factory):
    host = host_factory()
    host.adapter.release.clear()
    await host.send("Inspect status", "scope-start")
    await asyncio.wait_for(host.adapter.started.wait(), 2)
    child = host.loop.children[host.loop.bound_context_id]
    manager = object.__new__(CooperativeChatManager)
    manager.runtime = host.runtime
    manager.ingresses = {"conversation-A": host.ingress}
    manager.permission_policy = "ask"
    manager.permission_store = WorkLedgerStore(host.ledger.path)
    manager.permission_receipts = []
    manager.runtime.resolve_permission = AsyncMock(return_value={"accepted": True})
    record = host.runtime.get_run(child.run_id)
    return host, manager, record


def _event(record, event_type, payload):
    return {"provider": record.provider, "run_id": record.run_id, "type": event_type,
        "metadata": dict(record.metadata), "payload": payload}


async def test_cooperative_grant_uses_revoke_and_expiry_are_durable(host_factory):
    host, manager, record = await _manager(host_factory)
    call = {"connectionId": "peer", "toolName": "status", "arguments": {"images": True}}
    try:
        await manager._handle_provider_event(Method.PROVIDER_EVENT,
            _event(record, "permission.requested", {"permissionRequest": {
                "request_id": "grant-first", "capability": "mcp.tool.call", "action": "call_tool",
                "options": ["allow_once", "allow_work", "deny"], "mcpCall": call}}))
        permission, = manager.permission_store.list_cooperative_permission_requests("conversation-A")
        assert permission.options == ["allow_once", "allow_work", "deny"]
        assert permission.metadata["mcp_call"] == call
        action = {"session_id": "conversation-A", "run_id": record.run_id,
            "provider_request_id": "grant-first", "permission_request_id": permission.request_id}
        allowed = await manager.resolve_permission({**action, "decision": "allow_work"})
        assert allowed["ok"] is True, allowed
        assert manager.runtime.resolve_permission.await_args.args[1].scope == "work"
        await manager._handle_provider_event(Method.PROVIDER_EVENT, _event(record, "permission.used",
            {"request_id": "grant-first", "call_request_id": "repeat", "automatic": True}))
        stored = manager.permission_store.get_permission_request(permission.request_id)
        assert stored.metadata["use_count"] == 1
        revoked = await manager.resolve_permission({**action, "decision": "revoke_work"})
        assert revoked["ok"] is True, revoked
        response = manager.runtime.resolve_permission.await_args.args[1]
        assert response.allow is False and response.scope == "work"
        assert manager.permission_store.get_permission_request(permission.request_id).metadata["grant_status"] == "revoked"
        assert (await manager.resolve_permission({**action, "decision": "revoke_work"}))["ok"] is False
        assert manager.runtime.resolve_permission.await_count == 2
    finally:
        host.adapter.release.set()
        manager.permission_store.close()
        await host.close()


async def test_global_auto_policy_prevents_cooperative_read_only_deny_then_audits_resolution(
        host_factory, monkeypatch):
    host, manager, record = await _manager(host_factory)
    child = host.loop.children[host.loop.bound_context_id]
    from dataclasses import replace
    child.requirements = replace(child.requirements, workspace_access="read")
    try:
        monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", True)
        request = {"permissionRequest": {"request_id": "auto-first", "capability": "shell.execute",
            "action": "execute_command", "options": ["allow_once", "deny"]}}
        await manager._handle_provider_event(Method.PROVIDER_EVENT, _event(record, "permission.requested", request))
        permission, = manager.permission_store.list_cooperative_permission_requests("conversation-A")
        assert permission.status == "pending" and "allow_once" in permission.options
        assert manager.runtime.resolve_permission.await_count == 0
        await manager._handle_provider_event(Method.PROVIDER_EVENT, _event(record, "permission.allowed",
            {"request_id": "auto-first", "automatic": True, "decision": "allow_once"}))
        stored = manager.permission_store.get_permission_request(permission.request_id)
        assert stored.status == "allowed" and stored.metadata["resolution"] == "policy_allowed"
        assert stored.metadata["resolved_automatically"] is True
        monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", False)
        request["permissionRequest"]["request_id"] = "manual-second"
        await manager._handle_provider_event(Method.PROVIDER_EVENT, _event(record, "permission.requested", request))
        second = manager.permission_store.list_cooperative_permission_requests("conversation-A")[-1]
        assert second.status == "denied" and second.options == ["deny"]
        assert manager.runtime.resolve_permission.await_args.args[1].allow is False
    finally:
        host.adapter.release.set()
        manager.permission_store.close()
        await host.close()


@pytest.mark.parametrize("event_type", ["permission.expired", "recovery"])
async def test_cooperative_grant_expiry_preserves_approval_history(host_factory, monkeypatch, event_type):
    host, manager, record = await _manager(host_factory)
    try:
        permission = manager.permission_store.create_cooperative_permission_request(
            session_id="conversation-A", context_id=host.loop.bound_context_id,
            provider_run_id=record.run_id, capability="mcp.tool.call", action="call_tool",
            options=["allow_once", "allow_work", "deny"],
            metadata={"kind": "provider_permission", "provider_request_id": "native-grant"})
        allowed = manager.permission_store.resolve_permission_request(permission.request_id, "allowed",
            metadata={"resolution": "user_allowed_work", "grant_status": "active"})
        if event_type == "recovery":
            with monkeypatch.context() as lost_owner:
                lost_owner.setattr(host.runtime, "get_run", lambda _run: None)
                receipts = host.ingress.expire_unactionable_permissions(manager.permission_store, recovery={})
            assert len(receipts) == 1 and receipts[0]["state"] == "expired"
        else:
            await manager._handle_provider_event(Method.PROVIDER_EVENT, _event(record, event_type,
                {"request_id": "native-grant", "scope": "work", "automatic": True}))
        expired = manager.permission_store.get_permission_request(permission.request_id)
        assert expired.status == "allowed" and expired.resolved_at == allowed.resolved_at
        assert expired.metadata["grant_status"] == "expired"
        assert expired.metadata["resolution"] == "user_allowed_work"
    finally:
        host.adapter.release.set()
        manager.permission_store.close()
        await host.close()


@pytest.mark.parametrize("immediate", ["permission.used", "permission.expired"])
async def test_cooperative_grant_precedes_immediate_provider_activity(host_factory, immediate):
    host, manager, record = await _manager(host_factory)
    try:
        await manager._handle_provider_event(Method.PROVIDER_EVENT,
            _event(record, "permission.requested", {"permissionRequest": {"request_id": "instant",
                "capability": "mcp.tool.call", "action": "call_tool",
                "options": ["allow_once", "allow_work", "deny"]}}))
        permission, = manager.permission_store.list_cooperative_permission_requests("conversation-A")
        async def resolve(_run_id, _response):
            await manager._handle_provider_event(Method.PROVIDER_EVENT, _event(record, immediate,
                {"request_id": "instant", "call_request_id": "repeat", "scope": "work", "automatic": True}))
            return {"accepted": True}
        manager.runtime.resolve_permission = AsyncMock(side_effect=resolve)
        result = await manager.resolve_permission({"session_id": "conversation-A", "run_id": record.run_id,
            "provider_request_id": "instant", "permission_request_id": permission.request_id,
            "decision": "allow_work"})
        assert result["ok"] is True
        stored = manager.permission_store.get_permission_request(permission.request_id)
        if immediate == "permission.used":
            assert stored.metadata["use_count"] == 1
        else:
            assert stored.metadata["grant_status"] == "expired"
    finally:
        host.adapter.release.set()
        manager.permission_store.close()
        await host.close()


@pytest.mark.parametrize("raises", [False, True])
async def test_cooperative_dispatch_failure_expires_the_user_grant(host_factory, raises):
    host, manager, record = await _manager(host_factory)
    try:
        await manager._handle_provider_event(Method.PROVIDER_EVENT,
            _event(record, "permission.requested", {"permissionRequest": {"request_id": "dispatch",
                "capability": "mcp.tool.call", "action": "call_tool",
                "options": ["allow_once", "allow_work", "deny"]}}))
        permission, = manager.permission_store.list_cooperative_permission_requests("conversation-A")
        manager.runtime.resolve_permission = AsyncMock(side_effect=RuntimeError("transport unavailable")) if raises else AsyncMock(
            return_value={"accepted": False, "reason": "native_outcome_unknown"})
        action = {"session_id": "conversation-A", "run_id": record.run_id,
            "provider_request_id": "dispatch", "permission_request_id": permission.request_id,
            "decision": "allow_work"}
        if raises:
            with pytest.raises(RuntimeError, match="transport unavailable"):
                await manager.resolve_permission(action)
        else:
            result = await manager.resolve_permission(action)
            assert result["ok"] is False and result["error"] == "native_outcome_unknown"
        stored = manager.permission_store.get_permission_request(permission.request_id)
        assert stored.metadata["resolution"] == "user_allowed_work"
        assert stored.metadata["grant_status"] == "expired"
        assert manager.runtime.resolve_permission.await_count == 1
    finally:
        host.adapter.release.set()
        manager.permission_store.close()
        await host.close()
