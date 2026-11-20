"""Neutral mouth-animation signal routing.

TTS owns the signal value, while the application Host chooses its consumers.
The primary renderer must not depend on an optional external character host.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol


logger = logging.getLogger(__name__)


class MouthSignalSink(Protocol):
    """Consumer contract used by audio playback."""

    def publish_mouth_value(self, value: float) -> None:
        """Publish one normalized mouth-open value."""


class MouthSignalRouter:
    """Fan mouth values out to the local renderer and an optional side path.

    The local renderer is the product path. ``compatibility_sink`` exists for
    optional integrations such as VTube Studio and is deliberately isolated:
    either sink may be absent or fail without interrupting audio playback or
    the other sink.
    """

    def __init__(
        self,
        *,
        primary_sink: Callable[[float], object] | None = None,
        compatibility_sink: Callable[[float], object] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._primary_sink = primary_sink
        self._compatibility_sink = compatibility_sink

    def set_primary_sink(self, sink: Callable[[float], object] | None) -> None:
        with self._lock:
            self._primary_sink = sink

    def set_compatibility_sink(
        self,
        sink: Callable[[float], object] | None,
    ) -> None:
        with self._lock:
            self._compatibility_sink = sink

    def publish_mouth_value(self, value: float) -> None:
        mouth_value = float(value)
        with self._lock:
            sinks = (
                ("primary", self._primary_sink),
                ("compatibility", self._compatibility_sink),
            )
        for label, sink in sinks:
            if sink is None:
                continue
            try:
                sink(mouth_value)
            except Exception:
                # Mouth values are a high-frequency presentation hint. A sink
                # must never stall or abort physical audio playback.
                logger.debug("mouth %s sink rejected a value", label, exc_info=True)
