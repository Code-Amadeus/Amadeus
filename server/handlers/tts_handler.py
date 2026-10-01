"""Adapter for TTS playback events."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.event_bus import bus
from server.protocol import Method
from server.ws_handler import RequestHandler

logger = logging.getLogger(__name__)


class TtsHandler(RequestHandler):
    methods = [Method.TTS_SET_MODE, Method.TTS_INTERRUPT]

    def __init__(self, *, sentence_sequence_manager=None) -> None:
        self._playback_manager = None
        self._player = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_interrupt = None
        if sentence_sequence_manager is None:
            from tts.sentence_state import sentence_state_manager

            sentence_sequence_manager = sentence_state_manager
        self._sentence_sequence_manager = sentence_sequence_manager

    def configure(self, playback_manager, player, on_interrupt=None) -> None:
        self._playback_manager = playback_manager
        self._player = player
        self._on_interrupt = on_interrupt
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        previous_sentence_start = getattr(playback_manager, "on_sentence_start", None)
        previous_sentence_complete = getattr(playback_manager, "on_sentence_complete", None)
        previous_turn_complete = getattr(playback_manager, "on_turn_playback_complete", None)

        def emit_async(method: Method, params: dict[str, Any]) -> None:
            loop = self._loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(bus.emit(method, params))
                )

        def on_sentence_start(sentence_id: str) -> None:
            if previous_sentence_start is not None:
                previous_sentence_start(sentence_id)
            emit_async(Method.TTS_SENTENCE_START, {"sentence_id": sentence_id, "index": -1})

        def on_sentence_complete(sentence_id: str, text: str) -> None:
            if previous_sentence_complete is not None:
                previous_sentence_complete(sentence_id, text)
            emit_async(Method.TTS_SENTENCE_END, {"sentence_id": sentence_id, "text": text or ""})

        def on_turn_complete() -> None:
            if previous_turn_complete is not None:
                previous_turn_complete()
            emit_async(Method.TTS_TURN_COMPLETE, {})
            if hasattr(playback_manager, "clear_turn_tracking"):
                playback_manager.clear_turn_tracking()

        playback_manager.on_sentence_start = on_sentence_start
        playback_manager.on_sentence_complete = on_sentence_complete
        playback_manager.on_turn_playback_complete = on_turn_complete

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.TTS_SET_MODE:
            return await self._set_mode(params)
        if method == Method.TTS_INTERRUPT:
            return await self._interrupt(params)
        return None

    async def _set_mode(self, params: dict[str, Any]) -> dict[str, Any]:
        from tts.pipeline import reconfigure_tts_mode_name

        mode = reconfigure_tts_mode_name(str(params.get("mode", "")))
        return {"mode": mode}

    async def _interrupt(self, params: dict[str, Any]) -> dict[str, Any]:
        completed_text = ""
        if self._playback_manager and hasattr(self._playback_manager, "get_completed_turn_text"):
            try:
                completed_text = self._playback_manager.get_completed_turn_text()
            except Exception:
                logger.exception("failed to collect completed playback text")

        try:
            import tts.pipeline as tts_pipeline
            tts_pipeline.interrupt_pending_tts()
        except Exception:
            logger.exception("failed to invalidate pending TTS work")

        if self._playback_manager and hasattr(self._playback_manager, "interrupt"):
            await self._playback_manager.interrupt()
        elif self._playback_manager:
            try:
                self._playback_manager.pending_audio.clear()
                self._playback_manager.player_is_ready.set()
            except Exception:
                logger.exception("failed to clear playback manager during interrupt")

        # PlaybackManager.reset_sequence and sentence-id allocation are the
        # two halves of one turn boundary.  Direct Chat branches do not enter
        # ChatRuntime's legacy reset block, so the canonical TTS interrupt must
        # align both owners before any new foreground line can be enqueued.
        sequence_manager = self._sentence_sequence_manager
        if sequence_manager is not None:
            try:
                sequence_manager.begin_turn()
            except Exception:
                logger.exception("failed to reset sentence sequence during interrupt")

        if self._player:
            self._player.stop()
        if params.get("annotate_history") and self._on_interrupt is not None:
            result = self._on_interrupt(
                {
                    "source": params.get("source", ""),
                    "completed_text": completed_text,
                    "accumulated_text": params.get("accumulated_text", ""),
                    "turn_id": params.get("turn_id", ""),
                }
            )
            if hasattr(result, "__await__"):
                await result
        if self._playback_manager and hasattr(self._playback_manager, "clear_turn_tracking"):
            self._playback_manager.clear_turn_tracking()
        await bus.emit(Method.TTS_STATUS, {"status": "interrupted"})
        return {"status": "interrupted"}
