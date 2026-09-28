"""Real pinned Pi subprocess against a local deterministic model, no API keys.

Install agent_host/pi_runtime dependencies to run these optional integration
checks. They exercise the actual native extension and RPC session format.
"""
from dataclasses import replace
import json
import shutil

from aiohttp import web
import pytest

from agent_host.adapters.pi import PiAdapter, RUNTIME_DIR
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_types import ProviderPermissionResponse, ProviderRunRequest


pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not (RUNTIME_DIR / "node_modules/@earendil-works/pi-coding-agent/dist/cli.js").is_file(),
    reason="optional pinned Pi runtime not installed",
)


@pytest.fixture
async def native_pi(tmp_path):
    requests = []
    selected = {"tool": None, "args": {}}

    async def completions(request):
        data = await request.json()
        requests.append(data)
        last = data["messages"][-1]
        steps = selected.get("steps")
        if steps is not None:
            step = steps.pop(0) if steps else {}
            tool = step.get("tool")
            args = step.get("args", {})
        else:
            tool = selected["tool"] if last["role"] != "tool" else None
            args = selected["args"]
        if tool:
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": f"call_{len(requests)}",
                "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}]}
        else:
            delta = {"role": "assistant", "content": "Native Pi completed the fixture."}
        frames = [
            {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "fixture",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "fixture",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        ]
        return web.Response(text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
            + "data: [DONE]\n\n", content_type="text/event-stream")

    app = web.Application()
    app.router.add_post('/v1/chat/completions', completions)
    async def page(_):
        return web.Response(text="fixture news source", content_type="text/plain")
    app.router.add_get('/page', page)
    async def article(_):
        return web.Response(text='<html><head><title>Requested article</title></head><body><article>'
            '<h1>Requested article</h1><p>' + 'Author Chen reports the verified article fact. ' * 40
            + '</p></article></body></html>', content_type='text/html')
    app.router.add_get('/article', article)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    agent_dir = tmp_path / "pi"
    agent_dir.mkdir()
    (agent_dir / "web-tools.json").write_text(json.dumps({
        "ssrf": {"allowRanges": ["127.0.0.0/8"]}}), encoding="utf-8")
    (agent_dir / "models.json").write_text(json.dumps({"providers": {"fixture": {
        "baseUrl": url + "/v1", "api": "openai-completions", "apiKey": "local-fixture",
        "models": [{"id": "fixture", "reasoning": False, "input": ["text"]}],
    }}}), encoding="utf-8")
    adapter = PiAdapter(agent_dir=agent_dir, model_provider="fixture", model="fixture", timeout=20)
    try:
        adapter.require_startup_ready()
        yield adapter, requests, selected, url
    finally:
        await adapter.close()
        await runner.cleanup()


async def test_real_pi_writes_then_restores_context(native_pi, tmp_path):
    adapter, requests, selected, _ = native_pi
    target = tmp_path / "result.txt"
    selected.update(tool="write", args={"path": str(target), "content": "owned result"})
    req = ProviderRunRequest(provider="pi", task="Write the marker QUARTZ-461 in the requested file.",
        cwd=str(tmp_path), requirements=ProviderRequirements(task_kind="workspace_mutation",
            workspace_access="write", workspace_ownership="caller"))
    events = []

    async def emit(event):
        events.append(event)

    first = await adapter.run(req, "first", emit)
    assert first.status == "done", first
    assert target.read_text() == "owned result"
    assert first.activity_evidence.execution_items == 1
    assert any(event.type == "tool.result" and event.payload["ok"] for event in events)
    selected["tool"] = None
    req = replace(req, task="Explain the previous result.", session=first.session,
        requirements=replace(req.requirements, workspace_access="read"))
    second = await adapter.run(req, "second", emit)
    assert second.status == "done", second
    assert second.session == first.session
    assert any("QUARTZ-461" in str(message.get("content")) for message in requests[-1]["messages"])


async def test_real_pi_uses_existing_host_work_ledger(native_pi, tmp_path):
    from test_work_effect_executor import _host

    adapter, _requests, selected, _ = native_pi
    async with _host(tmp_path, provider="pi") as host:
        host.runtime.register(adapter)
        target = host.workspace / "daily-note.txt"
        selected.update(tool="write", args={"path": str(target), "content": "Host-owned work"})
        receipt = await host.executor.execute(host.effect_id)
        assert receipt["status"] == "terminal"
        assert target.read_text() == "Host-owned work"
        binding = receipt["binding"]
        attempt = host.work.get_attempt(binding["attempt_id"])
        assert attempt.execution_status == "succeeded"
        assert attempt.metadata["provider_session"]["provider"] == "pi"
        assert host.work.get_writer_lease(binding["attempt_id"]).status == "released"
        replay = await host.executor.execute(host.effect_id)
        assert replay["replayed"]
        assert len(host.work.list_attempts(binding["work_item_id"])) == 1


async def test_real_pi_cancel_releases_pending_native_confirmation(native_pi, tmp_path):
    adapter, _requests, selected, _ = native_pi
    selected.update(tool="bash", args={"command": "echo should-not-run"})
    req = ProviderRunRequest(provider="pi", task="Exercise cancellation.", cwd=str(tmp_path),
        requirements=ProviderRequirements(workspace_access="write", workspace_ownership="caller"))
    confirmations = []

    async def emit(event):
        if event.type == "permission.requested":
            confirmations.append(await adapter.cancel("cancel"))

    result = await adapter.run(req, "cancel", emit)
    assert result.status == "cancelled", result
    assert len(confirmations) == 1 and confirmations[0]["confirmed"]


@pytest.mark.parametrize("access,tool", [("read", "write"), ("none", "read"), ("write", "bash"), ("none", "web_fetch"), ("none", "web_read")])
async def test_real_pi_workspace_and_permission_policy(native_pi, tmp_path, access, tool):
    adapter, requests, selected, url = native_pi
    target = tmp_path / "protected.txt"
    target.write_text("preserve")
    selected.update(tool=tool, args={"path": str(target), "content": "changed"} if tool in {"write", "read"}
        else {"command": "echo should-not-run"} if tool == "bash" else {"url": url + "/page"})
    req = ProviderRunRequest(provider="pi", task="Exercise the selected tool.", cwd=str(tmp_path),
        requirements=ProviderRequirements(workspace_access=access, workspace_ownership="caller"))
    events = []

    async def emit(event):
        events.append(event)
        if event.type == "permission.requested":
            key = event.payload["permissionRequest"]["request_id"]
            assert (await adapter.resolve_permission("policy", ProviderPermissionResponse(key, False)))["accepted"]

    result = await adapter.run(req, "policy", emit)
    assert result.status == "done", result
    assert target.read_text() == "preserve"
    tools = [event for event in events if event.type == "tool.result"]
    assert len(tools) == 1
    assert tools[0].payload["ok"] == (tool in {"web_fetch", "web_read"})
    if tool == "bash":
        assert any(event.type == "permission.requested" for event in events)


@pytest.fixture
def local_search_mcp(tmp_path, monkeypatch):
    from config import settings
    # Native Pi -> Python helper -> official MCP client -> deterministic server.
    # Only the remote transport and OS side effects are substituted.
    helper = tmp_path / "record_open.py"
    opened = tmp_path / "opened.json"
    fail = tmp_path / "rate-limited"
    helper.write_text(f'''import json,sys
from pathlib import Path
from contextlib import asynccontextmanager
sys.path.insert(0, {str(RUNTIME_DIR.parents[1])!r})
from agent_host import pi_web_tools
from mcp import Client
from mcp.server import MCPServer
server = MCPServer("fixture-search")
@server.tool()
async def web_search_exa(query: str, objective: str, numResults: int = 5) -> str:
    if Path({str(fail)!r}).exists():
        raise RuntimeError("Search rate limited")
    return "Title: Requested article — Author Chen / 中文结果\\nURL: " + query
@asynccontextmanager
async def connection(spec):
    assert not spec.bearer_token_env_var and not spec.environment
    assert spec.url == "https://mcp.exa.ai/mcp?tools=web_search_exa"
    async with Client(server, raise_exceptions=False) as client:
        yield client
def open_url(url):
    Path({str(opened)!r}).write_text(json.dumps({{"url":url}}))
    return {{"status":"launch_requested","page_load_verified":False,"url":url}}
pi_web_tools.open_mcp_connection = connection
pi_web_tools.open_web_url = open_url
pi_web_tools.main()
''', encoding="utf-8")
    extension = tmp_path / "fixture.ts"
    extension.write_text('export default function() {\n'
        f'process.env.AMADEUS_PI_WEB_HELPER = {json.dumps(str(helper))};\n'
        '}\n', encoding="utf-8")
    monkeypatch.setattr(settings, "PI_EXTENSIONS_JSON", json.dumps([str(extension)]))
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    return opened, fail


async def test_real_pi_search_read_open_without_browser_provider(native_pi, tmp_path, local_search_mcp):
    adapter, requests, selected, url = native_pi
    opened, _ = local_search_mcp
    selected["steps"] = [
        {"tool": "web_search", "args": {"query": url + "/article", "objective": "Find the original article"}},
        {"tool": "web_read", "args": {"url": url + "/article"}},
        {"tool": "open_url", "args": {"url": url + "/article"}},
    ]
    events = []

    async def emit(event):
        events.append(event)

    result = await adapter.run(ProviderRunRequest(provider="pi", task="Find, read and open the article.",
        cwd=str(tmp_path), requirements=ProviderRequirements(workspace_access="none")), "journey", emit)
    assert result.status == "done", result
    outputs = [event for event in events if event.type == "tool.result"]
    assert len(outputs) == 3 and all(event.payload["ok"] for event in outputs), outputs
    assert result.activity_evidence.execution_items == 3
    transcript = json.dumps(requests[-1]["messages"], ensure_ascii=False)
    assert "verified article fact" in transcript and "launch_requested" in transcript
    assert "中文结果" in transcript
    assert json.loads(opened.read_text()) == {"url": url + "/article"}


async def test_real_pi_search_rate_limit_is_tool_failure_and_conversation_continues(native_pi, tmp_path, local_search_mcp):
    adapter, requests, selected, _ = native_pi
    _, fail = local_search_mcp
    fail.touch()
    selected.update(tool="web_search", args={"query": "an article", "objective": "Find original sources"})
    events = []

    async def emit(event):
        events.append(event)

    result = await adapter.run(ProviderRunRequest(provider="pi", task="Find an article.",
        cwd=str(tmp_path), requirements=ProviderRequirements(workspace_access="none")), "rate-limit", emit)
    assert result.status == "done", result
    output, = [event for event in events if event.type == "tool.result"]
    assert not output.payload["ok"]
    assert "Search rate limited" in json.dumps(requests[-1]["messages"])
