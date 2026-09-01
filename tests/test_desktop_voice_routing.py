from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import numpy as np
import pytest

from asr.wake_service import WakeService
from server.desktop_voice import (
    is_desktop_voice_exit_command,
    is_manual_wake_command,
)
from server.handlers.asr_handler import AsrHandler
from server.handlers.chat_handler import ChatHandler
from server.protocol import Method


@pytest.mark.parametrize("text", ["停止对话", "结束对话。", " 退出对话！ "])
def test_exact_desktop_voice_exit_commands(text: str) -> None:
    assert is_desktop_voice_exit_command(text) is True


@pytest.mark.parametrize("text", ["如何停止对话", "不要结束对话", "退出对话模式怎么用"])
def test_exit_words_inside_a_sentence_are_normal_chat(text: str) -> None:
    assert is_desktop_voice_exit_command(text) is False


@pytest.mark.parametrize(
    "text",
    ["Hi, Amadeus", " hi amadeus! ", "Ｈｉ，Ａｍａｄｅｕｓ。"],
)
def test_manual_wake_accepts_only_a_complete_configured_phrase(text: str) -> None:
    assert is_manual_wake_command(text, "Hi Amadeus,Hey Amadeus") is True


@pytest.mark.parametrize(
    "text",
    ["为什么 Hi Amadeus 没反应", "Hi Amadeus 今天天气如何", "Amadeus"],
)
def test_wake_phrase_inside_normal_chat_does_not_trigger_manual_wake(text: str) -> None:
    assert is_manual_wake_command(text, "Hi Amadeus,Hey Amadeus") is False


@pytest.mark.asyncio
async def test_exact_manual_wake_bypasses_the_llm_and_returns_control_receipt() -> None:
    awakened: list[dict] = []
    streamed: list[str] = []

    async def stream(text, **_kwargs):
        streamed.append(text)
        return "unexpected reply"

    async def activate(payload: dict) -> None:
        awakened.append(payload)

    handler = ChatHandler()
    handler.configure(
        stream_llm_query=stream,
        pending_sentence_items=None,
        manual_wake_handler=activate,
        manual_wake_phrases="Hi Amadeus,Hey Amadeus",
    )

    result = await handler.send_text("Hi, Amadeus")

    assert result == {"status": "awake", "control": "wake"}
    assert awakened == [{"text": "Hi, Amadeus", "source": "manual_text"}]
    assert streamed == []


@pytest.mark.asyncio
async def test_wake_exit_command_reaches_host_callback_but_not_public_transcript(monkeypatch) -> None:
    emitted: list[tuple[object, dict]] = []

    async def capture(method, payload):
        emitted.append((method, payload))

    callback = AsyncMock()
    handler = AsrHandler()
    handler._source = "wake"
    handler._on_recognized = callback
    monkeypatch.setattr("server.handlers.asr_handler.bus.emit", capture)

    await handler._dispatch_recognized("停止对话")

    callback.assert_awaited_once()
    assert callback.await_args.args[0]["control"] == "stop"
    assert not any(method == Method.ASR_RECOGNIZED for method, _ in emitted)


@pytest.mark.asyncio
async def test_normal_wake_text_is_published_and_callback_runs_once(monkeypatch) -> None:
    emitted: list[tuple[object, dict]] = []

    async def capture(method, payload):
        emitted.append((method, payload))

    callback = AsyncMock()
    handler = AsrHandler()
    handler._source = "wake"
    handler._on_recognized = callback
    monkeypatch.setattr("server.handlers.asr_handler.bus.emit", capture)

    await handler._dispatch_recognized("今天过得怎么样")

    callback.assert_awaited_once()
    recognized = [payload for method, payload in emitted if method == Method.ASR_RECOGNIZED]
    assert len(recognized) == 1
    assert recognized[0]["text"] == "今天过得怎么样"


