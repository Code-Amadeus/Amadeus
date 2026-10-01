"""Input controls for the standalone Companion card."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from server.event_bus import bus
from server.protocol import Method
from server.ws_handler import RequestHandler


AsyncCall = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]
ChatSend = Callable[[str, bool], Awaitable[dict[str, Any] | None]]
logger = logging.getLogger(__name__)


class CompanionControlHandler(RequestHandler):
    methods = [
        Method.COMPANION_STATUS,
        Method.COMPANION_INPUT_SET,
        Method.COMPANION_CHARACTER_LIST,
        Method.COMPANION_CHARACTER_SWITCH,
        Method.COMPANION_CHARACTER_STATUS,
    ]

    def __init__(self) -> None:
        self._asr_control: AsyncCall | None = None
        self._asr_state: Callable[[], dict[str, Any]] | None = None
        self._chat_send: ChatSend | None = None
        self._vision_enabled = False

    def configure(
        self,
        *,
        asr_control: AsyncCall,
        asr_state: Callable[[], dict[str, Any]],
        chat_send: ChatSend | None = None,
    ) -> None:
        self._asr_control = asr_control
        self._asr_state = asr_state
        self._chat_send = chat_send

    @property
    def vision_enabled(self) -> bool:
        return self._vision_enabled

    def status(self) -> dict[str, Any]:
        asr = self._asr_state() if self._asr_state else {}
        listening = bool(asr.get("active") and asr.get("source") == "companion")
        voice_available = self._asr_control is not None
        text_available = self._chat_send is not None
        return {
            "status": "active",
            "inputs": {
                "session_id": "companion",
                "kind": "ask",
                "voice": {
                    "enabled": listening,
                    "listening": listening,
                    "starting": False,
                    "available": voice_available,
                    "error": "",
                    "reason": "ready" if voice_available else "asr_unavailable",
                },
                "vision": {
                    "mode": "on_question" if self._vision_enabled else "off",
                    "enabled": self._vision_enabled,
                    "available": True,
                    "error": "",
                    "reason": "ready",
                },
                "text": {
                    "enabled": False,
                    "available": text_available,
                    "error": "",
                    "reason": "ready" if text_available else "chat_unavailable",
                },
            },
        }

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.COMPANION_STATUS:
            return self.status()
        if method == Method.COMPANION_INPUT_SET:
            return await self._set_inputs(params)
        if method == Method.COMPANION_CHARACTER_LIST:
            from core.character_profile import current_character, list_characters

            return {"characters": list_characters(), "current": current_character()}
        if method == Method.COMPANION_CHARACTER_SWITCH:
            from core.character_profile import switch_character

            character_id = str(params.get("character_id") or params.get("id") or "").strip()
            if not character_id:
                raise ValueError("character_id is required")
            character = switch_character(character_id)
            voice_reload = {"ok": True, "reloaded": False, "error": ""}
            try:
                from tts.pipeline import reload_active_character_voice

                voice_reload["reloaded"] = bool(
                    await asyncio.to_thread(reload_active_character_voice)
                )
            except Exception as exc:
                logger.exception("failed to reload character voice model")
                voice_reload.update(ok=False, error=str(exc))
            return {
                "ok": True,
                "character": character,
                "voice_reload": voice_reload,
            }
        if method == Method.COMPANION_CHARACTER_STATUS:
            from core.character_profile import current_character, list_characters

            return {"current": current_character(), "characters": list_characters()}
        return None

    async def _set_inputs(self, params: dict[str, Any]) -> dict[str, Any]:
        voice = params.get("voice")
        if voice is not None and not isinstance(voice, bool):
            raise ValueError("Companion voice input must be boolean")
        vision_mode = params.get("vision_mode")
        if vision_mode is not None and vision_mode not in {"off", "on_question"}:
            raise ValueError("Unsupported Companion vision mode")
        if vision_mode is not None:
            self._vision_enabled = vision_mode == "on_question"
        text = params.get("text")
        if text is not None:
            if not isinstance(text, str):
                raise ValueError("Companion text input must be a string")
            text = text.strip()
            if not text:
                raise ValueError("Companion text input must not be empty")
            if len(text) > 4000:
                raise ValueError("Companion text input is too long")
            if self._chat_send is None:
                raise RuntimeError("Companion chat is unavailable")
            await self._chat_send(text, self._vision_enabled)
        if voice is not None:
            if self._asr_control is None:
                raise RuntimeError("ASR is unavailable")
            if voice:
                result = await self._asr_control(Method.ASR_START, {
                    "source": "companion",
                    "one_shot": False,
                    "finish_after_turn_complete": False,
                    "source_payload": {"companion": True},
                })
                if str((result or {}).get("status")) not in {"listening", "awake", "starting", "already_listening"}:
                    raise RuntimeError(str((result or {}).get("error") or "Microphone could not start."))
            else:
                await self._asr_control(Method.ASR_STOP, {"source": "companion"})
        result = self.status()
        await bus.emit(Method.COMPANION_STATUS, result)
        return result
