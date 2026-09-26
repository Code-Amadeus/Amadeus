"""Speech-onset boundary shared by synthesis output and first-sound timing.

GPT-SoVITS generates a pause before the first word of every request: 0.79-1.01 s
across 58 production outputs, independent of CFM steps and chunking. The
synthesis adapter removes that lead and playback times first sound with this
one definition of a voiced frame, so the inter-unit gap belongs to the host and
the first-sound marker means audible speech rather than the first device write.
"""

from __future__ import annotations

import numpy as np

# Generated leads stay at or below -48 dBFS (a decaying start transient over
# digital silence). Every measured onset crossed -45 dBFS within 50 ms of
# leaving the silence floor, so the pre-roll keeps the whole rise.
VOICED_FRAME_DBFS = -45.0
SPEECH_PREROLL_SECONDS = 0.05
_FRAME_SECONDS = 0.01
_VOICED_FRAME_RMS = 10.0 ** (VOICED_FRAME_DBFS / 20.0)


def first_voiced_sample(audio, sample_rate: int) -> int | None:
    """Return where the first voiced 10 ms frame starts, or None if none is voiced."""

    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if samples.size == 0:
        return None
    frame = max(1, int(round(float(sample_rate) * _FRAME_SECONDS)))
    starts = np.arange(0, samples.size, frame)
    lengths = np.diff(np.append(starts, samples.size))
    energy = np.add.reduceat(np.square(samples, dtype=np.float64), starts) / lengths
    voiced = np.flatnonzero(energy >= _VOICED_FRAME_RMS**2)
    return int(starts[voiced[0]]) if voiced.size else None


def _preroll_start(onset: int, sample_rate: int) -> int:
    return max(0, onset - int(round(float(sample_rate) * SPEECH_PREROLL_SECONDS)))


def speech_start(audio, sample_rate: int) -> int:
    """Return the sample a synthesized item should start from.

    Audio that never becomes voiced is left whole (start 0).
    """

    onset = first_voiced_sample(audio, sample_rate)
    return 0 if onset is None else _preroll_start(onset, sample_rate)


class SpeechOnsetGate:
    """Apply ``speech_start`` to one item whose audio arrives as consecutive chunks."""

    def __init__(self, sample_rate: int):
        self._sample_rate = int(sample_rate)
        self._held: list[np.ndarray] = []
        self._open = False

    def push(self, chunk) -> np.ndarray | None:
        """Return audio to emit now, or None while the item is still silent."""

        if self._open:
            return chunk
        self._held.append(np.asarray(chunk, dtype=np.float32).reshape(-1))
        pending = np.concatenate(self._held)
        onset = first_voiced_sample(pending, self._sample_rate)
        if onset is None:
            return None
        self._open = True
        self._held = []
        return pending[_preroll_start(onset, self._sample_rate):]

    def flush(self) -> np.ndarray | None:
        """Release an item that never became voiced, unchanged."""

        if self._open or not self._held:
            return None
        pending = np.concatenate(self._held)
        self._held = []
        return pending
