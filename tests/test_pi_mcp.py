"""Host-bound MCP calls through the real Pi CLI and local synthetic servers."""
from dataclasses import replace
from contextlib import suppress
import asyncio
import json
from pathlib import Path
import sys
import unicodedata

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
    from agent_host.mcp_connections import pi_mcp_config

    monkeypatch.setenv("AMADEUS_PI_TEST_HOST_SECRET", "unrelated-secret")
    active = connection(tmp_path / "active")
    disabled = connection(tmp_path / "disabled", connection_id="disabled", enabled=False)
    other = connection(tmp_path / "other", connection_id="other", provider_ids=("codex",),
        environment={"PI_MCP_OTHER_SECRET": "other-secret"})
    projected = pi_mcp_config((active, disabled, other), cwd=str(tmp_path))
    entries = projected["servers"]
    assert projected["errors"] == []
    assert [entry["name"] for entry in entries] == ["fixture"]
    environment = entries[0]["config"]["env"]
    assert environment["PI_MCP_LITERAL"] == "!literal ${NOT_EXPANDED}"
    assert "AMADEUS_PI_TEST_HOST_SECRET" not in environment
    assert "PI_MCP_OTHER_SECRET" not in environment
    assert "AMADEUS_PI_MCP_SERVERS" not in environment
    assert entries[0]["config"]["exposure"] == "direct"
    collisions = pi_mcp_config((active, replace(active, connection_id="a-b"),
                               replace(active, connection_id="a_b")), cwd=str(tmp_path))
    assert [entry["name"] for entry in collisions["servers"]] == ["fixture"]
    assert len(collisions["errors"]) == 2
    assert all("namespace" in error for error in collisions["errors"])


def test_pi_http_projection_uses_bound_token_and_fails_closed_when_missing(tmp_path, monkeypatch):
    from agent_host.mcp_connections import pi_mcp_config

    monkeypatch.setenv("PI_MCP_TEST_TOKEN", "ambient-fallback")
    spec = McpConnectionSpec(connection_id="http", display_name="HTTP", transport="http", enabled=True,
        provider_ids=("pi",), url="https://mcp.example.test/mcp", bearer_token_env_var="PI_MCP_TEST_TOKEN",
        environment={"PI_MCP_TEST_TOKEN": "bound-${LITERAL}"})
    config = pi_mcp_config((spec,), cwd=str(tmp_path))["servers"][0]["config"]
    assert config["headers"] == {"Authorization": "Bearer bound-${LITERAL}"}
    assert "env" not in config
    assert "auth" not in config and "oauth" not in config
    monkeypatch.delenv("PI_MCP_TEST_TOKEN")
    missing = pi_mcp_config((replace(spec, environment={}),), cwd=str(tmp_path))
    assert missing["servers"] == []
    assert "bearer token is unavailable" in missing["errors"][0]


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
    notice, = [event for event in events if event.type == "runtime.notice"]
    assert "Settings -> Providers -> MCP" in notice.payload["message"]
    assert "/mcp" not in notice.payload["message"]
    assert not any(event.type == "tool.call" for event in events)
    assert result.activity_evidence.execution_items == 0
    assert result.activity_evidence.progress_milestones == 0


async def test_bad_bindings_are_visible_without_disabling_healthy_tools(native_pi, tmp_path, monkeypatch):
    adapter, requests, selected, _ = native_pi
    root = tmp_path / "service"
    good = connection(root)
    monkeypatch.delenv("PI_MCP_MISSING_TOKEN", raising=False)
    adapter.mcp_connections = (good,
        replace(good, connection_id="a-b"), replace(good, connection_id="a_b"),
        McpConnectionSpec(connection_id="missing", display_name="Missing credential", transport="http",
            enabled=True, provider_ids=("pi",), url="http://127.0.0.1:9/mcp",
            bearer_token_env_var="PI_MCP_MISSING_TOKEN"))
    selected.update(tool="mcp__fixture__inspect_fixture", args={})
    events = []

    async def emit(event):
        events.append(event)
        if event.type == "permission.requested":
            await adapter.resolve_permission("bad-bindings", ProviderPermissionResponse(
                event.payload["permissionRequest"]["request_id"], True))

    result = await adapter.run(request(tmp_path, "read"), "bad-bindings", emit)
    assert result.status == "done", result
    assert calls(root) == ["inspect"]
    names = [tool["function"]["name"] for tool in requests[0]["tools"]]
    assert not any(name.startswith(("mcp__a_b__", "mcp__missing__")) for name in names)
    notices = "\n".join(event.payload["message"] for event in events if event.type == "runtime.notice")
    assert "a-b" in notices and "a_b" in notices and "Missing credential" in notices
    assert "Settings -> Providers -> MCP" in notices
    assert result.activity_evidence.progress_milestones == 0


