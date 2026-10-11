"""Small standalone TTS API. This package never imports the Amadeus Host."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable

__version__ = "0.1.0"


class CancelledError(Exception):
    """Cooperative request cancellation."""


@dataclass(frozen=True)
class TTSRequest:
    utterance_id: str
    text: str
    language: str
    speed: float = 1.0
    deadline_ms: int | None = None


@dataclass(frozen=True)
class AudioChunk:
    sample_rate: int
    pcm_s16le: bytes


class RequestContext:
    def __init__(self, *, config: dict[str, Any] | None = None,
                 secrets: dict[str, str] | None = None, paths: dict[str, str] | None = None) -> None:
        self.config = MappingProxyType(dict(config or {}))
        self.secrets = MappingProxyType(dict(secrets or {}))
        self.paths = MappingProxyType(dict(paths or {}))
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], Any]] = []

    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def check_cancelled(self) -> None:
        if self.cancelled():
            raise CancelledError()

    def on_cancel(self, callback: Callable[[], Any]) -> Callable[[], Any]:
        """Register resource cleanup, e.g. closing a streaming HTTP response."""
        with self._lock:
            already_cancelled = self.cancelled()
            if not already_cancelled:
                self._callbacks.append(callback)
        if already_cancelled:
            callback()
        return callback

    def _cancel(self) -> None:
        with self._lock:
            if self.cancelled():
                return
            self._cancelled.set()
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass


class Extension:
    def __init__(self) -> None:
        self._tts: dict[str, Callable] = {}
        self._initialize: Callable | None = None
        self._shutdown: Callable | None = None

    def tts(self, contribution_id: str):
        def register(function: Callable) -> Callable:
            if contribution_id in self._tts:
                raise ValueError("duplicate TTS contribution")
            self._tts[contribution_id] = function
            return function
        return register

    def initialize(self, function: Callable) -> Callable:
        self._initialize = function
        return function

    def shutdown(self, function: Callable) -> Callable:
        self._shutdown = function
        return function


__all__ = ["Extension", "TTSRequest", "RequestContext", "AudioChunk", "CancelledError"]
