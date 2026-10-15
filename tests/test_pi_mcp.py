"""Host-bound MCP calls through the real Pi CLI and local synthetic servers."""
from dataclasses import replace
from contextlib import suppress
import json
from pathlib import Path
import sys

import psutil
import pytest
from aiohttp import web

from agent_host.mcp_connections import McpConnectionSpec
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_types import ProviderPermissionResponse, ProviderRunRequest
from test_pi_runtime import native_pi as native_pi, pytestmark as pytestmark

PEER = Path(__file__).parent / "fixtures/pi_mcp_server.py"


@pytest.fixture(autouse=True)
def cleanup_synthetic_servers(tmp_path):
    yield
    for record in tmp_path.rglob("pids.txt"):
        for pid in record.read_text(encoding="utf-8").splitlines():
            with suppress(psutil.NoSuchProcess):
                process = psutil.Process(int(pid))
                if str(PEER) in process.cmdline():
                    process.kill()
                    process.wait(timeout=5)


def connection(root, **overrides):
    values = dict(connection_id="fixture", display_name="Fixture", transport="stdio",
        enabled=True, provider_ids=("pi",), command=sys.executable, arguments=(str(PEER),),
        environment={"PI_MCP_FIXTURE_ROOT": str(root), "PI_MCP_LITERAL": "!literal ${NOT_EXPANDED}"})
    values.update(overrides)
    return McpConnectionSpec(**values)


def request(tmp_path, access="none", **overrides):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return ProviderRunRequest(provider="pi", task="Use the configured synthetic service.", cwd=str(workspace),
        requirements=ProviderRequirements(workspace_access=access, workspace_ownership="caller"), **overrides)


def calls(root):
    path = root / "calls.txt"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_pi_projection_filters_bindings_and_keeps_credentials_literal(tmp_path, monkeypatch):
    from agent_host.mcp_connections import pi_mcp_servers

    monkeypatch.setenv("AMADEUS_PI_TEST_HOST_SECRET", "unrelated-secret")
    active = connection(tmp_path / "active")
    disabled = connection(tmp_path / "disabled", connection_id="disabled", enabled=False)
    other = connection(tmp_path / "other", connection_id="other", provider_ids=("codex",),
        environment={"PI_MCP_OTHER_SECRET": "other-secret"})
    entries = pi_mcp_servers((active, disabled, other), cwd=str(tmp_path))
    assert [entry["name"] for entry in entries] == ["fixture"]
    environment = entries[0]["config"]["env"]
    assert environment["PI_MCP_LITERAL"] == "!literal ${NOT_EXPANDED}"
    assert "AMADEUS_PI_TEST_HOST_SECRET" not in environment
    assert "PI_MCP_OTHER_SECRET" not in environment
    assert "AMADEUS_PI_MCP_SERVERS" not in environment
    assert entries[0]["config"]["exposure"] == "direct"
    with pytest.raises(ValueError, match="namespace"):
        pi_mcp_servers((replace(active, connection_id="a-b"), replace(active, connection_id="a_b")), cwd=str(tmp_path))


def test_pi_http_projection_uses_bound_token_and_fails_closed_when_missing(tmp_path, monkeypatch):
    from agent_host.mcp_connections import pi_mcp_servers

    monkeypatch.setenv("PI_MCP_TEST_TOKEN", "ambient-fallback")
    spec = McpConnectionSpec(connection_id="http", display_name="HTTP", transport="http", enabled=True,
        provider_ids=("pi",), url="https://mcp.example.test/mcp", bearer_token_env_var="PI_MCP_TEST_TOKEN",
        environment={"PI_MCP_TEST_TOKEN": "bound-${LITERAL}"})
    config = pi_mcp_servers((spec,), cwd=str(tmp_path))[0]["config"]
    assert config["headers"] == {"Authorization": "Bearer bound-${LITERAL}"}
    assert "env" not in config
    assert "auth" not in config and "oauth" not in config
    monkeypatch.delenv("PI_MCP_TEST_TOKEN")
    with pytest.raises(ValueError, match="bearer token is unavailable"):
        pi_mcp_servers((replace(spec, environment={}),), cwd=str(tmp_path))


