"""Browser ownership gates reject only the conflicting effect, not later Chat."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_runtime import ProviderRuntime
from core import session_manager as sm
import core.turn_coordinator as tc
from server.control_ledger import ControlLedgerStore
from server.cooperative_chat_ingress import CooperativeChatManager
from server.handlers.chat_handler import ChatHandler
from server.interaction_branch import InteractionBranchCoordinator, InteractionBranchState
from server.protocol import Method
from server.event_bus import bus
import server.interaction_branch as branch_module
from test_cooperative_context_recovery import InteractiveNativeFixture


@pytest.fixture
async def browser_gate_host(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    session = "browser-gates"
    sm.create_session(session)
    ledger = ControlLedgerStore(tmp_path / "control.sqlite3")
    runtime, handler = ProviderRuntime(), ChatHandler()
    adapter = InteractiveNativeFixture()
    runtime.register(adapter)
    calls, stops, frames = [], [], []

    async def run(params):
        calls.append(params)
        return {"run": {"run_id": f"browser-{len(calls)}", "provider": "browser", "status": "running"}}

    async def cancel(run_id, **kwargs):
        stops.append(run_id)
        return {"confirmed": False, "cancelled": False, "reason": "browser_transport_unknown"}

    browser = InteractionBranchCoordinator(provider_run=run, provider_cancel=cancel, root=tmp_path / "branches")
    branch = InteractionBranchState(branch_id="counter-branch", parent_session_id=session,
        provider="browser", status="idle", goal="Use the counter", browser_session_id="counter-native",
        title="Counter", url="https://example.test/counter", page_summary="Counter is 0", expires_at=10**12)
    browser._active_by_session[session] = branch
    monkeypatch.setattr(branch_module, "_current_coordinator", None)
    browser.configure()

    async def query(messages, **kwargs):
        frame = json.loads(messages[-1]["content"])
        frames.append(frame)
        if frame["source_kind"] != "user":
            return "確認したわ。"
        text = frame["current"]["text"]
        action = {"再点一次加一。": {"op": "browser", "intent": "continue"},
            "关闭网页。": {"op": "browser", "intent": "close"},
            "打开 https://example.test/new。": {"op": "browser", "intent": "open", "target": "https://example.test/new"}}.get(text)
        return json.dumps({"action": action, "say": "好的。"}, ensure_ascii=False)

    def allocate(*args):
        raise AssertionError("Browser or ordinary Chat cannot allocate a native agent context")

    manager = CooperativeChatManager(handler, ledger=ledger, fence_scope="browser-gates",
        provider=adapter.provider_id, runtime=runtime,
        context_requirements={adapter.provider_id: ProviderRequirements(workspace_access="write", workspace_ownership="caller", resume="attach")},
        allocate=allocate, query=query, publish_factory=lambda session: lambda event: True)
    manager.configure_browser(browser)
    manager.install()

    async def send(text, key):
        accepted = await handler.send_text(text, session_id=session, turn_id=key)
        if accepted["status"] == "replayed":
            return accepted
        assert accepted["status"] == "ok"
        await asyncio.wait_for(handler._stream_task, 3)
        return manager.ingresses[session].receipts[key]

    async def terminal(run_id, branch_id="counter-branch"):
        await browser._on_provider_result(Method.PROVIDER_RESULT, {
            "provider": "browser", "run_id": run_id, "status": "done", "result": "Counter updated",
            "metadata": {"session_id": session, "interaction_branch_id": branch_id,
                "browser": {"browser_session_id": "counter-native", "current_url": branch.url},
                "provider_branch": {"branch_id": branch_id, "actions": [], "final_report": "Counter updated"}}})

    try:
        yield SimpleNamespace(send=send, terminal=terminal, browser=browser, branch=branch,
            calls=calls, stops=stops, frames=frames, manager=manager, session=session)
    finally:
        await handler.close()
        await manager.close()
        await runtime.close()
        bus.off(Method.PROVIDER_RESULT, browser._on_provider_result)
        bus.off(Method.PROVIDER_EVENT, browser._on_provider_event)
        ledger.close()


async def test_same_browser_words_new_turn_continue_but_transport_replay_does_not(browser_gate_host):
    host = browser_gate_host
    first = await host.send("再点一次加一。", "click-one")
    assert first["state"] == "browser_accepted"
    await host.terminal(first["run_id"])
    assert (await host.send("再点一次加一。", "click-one"))["status"] == "replayed"
    assert len(host.calls) == 1
    second = await host.send("再点一次加一。", "click-two")
    assert second["state"] == "browser_accepted"
    assert len(host.calls) == 2
    assert {call["metadata"]["interaction_branch_id"] for call in host.calls} == {"counter-branch"}
    assert {call["metadata"]["browser_session_id"] for call in host.calls} == {"counter-native"}
    assert (await host.send("谢谢，聊点别的。", "chat"))["state"] == "no_action"
    await host.terminal(second["run_id"])


async def test_unconfirmed_browser_close_blocks_conflicting_effect_but_not_chat(browser_gate_host):
    host = browser_gate_host
    first = await host.send("再点一次加一。", "click")
    closed = await host.send("关闭网页。", "close")
    assert closed["state"] == "browser_unknown"
    assert host.stops == [first["run_id"]]
    assert (await host.send("谢谢，聊点别的。", "chat-one"))["state"] == "no_action"
    blocked = await host.send("打开 https://example.test/new。", "new-conflict")
    assert blocked["state"] not in {"browser_accepted", "started"}
    assert len(host.calls) == 1
    # An unrelated terminal event cannot remove the exact uncertain owner.
    await host.terminal("unrelated-run", "unrelated-branch")
    assert host.browser.termination_pending_for_session(host.session)
    assert (await host.send("谢谢，聊点别的。", "chat-two"))["state"] == "no_action"
    await host.terminal(first["run_id"])
    assert not host.browser.termination_pending_for_session(host.session)
    opened = await host.send("打开 https://example.test/new。", "new-after-terminal")
    assert opened["state"] == "browser_accepted"
    assert len(host.calls) == 2