@pytest.mark.asyncio
async def test_wake_bridge_routes_exact_stop_privately_and_normal_sentence_once(monkeypatch) -> None:
    wake_events: list[tuple[object, dict]] = []
    host_events: list[tuple[object, dict]] = []
    routed_chat: list[str] = []
    scheduled: list[object] = []

    async def capture_host_event(method, payload):
        host_events.append((method, payload))

    async def route_from_host(payload: dict) -> None:
        if payload.get("control") != "stop":
            routed_chat.append(str(payload.get("text") or ""))

    handler = AsrHandler()
    handler._source = "wake"
    handler._on_recognized = route_from_host
    monkeypatch.setattr("server.handlers.asr_handler.bus.emit", capture_host_event)
    monkeypatch.setattr("asr.wake_service.WAKE_BRIDGE_AUTO_SEND", True)
    monkeypatch.setattr("asr.wake_service.WAKE_MIN_SEGMENT_RMS", 0.0)

    service = WakeService(
        on_awake_text=lambda payload: handler._dispatch_recognized(payload["text"]),
        backend_name="sense_voice",
    )
    service._backend = object()
    service._bridge_until = time.time() + 60.0
    service._bridge_wake_payload = {"phrase": "hi amadeus"}
    service._match_template = lambda _audio, _duration_ms: None
    service._emit = lambda method, payload: wake_events.append((method, payload))
    service._run_coro = scheduled.append

    recognized_text = "停止对话"
    service._recognize_and_match = lambda _audio, _backend: (
        recognized_text,
        False,
        "",
        0.9,
        "zh",
    )
    audio = np.full(8000, 0.1, dtype=np.float32)

    service._handle_segment(audio)
    while scheduled:
        await scheduled.pop(0)

    recognized_text = "如何停止对话"
    service._handle_segment(audio)
    while scheduled:
        await scheduled.pop(0)

    public_texts = [
        str(payload.get("text") or "")
        for method, payload in [*wake_events, *host_events]
        if method == Method.ASR_RECOGNIZED
    ]
    assert "停止对话" not in public_texts
    assert public_texts.count("如何停止对话") == 1
    assert routed_chat == ["如何停止对话"]


@pytest.mark.asyncio
async def test_stop_listening_does_not_cancel_its_current_listener_task(monkeypatch) -> None:
    emitted: list[tuple[object, dict]] = []

    async def capture(method, payload):
        emitted.append((method, payload))

    handler = AsrHandler()
    handler._active = True
    handler._source = "wake"
    handler._awake_seconds = 60.0
    handler._awake_until = 160.0
    handler._listen_task = asyncio.current_task()
    monkeypatch.setattr("server.handlers.asr_handler.bus.emit", capture)

    await handler.stop_listening("voice_stop_command")

    assert handler._listen_task is None
    assert emitted[-1][1]["status"] == "idle"
    assert emitted[-1][1]["reason"] == "voice_stop_command"


@pytest.mark.asyncio
async def test_awake_status_contains_wall_clock_deadline(monkeypatch) -> None:
    emitted: list[tuple[object, dict]] = []

    async def capture(method, payload):
        emitted.append((method, payload))

    handler = AsrHandler()
    handler._source = "wake"
    handler._awake_seconds = 60.0
    handler._awake_until = 112.0
    monkeypatch.setattr("server.handlers.asr_handler.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("server.handlers.asr_handler.time.time", lambda: 1_000.0)
    monkeypatch.setattr("server.handlers.asr_handler.bus.emit", capture)

    await handler._emit_listening_status()

    payload = emitted[-1][1]
    assert payload["status"] == "awake"
    assert payload["awake_remaining"] == pytest.approx(12.0)
    assert payload["awake_deadline_ms"] == 1_012_000


@pytest.mark.asyncio
async def test_turn_complete_resets_and_publishes_full_hot_window(monkeypatch) -> None:
    emitted: list[tuple[object, dict]] = []

    async def capture(method, payload):
        emitted.append((method, payload))

    handler = AsrHandler()
    handler._active = True
    handler._source = "wake"
    handler._awake_seconds = 60.0
    handler._awake_until = 120.0
    handler._waiting_turn_complete = True
    monkeypatch.setattr("server.handlers.asr_handler.time.monotonic", lambda: 200.0)
    monkeypatch.setattr("server.handlers.asr_handler.time.time", lambda: 1_000.0)
    monkeypatch.setattr("server.handlers.asr_handler.bus.emit", capture)

    await handler.notify_turn_complete("playback")

    payload = emitted[-1][1]
    assert payload["status"] == "turn_complete"
    assert payload["awake_remaining"] == pytest.approx(60.0)
    assert payload["awake_deadline_ms"] == 1_060_000