async def test_only_host_bound_servers_load_despite_ambient_configuration(native_pi, tmp_path, monkeypatch):
    from config import settings

    adapter, requests, _, _ = native_pi
    bound = tmp_path / "bound"
    ambient = tmp_path / "ambient"
    disabled = tmp_path / "disabled"
    unbound = tmp_path / "unbound"
    adapter.mcp_connections = (connection(bound), connection(disabled, connection_id="disabled", enabled=False),
        connection(unbound, connection_id="other", provider_ids=("codex",)))
    req = request(tmp_path)
    config = {"command": sys.executable, "args": [str(PEER)], "exposure": "direct",
              "env": {"PI_MCP_FIXTURE_ROOT": str(ambient)}}
    for directory in (adapter.agent_dir, Path(req.cwd) / ".pi"):
        directory.mkdir(exist_ok=True)
        (directory / "mcp.json").write_text(json.dumps({"mcpServers": {"fixture": config}}), encoding="utf-8")
    extension = tmp_path / "ambient_registration.ts"
    extension.write_text(f"export default function(pi) {{ pi.registerMcpServer('extra', {json.dumps(config)}); }}",
                         encoding="utf-8")
    monkeypatch.setattr(settings, "PI_EXTENSIONS_JSON", json.dumps([str(extension)]))

    async def emit(_event):
        pass

    result = await adapter.run(req, "bindings", emit)
    assert result.status == "done", result
    names = {tool["function"]["name"] for tool in requests[0]["tools"]}
    assert "mcp__fixture__inspect_fixture" in names
    assert "codemode" not in names and "tool_search" not in names
    assert not any(name.startswith(("mcp__extra__", "mcp__disabled__", "mcp__other__")) for name in names)
    assert (bound / "started.json").exists()
    assert all(not root.exists() for root in (ambient, disabled, unbound))


async def test_failed_mcp_connection_is_observable_without_claiming_execution(native_pi, tmp_path):
    adapter, _, _, _ = native_pi
    adapter.mcp_connections = (connection(tmp_path / "service", command=str(tmp_path / "missing-executable")),)
    events = []

    async def emit(event):
        events.append(event)

    result = await adapter.run(request(tmp_path), "failed-connection", emit)
    assert result.status == "done", result
    assert any(event.type == "semantic.progress" and event.payload["source"] == "pi_native_notification" for event in events)
    assert not any(event.type == "tool.call" for event in events)
    assert result.activity_evidence.execution_items == 0


@pytest.mark.parametrize("access", ["none", "read", "write"])
@pytest.mark.parametrize("allow", [False, True])
async def test_mcp_call_requires_host_approval_independent_of_workspace(native_pi, tmp_path, monkeypatch, access, allow):
    adapter, requests, selected, _ = native_pi
    root = tmp_path / "service"
    monkeypatch.setenv("AMADEUS_PI_TEST_HOST_SECRET", "unrelated-secret")
    adapter.mcp_connections = (connection(root),)
    selected.update(tool="mcp__fixture__inspect_fixture", args={})
    events = []

    async def emit(event):
        events.append(event)
        if event.type == "permission.requested":
            key = event.payload["permissionRequest"]["request_id"]
            assert (await adapter.resolve_permission("mcp", ProviderPermissionResponse(key, allow)))["accepted"]
            assert not (await adapter.resolve_permission("mcp", ProviderPermissionResponse(key, allow)))["accepted"]

    result = await adapter.run(request(tmp_path, access), "mcp", emit)
    assert result.status == "done", result
    assert sum(event.type == "permission.requested" for event in events) == 1
    assert calls(root) == (["inspect"] if allow else [])
    output, = [event for event in events if event.type == "tool.result"]
    assert output.payload["ok"] is allow
    if allow:
        text = json.dumps(requests[-1]["messages"])
        assert "!literal ${NOT_EXPANDED}" in text
        assert "unrelated-secret" not in text
        value = json.loads(output.payload["raw"]["output"]["content"][0]["text"])
        assert value == {"literal": "!literal ${NOT_EXPANDED}", "unrelated": None,
                         "other_connection": None, "host_registry": None}
    assert not psutil.pid_exists(json.loads((root / "started.json").read_text())["pid"])


async def test_mcp_mutation_does_not_grant_local_file_writes(native_pi, tmp_path):
    adapter, _, selected, _ = native_pi
    root = tmp_path / "service"
    adapter.mcp_connections = (connection(root),)
    req = request(tmp_path)
    local = Path(req.cwd) / "local.txt"
    selected["steps"] = [
        {"tool": "mcp__fixture__write_note", "args": {"text": "approved external write"}},
        {"tool": "write", "args": {"path": str(local), "content": "must not write"}},
    ]
    events = []

    async def emit(event):
        events.append(event)
        if event.type == "permission.requested":
            await adapter.resolve_permission("write", ProviderPermissionResponse(event.payload["permissionRequest"]["request_id"], True))

    result = await adapter.run(req, "write", emit)
    assert result.status == "done", result
    assert (root / "note.txt").read_text() == "approved external write"
    assert not local.exists()
    assert [event.payload["ok"] for event in events if event.type == "tool.result"] == [True, False]


