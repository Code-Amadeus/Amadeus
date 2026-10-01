"""Deadline-aware aggregation budget helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from config.settings import (
    TTS_CHARS_PER_SEC,
    TTS_COVER_SAFETY_MARGIN_SEC,
    TTS_DEADLINE_AGGREGATION,
    TTS_RTF_INITIAL,
)


def estimate_synthesis_seconds(
    char_count: int,
    *,
    rtf: float,
    chars_per_sec: float | None = None,
) -> float:
    cps = TTS_CHARS_PER_SEC if chars_per_sec is None else float(chars_per_sec)
    cps = max(0.001, cps)
    return (max(0, int(char_count)) / cps) * max(0.0, float(rtf))


def deadline_budget_exceeded(
    char_count: int,
    *,
    cover_seconds_getter: Callable[[], float | None] | None,
    rtf_getter: Callable[[], float | None] | None = None,
    enabled: bool | None = None,
    cover_safety_margin_sec: float | None = None,
    chars_per_sec: float | None = None,
    logger: Any = None,
) -> bool | None:
    """Return True when estimated synthesis time exceeds current playback cover.

    None means the required estimator was unavailable, so callers should keep
    their previous max_chars-only behavior.
    """
    is_enabled = TTS_DEADLINE_AGGREGATION if enabled is None else bool(enabled)
    if not is_enabled:
        return False
    if cover_seconds_getter is None:
        return None

    try:
        cover = cover_seconds_getter()
        if cover is None:
            return None
        cover = float(cover)
    except Exception:
        if logger is not None:
            logger.debug("deadline cover estimator unavailable", exc_info=True)
        return None

    try:
        if rtf_getter is None:
            rtf = float(TTS_RTF_INITIAL)
        else:
            rtf_value = rtf_getter()
            if rtf_value is None:
                return None
            rtf = float(rtf_value)
    except Exception:
        if logger is not None:
            logger.debug("deadline rtf estimator unavailable", exc_info=True)
        return None

    margin = (
        TTS_COVER_SAFETY_MARGIN_SEC
        if cover_safety_margin_sec is None
        else float(cover_safety_margin_sec)
    )
    est_synth = estimate_synthesis_seconds(
        char_count,
        rtf=rtf,
        chars_per_sec=chars_per_sec,
    )
    return est_synth > (cover - margin)
