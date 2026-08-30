from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from server.desktop_voice import is_desktop_voice_exit_command
from server.handlers.asr_handler import AsrHandler
from server.protocol import Method


@pytest.mark.parametrize("text", ["停止对话", "结束对话。", " 退出对话！ "])
def test_exact_desktop_voice_exit_commands(text: str) -> None:
    assert is_desktop_voice_exit_command(text) is True


@pytest.mark.parametrize("text", ["如何停止对话", "不要结束对话", "退出对话模式怎么用"])
def test_exit_words_inside_a_sentence_are_normal_chat(text: str) -> None:
    assert is_desktop_voice_exit_command(text) is False


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
