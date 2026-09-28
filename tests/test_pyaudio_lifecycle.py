"""Native lifecycle calls serialize while stream I/O retains its own owners."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from core.pyaudio_lifecycle import initialize_pyaudio, terminate_pyaudio


def test_initialization_and_termination_share_one_lock():
    barrier = threading.Barrier(4)
    active = maximum = 0

    def native_call():
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        time.sleep(0.005)
        active -= 1

    class Audio:
        def __init__(self):
            native_call()

        def terminate(self):
            native_call()

    def worker():
        barrier.wait()
        audio = initialize_pyaudio(Audio)
        terminate_pyaudio(audio)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: worker(), range(4)))
    assert maximum == 1 and active == 0


def test_stream_io_does_not_hold_the_native_lifecycle_lock():
    writing, release, initialized = threading.Event(), threading.Event(), threading.Event()

    class Audio:
        def write(self):
            writing.set()
            assert release.wait(2)

    def writer():
        initialize_pyaudio(Audio).write()

    def another_owner():
        initialize_pyaudio(Audio)
        initialized.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(writer)
        assert writing.wait(2)
        second = pool.submit(another_owner)
        try:
            assert initialized.wait(1)
        finally:
            release.set()
        first.result()
        second.result()


def test_failed_initialization_releases_lock_and_does_not_retry():
    calls = []

    def unavailable():
        calls.append(True)
        raise OSError("device unavailable")

    with pytest.raises(OSError, match="device unavailable"):
        initialize_pyaudio(unavailable)
    assert len(calls) == 1
    assert initialize_pyaudio(lambda: "next owner") == "next owner"
