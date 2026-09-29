"""Producer cost observations and a single playback budget snapshot."""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Hashable
from statistics import median

from config.settings import TTS_COVER_SAFETY_MARGIN_SEC, TTS_DEADLINE_AGGREGATION


class SynthesisCostModel:
    """Robust affine fits per inference profile, with five recent observations.

    The bounded window rejects a lone cold capture without retaining a whole
    session's old performance. The caller supplies completed producer time.
    """

    def __init__(self):
        self._samples: dict[Hashable, deque[tuple[int, float]]] = {}

    def clear(self) -> None:
        self._samples.clear()

    def observe(self, profile: Hashable, chars: int, elapsed: float) -> None:
        if chars > 0 and math.isfinite(elapsed) and elapsed > 0:
            self._samples.setdefault(profile, deque(maxlen=5)).append((chars, elapsed))

    def predict(self, profile: Hashable, chars: int, *, initial_seconds_per_char: float) -> float:
        slope = max(0.001, initial_seconds_per_char)
        samples = list(self._samples.get(profile, ()))
        # One cold capture is not calibration; two points cannot distinguish
        # an outlier from fixed request overhead either.
        if len(samples) < 3:
            return max(0, chars) * slope
        slopes = [(y2 - y1) / (x2 - x1)
                  for i, (x1, y1) in enumerate(samples)
                  for x2, y2 in samples[i + 1:] if x1 != x2]
        if slopes and len(samples) >= 5:
            slope = max(0.0, median(slopes))
        else:
            # At one length the two coefficients are not identifiable. Keep
            # the prior slope unless it alone exceeds the typical total cost.
            slope = min(slope, median(y / x for x, y in samples))
        intercept = max(0.0, median(y - slope * x for x, y in samples))
        return max(0.0, intercept + slope * max(0, chars))


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
