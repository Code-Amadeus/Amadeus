"""TTS interrupt ownership and sequence-boundary regression tests."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from server.handlers.tts_handler import TtsHandler
from server.protocol import Method
from tts.sentence_state import SentenceStateManager


class _Playback:
    def __init__(self) -> None:
        self.next_seq_to_play = 6
        self.pending_audio = {6: object()}
        self.player_is_ready = asyncio.Event()
        self.player_is_ready.set()
        self.interrupt_calls = 0

    async def interrupt(self) -> None:
        self.interrupt_calls += 1
        self.pending_audio.clear()
        self.next_seq_to_play = 1

    def clear_turn_tracking(self) -> None:
        return None


class _Player:
    def stop(self) -> None:
        return None


def test_tts_interrupt_aligns_direct_branch_allocator_with_playback_sequence() -> None:
    async def run() -> None:
        sequence = SentenceStateManager()
        for index in range(5):
            sequence.create_sentence(f"old-{index}")
        playback = _Playback()
        handler = TtsHandler(sentence_sequence_manager=sequence)
        handler.configure(playback, _Player())

        with patch("tts.pipeline.interrupt_pending_tts", return_value=2):
            result = await handler.handle(
                Method.TTS_INTERRUPT,
                {"source": "new_chat_turn_presentation"},
            )

        assert result == {"status": "interrupted"}
        assert playback.interrupt_calls == 1
        assert playback.next_seq_to_play == 1
        assert sequence.create_sentence("direct-branch-line").startswith("sentence_1_")

    asyncio.run(run())


def test_tts_mode_method_applies_real_pipeline_mode() -> None:
    async def run() -> None:
        import tts.pipeline as pipeline

        old_mode = pipeline.current_tts_mode()
        try:
            handler = TtsHandler()
            result = await handler.handle(Method.TTS_SET_MODE, {"mode": "cuda_graph"})
            assert result == {"mode": "cuda_graph"}
            assert pipeline.current_tts_mode() == "cuda_graph"
        finally:
            pipeline.reconfigure_tts_mode_name(old_mode)

    asyncio.run(run())
