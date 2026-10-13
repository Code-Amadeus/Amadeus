"""Preparation follows backend readiness, including deferred graph capture."""
import threading
from types import SimpleNamespace

from asr.manager import ASRManager


def test_resident_backend_gets_a_background_preparation_opportunity():
    prepared, release = threading.Event(), threading.Event()

    def prepare():
        prepared.set()
        assert release.wait(2)

    manager = ASRManager.__new__(ASRManager)
    manager._backend = SimpleNamespace(parked=False, prepare=prepare)
    try:
        manager.prepare_backend_async()
        # The caller is free to continue recording while preparation is active.
        assert prepared.wait(2)
    finally:
        release.set()


def test_backend_without_preparation_support_remains_usable():
    manager = ASRManager.__new__(ASRManager)
    manager._backend = SimpleNamespace()
    manager.prepare_backend_async()
