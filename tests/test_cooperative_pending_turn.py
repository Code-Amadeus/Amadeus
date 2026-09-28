"""Speculative interpretation shares the confirmed Chat execution owner."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core import session_manager as sm
import core.turn_coordinator as tc
from server.cooperative_chat_ingress import CooperativeChatManager
from server.cooperative_delivery import CooperativeHostDelivery
from agent_host.work_ledger_store import WorkLedgerConflict
from server.event_bus import bus
from server.handlers.chat_handler import ChatHandler
from server.protocol import Method
from server.speculative_turn import SpeculativeTurnLauncher
from server.work_destination_service import WorkDestinationService
from test_work_effect_executor import _host, _payload


@pytest.fixture
async def pending_host(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    monkeypatch.setattr("config.settings.ASR_SPECULATIVE_LLM_START", True)
    session_id = "speculative-session"
    sm.create_session(session_id)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("config.settings.WORK_SCRATCH_ROOT", str(scratch))
    async with _host(tmp_path) as host:
        queried, release_query = asyncio.Event(), asyncio.Event()
        release_query.set()
        publications, spoken, visible, frames = [], [], [], []

        async def query(messages, **_kwargs):
            frame = json.loads(messages[-1]["content"])
            frames.append(frame)
            if frame["source_kind"] == "user":
                queried.set()
                await release_query.wait()
                if frame["current"]["text"] == "malformed":
                    return "not JSON"
                action = {"op":"work", "intent":"execute"} if frame["current"]["text"] != "thanks" else None
            else:
                return "Ready."
            return json.dumps({"say":"Ready.", "action":action})

        async def capture(method, params):
            visible.append((method, params))

        async def voice(payload):
            spoken.append(payload)
            return {"status":"queued"}

        methods = (Method.CHAT_TOKEN, Method.CHAT_COMPLETE, Method.CHAT_ERROR)
        for method in methods:
            bus.on(method, capture)
        handler = ChatHandler()
        manager = CooperativeChatManager(handler, ledger=host.control_store,
            fence_scope="cooperative:pending-test", provider=host.adapter.provider_id,
            runtime=host.runtime, context_requirements={host.adapter.provider_id:
                _payload(host.project.project_id, host.adapter.provider_id).requirements},
            allocate=Mock(side_effect=AssertionError("Work must retain its own destination")),
            query=query, publish_factory=lambda session:CooperativeHostDelivery(
                session_id=session, display=lambda event:publications.append(event) or True,
                narration_sink=voice, record_display=sm.append_session_message),
            destination=WorkDestinationService(host.work, registry_check=lambda _path:True,
                scratch_root_provider=lambda:scratch))
        manager.configure_work(host.control, host.executor)
        interrupt = AsyncMock()
        manager.install(background_interaction_interrupt=interrupt)
        launcher = SpeculativeTurnLauncher()
        launcher.configure(
            send_pending=lambda text, **kwargs:handler.send_text(text, pending=True, **kwargs),
            confirm=handler.confirm_pending_turn, discard=handler.discard_pending_turn,
            provider_getter=lambda:"hybrid3", asr_source_getter=lambda:"wake",
            chat_busy_fn=handler.is_busy, voice_allowed_fn=AsyncMock(return_value=True),
            session_id_factory=lambda:session_id)

        async def finish():
            if handler._stream_task is not None:
                await asyncio.gather(handler._stream_task, return_exceptions=True)
            if manager._work_tasks:
                await asyncio.gather(*tuple(manager._work_tasks))

        try:
            yield SimpleNamespace(host=host, handler=handler, manager=manager,
                launcher=launcher, queried=queried, release_query=release_query,
                publications=publications, spoken=spoken, visible=visible, frames=frames,
                session_id=session_id, interrupt=interrupt, finish=finish)
        finally:
            release_query.set()
            await launcher.abandon("test_cleanup")
            await handler.close()
            await manager.close()
            for method in methods:
                bus.off(method, capture)


def assert_pending_is_private(context):
    ingress = context.manager.ingresses[context.session_id]
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []
    assert context.publications == context.visible == []
    assert context.spoken == []
    assert ingress.loop.history == []
    assert sm.conversation_history.snapshot().dialog == []
    context.interrupt.assert_not_awaited()


async def test_handler_gui_callback_reaches_existing_ingress_loop(pending_host):
    context = pending_host
    ingress = await context.manager._ingress_for(context.session_id)
    original_submit = ingress.loop.submit
    callbacks = []

    async def submit(*args, **kwargs):
        callbacks.append(kwargs.get("gui_callback"))
        return await original_submit(*args, **kwargs)

    ingress.loop.submit = submit
    await context.handler.send_text("thanks", session_id=context.session_id,
        turn_id="gui-callback-forwarded")
    await context.finish()
    callback, = callbacks
    assert callable(callback)
    assert context.host.adapter.calls == 0


@pytest.mark.parametrize("during_query", [False, True])
async def test_confirmation_reuses_one_interpretation_and_dispatch_then_normal_voice(pending_host, during_query):
    context = pending_host
    if during_query:
        context.release_query.clear()
    assert await context.launcher.launch("Build the page")
    turn_id = context.launcher._slot_turn_id
    await asyncio.wait_for(context.queried.wait(), 3)
    assert_pending_is_private(context)
    assert tc.coordinator.turn_gate(turn_id) == "wait"
    assert await context.launcher.resolve("Build the page")
    context.release_query.set()
    await context.finish()
    assert context.host.adapter.calls == 1
    assert len(context.frames) == 1
    assert len(context.publications) == len(context.spoken) == 1
    assert context.publications[0]["text"] == context.spoken[0]["display_text"] == "Ready."
    assert not await context.handler.confirm_pending_turn(turn_id)
    replay = await context.handler.send_text("Build the page", session_id=context.session_id,
        turn_id=turn_id, source="wake")
    assert replay["status"] == "replayed"
    assert context.host.adapter.calls == 1
    assert await context.handler.send_text("thanks", session_id=context.session_id,
        turn_id="normal-voice", source="wake") == {"status":"ok", "turn_id":"normal-voice"}
    await context.finish()
    assert context.host.adapter.calls == 1
    assert len([frame for frame in context.frames if frame["source_kind"] == "user"]) == 2
    assert context.host.control_store.find_admission("chat:" + context.session_id,
        turn_id)["plan_id"]


@pytest.mark.parametrize("during_query", [False, True])
async def test_discarded_speculation_never_dispatches_and_corrected_voice_runs_once(pending_host, during_query):
    context = pending_host
    if during_query:
        context.release_query.clear()
    assert await context.launcher.launch("Draft mistaken instruction")
    turn_id = context.launcher._slot_turn_id
    await asyncio.wait_for(context.queried.wait(), 3)
    assert_pending_is_private(context)
    assert not await context.launcher.resolve("Build the corrected page")
    context.release_query.set()
    ingress = context.manager.ingresses[context.session_id]
    await asyncio.gather(*(task for _, task in ingress.loop._inputs.values()), return_exceptions=True)
    await context.finish()
    assert tc.coordinator.turn_gate(turn_id) == "drop"
    assert_pending_is_private(context)
    assert not await context.handler.confirm_pending_turn(turn_id)
    replay = await context.handler.send_text("Draft mistaken instruction", session_id=context.session_id,
        turn_id=turn_id, source="wake")
    assert replay["status"] == "replayed"
    assert_pending_is_private(context)
    await context.handler.send_text("Build the corrected page", session_id=context.session_id,
        turn_id="corrected-voice", source="wake")
    await context.finish()
    assert context.host.adapter.calls == 1
    assert context.host.adapter.requests[0]["request"].task == "Build the corrected page"


async def test_failed_pending_interpretation_has_no_visible_error_or_dispatch(pending_host):
    context = pending_host
    assert await context.launcher.launch("malformed")
    await context.finish()
    assert_pending_is_private(context)
    assert not await context.launcher.resolve("malformed")


async def test_dispatch_rejection_never_publishes_the_precomputed_work_reply(pending_host, monkeypatch):
    context = pending_host
    monkeypatch.setattr(context.host.executor, "dispatch",
        AsyncMock(side_effect=WorkLedgerConflict("dispatch rejected")))
    await context.handler.send_text("Build the rejected page", session_id=context.session_id,
        turn_id="rejected-work", source="wake")
    await context.finish()
    assert len(context.frames) == 1
    assert context.publications == context.spoken == []
    assert context.host.adapter.calls == 0 and context.host.work.list_work_items() == []
    assert any(method == Method.CHAT_ERROR for method, _ in context.visible)


async def test_pending_confirmation_timeout_retires_source_without_execution(pending_host, monkeypatch):
    context = pending_host
    monkeypatch.setattr("server.cooperative_chat_ingress.PENDING_TURN_GATE_TIMEOUT_S", 0.0)
    assert await context.launcher.launch("Unconfirmed page")
    await context.finish()
    assert_pending_is_private(context)
    assert not await context.launcher.resolve("Unconfirmed page")


@pytest.mark.parametrize("action", ["none", "leave", "step"])
async def test_pending_auip_waits_for_confirmation_before_read_action_or_rendering(pending_host, action):
    context = pending_host
    captured = asyncio.Event()
    decision = SimpleNamespace(status="ok", action=action, app_session_id="app",
        read_facets=("state",) if action == "none" else (),
        control_attrs=lambda:{"action":action, "app_session_id":"app"})

    def capture(**_kwargs):
        captured.set()
        return decision

    render = Mock(return_value="Current application state.")
    context.manager.auip_decider = SimpleNamespace(capture=capture, render_read_only_answer=render)
    context.manager.auip_router = AsyncMock(return_value={"ok":True})
    assert await context.launcher.launch("Read the application")
    await asyncio.wait_for(captured.wait(), 3)
    assert_pending_is_private(context)
    render.assert_not_called()
    context.manager.auip_router.assert_not_awaited()
    assert await context.launcher.resolve("Read the application")
    await context.finish()
    if action == "none":
        render.assert_called_once()
        context.manager.auip_router.assert_not_awaited()
    else:
        context.manager.auip_router.assert_awaited_once()
    assert any(method == Method.CHAT_COMPLETE for method, _ in context.visible)
    assert context.host.adapter.calls == 0
