"""PCM-position contracts: no sound device or wall-clock sleeps are required."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from tts import playback


class RecordingDevice:
    def __init__(self, sample_rate):
        self.sample_rate = sample_rate
        self.position = 0
        self.events = []
        self.audio = []
        self.on_write = lambda: None

    def publish_mouth_value(self, value):
        self.events.append(("mouth", self.position, float(value)))

    def write(self, data):
        chunk = np.frombuffer(data, dtype=np.float32).copy()
        self.events.append(("write", self.position, len(chunk)))
        self.audio.append(chunk)
        self.position += len(chunk)
        self.on_write()


@pytest.fixture
def player_factory(monkeypatch):
    players = []
    noop = lambda *args, **kwargs: None
    aec = SimpleNamespace(start=noop, stop=noop, push_reference=noop)
    monkeypatch.setattr(playback, "get_realtime_aec_processor", lambda: aec)
    monkeypatch.setattr(playback, "get_aec_debug_capture", lambda: aec)
    monkeypatch.setattr(playback, "_observe_audio_write_completed", noop)
    # Deterministic device-time progression also reproduces the old wall-clock
    # throttle. It is a submission clock, not a simulation of speaker latency.
    clock = SimpleNamespace(time=lambda: 0.0)
    monkeypatch.setattr(playback, "time", clock)

    def make(sample_rate):
        device = RecordingDevice(sample_rate)
        clock.time = lambda: device.position / sample_rate
        player = playback.StreamPlayerWithBuffer(device)
        player.stream = device
        player.is_playing = True
        player.sample_rate = player._current_rate = sample_rate
        player.initialize = lambda _rate: None
        players.append(player)
        return player, device

    yield make
    for player in players:
        player._stop_audio_writer()


async def play_path(player, audio, rate, path, is_current=None):
    if path == "stream":
        await player.write_audio_async(
            audio, mouth_envelope=True, sample_rate=rate,
            first_mouth_minimum=0.12, is_current=is_current,
        )
    elif path == "full":
        await player.play_full_audio_and_signal_completion(
            audio, rate, "sentence_2_timing", "", asyncio.Event(),
            is_current=is_current,
        )
    elif path == "chunk":
        await player.play_audio_chunk_and_signal_completion(
            audio, rate, "sentence_2_timing", "", asyncio.Event(),
            is_last_chunk=False, is_current=is_current,
        )
    else:
        player.play_chunk(audio)


@pytest.mark.parametrize("path", ["stream", "full", "chunk", "sync"])
@pytest.mark.parametrize("rate", [16000, 24000, 32000, 44100, 48000])
@pytest.mark.parametrize("gap", [(70, 140), (83, 108)])
def test_short_pause_and_reopening_follow_pcm_within_10ms(player_factory, path, rate, gap):
    player, device = player_factory(rate)
    start, end = (round(rate * ms / 1000) for ms in gap)
    audio = np.full(round(rate * 0.3), 0.1, dtype=np.float32)
    audio[start:end] = 0
    tail = round(rate * 0.25)
    audio[tail:] = 0
    asyncio.run(play_path(player, audio, rate, path))

    np.testing.assert_array_equal(np.concatenate(device.audio), audio)
    mouth = [(pos, value > 0.08) for kind, pos, value in device.events if kind == "mouth"]
    closed = [pos for pos, opened in mouth if not opened and start <= pos < end]
    assert closed, "the real pause must not disappear inside the RMS window"
    tolerance = round(rate * 0.01)
    assert closed[0] - start <= tolerance
    reopened = [pos for pos, opened in mouth if opened and closed[0] < pos < tail]
    assert reopened and abs(reopened[0] - end) <= tolerance
    tail_close = [pos for pos, opened in mouth if not opened and tail <= pos]
    assert tail_close and tail_close[0] - tail <= tolerance
    # Every physical write, including resumption after silence, has a fresh
    # envelope for these samples before it reaches the device.
    for index, event in enumerate(device.events):
        if event[0] == "write":
            assert index > 0
            assert device.events[index - 1][:2] == ("mouth", event[1])


@pytest.mark.parametrize("path", ["stream", "full", "chunk", "sync"])
def test_stopping_after_a_window_sends_no_more_pcm_or_open_mouth(player_factory, path):
    player, device = player_factory(24000)
    current = [True]

    def interrupt():
        current[0] = False
        player.is_playing = False

    device.on_write = interrupt
    asyncio.run(play_path(player, np.full(2400, 0.1, dtype=np.float32), 24000, path,
                          is_current=lambda: current[0]))
    assert len(device.audio) == 1
    assert device.position <= 240  # at most one 10ms window escaped the interrupt
    write_index = next(i for i, event in enumerate(device.events) if event[0] == "write")
    assert not any(kind == "mouth" and value > 0.08
                   for kind, _, value in device.events[write_index + 1:])


@pytest.mark.parametrize("path", ["stream", "full", "chunk", "sync"])
def test_leading_silence_is_not_primed_open_and_tail_samples_are_preserved(player_factory, path):
    rate = 24000
    player, device = player_factory(rate)
    audio = np.concatenate([np.zeros(480, dtype=np.float32), np.full(737, 0.1, dtype=np.float32)])
    asyncio.run(play_path(player, audio, rate, path))
    np.testing.assert_array_equal(np.concatenate(device.audio), audio)
    assert not any(kind == "mouth" and pos < 480 and value > 0.08
                   for kind, pos, value in device.events)


def test_empty_or_stale_audio_does_not_publish_an_envelope(player_factory):
    for stale in (False, True):
        player, device = player_factory(24000)
        asyncio.run(player.write_audio_async(
            np.ones(240 if stale else 0, dtype=np.float32), mouth_envelope=True,
            is_current=lambda: not stale,
        ))
        assert device.events == []


def test_irregular_producer_chunks_preserve_short_pause_and_all_samples(player_factory):
    player, device = player_factory(24000)
    audio = np.full(4800, 0.1, dtype=np.float32)
    audio[1992:2592] = 0  # 83--108ms, deliberately not aligned to producer/windows

    async def run():
        cuts = [0, 437, 1965, 2179, 2587, 3011, len(audio)]
        for start, end in zip(cuts, cuts[1:]):
            await player.write_audio_async(audio[start:end], mouth_envelope=True, sample_rate=24000)

    asyncio.run(run())
    np.testing.assert_array_equal(np.concatenate(device.audio), audio)
    closed = [pos for kind, pos, value in device.events
              if kind == "mouth" and 1992 <= pos < 2592 and value <= 0.08]
    assert closed and closed[0] - 1992 <= 240
    reopened = [pos for kind, pos, value in device.events
                if kind == "mouth" and pos > closed[0] and value > 0.08]
    assert reopened and abs(reopened[0] - 2592) <= 240


def test_draining_queued_audio_releases_waiters_without_playing_it(player_factory, monkeypatch):
    player, device = player_factory(24000)
    # Hold jobs in the queue, as if another write were occupying the writer.
    monkeypatch.setattr(player, "_ensure_audio_writer", lambda: None)

    async def run():
        task = asyncio.create_task(player.write_audio_async(np.ones(240), mouth_envelope=True))
        await asyncio.sleep(0)
        assert not task.done()
        player.stop()
        await asyncio.wait_for(task, 1)

    asyncio.run(run())
    assert not device.audio
    assert not any(kind == "mouth" and value > 0 for kind, _, value in device.events)


def test_failed_device_write_closes_primed_mouth_without_recording_success(player_factory):
    player, device = player_factory(24000)
    observed = []

    def fail(_data):
        raise OSError("output device lost")

    device.write = fail
    with pytest.raises(OSError, match="output device lost"):
        asyncio.run(player.write_audio_async(
            np.full(2400, 0.1, dtype=np.float32), mouth_envelope=True,
            after_first_write=lambda: observed.append(True),
        ))
    assert observed == []
    assert device.events[-1] == ("mouth", 0, 0.0)


def test_interrupt_during_window_preparation_drops_its_mouth_and_pcm(player_factory):
    player, device = player_factory(24000)
    current = [True]
    player._write_audio_sync(
        np.ones(240, dtype=np.float32), is_current=lambda: current[0],
        before_window=lambda _chunk: current.__setitem__(0, False),
    )
    assert device.events == []


def _record_first_sound(monkeypatch, device):
    marks = []

    def mark(_logger, stage, clear=False, **fields):
        marks.append((stage, device.position, fields))
        return 0.0

    monkeypatch.setattr(playback, "log_latency_marker", mark)
    return marks


def _opens_with_silence(rate, silence_seconds=0.9, speech_seconds=0.3):
    return np.concatenate([
        np.zeros(int(rate * silence_seconds), dtype=np.float32),
        np.full(int(rate * speech_seconds), 0.2, dtype=np.float32),
    ])


def test_full_audio_first_sound_marks_the_first_voiced_window(player_factory, monkeypatch) -> None:
    rate = 24000
    player, device = player_factory(rate)
    marks = _record_first_sound(monkeypatch, device)

    async def run():
        await player.play_full_audio_and_signal_completion(
            _opens_with_silence(rate), rate, "sentence_1_first", "", asyncio.Event(),
        )

    asyncio.run(run())

    assert [(stage, position) for stage, position, _fields in marks] == [("first_play", int(rate * 0.9))]
    assert marks[0][2]["lead_ms"] == "900"


def test_stream_first_sound_marks_the_first_voiced_window(player_factory, monkeypatch) -> None:
    rate = 24000
    player, device = player_factory(rate)
    monkeypatch.setattr(playback.time, "monotonic", lambda: device.position / rate, raising=False)
    marks = _record_first_sound(monkeypatch, device)
    audio = _opens_with_silence(rate)

    async def run():
        manager = playback.PlaybackManager(player)
        chunks: asyncio.Queue = asyncio.Queue()
        await chunks.put((rate, audio[: int(rate * 0.5)]))
        await chunks.put((rate, audio[int(rate * 0.5) :]))
        await chunks.put(None)
        await manager.play_s1_stream(
            chunks, "sentence_1_first", "", playback_epoch=manager.playback_epoch,
        )

    asyncio.run(run())

    assert [(stage, position) for stage, position, _fields in marks] == [("first_play", int(rate * 0.9))]
    assert marks[0][2]["lead_ms"] == "900"
