"""Process-local pre-translation state shared by chat and presentation code."""

from __future__ import annotations

import os
from threading import RLock

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def configured_default_enabled() -> bool:
    """Read the startup default, preserving the historical typo alias."""

    raw = (
        os.environ.get("AMADEUS_PRE_TRANSLATION_ENABLED")
        or os.environ.get("AMADUES_PRE_TRANSLATION_ENABLED")
        or "0"
    )
    return str(raw).strip().lower() in _TRUE_VALUES


class PreTranslationRuntime:
    """Own the effective per-process value after presentation policy is applied."""

    def __init__(self, enabled: bool) -> None:
        self._lock = RLock()
        self._enabled = bool(enabled)

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def configure(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)


runtime = PreTranslationRuntime(configured_default_enabled())
