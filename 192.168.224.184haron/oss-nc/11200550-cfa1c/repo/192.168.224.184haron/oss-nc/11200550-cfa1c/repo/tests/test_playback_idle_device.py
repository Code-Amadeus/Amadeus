"""Turn invalidation preserves an idle output device, but stops active audio."""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from server.handlers.tts_handler import TtsHandler
from tts.playback import PlaybackManager, StreamPlayer


@pytest.fixture
def audio(monkeypatch):
    import core.turn_coordinator as tc
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    streams = []

    def open_stream(**kwargs):
        stream = SimpleNamespace(
            rate=kwargs["rate"], stop_stream=Mock(), close=Mock(),
            is_active=lambda: True, write=Mock(),
        )
        streams.append(stream)
        return stream

    pa = SimpleNamespace(open=open_stream, terminate=Mock())
    monkeypatch.setitem(sys.modules, "pyaudio", SimpleNamespace(
        PyAudio=lambda: pa, paFloat32=1,
    ))
    # Exercise the canonical handler without invalidating the test process's
    # shared synthesis state or publishing notifications.
    monkeypatch.setattr("tts.pipeline.interrupt_pending_tts", Mock())
    monkeypatch.setattr("server.handlers.tts_handler.bus.emit", AsyncMock())
    player = StreamPlayer(SimpleNamespace(publish_mouth_value=Mock()))
    manager = PlaybackManager(player)
    handler = TtsHandler()
    handler.configure(manager, player)
    player.initialize(24000)
    yield SimpleNamespace(player=player, manager=manager, handler=handler, streams=streams)
    player.cleanup()


@pytest.mark.parametrize("queued", [False, True])
async def test_idle_turn_boundary_reuses_device_and_rejects_old_audio(audio, queued):
    manager = audio.manager
    old_epoch = manager.playback_epoch
    stream = audio.player.stream
    if queued:
        await manager.add_to_playlist(
            np.ones(240, dtype=np.float32), 24000, "sentence_1_old", "old",
            playback_epoch=old_epoch,
        )
    await audio.handler._interrupt({"source": "new_chat_turn_presentation"})
    audio.player.initialize(24000)

    assert audio.player.stream is stream
    assert len(audio.streams) == 1
    stream.stop_stream.assert_not_called()
    stream.close.assert_not_called()
    assert manager.player_is_ready.is_set()
    assert manager.next_seq_to_play == 1
    assert not manager.pending_audio
    assert not manager.is_epoch_current(old_epoch)
    await manager.add_to_playlist(
        np.ones(240, dtype=np.float32), 24000, "sentence_1_late", "late",
        playback_epoch=old_epoch,
    )
    assert not manager.pending_audio
    await audio.player.write_audio_async(
        np.ones(240, dtype=np.float32),
        is_current=lambda: manager.is_epoch_current(old_epoch),
    )
    stream.write.assert_not_called()


async def test_active_interruption_closes_device_once_and_next_turn_can_reopen(audio):
    manager = audio.manager
    manager.player_is_ready.clear()
    manager.current_playing_id = "sentence_1_active"
    old_epoch = manager.playback_epoch
    old_stream = audio.player.stream
    stop = Mock(wraps=audio.player.stop)
    audio.player.stop = stop
    await audio.handler._interrupt({"source": "barge_in"})

    stop.assert_called_once_with()
    old_stream.stop_stream.assert_called_once_with()
    old_stream.close.assert_called_once_with()
    assert audio.player.stream is None
    assert not audio.player.is_playing
    assert not manager.is_epoch_current(old_epoch)
    assert manager.player_is_ready.is_set()
    audio.player.initialize(24000)
    assert len(audio.streams) == 2
    assert audio.player.stream is not old_stream


async def test_reusing_idle_device_does_not_prevent_sample_rate_change(audio):
    old_stream = audio.player.stream
    await audio.handler._interrupt({})
    audio.player.initialize(48000)
    assert audio.player.stream.rate == 48000
    assert len(audio.streams) == 2
    old_stream.close.assert_called_once_with()


async def test_handler_without_interrupt_owner_still_stops_player(monkeypatch):
    monkeypatch.setattr("tts.pipeline.interrupt_pending_tts", Mock())
    monkeypatch.setattr("server.handlers.tts_handler.bus.emit", AsyncMock())
    player = SimpleNamespace(stop=Mock())
    manager = SimpleNamespace(pending_audio={1: object()}, player_is_ready=asyncio.Event())
    handler = TtsHandler()
    handler.configure(manager, player)
    await handler._interrupt({})
    player.stop.assert_called_once_with()
    assert not manager.pending_audio
    assert manager.player_is_ready.is_set()
