from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from server.handlers.asr_handler import AsrHandler
from server.handlers.vn_player_handler import VNPlayerHandler
from server.protocol import Method
from server import visual_runtime


def fixture():
    handler = VNPlayerHandler()
    status = {"status": "active", "capabilities": {"interaction": {"enabled": True, "reason": "ready"}},
              "visual": {"supported": True, "reason": "ready"}}
    runtime = SimpleNamespace(enabled=True, profile=SimpleNamespace(session_id="game_a"), status=lambda: status,
                              player_intervention=AsyncMock(return_value={"status": "ok"}))
    async def stop(_params):
        runtime.enabled = False
        status["status"] = "stopped"
    runtime.stop = stop
    handler._runtime = runtime
    handler._event_emit = AsyncMock()
    state = {"active": False, "source": "", "source_payload": {}}
    async def asr(method, params):
        if method == Method.ASR_START:
            state.update(active=True, source=params["source"], source_payload=params["source_payload"])
            return {"status": "listening"}
        if state["source_payload"].get("input_id") == params.get("input_id"):
            state.update(active=False, source="", source_payload={})
        return {"status": "stopped"}
    handler._asr_control = AsyncMock(side_effect=asr)
    handler._asr_state = lambda: state
    handler._capture_game_view = AsyncMock(return_value={"visual_context": {"frame": {"dataUrl": "game-frame"}}})
    return handler, runtime, status, state


def test_voice_is_session_owned_and_revokes_late_recognition():
    async def run():
        handler, runtime, _, state = fixture()
        assert handler.status()["inputs"]["voice"]["enabled"] is False
        await handler.set_inputs({"session_id": "game_a", "voice": True})
        first = dict(state["source_payload"])
        await handler.handle_asr({"text": "hello", "source_payload": first})
        assert runtime.player_intervention.await_count == 1
        await handler.set_inputs({"session_id": "game_a", "voice": False})
        assert (await handler.handle_asr({"text": "late", "source_payload": first}))["status"] == "ignored"
        await handler.set_inputs({"session_id": "game_a", "voice": True})
        assert state["source_payload"]["input_id"] != first["input_id"]
        assert (await handler.handle_asr({"text": "old generation", "source_payload": first}))["status"] == "ignored"
        with pytest.raises(ValueError, match="ended or changed"):
            await handler.set_inputs({"session_id": "previous", "voice": False})
        await handler.handle(Method.VN_STOP, {})
        assert state["active"] is False
        assert handler.status()["inputs"]["vision"]["mode"] == "off"
    asyncio.run(run())


def test_voice_requires_interaction_even_for_notes_and_stops_when_unavailable():
    async def run():
        handler, runtime, status, state = fixture()
        await handler.set_inputs({"session_id": "game_a", "voice": True, "kind": "note"})
        payload = dict(state["source_payload"])
        status["capabilities"]["interaction"].update(enabled=False, reason="model_unavailable")
        assert (await handler.handle_asr({"text": "not a permitted note", "source_payload": payload}))["status"] == "ignored"
        await handler.handle(Method.VN_STATUS, {})
        assert state["active"] is False
        with pytest.raises(RuntimeError):
            await handler.set_inputs({"session_id": "game_a", "voice": True})
        runtime.player_intervention.assert_not_awaited()
    asyncio.run(run())


def test_vn_visual_mode_is_independent_of_general_vision_and_shared_by_text_and_voice(monkeypatch):
    # Any attempt to consult or change the general capture policy is a regression.
    monkeypatch.setattr(visual_runtime, "get_config", Mock(side_effect=AssertionError("general vision consulted")))
    monkeypatch.setattr(visual_runtime, "set_config", Mock(side_effect=AssertionError("general vision changed")))
    async def run():
        handler, runtime, _, state = fixture()
        handler._capture_game_view.side_effect = [
            {"visual_context": {"frame": {"dataUrl": "fresh-typed"}}},
            {"visual_context": {"frame": {"dataUrl": "fresh-spoken"}}},
        ]
        await handler.set_inputs({"session_id": "game_a", "voice": True, "vision_mode": "on_question"})
        await handler.handle(Method.VN_PLAYER_ASK, {"text": "typed", "session_id": "game_a"})
        assert runtime.player_intervention.await_args.args[1]["visual_context"]["frame"]["dataUrl"] == "fresh-typed"
        await handler.handle_asr({"text": "spoken", "source_payload": dict(state["source_payload"])})
        assert runtime.player_intervention.await_args.args[1]["visual_context"]["frame"]["dataUrl"] == "fresh-spoken"
        await handler.handle(Method.VN_PLAYER_NOTE, {"text": "note"})
        assert "visual_context" not in runtime.player_intervention.await_args.args[1]
        await handler.set_inputs({"session_id": "game_a", "vision_mode": "off"})
        assert handler.status()["inputs"]["voice"]["enabled"] is True
        await handler.handle(Method.VN_PLAYER_ASK, {"text": "text only"})
        assert "visual_context" not in runtime.player_intervention.await_args.args[1]
        assert handler._capture_game_view.await_count == 2
    asyncio.run(run())


