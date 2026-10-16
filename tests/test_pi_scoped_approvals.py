"""真实 Pi/MCP 的精确调用授权、撤销与运行隔离。"""
from dataclasses import replace

import pytest

from agent_host.provider_types import ProviderPermissionResponse
from test_pi_mcp import calls, connection, request
from test_pi_runtime import native_pi as native_pi, pytestmark as pytestmark


@pytest.mark.parametrize("scope,approval_count", [("once", 3), ("work", 1)])
async def test_repeated_mcp_calls_only_reuse_explicit_work_scope(native_pi, tmp_path, scope, approval_count):
    adapter, _, selected, _ = native_pi
    root = tmp_path / "service"
    adapter.mcp_connections = (connection(root),)
    selected["steps"] = [{"tool": "mcp__fixture__inspect_fixture", "args": {}} for _ in range(3)]
    events, approvals = [], []

    async def emit(event):
        events.append(event)
        if event.type == "permission.requested":
            permission = event.payload["permissionRequest"]
            approvals.append(permission)
            assert "allow_work" in permission["options"]
            assert permission["mcpCall"]["connectionId"] == "fixture"
            assert permission["mcpCall"]["toolName"] == "inspect_fixture"
            assert permission["mcpCall"]["arguments"] == {}
            outcome = await adapter.resolve_permission("repeat", ProviderPermissionResponse(
                permission["request_id"], True, scope=scope))
            assert outcome["accepted"]

    result = await adapter.run(request(tmp_path), "repeat", emit)
    assert result.status == "done", result
    assert calls(root) == ["inspect"] * 3
    assert len(approvals) == approval_count
    assert len([event for event in events if event.type == "permission.used"]) == 3 - approval_count
    expired = [event for event in events if event.type == "permission.expired" and event.payload.get("scope") == "work"]
    assert len(expired) == (1 if scope == "work" else 0)


async def test_exact_grant_does_not_cover_changed_arguments_or_tool(native_pi, tmp_path):
    adapter, _, selected, _ = native_pi
    root = tmp_path / "service"
    adapter.mcp_connections = (connection(root),)
    selected["steps"] = [
        {"tool": "mcp__fixture__write_note", "args": {"text": text}}
        for text in ("first", "first", "changed", "first")
    ] + [{"tool": "mcp__fixture__inspect_fixture", "args": {}}]
    approvals = []

    async def emit(event):
        if event.type == "permission.requested":
            permission = event.payload["permissionRequest"]
            approvals.append(permission)
            await adapter.resolve_permission("arguments", ProviderPermissionResponse(
                permission["request_id"], len(approvals) == 1,
                scope="work" if len(approvals) == 1 else "once"))

    assert (await adapter.run(request(tmp_path), "arguments", emit)).status == "done"
    assert len(approvals) == 3
    assert calls(root) == ["write"] * 3
    assert (root / "note.txt").read_text(encoding="utf-8") == "first"


async def test_revocation_requires_fresh_approval_on_next_identical_call(native_pi, tmp_path):
    adapter, _, selected, _ = native_pi
    root = tmp_path / "service"
    adapter.mcp_connections = (connection(root),)
    selected["steps"] = [{"tool": "mcp__fixture__inspect_fixture", "args": {}} for _ in range(3)]
    approvals, uses = [], []

    async def emit(event):
        if event.type == "permission.requested":
            permission = event.payload["permissionRequest"]
            approvals.append(permission)
            await adapter.resolve_permission("revoke", ProviderPermissionResponse(
                permission["request_id"], len(approvals) == 1,
                scope="work" if len(approvals) == 1 else "once"))
        elif event.type == "permission.used":
            uses.append(event)
            outcome = await adapter.resolve_permission("revoke", ProviderPermissionResponse(
                approvals[0]["request_id"], False, scope="work"))
            assert outcome["accepted"]

    assert (await adapter.run(request(tmp_path), "revoke", emit)).status == "done"
    assert len(approvals) == 2 and len(uses) == 1
    assert calls(root) == ["inspect", "inspect"]
    assert not (await adapter.resolve_permission("revoke", ProviderPermissionResponse(
        approvals[0]["request_id"], False, scope="work")))["accepted"]


async def test_work_grants_do_not_survive_native_session_restore_or_connection_change(native_pi, tmp_path):
    adapter, _, selected, _ = native_pi
    root, changed = tmp_path / "service", tmp_path / "changed"
    adapter.mcp_connections = (connection(root),)
    approvals = []

    async def emit(event):
        if event.type == "permission.requested":
            permission = event.payload["permissionRequest"]
            approvals.append(permission)
            await adapter.resolve_permission(event.run_id, ProviderPermissionResponse(
                permission["request_id"], True, scope="work"))

    req = request(tmp_path)
    for index in range(3):
        selected["steps"] = [{"tool": "mcp__fixture__inspect_fixture", "args": {}}] * 2
        if index == 2:
            adapter.mcp_connections = (connection(changed),)
        result = await adapter.run(req, f"run-{index}", emit)
        assert result.status == "done", result
        req = replace(req, session=result.session)
    assert len(approvals) == 3
    assert calls(root) == ["inspect"] * 4
    assert calls(changed) == ["inspect"] * 2


async def test_cancelled_grant_cannot_authorize_a_later_work(native_pi, tmp_path):
    adapter, _, selected, _ = native_pi
    root = tmp_path / "service"
    adapter.mcp_connections = (connection(root),)
    approvals, expirations = [], []

    async def emit(event):
        if event.type == "permission.requested":
            permission = event.payload["permissionRequest"]
            approvals.append(permission)
            await adapter.resolve_permission(event.run_id, ProviderPermissionResponse(
                permission["request_id"], True, scope="work"))
        elif event.type == "permission.used":
            await adapter.cancel(event.run_id)
        elif event.type == "permission.expired":
            expirations.append(event)

    selected["steps"] = [{"tool": "mcp__fixture__inspect_fixture", "args": {}}] * 2
    assert (await adapter.run(request(tmp_path), "cancel", emit)).status == "cancelled"
    assert expirations
    selected["steps"] = [{"tool": "mcp__fixture__inspect_fixture", "args": {}}]
    assert (await adapter.run(request(tmp_path), "next", emit)).status == "done"
    assert len(approvals) == 2


async def test_same_named_tool_on_another_connection_needs_its_own_grant(native_pi, tmp_path):
    adapter, _, selected, _ = native_pi
    root, other = tmp_path / "service", tmp_path / "other"
    adapter.mcp_connections = (connection(root), connection(other, connection_id="other"))
    selected["steps"] = [
        {"tool": "mcp__fixture__inspect_fixture", "args": {}},
        {"tool": "mcp__other__inspect_fixture", "args": {}},
    ]
    approvals = []

    async def emit(event):
        if event.type == "permission.requested":
            permission = event.payload["permissionRequest"]
            approvals.append(permission)
            await adapter.resolve_permission("connections", ProviderPermissionResponse(
                permission["request_id"], len(approvals) == 1,
                scope="work" if len(approvals) == 1 else "once"))

    assert (await adapter.run(request(tmp_path), "connections", emit)).status == "done"
    assert len(approvals) == 2
    assert calls(root) == ["inspect"] and calls(other) == []
