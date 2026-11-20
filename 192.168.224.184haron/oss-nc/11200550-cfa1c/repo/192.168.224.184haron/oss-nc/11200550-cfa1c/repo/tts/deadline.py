"""Producer cost observations and a single playback budget snapshot."""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from statistics import median

from config.settings import TTS_COVER_SAFETY_MARGIN_SEC, TTS_DEADLINE_AGGREGATION


@dataclass
class _CostHistory:
    samples: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=5))
    # slope, intercept and the measured length interval; refreshed on observation.
    fit: tuple[float, float, int, int] | None = None

    def observe(self, chars: int, elapsed: float) -> None:
        self.samples.append((chars, elapsed))
        self.fit = None
        samples = list(self.samples)
        lengths = sorted(x for x, _ in samples)
        # Two samples at each end must support a twofold length difference.
        # One unusually short/long request is not enough to identify overhead.
        if len(samples) < 5 or lengths[-2] < 2 * lengths[1]:
            return
        slopes = [(y2 - y1) / (x2 - x1)
                  for i, (x1, y1) in enumerate(samples)
                  for x2, y2 in samples[i + 1:] if x1 != x2]
        slope = median(slopes)
        if slope <= 0:
            return
        intercept = max(0.0, median(y - slope * x for x, y in samples))
        self.fit = (slope, intercept, lengths[0], lengths[-1])


class SynthesisCostModel:
    """Interpolate measured profiles; conservatively bound unsupported lengths.

    The bounded window rejects a lone cold capture without retaining a whole
    session's old performance. The caller supplies completed producer time.
    """

    def __init__(self):
        self._profiles: dict[Hashable, _CostHistory] = {}

    def clear(self) -> None:
        self._profiles.clear()

    def observe(self, profile: Hashable, chars: int, elapsed: float) -> None:
        if chars > 0 and math.isfinite(elapsed) and elapsed > 0:
            self._profiles.setdefault(profile, _CostHistory()).observe(chars, elapsed)

    def predict(self, profile: Hashable, chars: int, *, initial_seconds_per_char: float) -> float:
        prior = max(0.001, initial_seconds_per_char)
        history = self._profiles.get(profile)
        # One cold capture is not calibration; two points cannot distinguish
        # an outlier from fixed request overhead either.
        if history is None or len(history.samples) < 3:
            return max(0, chars) * prior
        # For nonnegative a,b and an observation t=a+b*n, the cost at m is at
        # most t*max(1,m/n). Use the median of these bounds to reject one noisy
        # observation without pretending we have identified a and b separately.
        envelope = median(t * max(1.0, chars / n) for n, t in history.samples)
        if history.fit is None:
            return envelope
        slope, intercept, shortest, longest = history.fit
        fitted = intercept + slope * max(chars, shortest)
        return max(fitted, envelope) if chars > longest else fitted


def playback_budget_seconds(
    cover_seconds_getter: Callable[[], float | None] | None,
    *, enabled: bool | None = None, cover_safety_margin_sec: float | None = None,
    logger=None,
) -> float | None:
    """Snapshot cover minus margin once; None selects fixed-count fallback."""
    if not (TTS_DEADLINE_AGGREGATION if enabled is None else enabled):
        return None
    if cover_seconds_getter is None:
        return None
    try:
        cover = cover_seconds_getter()
        if cover is None or not math.isfinite(float(cover)):
            return None
        margin = TTS_COVER_SAFETY_MARGIN_SEC if cover_safety_margin_sec is None else cover_safety_margin_sec
        return float(cover) - max(0.0, float(margin))
    except Exception:
        if logger is not None:
            logger.debug("playback cover unavailable", exc_info=True)
        return None