def test_capture_failure_is_visible_and_does_not_silently_answer_without_vision():
    async def run():
        handler, runtime, _, _ = fixture()
        await handler.set_inputs({"session_id": "game_a", "vision_mode": "on_question"})
        handler._capture_game_view.side_effect = RuntimeError("Game window closed")
        result = await handler.handle(Method.VN_PLAYER_ASK, {"text": "look"})
        assert result["reason"] == "visual_capture_failed"
        assert result["error"] == "Game window closed"
        runtime.player_intervention.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["voice_off", "session_changed", "vision_off"])
def test_pending_capture_obeys_current_session_and_input_permissions(change):
    async def run():
        handler, runtime, _, state = fixture()
        await handler.set_inputs({"session_id": "game_a", "voice": True, "vision_mode": "on_question"})
        entered, release = asyncio.Event(), asyncio.Event()
        async def capture():
            entered.set()
            await release.wait()
            return {"visual_context": {"frame": {"dataUrl": "late-frame"}}}
        handler._capture_game_view = capture
        task = asyncio.create_task(handler.handle_asr({"text": "look", "source_payload": dict(state["source_payload"])}))
        await entered.wait()
        if change == "session_changed":
            runtime.profile.session_id = "game_b"
        elif change == "voice_off":
            await handler.set_inputs({"session_id": "game_a", "voice": False})
        else:
            await handler.set_inputs({"session_id": "game_a", "vision_mode": "off"})
        release.set()
        await task
        if change == "vision_off":
            assert "visual_context" not in runtime.player_intervention.await_args.args[1]
        else:
            runtime.player_intervention.assert_not_awaited()
    asyncio.run(run())


def test_asr_stop_cannot_stop_another_scene_session_or_new_input():
    async def run():
        asr = AsrHandler()
        asr._source = "vn_player"
        asr._source_payload = {"session_id": "new", "input_id": "new_input"}
        asr.stop_listening = AsyncMock(return_value={"status": "stopped"})
        for params in ({"source": "chat"}, {"source": "vn_player", "session_id": "old"},
                       {"source": "vn_player", "session_id": "new", "input_id": "old_input"}):
            assert (await asr.handle(Method.ASR_STOP, params))["status"] == "ignored"
        asr.stop_listening.assert_not_awaited()
        await asr.handle(Method.ASR_STOP, {"source": "vn_player", "session_id": "new", "input_id": "new_input"})
        asr.stop_listening.assert_awaited_once()
    asyncio.run(run())


def test_stopping_during_microphone_start_does_not_leave_a_late_listener():
    async def run():
        handler, _, _, state = fixture()
        entered, release = asyncio.Event(), asyncio.Event()
        original = handler._asr_control
        async def slow_asr(method, params):
            if method == Method.ASR_START:
                entered.set()
                await release.wait()
            return await original(method, params)
        handler._asr_control = slow_asr
        starting = asyncio.create_task(handler.set_inputs({"session_id": "game_a", "voice": True}))
        await entered.wait()
        await handler.handle(Method.VN_STOP, {})
        release.set()
        await starting
        assert state["active"] is False
        assert handler.status()["inputs"]["voice"]["enabled"] is False
    asyncio.run(run())


def test_session_start_applies_saved_defaults_without_capturing_a_frame():
    async def run():
        handler, runtime, status, _ = fixture()
        runtime.start = AsyncMock(return_value=status)
        result = await handler.handle(Method.VN_START, {"vision_mode": "on_question", "voice_input": True})
        assert result["inputs"]["voice"]["enabled"] is True
        assert result["inputs"]["vision"]["mode"] == "on_question"
        handler._capture_game_view.assert_not_awaited()
        await handler.handle(Method.VN_STOP, {})
        assert handler.status()["inputs"]["vision"]["mode"] == "off"
    asyncio.run(run())