@pytest.mark.parametrize("unusual_name", [False, True])
async def test_hashed_mcp_tool_approval_keeps_service_and_original_name(native_pi, tmp_path, unusual_name):
    adapter, requests, selected, _ = native_pi
    root = tmp_path / "service"
    original = "inspect_fixture" + ("\n\t\x1b\u202e" + "x" * 400 if unusual_name else "")
    spec = connection(root, connection_id="ext_" + "a" * 40,
        display_name="Synthetic\nNotes\u202e" if unusual_name else "Synthetic Notes")
    adapter.mcp_connections = (replace(spec, environment={**spec.environment, "PI_MCP_INSPECT_NAME": original}),)

    def inspect_tool(data):
        name = next(tool["function"]["name"] for tool in data["tools"]
                    if original in tool["function"]["description"])
        assert len(name) == 64 and not name.endswith("inspect_fixture")
        return {"tool": name, "args": {}}

    selected["steps"] = [inspect_tool]
    permissions = []

    async def emit(event):
        if event.type == "permission.requested":
            permission = event.payload["permissionRequest"]
            permissions.append(permission)
            await adapter.resolve_permission("readable-name", ProviderPermissionResponse(permission["request_id"], True))

    result = await adapter.run(request(tmp_path, "read"), "readable-name", emit)
    assert result.status == "done", result
    permission, = permissions
    assert "Synthetic Notes" in permission["reason"] and "inspect_fixture" in permission["reason"]
    assert permission["capability"] == "mcp.tool.call"
    assert permission["action"] == "call_tool"
    title = permission["reason"].split("\n", 1)[0]
    assert len(title) <= 330
    assert not any(unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} for char in title)
    if unusual_name:
        assert "…" in title
        assert len(permission["reason"].splitlines()) == 2
    assert calls(root) == ["inspect"]
    assert len(requests) == 2


async def test_hung_server_does_not_hold_the_first_model_request_for_native_default(native_pi, tmp_path):
    adapter, _, selected, _ = native_pi
    # The synthetic process consumes stdin but never answers initialization.
    # Observe its launch and the model request separately from Pi boot time.
    root = tmp_path / "hung"
    script = tmp_path / "hung_server.py"
    script.write_text("import os, pathlib, sys\n"
        "pathlib.Path(os.environ['PI_MCP_FIXTURE_ROOT']).mkdir()\n"
        "for line in sys.stdin: pass\n", encoding="utf-8")
    adapter.mcp_connections = (connection(root, arguments=(str(script),)),)
    prompted = asyncio.Event()
    selected["steps"] = [lambda _data: prompted.set() or {}]
    events = []

    async def emit(event):
        events.append(event)

    running = asyncio.create_task(adapter.run(request(tmp_path), "hung", emit))
    try:
        async with asyncio.timeout(15):
            while not root.exists():
                await asyncio.sleep(0.02)
        # The managed budget is 3s. Leave scheduling headroom, but remain below
        # the native 10s default that previously stalled every turn.
        await asyncio.wait_for(prompted.wait(), 7)
        result = await running
        assert result.status == "done", result
        assert any(event.type == "runtime.notice" and "still connecting" in event.payload["message"] for event in events)
        assert result.activity_evidence.progress_milestones == 0
    finally:
        if not running.done():
            await adapter.cancel("hung")
            await running


