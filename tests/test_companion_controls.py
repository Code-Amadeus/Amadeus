from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from server.handlers.companion_control_handler import CompanionControlHandler
from server.protocol import Method


class FakeAsr:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.active = False

    async def handle(self, method: str, params: dict):
        self.calls.append((method, params))
        if method == Method.ASR_START:
            self.active = str(params.get("source") or "") == "companion"
            return {"status": "listening"}
        if method == Method.ASR_STOP:
            self.active = False
            return {"status": "idle"}
        return None

    def state(self):
        return {"active": self.active, "source": "companion" if self.active else ""}


def test_companion_controls_start_voice_and_vision() -> None:
    async def run() -> None:
        asr = FakeAsr()
        handler = CompanionControlHandler()
        handler.configure(asr_control=asr.handle, asr_state=asr.state)
        with patch("server.handlers.companion_control_handler.bus.emit", new=AsyncMock()) as emit:
            result = await handler.handle(
                Method.COMPANION_INPUT_SET,
                {"voice": True, "vision_mode": "on_question"},
            )
        assert asr.calls[0][0] == Method.ASR_START
        assert asr.calls[0][1]["source"] == "companion"
        assert handler.vision_enabled is True
        assert result["inputs"]["voice"]["enabled"] is True
        assert result["inputs"]["vision"]["mode"] == "on_question"
        emit.assert_awaited_once()

    asyncio.run(run())


def test_companion_controls_send_text_to_chat() -> None:
    async def run() -> None:
        asr = FakeAsr()
        sent: list[tuple[str, bool]] = []

        async def chat_send(text: str, visual: bool):
            sent.append((text, visual))
            return {"status": "ok"}

        handler = CompanionControlHandler()
        handler.configure(
            asr_control=asr.handle,
            asr_state=asr.state,
            chat_send=chat_send,
        )
        with patch("server.handlers.companion_control_handler.bus.emit", new=AsyncMock()):
            await handler.handle(
                Method.COMPANION_INPUT_SET,
                {"text": "  用文字问一句  ", "vision_mode": "on_question"},
            )
        assert sent == [("用文字问一句", True)]

    asyncio.run(run())


def test_companion_controls_stop_voice() -> None:
    async def run() -> None:
        asr = FakeAsr()
        handler = CompanionControlHandler()
        handler.configure(asr_control=asr.handle, asr_state=asr.state)
        with patch("server.handlers.companion_control_handler.bus.emit", new=AsyncMock()):
            await handler.handle(Method.COMPANION_INPUT_SET, {"voice": True})
            result = await handler.handle(Method.COMPANION_INPUT_SET, {"voice": False})
        assert asr.calls[-1][0] == Method.ASR_STOP
        assert result["inputs"]["voice"]["enabled"] is False

    asyncio.run(run())


def test_companion_character_switch_reloads_voice_model() -> None:
    async def run() -> None:
        handler = CompanionControlHandler()
        character = {"id": "yachiyo", "name": "月见八千代"}
        with patch(
            "core.character_profile.switch_character",
            return_value=character,
        ), patch(
            "tts.pipeline.reload_active_character_voice",
            return_value=True,
        ) as reload_voice:
            result = await handler.handle(
                Method.COMPANION_CHARACTER_SWITCH,
                {"character_id": "yachiyo"},
            )
        reload_voice.assert_called_once_with()
        assert result["character"] == character
        assert result["voice_reload"] == {
            "ok": True,
            "reloaded": True,
            "error": "",
        }

    asyncio.run(run())
