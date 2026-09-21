"""Pi RPC acceptance, native continuity and execution/permission boundaries."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from agent_host.adapters.pi import PiAdapter, PiStartupUnavailable
from agent_host.pi_rpc import PiCommandRejected, PiRpcClient, PiTransportError
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_types import ProviderPermissionResponse, ProviderRunRequest, ProviderSessionHandle


class FakePi:
    instances = []
    mode = "normal"

    def __init__(self, command, *, cwd, env):
        self.command, self.cwd, self.env = command, cwd, env
        self.events = asyncio.Queue()
        self.calls = []
        self.closed = False
        self.path = str(Path(env["PI_CODING_AGENT_DIR"]) / "session.jsonl")
        self.instances.append(self)

    async def start(self):
        if not Path(self.path).exists():
            Path(self.path).write_text(json.dumps({"type": "session", "cwd": self.cwd}) + "\n", encoding="utf-8")

    def finish(self, text="finished", reason="stop"):
        self.events.put_nowait({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": text}})
        self.events.put_nowait({"type": "message_end", "message": {"role": "assistant",
            "content": [{"type": "text", "text": text}], "stopReason": reason}})
        self.events.put_nowait({"type": "agent_end", "messages": []})
        self.events.put_nowait({"type": "agent_settled"})

    async def request(self, kind, **params):
        self.calls.append((kind, params))
        if kind == "get_state":
            active = self.mode in {"waiting", "permission"} and any(call == "prompt" for call, _ in self.calls)
            return {"sessionFile": self.path, "isStreaming": active, "pendingMessageCount": 0}
        if kind == "switch_session":
            self.path = params["sessionPath"]
            return {"cancelled": False}
        if kind == "prompt":
            if self.mode == "reject":
                raise PiCommandRejected("Pi rejected prompt")
            if self.mode == "lost_ack":
                raise PiTransportError("Pi prompt acknowledgement timed out")
            if self.mode == "normal":
                self.finish()
            elif self.mode == "permission":
                self.events.put_nowait({"type": "extension_ui_request", "method": "confirm",
                    "id": "approval-1", "title": "Execute shell", "message": "command"})
            elif self.mode == "lost_stream":
                self.events.put_nowait(PiTransportError("EOF"))
        if kind == "abort":
            self.finish(reason="aborted")
        return {}

    async def send(self, message):
        self.calls.append(("send", message))
        if message.get("type") == "extension_ui_response":
            self.finish("approved" if message.get("confirmed") else "denied")

    async def next_event(self):
        event = await self.events.get()
        if isinstance(event, Exception):
            raise event
        return event

    async def close(self):
        self.closed = True


@pytest.fixture
def pi_host(tmp_path, monkeypatch):
    monkeypatch.setattr(FakePi, "instances", [])
    monkeypatch.setattr(FakePi, "mode", "normal")
    adapter = PiAdapter(client_factory=FakePi, agent_dir=tmp_path / "pi", timeout=3)
    request = ProviderRunRequest(provider="pi", task="inspect this task", cwd=str(tmp_path),
        requirements=ProviderRequirements(task_kind="general", workspace_access="read", workspace_ownership="caller"))
    events = []

    async def emit(event):
        events.append(event)

    return adapter, request, events, emit


async def test_pi_retains_exact_session_across_processes(pi_host):
    adapter, request, events, emit = pi_host
    first = await adapter.run(request, "one", emit)
    assert first.status == "done" and first.result == "finished"
    assert first.session.scope == "interaction"
    assert first.activity_evidence.terminal_observed
    request.session = first.session
    second = await adapter.run(request, "two", emit)
    assert second.session == first.session
    assert ("switch_session", {"sessionPath": first.session.session_id}) in FakePi.instances[-1].calls
    assert all(client.closed for client in FakePi.instances)
    assert sum(event.type == "session.opened" for event in events) == 2


async def test_pi_forwards_host_presentation_language(pi_host, monkeypatch):
    from agent_host.adapters import pi

    adapter, request, _, emit = pi_host
    request.metadata["presentation_locale"] = "zh-CN"
    locales = []
    original = pi.with_progress_contract

    def contract(task, *, presentation_locale=None):
        locales.append(presentation_locale)
        return original(task, presentation_locale=presentation_locale)

    monkeypatch.setattr(pi, "with_progress_contract", contract)
    assert (await adapter.run(request, "localized", emit)).status == "done"
    assert locales == ["zh-CN"]


@pytest.mark.parametrize("mode,status", [("reject", "error"), ("lost_ack", "orphaned"), ("lost_stream", "orphaned")])
async def test_pi_never_retries_uncertain_submission(pi_host, monkeypatch, mode, status):
    adapter, request, events, emit = pi_host
    monkeypatch.setattr(FakePi, "mode", mode)
    result = await adapter.run(request, "one", emit)
    assert result.status == status
    assert sum(kind == "prompt" for kind, _ in FakePi.instances[0].calls) == 1
    assert FakePi.instances[0].closed


async def test_pi_appends_and_cancels_exact_active_run(pi_host, monkeypatch):
    adapter, request, events, emit = pi_host
    monkeypatch.setattr(FakePi, "mode", "waiting")
    running = asyncio.create_task(adapter.run(request, "one", emit))
    while not FakePi.instances or not any(kind == "prompt" for kind, _ in FakePi.instances[0].calls):
        await asyncio.sleep(0)
    assert (await adapter.append_input("one", "also inspect X")).state == "delivered"
    client = FakePi.instances[0]
    client.events.put_nowait({"type": "agent_end", "willRetry": True, "messages": []})
    await asyncio.sleep(0)
    assert not running.done()
    assert ("steer", {"message": "also inspect X"}) in client.calls
    cancelled = await adapter.cancel("one")
    assert cancelled["confirmed"] and cancelled["cancelled"]
    result = await running
    assert result.status == "cancelled"
    kinds = [kind for kind, _ in client.calls]
    assert kinds.index("clear_queue") < kinds.index("abort")
    assert (await adapter.append_input("one", "late")).state == "rejected"


@pytest.mark.parametrize("allow", [True, False])
async def test_pi_permission_uses_host_response(pi_host, monkeypatch, allow):
    adapter, request, events, emit = pi_host
    monkeypatch.setattr(FakePi, "mode", "permission")

    async def authorize(event):
        await emit(event)
        if event.type == "permission.requested":
            result = await adapter.resolve_permission("one", ProviderPermissionResponse("approval-1", allow))
            assert result["accepted"]

    result = await adapter.run(request, "one", authorize)
    assert result.result == ("approved" if allow else "denied")
    assert not (await adapter.resolve_permission("one", ProviderPermissionResponse("approval-1", True)))["accepted"]


@pytest.mark.parametrize("state", ["queued_after_settlement", "query_rejected"])
async def test_pi_append_ack_cannot_be_downgraded_to_safe_rejection(pi_host, monkeypatch, state):
    adapter, request, events, emit = pi_host
    monkeypatch.setattr(FakePi, "mode", "waiting")
    running = asyncio.create_task(adapter.run(request, "one", emit))
    while not FakePi.instances or not any(kind == "prompt" for kind, _ in FakePi.instances[0].calls):
        await asyncio.sleep(0)
    client = FakePi.instances[0]
    original = client.request

    async def race(kind, **params):
        if kind == "get_state":
            if state == "query_rejected":
                raise PiCommandRejected("Pi rejected get_state")
            return {"isStreaming": False, "pendingMessageCount": 1}
        return await original(kind, **params)

    monkeypatch.setattr(client, "request", race)
    try:
        assert (await adapter.append_input("one", "additional instruction")).state == "unknown"
        assert sum(kind == "steer" for kind, _ in client.calls) == 1
    finally:
        client.finish()
        await running


async def test_pi_rejects_missing_session_without_starting_new_conversation(pi_host):
    adapter, request, events, emit = pi_host
    request.session = ProviderSessionHandle("pi", str(adapter.agent_dir / "missing.jsonl"), "interaction")
    result = await adapter.run(request, "one", emit)
    assert result.status == "error"
    assert not any(kind == "prompt" for kind, _ in FakePi.instances[0].calls)


def test_pi_unavailable_runtime_uses_existing_startup_status(monkeypatch):
    from config import settings
    from agent_host.provider_bootstrap import builtin_provider_specs
    from agent_host.provider_runtime import ProviderRuntime
    from server.handlers import provider_handler

    monkeypatch.setattr(settings, "PI_PROVIDER_ENABLED", True)
    monkeypatch.setattr(settings, "PI_NODE_PATH", "amadeus-no-such-pi-node")
    spec = next(row for row in builtin_provider_specs() if row.provider_id == "pi")
    monkeypatch.setattr(provider_handler, "builtin_provider_specs", lambda: (spec,))
    monkeypatch.setattr(provider_handler, "acp_provider_specs", lambda: ())
    runtime = ProviderRuntime()
    monkeypatch.setattr(provider_handler, "runtime", runtime)
    handler = provider_handler.ProviderHandler()
    availability, = handler.provider_availability()
    assert availability["reason"] == "pi_node_unavailable"
    assert not availability["registered"]
    assert runtime.get_manifest("pi") is None


def _installed_pi_runtime(tmp_path, monkeypatch):
    from agent_host.adapters import pi
    from config import settings

    runtime = tmp_path / "pi_runtime"
    packages = {
        "@earendil-works/pi-coding-agent": pi.PI_VERSION,
        "pi-simple-web-tools": "0.1.0",
    }
    for name, version in packages.items():
        package = runtime / "node_modules" / name / "package.json"
        package.parent.mkdir(parents=True, exist_ok=True)
        package.write_text(json.dumps({"version": version}), encoding="utf-8")
    cli = runtime / "node_modules/@earendil-works/pi-coding-agent/dist/cli.js"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.touch()
    monkeypatch.setattr(pi, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(settings, "PI_EXTENSIONS_JSON", "[]")
    return runtime


def test_pi_startup_reuses_effective_model_credentials_and_checks_native_auth(tmp_path, monkeypatch):
    from config import settings

    runtime = _installed_pi_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "effective-dotenv-secret")
    observed = {}

    def probe(command, **kwargs):
        observed.update(command=command, **kwargs)
        return SimpleNamespace(returncode=0,
            stdout=json.dumps({"status": "ready", "provider": "deepseek", "authType": "api_key"}))

    adapter = PiAdapter(node_path=sys.executable, agent_dir=tmp_path / "agent",
        model_provider="deepseek", model="deepseek-v4-flash", startup_probe=probe)
    adapter.require_startup_ready()

    assert observed["command"] == [sys.executable, str(adapter._cli_path()), "auth", "check",
        "--provider", "deepseek", "--model", "deepseek-v4-flash", "--json", "--no-refresh"]
    assert observed["cwd"] == str(runtime)
    assert observed["env"]["DEEPSEEK_API_KEY"] == "effective-dotenv-secret"
    assert "effective-dotenv-secret" not in observed["command"]
    assert adapter._startup_readiness["authentication"] == "api_key"


def test_pi_startup_rejects_missing_selected_model_credentials(tmp_path, monkeypatch):
    _installed_pi_runtime(tmp_path, monkeypatch)

    def probe(_command, **_kwargs):
        return SimpleNamespace(returncode=1,
            stdout=json.dumps({"status": "not_ready", "provider": "deepseek",
                "reason": "credentials_not_configured"}))

    adapter = PiAdapter(node_path=sys.executable, agent_dir=tmp_path / "agent",
        model_provider="deepseek", model="deepseek-v4-flash", startup_probe=probe)
    with pytest.raises(PiStartupUnavailable) as raised:
        adapter.require_startup_ready()

    assert raised.value.availability["reason"] == "pi_model_credentials_unavailable"
    assert raised.value.availability["authentication"] == "unavailable"
    assert "Settings -> Models" in raised.value.availability["diagnostic"]


async def test_pi_rejects_concurrent_attachment_to_a_new_live_session(pi_host, monkeypatch):
    adapter, request, events, emit = pi_host
    monkeypatch.setattr(FakePi, "mode", "waiting")
    running = asyncio.create_task(adapter.run(request, "one", emit))
    while not events:
        await asyncio.sleep(0)
    request.session = events[0].session
    second = await adapter.run(request, "two", emit)
    assert second.status == "error"
    assert len(FakePi.instances) == 1
    FakePi.instances[0].finish()
    assert (await running).status == "done"


async def test_pi_refuses_native_restore_into_a_different_workspace(pi_host, tmp_path):
    adapter, request, events, emit = pi_host
    first = await adapter.run(request, "one", emit)
    other = tmp_path / "other"
    other.mkdir()
    request.session, request.cwd = first.session, str(other)
    second = await adapter.run(request, "two", emit)
    assert second.status == "error"
    assert not any(kind == "prompt" for kind, _ in FakePi.instances[-1].calls)


async def test_native_rpc_frames_unicode_and_correlates_out_of_order_responses(tmp_path):
    peer = tmp_path / "peer.py"
    peer.write_text('''import sys,json
items=[json.loads(sys.stdin.readline()) for _ in range(2)]
print(json.dumps({"type":"message_update","text":"first\\u2028second\\u2029third"}),flush=True)
for item in reversed(items):
 print(json.dumps({"type":"response","id":item["id"],"command":item["type"],"success":True,"data":{"echo":item["type"]}}),flush=True)
sys.stdin.readline()
''', encoding="utf-8")
    client = PiRpcClient([sys.executable, str(peer)], cwd=str(tmp_path), env={})
    try:
        await client.start()
        replies = await asyncio.gather(client.request("first"), client.request("second"))
        assert replies == [{"echo": "first"}, {"echo": "second"}]
        assert (await client.next_event())["text"] == "first\u2028second\u2029third"
    finally:
        await client.close()


async def test_native_rpc_eof_does_not_claim_acceptance(tmp_path):
    client = PiRpcClient([sys.executable, "-c", "import sys; sys.stdin.readline()"], cwd=str(tmp_path), env={})
    try:
        await client.start()
        with pytest.raises(PiTransportError):
            await client.request("prompt", message="do not retry me")
    finally:
        await client.close()
