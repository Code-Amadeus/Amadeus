"""Monotonic latency anchors for first-sentence end-to-end tracing."""

from __future__ import annotations

import time
from threading import Lock

_t0: float | None = None
_lock = Lock()


def mark_llm_stream_request_sent() -> None:
    """Record the E2E start anchor for the current turn."""
    global _t0
    with _lock:
        _t0 = time.perf_counter()


def read_ms_since_stream_request_sent(clear: bool = False) -> float | None:
    """Return ms since the anchor, optionally clearing it."""
    global _t0
    with _lock:
        if _t0 is None:
            return None
        ms = (time.perf_counter() - _t0) * 1000.0
        if clear:
            _t0 = None
        return ms


def log_latency_marker(logger, stage: str, clear: bool = False, **fields) -> float | None:
    """Emit a stable ASCII latency marker line.

    Example:
        [LATENCY] stage=first_chunk ms=123.4 id=sentence_1_x samples=4096
    """
    ms = read_ms_since_stream_request_sent(clear=clear)
    if ms is None:
        return None
    extras = []
    for key, value in fields.items():
        if value is None:
            continue
        extras.append(f"{key}={value}")
    suffix = f" {' '.join(extras)}" if extras else ""
    logger.info(f"[LATENCY] stage={stage} ms={ms:.1f}{suffix}")
    return ms


def consume_ms_since_stream_request_sent() -> float | None:
    """Compatibility helper for the existing first-play log."""
    return read_ms_since_stream_request_sent(clear=True)
