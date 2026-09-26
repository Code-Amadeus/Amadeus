"""Speech-onset contract shared by synthesis trimming and first-sound timing."""
from __future__ import annotations

import numpy as np
import pytest

from tts.speech_onset import (
    SPEECH_PREROLL_SECONDS,
    SpeechOnsetGate,
    first_voiced_sample,
    speech_start,
)

RATE = 24000
PREROLL = int(RATE * SPEECH_PREROLL_SECONDS)


def _generated_item(*, lead_seconds: float = 0.9, rise_seconds: float = 0.03) -> tuple[np.ndarray, int]:
    """A GPT-SoVITS-shaped item: faint start transient, silence floor, rising speech."""
    rng = np.random.default_rng(0)
    audio = rng.normal(0.0, 10 ** (-90 / 20), int(RATE * 1.4)).astype(np.float32)
    transient = int(RATE * 0.2)
    audio[:transient] += (
        rng.normal(0.0, 10 ** (-49 / 20), transient) * np.linspace(1.0, 0.05, transient)
    ).astype(np.float32)
    onset = int(RATE * lead_seconds)
    t = np.arange(audio.size - onset) / RATE
    speech = np.sin(2 * np.pi * 220 * t) * 0.14
    rise = int(RATE * rise_seconds)
    speech[:rise] *= np.linspace(0.001, 1.0, rise)
    audio[onset:] += speech.astype(np.float32)
    return audio, onset


def test_generated_lead_is_trimmed_to_the_preroll_and_keeps_the_rise() -> None:
    audio, onset = _generated_item()

    voiced = first_voiced_sample(audio, RATE)
    start = speech_start(audio, RATE)

    assert voiced is not None and onset <= voiced < onset + int(RATE * 0.03)
    assert start == voiced - PREROLL
    assert start <= onset  # the whole onset rise survives
    assert onset - start < PREROLL + int(RATE * 0.01)  # nothing else of the lead does


def test_audio_that_never_becomes_voiced_is_left_whole() -> None:
    audio, onset = _generated_item()
    silent = audio[:onset]

    assert first_voiced_sample(silent, RATE) is None
    assert speech_start(silent, RATE) == 0


def test_audio_voiced_from_the_start_is_not_trimmed() -> None:
    audio = (np.sin(2 * np.pi * 220 * np.arange(RATE) / RATE) * 0.14).astype(np.float32)

    assert first_voiced_sample(audio, RATE) == 0
    assert speech_start(audio, RATE) == 0


@pytest.mark.parametrize("chunk_seconds", [0.1, 0.35, 0.8, 2.0])
def test_gate_matches_whole_item_trimming_for_any_chunking(chunk_seconds: float) -> None:
    audio, _onset = _generated_item()
    size = int(RATE * chunk_seconds)
    gate = SpeechOnsetGate(RATE)

    emitted = [gate.push(audio[i : i + size]) for i in range(0, audio.size, size)]
    tail = gate.flush()

    voiced_chunk = first_voiced_sample(audio, RATE) // size
    assert all(piece is None for piece in emitted[:voiced_chunk])
    assert tail is None
    streamed = np.concatenate([piece for piece in emitted if piece is not None])
    np.testing.assert_array_equal(streamed, audio[speech_start(audio, RATE):])


def test_gate_releases_a_silent_item_unchanged() -> None:
    audio, onset = _generated_item()
    silent = audio[:onset]
    gate = SpeechOnsetGate(RATE)

    assert [gate.push(silent[i : i + 4800]) for i in range(0, silent.size, 4800)] == [None] * 5
    np.testing.assert_array_equal(gate.flush(), silent)
    assert gate.flush() is None
