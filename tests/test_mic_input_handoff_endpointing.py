from __future__ import annotations

import logging
from types import SimpleNamespace

import numpy as np

import asr.echo_guard as echo_guard
import asr.mic_input_service as mic_module
from asr.mic_input_service import MicFrame, MicInputService


_SAMPLE_RATE = 16_000
_CHUNK_SAMPLES = 512
_CHUNK_SECONDS = _CHUNK_SAMPLES / _SAMPLE_RATE


class _FakeClock:
    def __init__(self) -> None:
        # Capture code treats 0.0 as "not started"; use a realistic monotonic
        # origin so the tests exercise the production predicates.
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class _ScriptedCursor:
    def __init__(
        self,
        clock: _FakeClock,
        *,
        frame_count: int,
        rms: float = 0.05,
        value: float = 0.25,
    ) -> None:
        self.clock = clock
        self.frame_count = int(frame_count)
        self.rms = float(rms)
        self.value = float(value)
        self.read_count = 0

    def read(self, timeout: float = 0.25) -> MicFrame | None:
        if self.read_count >= self.frame_count:
            self.clock.advance(timeout)
            return None
        self.clock.advance(_CHUNK_SECONDS)
        seq = self.read_count
        self.read_count += 1
        audio = np.full(_CHUNK_SAMPLES, self.value, dtype=np.float32)
        return MicFrame(
            seq=seq,
            timestamp=self.clock.monotonic(),
            audio=audio,
            rms=self.rms,
            raw_audio=audio.copy(),
        )


class _ScriptedVAD:
    def __init__(self, events: dict[int, dict[str, int]] | None = None) -> None:
        self.events = dict(events or {})
        self.call_count = 0
        self.reset_count = 0

    def __call__(self, _chunk, return_seconds: bool = False):
        del return_seconds
        self.call_count += 1
        return self.events.get(self.call_count)

    def reset_states(self) -> None:
        self.reset_count += 1


def _handoff_frames(count: int = 40) -> list[MicFrame]:
    frames: list[MicFrame] = []
    for seq in range(count):
        audio = np.full(_CHUNK_SAMPLES, -0.25, dtype=np.float32)
        frames.append(
            MicFrame(
                seq=seq,
                timestamp=99.0 + seq * _CHUNK_SECONDS,
                audio=audio,
                rms=0.05,
                raw_audio=audio.copy(),
            )
        )
    return frames


def _capture(
    monkeypatch,
    *,
    cursor: _ScriptedCursor,
    vad: _ScriptedVAD,
    handoff_frames: list[MicFrame] | None = None,
    timeout_s: float = 15.0,
    max_speech_sec: float = 30.0,
    handoff_max_capture_sec: float = 5.0,
) -> np.ndarray | None:
    service = MicInputService()
    monkeypatch.setattr(service, "start", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "cursor", lambda *args, **kwargs: cursor)
    monkeypatch.setattr(
        service,
        "consume_handoff_frames",
        lambda *args, **kwargs: list(handoff_frames or []),
    )
    monkeypatch.setattr(mic_module.time, "monotonic", cursor.clock.monotonic)

    import silero_vad

    monkeypatch.setattr(silero_vad, "VADIterator", lambda *args, **kwargs: vad)
    monkeypatch.setattr(
        echo_guard,
        "should_drop_handoff_candidate",
        lambda **kwargs: SimpleNamespace(drop=False, reason="near_end_or_uncertain"),
    )

    return service.capture_utterance(
        vad_model=object(),
        threshold=0.45,
        timeout_s=timeout_s,
        min_silence_ms=350,
        speech_pad_ms=60,
        min_speech_ms=150,
        max_speech_sec=max_speech_sec,
        preroll_ms=500,
        energy_end_rms=0.008,
        energy_end_ms=450,
        handoff_max_capture_sec=handoff_max_capture_sec,
        consume_handoff=True,
    )


