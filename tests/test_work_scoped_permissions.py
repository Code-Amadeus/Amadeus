"""Exact-call approval authority across durable Work and cooperative surfaces."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from agent_host.provider_types import ProviderRunRequest
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.canvas_action_router import CanvasActionRouter
from server.handlers.work_ledger_handler import WorkLedgerHandler
from server.protocol import Method
from server.work_ledger_coordinator import WorkLedgerCoordinator


MCP_CALL = {"connectionId": "status-peer", "toolName": "instant_status", "arguments": {"images": True}}


async def _pending(tmp_path, *, options=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = WorkLedgerStore(tmp_path / "ledger.sqlite3")
    coordinator = WorkLedgerCoordinator(store)
    request = ProviderRunRequest(provider="pi", task="Poll status", cwd=str(workspace),
        metadata={"session_id": "session-scope"})
    coordinator.prepare_request(request)
    for event_type, payload in (("run.created", {}), ("run.status", {"status": "running"}),
            ("permission.requested", {"permissionRequest": {
                "request_id": "approval-original", "capability": "mcp.tool.call", "action": "call_tool",
                "options": options or ["allow_once", "allow_work", "deny"], "mcpCall": MCP_CALL}})):
        await coordinator._on_provider_event(Method.PROVIDER_EVENT,
            {"provider": "pi", "run_id": "run-scope", "type": event_type,
                "metadata": request.metadata, "payload": payload})
    item_id, attempt_id = (request.metadata["work"][key] for key in ("work_item_id", "attempt_id"))
    permission = store.list_permission_requests(item_id, attempt_id=attempt_id)[0]
    return store, coordinator, request, permission


def _action(coordinator, permission, decision):
    return {"target": "permission", "action": decision, "decision": decision,
        "permissionRequestId": permission.request_id, "workItemId": permission.work_item_id,
        "attemptId": permission.attempt_id, "runId": "run-scope", "sessionId": "session-scope",
        "revision": coordinator.snapshot()["revision"]}


def test_work_grant_remains_visible_records_uses_and_revokes(tmp_path):
    async def run():
        store, coordinator, request, permission = await _pending(tmp_path)
        runtime = AsyncMock(return_value={"accepted": True})
        handler = WorkLedgerHandler(coordinator, provider_permission=runtime)
        try:
            assert permission.options == ["allow_once", "allow_work", "deny"]
            assert coordinator.snapshot()["permissionRequest"]["mcpCall"] == MCP_CALL
            result = await handler.route_action(_action(coordinator, permission, "allow_work"))
            assert result["ok"] is True, result
            response = runtime.await_args.args[1]
            assert response.allow is True and response.scope == "work"
            allowed = store.get_permission_request(permission.request_id)
            assert allowed.metadata["resolution"] == "user_allowed_work"
            assert allowed.metadata["grant_status"] == "active"
            snapshot = coordinator.snapshot()
            assert snapshot["permissionRequest"] is None
            assert snapshot["activePermissionGrants"][0]["mcpCall"] == MCP_CALL
            initial_revision = snapshot["revision"]
            used = {"provider": "pi", "run_id": "run-scope", "type": "permission.used",
                "metadata": request.metadata, "payload": {"request_id": "approval-original",
                    "call_request_id": "approval-repeat", "decision": "allow_work", "automatic": True}}
            await coordinator._on_provider_event(Method.PROVIDER_EVENT, used)
            await coordinator._on_provider_event(Method.PROVIDER_EVENT, used)
            snapshot = coordinator.snapshot()
            assert snapshot["activePermissionGrants"][0]["useCount"] == 1
            assert snapshot["revision"] != initial_revision
            revoked = await handler.route_action(_action(coordinator, permission, "revoke_work"))
            assert revoked["ok"] is True, revoked
            response = runtime.await_args.args[1]
            assert response.allow is False and response.scope == "work"
            assert store.get_permission_request(permission.request_id).metadata["grant_status"] == "revoked"
            assert coordinator.snapshot()["activePermissionGrants"] == []
            replay = await handler.route_action(_action(coordinator, permission, "revoke_work"))
            assert replay["ok"] is False
            assert runtime.await_count == 2
        finally:
            coordinator.close()
            store.close()
    asyncio.run(run())


def test_scope_options_and_owner_identity_are_validated_before_runtime(tmp_path):
    async def run():
        store, coordinator, _, permission = await _pending(tmp_path, options=["allow_once", "deny"])
        runtime = AsyncMock(return_value={"accepted": True})
        handler = WorkLedgerHandler(coordinator, provider_permission=runtime)
        try:
            result = await handler.route_action(_action(coordinator, permission, "allow_work"))
            assert result["error"] == "permission_option_not_allowed"
            for override in ({"runId": "other-run"}, {"sessionId": "other-session"}):
                result = await handler.route_action({**_action(coordinator, permission, "allow_once"), **override})
                assert result["ok"] is False
            assert runtime.await_count == 0
            assert store.get_permission_request(permission.request_id).status == "pending"
        finally:
            coordinator.close()
            store.close()
    asyncio.run(run())


def test_terminal_attempt_expires_grant_without_overwriting_original_decision(tmp_path):
    async def run():
        store, coordinator, _, permission = await _pending(tmp_path)
        try:
            store.resolve_permission_request(permission.request_id, "allowed",
                metadata={"resolution": "user_allowed_work", "grant_status": "active", "use_count": 0})
            before = store.get_permission_request(permission.request_id)
            terminal = store.update_attempt(permission.attempt_id, execution_status="cancelled")
            coordinator.permission_service.expire_provider_checkpoints(terminal, resolution="attempt_terminal")
            after = store.get_permission_request(permission.request_id)
            assert after.status == "allowed" and after.resolved_at == before.resolved_at
            assert after.metadata["grant_status"] == "expired"
            assert after.metadata["resolution"] == "user_allowed_work"
            assert coordinator.snapshot()["activePermissionGrants"] == []
        finally:
            coordinator.close()
            store.close()
    asyncio.run(run())


def test_grant_update_is_atomic_and_cannot_rewrite_an_ordinary_approval(tmp_path):
    async def run():
        store, coordinator, _, permission = await _pending(tmp_path)
        try:
            store.resolve_permission_request(permission.request_id, "allowed", metadata={"resolution": "user_allowed"})
            with pytest.raises(WorkLedgerConflict):
                store.update_permission_grant(permission.request_id, status="revoked")
        finally:
            coordinator.close()
            store.close()
    asyncio.run(run())


def test_grant_usage_and_retirement_preserve_original_decision(tmp_path):
    async def run():
        store, coordinator, _, permission = await _pending(tmp_path)
        try:
            allowed = store.resolve_permission_request(permission.request_id, "allowed",
                metadata={"resolution": "user_allowed_work", "grant_status": "active"})
            first = store.update_permission_grant(permission.request_id, call_request_id="repeat-1")
            duplicate = store.update_permission_grant(permission.request_id, call_request_id="repeat-1")
            assert first.metadata["use_count"] == duplicate.metadata["use_count"] == 1
            revoked = store.update_permission_grant(permission.request_id, status="revoked")
            assert revoked.status == "allowed" and revoked.resolved_at == allowed.resolved_at
            with pytest.raises(WorkLedgerConflict):
                store.update_permission_grant(permission.request_id, call_request_id="after-revocation")
            with pytest.raises(WorkLedgerConflict):
                store.update_permission_grant(permission.request_id, status="expired")
        finally:
            coordinator.close()
            store.close()
    asyncio.run(run())


def test_canvas_router_preserves_work_scope_decision_and_cooperative_identity():
    async def run():
        resolver = AsyncMock(return_value={"ok": True})
        router = CanvasActionRouter(cooperative_permission_action=resolver)
        for decision in ("allow_work", "revoke_work"):
            result = await router.route({"target": "permission", "action": decision,
                "owner_kind": "cooperative_run", "permission_request_id": "host-id",
                "session_id": "session", "run_id": "run", "provider_request_id": "native-id"})
            assert result["ok"] is True, result
            assert resolver.await_args.args[0]["decision"] == decision
    asyncio.run(run())


def test_automatic_work_resolution_is_durable_and_does_not_create_an_exact_grant(tmp_path):
    async def run():
        store, coordinator, request, permission = await _pending(tmp_path)
        try:
            await coordinator._on_provider_event(Method.PROVIDER_EVENT,
                {"provider": "pi", "run_id": "run-scope", "type": "permission.allowed",
                    "metadata": request.metadata, "payload": {"request_id": "approval-original",
                        "automatic": True, "decision": "allow_once", "reason": "work_permission_auto_approve"}})
            after = store.get_permission_request(permission.request_id)
            assert after.status == "allowed"
            assert after.metadata["resolution"] == "policy_allowed"
            assert after.metadata["resolved_automatically"] is True
            assert coordinator.snapshot()["activePermissionGrants"] == []
        finally:
            coordinator.close()
            store.close()
    asyncio.run(run())


def test_work_auto_approval_hot_setting_is_strict_and_current(monkeypatch):
    from config import settings
    from server.handlers.system_handler import SystemHandler
    async def run():
        handler = SystemHandler()
        monkeypatch.setattr(settings, "WORK_PERMISSION_AUTO_APPROVE", False)
        monkeypatch.setattr(handler, "_get_config", AsyncMock(return_value={}))
        monkeypatch.setattr("server.handlers.system_handler.bus.emit", AsyncMock())
        with pytest.raises(ValueError, match="boolean"):
            await handler._set_config({"values": {"work_permission_auto_approve": "false"}})
        assert settings.WORK_PERMISSION_AUTO_APPROVE is False
        for expected in (True, False):
            result = await handler._set_config({"values": {"work_permission_auto_approve": expected}})
            assert settings.WORK_PERMISSION_AUTO_APPROVE is expected
            assert "work_permission_auto_approve" in result["updated"]
    asyncio.run(run())


@pytest.mark.parametrize("immediate", ["permission.used", "permission.expired"])
def test_work_grant_is_durable_before_provider_can_emit_immediate_activity(tmp_path, immediate):
    async def run():
        store, coordinator, request, permission = await _pending(tmp_path)
        async def resolve(_run_id, _response):
            await coordinator._on_provider_event(Method.PROVIDER_EVENT,
                {"provider": "pi", "run_id": "run-scope", "type": immediate,
                    "metadata": request.metadata, "payload": {"request_id": "approval-original",
                        "call_request_id": "immediate-repeat", "scope": "work", "automatic": True}})
            return {"accepted": True}
        try:
            result = await WorkLedgerHandler(coordinator, provider_permission=resolve).route_action(
                _action(coordinator, permission, "allow_work"))
            assert result["ok"] is True
            stored = store.get_permission_request(permission.request_id)
            if immediate == "permission.used":
                assert stored.metadata["use_count"] == 1
            else:
                assert stored.metadata["grant_status"] == "expired"
        finally:
            coordinator.close()
            store.close()
    asyncio.run(run())


@pytest.mark.parametrize("raises", [False, True])
def test_work_grant_dispatch_failure_expires_user_authorization_without_retry(tmp_path, raises):
    async def run():
        store, coordinator, _, permission = await _pending(tmp_path)
        callback = AsyncMock(side_effect=RuntimeError("transport unavailable")) if raises else AsyncMock(
            return_value={"accepted": False, "reason": "native_outcome_unknown"})
        handler = WorkLedgerHandler(coordinator, provider_permission=callback)
        try:
            if raises:
                with pytest.raises(RuntimeError, match="transport unavailable"):
                    await handler.route_action(_action(coordinator, permission, "allow_work"))
            else:
                result = await handler.route_action(_action(coordinator, permission, "allow_work"))
                assert result["ok"] is False and result["error"] == "native_outcome_unknown"
            stored = store.get_permission_request(permission.request_id)
            assert stored.metadata["resolution"] == "user_allowed_work"
            assert stored.metadata["grant_status"] == "expired"
            assert callback.await_count == 1
        finally:
            coordinator.close()
            store.close()
    asyncio.run(run())