@pytest.mark.parametrize("allow", [False, True])
async def test_resource_approvals_name_the_exact_bound_connection(native_pi, tmp_path, allow):
    adapter, _, selected, _ = native_pi
    root, other = tmp_path / "notes", tmp_path / "other"
    server_id = "ext_" + "b" * 40
    adapter.mcp_connections = (connection(root, connection_id=server_id, display_name="Synthetic Notes"),
        connection(other, connection_id="other", display_name="Other Service"))
    steps = [
        {"tool": "list_mcp_resources", "args": {"server": f" {server_id} "}},
        {"tool": "list_mcp_resource_templates", "args": {"server": server_id}},
        {"tool": "read_mcp_resource", "args": {"server": server_id, "uri": "fixture://note"}},
    ]
    selected["steps"] = list(steps)
    events, permissions = [], []

    async def emit(event):
        events.append(event)
        if event.type == "permission.requested":
            permission = event.payload["permissionRequest"]
            permissions.append(permission)
            title, arguments = permission["reason"].split("\n", 1)
            step = steps[len(permissions) - 1]
            assert "Synthetic Notes" in title and server_id in title and step["tool"] in title
            assert "Other Service" not in title
            assert json.loads(arguments) == step["args"]
            assert permission["capability"] == "mcp.tool.call"
            assert permission["action"] == "call_tool"
            await adapter.resolve_permission("resources", ProviderPermissionResponse(permission["request_id"], allow))

    result = await adapter.run(request(tmp_path, "read"), "resources", emit)
    assert result.status == "done", result
    assert len(permissions) == 3
    results = [event for event in events if event.type == "tool.result"]
    assert len(results) == 3 and all(event.payload["ok"] is allow for event in results)
    assert calls(root) == (["resource"] if allow else [])
    assert calls(other) == []


async def test_resource_listing_without_server_displays_all_bound_scope(native_pi, tmp_path):
    adapter, _, selected, _ = native_pi
    adapter.mcp_connections = (connection(tmp_path / "one", connection_id="one", display_name="Notes One"),
        connection(tmp_path / "two", connection_id="two", display_name="Notes Two"),
        connection(tmp_path / "disabled", connection_id="disabled", display_name="Disabled Service", enabled=False))
    selected["steps"] = [{"tool": name, "args": {}}
        for name in ("list_mcp_resources", "list_mcp_resource_templates")]
    events, permissions = [], []

    async def emit(event):
        events.append(event)
        if event.type == "permission.requested":
            permission = event.payload["permissionRequest"]
            permissions.append(permission)
            assert "All bound connections" in permission["reason"]
            assert "Notes One" in permission["reason"] and "Notes Two" in permission["reason"]
            assert "Disabled Service" not in permission["reason"]
            await adapter.resolve_permission("all-resources", ProviderPermissionResponse(permission["request_id"], True))

    result = await adapter.run(request(tmp_path, "read"), "all-resources", emit)
    assert result.status == "done" and len(permissions) == 2, result
    outputs = [event for event in events if event.type == "tool.result"]
    assert all(event.payload["ok"] for event in outputs)
    listing = json.loads(outputs[0].payload["raw"]["output"]["content"][0]["text"])
    assert {resource["server"] for resource in listing["resources"]} == {"one", "two"}


async def test_resource_approval_does_not_misidentify_an_unknown_server(native_pi, tmp_path):
    adapter, _, selected, _ = native_pi
    root = tmp_path / "service"
    adapter.mcp_connections = (connection(root, display_name="Known Service"),)
    selected.update(tool="read_mcp_resource", args={"server": "missing", "uri": "fixture://note"})
    events = []

    async def emit(event):
        events.append(event)
        if event.type == "permission.requested":
            permission = event.payload["permissionRequest"]
            assert "missing" in permission["reason"] and "Known Service" not in permission["reason"]
            await adapter.resolve_permission("unknown-resource", ProviderPermissionResponse(permission["request_id"], True))

    result = await adapter.run(request(tmp_path, "read"), "unknown-resource", emit)
    assert result.status == "done", result
    output, = [event for event in events if event.type == "tool.result"]
    assert output.payload["ok"] is False and calls(root) == []


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
            assert event.payload["permissionRequest"]["capability"] == "mcp.tool.call"
            assert event.payload["permissionRequest"]["action"] == "call_tool"
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