def test_handoff_vad_takeover_disarms_recovery_watchdog_and_preserves_preroll(
    monkeypatch,
    caplog,
) -> None:
    clock = _FakeClock()
    cursor = _ScriptedCursor(clock, frame_count=220)
    # Conversation VAD takes ownership immediately, then reports the real end
    # after more than six seconds of continued live speech.
    vad = _ScriptedVAD({1: {"start": 0}, 190: {"end": 1}})
    handoff = _handoff_frames()

    caplog.set_level(logging.INFO, logger="asr.mic_input_service")
    audio = _capture(
        monkeypatch,
        cursor=cursor,
        vad=vad,
        handoff_frames=handoff,
    )

    assert audio is not None
    assert cursor.read_count == 190
    assert "handoff Conversation VAD took ownership" in caplog.text
    assert "reason=vad_end" in caplog.text
    assert "reason=handoff_max" not in caplog.text
    # The endpoint change must not discard or rewrite the existing handoff
    # pre-roll.  It remains the prefix of the recognizer waveform.
    np.testing.assert_array_equal(audio[:_CHUNK_SAMPLES], handoff[0].audio)
    assert audio.size >= len(handoff) * _CHUNK_SAMPLES


def test_handoff_without_vad_takeover_keeps_five_second_watchdog(
    monkeypatch,
    caplog,
) -> None:
    clock = _FakeClock()
    cursor = _ScriptedCursor(clock, frame_count=400)
    vad = _ScriptedVAD()

    caplog.set_level(logging.INFO, logger="asr.mic_input_service")
    audio = _capture(
        monkeypatch,
        cursor=cursor,
        vad=vad,
        handoff_frames=_handoff_frames(),
    )

    assert audio is not None
    assert 150 <= cursor.read_count <= 160
    assert "reason=handoff_max" in caplog.text
    assert "handoff Conversation VAD took ownership" not in caplog.text


def test_wait_for_speech_timeout_does_not_shorten_late_started_utterance(
    monkeypatch,
    caplog,
) -> None:
    clock = _FakeClock()
    cursor = _ScriptedCursor(clock, frame_count=700)
    # Speech begins after about 14 seconds of waiting and continues for about
    # six more seconds.  The 15-second wait-for-start timeout must not truncate
    # it one second after it begins.
    vad = _ScriptedVAD({438: {"start": 0}, 625: {"end": 1}})

    caplog.set_level(logging.INFO, logger="asr.mic_input_service")
    audio = _capture(monkeypatch, cursor=cursor, vad=vad)

    assert audio is not None
    assert cursor.read_count == 625
    assert audio.size / _SAMPLE_RATE > 5.5
    assert "reason=vad_end" in caplog.text
    assert "reason=timeout" not in caplog.text


def test_wait_for_speech_still_times_out_when_no_speech_starts(monkeypatch) -> None:
    clock = _FakeClock()
    cursor = _ScriptedCursor(clock, frame_count=700, rms=0.0, value=0.0)
    vad = _ScriptedVAD()

    audio = _capture(monkeypatch, cursor=cursor, vad=vad)

    assert audio is None
    assert 468 <= cursor.read_count <= 470


def test_vad_owned_handoff_still_honors_absolute_max_speech(monkeypatch, caplog) -> None:
    clock = _FakeClock()
    cursor = _ScriptedCursor(clock, frame_count=400)
    vad = _ScriptedVAD({1: {"start": 0}})

    caplog.set_level(logging.INFO, logger="asr.mic_input_service")
    audio = _capture(
        monkeypatch,
        cursor=cursor,
        vad=vad,
        handoff_frames=_handoff_frames(),
        max_speech_sec=7.0,
    )

    assert audio is not None
    assert cursor.read_count > 160  # passed the old five-second handoff cap
    assert audio.size / _SAMPLE_RATE <= 7.1
    assert "reason=max_speech" in caplog.text
    assert "reason=handoff_max" not in caplog.text
