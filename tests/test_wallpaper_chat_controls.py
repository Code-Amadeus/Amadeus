"""Windows composer actions reuse the existing chat and voice lifecycle owners."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from server.event_bus import EventBus
from server.handlers.asr_handler import AsrHandler
from server.handlers.wallpaper_handler import WallpaperHandler
from server.protocol import Method
from server.wallpaper_chat import wallpaper_chat_control


def owners():
    return dict(
        session=SimpleNamespace(
            ensure_current_session=AsyncMock(return_value={"ok": True, "current_session_id": "s1"}),
            handle=AsyncMock(return_value={"ok": True, "current_session_id": "s2"}),
        ),
        asr=SimpleNamespace(
            listening_state=Mock(return_value={"active": False, "source": "", "continuous": False}),
            handle=AsyncMock(return_value={"status": "stopped"}),
        ),
        system=SimpleNamespace(handle=AsyncMock(return_value={"chat_supports_images": True})),
        wake=SimpleNamespace(status=AsyncMock(return_value={"running": True})),
        voice_start=AsyncMock(return_value={"status": "awake"}),
    )


async def test_button_enters_the_existing_wake_route(monkeypatch):
    monkeypatch.setattr("config.settings.WAKE_AUTO_SEND_TO_CHAT", True)
    deps = owners()
    assert await wallpaper_chat_control({"action": "voice_start"}, **deps) == {"ok": True}
    deps["voice_start"].assert_awaited_once_with()
    deps["asr"].handle.assert_not_awaited()


async def test_button_does_not_take_another_surface_microphone(monkeypatch):
    monkeypatch.setattr("config.settings.WAKE_AUTO_SEND_TO_CHAT", True)
    deps = owners()
    deps["asr"].listening_state.return_value = {"active": True, "source": "vn_player"}
    assert (await wallpaper_chat_control({"action": "voice_start"}, **deps))["error"] == "already_listening"
    deps["voice_start"].assert_not_awaited()


async def test_new_chat_uses_shared_session_owner_and_stops_only_wake_voice():
    deps = owners()
    result = await wallpaper_chat_control({"action": "new_chat"}, **deps)
    assert result["current_session_id"] == "s2"
    deps["asr"].handle.assert_awaited_once_with(Method.ASR_STOP, {"source": "wake"})
    deps["session"].handle.assert_awaited_once_with(Method.SESSION_CREATE, {"source": "wallpaper_keyboard"})


@pytest.mark.parametrize("source,stops", [("", False), ("vn_player", False), ("wake", True)])
async def test_source_scoped_stop_respects_microphone_owner(source, stops):
    handler = AsrHandler()
    handler._source = source
    handler.stop_listening = AsyncMock(return_value={"status": "stopped"})
    await handler.handle(Method.ASR_STOP, {"source": "wake"})
    assert handler.stop_listening.await_count == int(stops)
    # Existing unscoped Chat stop retains its behavior.
    await handler.handle(Method.ASR_STOP, {})
    assert handler.stop_listening.await_count == int(stops) + 1


@pytest.mark.parametrize("continuous,expected", [(False, ["first"]), (True, ["first", "second"])])
async def test_continuous_voice_reuses_wake_delivery_and_survives_silence(monkeypatch, continuous, expected):
    monkeypatch.setattr("server.handlers.asr_handler.bus", EventBus())
    handler = AsrHandler()
    heard = []
    calls = 0

    def listen_for_speech(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "first"
        if calls == 2:
            handler._awake_until = time.monotonic() - 1
            return None
        if calls == 3:
            return "second"
        raise AssertionError("unexpected additional microphone read")

    async def recognized(payload):
        assert payload["source"] == "wake"
        heard.append(payload["text"])
        await handler.notify_turn_complete("test_reply_finished")
        if len(heard) == 2:
            handler._active = False

    handler.configure(asr_manager=SimpleNamespace(is_ready=True, listen_for_speech=listen_for_speech), on_recognized=recognized)
    monkeypatch.setattr(handler, "schedule_unload", Mock())
    await handler.start_listening({"source": "wake", "awake_seconds": 30, "continuous": continuous, "finish_after_turn_complete": False})
    await asyncio.wait_for(handler._listen_task, 3)
    assert heard == expected
    await handler.stop_listening()
    assert handler.listening_state() == {"active": False, "source": "", "continuous": False}


async def test_selected_window_and_watching_share_visual_runtime():
    deps = owners()
    deps["system"].handle.return_value = {"chat_supports_images": True, "vision_enabled": True, "vision_mode": "watching"}
    await wallpaper_chat_control({"action": "vision_select", "hwnd": "123"}, **deps)
    deps["system"].handle.assert_awaited_with(Method.SYSTEM_SET_CONFIG, {"values": {
        "vision_enabled": True, "vision_mode": "watching", "vision_scope": "selected_window", "vision_window_handle": "123",
    }})


async def test_image_is_forwarded_with_the_same_session_as_text():
    handler = WallpaperHandler()
    handler._ensure_chat_session_fn = AsyncMock(return_value={"ok": True, "current_session_id": "s1"})
    handler._chat_send_fn = AsyncMock(return_value={"status": "ok"})
    visual = {"mode": "attachment", "frame": {"dataUrl": "data:image/jpeg;base64,AA=="}}
    assert (await handler._route_chat_submit({"text": "look", "visual": visual}))["ok"]
    handler._chat_send_fn.assert_awaited_once_with("look", "s1", visual)


@pytest.mark.parametrize("enabled", [False, True])
async def test_voice_projection_only_reaches_opted_in_composer(enabled):
    handler = WallpaperHandler()
    host = SimpleNamespace(composer_event=Mock())
    handler._wallpaper_host = host
    handler._chat_control_fn = AsyncMock() if enabled else None
    await handler._forward_composer_event(Method.WAKE_STATUS, {
        "status": "listening", "running": True, "heard": "private speech", "score": 0.9,
    })
    if enabled:
        host.composer_event.assert_called_once_with({
            "method": Method.WAKE_STATUS, "params": {"status": "listening", "running": True},
        })
    else:
        host.composer_event.assert_not_called()


async def test_wallpaper_stop_revokes_continuous_voice_and_bridge_before_cleanup(monkeypatch):
    monkeypatch.setattr("server.handlers.asr_handler.bus", EventBus())
    monkeypatch.setattr("server.handlers.wallpaper_handler.bus", EventBus())
    monkeypatch.setattr("server.handlers.wallpaper_handler.WAKE_ENABLED", True)
    monkeypatch.setattr("server.handlers.wallpaper_handler.WAKE_AUTO_START_WITH_WALLPAPER", True)
    asr = AsrHandler()
    asr._active, asr._source, asr._continuous_awake = True, "wake", True
    listener = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    asr._listen_task = listener
    monkeypatch.setattr(asr, "schedule_unload", Mock())
    handler = WallpaperHandler()
    host = SimpleNamespace(stop=Mock())
    handler._wallpaper_host = host
    handler._wake_stop_fn = AsyncMock()

    async def control(payload):
        assert handler.is_running() is False
        return await asr.handle(Method.ASR_STOP, {"source": "wake"})

    handler._chat_control_fn = control
    await handler.handle(Method.WALLPAPER_STOP, {})
    assert asr.listening_state() == {"active": False, "source": "", "continuous": False}
    await asyncio.gather(listener, return_exceptions=True)
    assert listener.cancelled()
    host.stop.assert_called_once()
    handler._wake_stop_fn.assert_awaited_once()