async def test_mcp_cancel_during_approval_does_not_execute(native_pi, tmp_path):
    adapter, _, selected, _ = native_pi
    root = tmp_path / "service"
    adapter.mcp_connections = (connection(root),)
    selected.update(tool="mcp__fixture__write_note", args={"text": "must not write"})
    cancelled = []

    async def emit(event):
        if event.type == "permission.requested":
            cancelled.append(await adapter.cancel("cancel"))

    result = await adapter.run(request(tmp_path), "cancel", emit)
    assert result.status == "cancelled", result
    assert cancelled and cancelled[0]["confirmed"]
    assert calls(root) == []
    assert not psutil.pid_exists(json.loads((root / "started.json").read_text())["pid"])


async def test_mcp_restore_uses_current_connections_and_closes_each_run(native_pi, tmp_path):
    adapter, requests, selected, _ = native_pi
    root = tmp_path / "service"
    adapter.mcp_connections = (connection(root),)
    selected.update(tool="read_mcp_resource", args={"server": "fixture", "uri": "fixture://note"})
    events = []

    async def emit(event):
        events.append(event)
        if event.type == "permission.requested":
            await adapter.resolve_permission("restore", ProviderPermissionResponse(event.payload["permissionRequest"]["request_id"], True))

    req = request(tmp_path)
    first = await adapter.run(req, "restore", emit)
    assert first.status == "done", first
    assert calls(root) == ["resource"]
    assert not psutil.pid_exists(json.loads((root / "started.json").read_text())["pid"])
    second = await adapter.run(replace(req, session=first.session), "restore", emit)
    assert second.status == "done" and second.session == first.session
    assert calls(root) == ["resource", "resource"], [event.payload for event in events if event.type == "tool.result"]
    assert all(not psutil.pid_exists(int(pid)) for pid in (root / "pids.txt").read_text().splitlines())
    adapter.mcp_connections = ()
    selected["tool"] = None
    third = await adapter.run(replace(req, session=first.session), "restore", emit)
    assert third.status == "done" and third.session == first.session
    names = {tool["function"]["name"] for tool in requests[-1]["tools"]}
    assert "read_mcp_resource" not in names
    assert not any(name.startswith("mcp__") for name in names)


async def test_http_mcp_uses_only_explicit_bearer_and_host_approval(native_pi, tmp_path):
    adapter, _, selected, _ = native_pi
    seen = []

    async def http_mcp(incoming):
        if incoming.method != "POST":
            return web.Response(status=405)
        message = await incoming.json()
        seen.append((message["method"], incoming.headers.get("Authorization")))
        if "id" not in message:
            return web.Response(status=202)
        result = {"initialize": {"protocolVersion": "2025-11-25", "serverInfo": {"name": "fixture", "version": "1"}, "capabilities": {"tools": {}}},
            "tools/list": {"tools": [{"name": "inspect", "description": "Synthetic HTTP tool", "inputSchema": {"type": "object", "properties": {}}}]},
            "tools/call": {"content": [{"type": "text", "text": "HTTP fixture result"}]}}[message["method"]]
        return web.json_response({"jsonrpc": "2.0", "id": message["id"], "result": result})

    app = web.Application()
    app.router.add_route("*", "/mcp", http_mcp)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/mcp"
    adapter.mcp_connections = (McpConnectionSpec(connection_id="http_fixture", display_name="HTTP fixture",
        transport="http", enabled=True, provider_ids=("pi",), url=url,
        bearer_token_env_var="FIXTURE_TOKEN", environment={"FIXTURE_TOKEN": "literal-${TOKEN}"}),)
    selected.update(tool="mcp__http_fixture__inspect", args={})
    approvals = []

    async def emit(event):
        if event.type == "permission.requested":
            approvals.append(event)
            await adapter.resolve_permission("http", ProviderPermissionResponse(event.payload["permissionRequest"]["request_id"], True))

    try:
        result = await adapter.run(request(tmp_path), "http", emit)
        assert result.status == "done", result
        assert len(approvals) == 1
        assert any(method == "tools/call" for method, _ in seen)
        assert all(token == "Bearer literal-${TOKEN}" for _, token in seen)
    finally:
        await runner.cleanup()
