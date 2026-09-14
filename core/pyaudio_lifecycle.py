"""Serialize process-wide PortAudio initialization/termination, not audio I/O."""
from threading import RLock
from typing import Callable, TypeVar


T = TypeVar("T")
_lifecycle_lock = RLock()


def initialize_pyaudio(factory: Callable[[], T]) -> T:
    # PyAudio releases the GIL in native initialization. Concurrent instances
    # can otherwise race PortAudio's process-wide initialization/refcount.
    with _lifecycle_lock:
        return factory()


def terminate_pyaudio(instance) -> None:
    with _lifecycle_lock:
        instance.terminate()
