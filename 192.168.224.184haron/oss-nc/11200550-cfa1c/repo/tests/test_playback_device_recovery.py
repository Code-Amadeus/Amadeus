"""Output lifecycle, bounded open recovery, and shared native ownership."""
import asyncio
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from tts.playback import StreamPlayer


@pytest.fixture
def device_factory(monkeypatch):
    devices = []
    stream = SimpleNamespace(write=Mock(), close=Mock(), stop_stream=Mock(), is_active=lambda: True)

    def device(error=None):
        pa = SimpleNamespace(open=Mock(side_effect=error) if error else Mock(return_value=stream),
                             terminate=Mock())
        devices.append(pa)
        return pa

    factory = Mock()
    monkeypatch.setitem(sys.modules, "pyaudio", SimpleNamespace(PyAudio=factory, paFloat32=1))
    player = StreamPlayer(SimpleNamespace(publish_mouth_value=Mock()))
    yield player, factory, device, stream
    player.cleanup()


def test_stale_output_recovers_and_writes_same_audio(device_factory, caplog):
    player, factory, device, stream = device_factory
    stale, fresh = device(OSError(-9986, "Internal PortAudio error")), device()
    # Reproduce a long-lived backend which already owns the stale instance.
    player.pyaudio_instance = stale
    factory.return_value = fresh
    assert player.prewarm(44100, silence_ms=10)
    stale.terminate.assert_called_once_with()
    factory.assert_called_once_with()
    assert player.pyaudio_instance is fresh
    assert player.stream is stream and player.is_playing
    assert len(stream.write.call_args.args[0]) == 441 * 4
    assert "AUDIO-RECOVERY" in caplog.text
    player.initialize(44100)
    fresh.open.assert_called_once()


def test_persistent_failure_is_bounded_and_next_call_can_recover(device_factory):
    player, factory, device, stream = device_factory
    first = device(OSError(-9986, "Internal PortAudio error"))
    second = device(OSError(-9986, "Still unavailable"))
    healthy = device()
    factory.side_effect = [first, second, healthy]
    with pytest.raises(OSError, match="Still unavailable"):
        player.initialize(44100)
    assert factory.call_count == 2
    first.terminate.assert_called_once_with()
    second.terminate.assert_called_once_with()
    assert player.pyaudio_instance is None and player.stream is None
    assert not player.is_playing
    player.initialize(44100)
    assert player.stream is stream


def test_unsupported_format_is_reported_without_retry(device_factory):
    player, factory, device, _ = device_factory
    invalid = device(OSError(-9997, "Invalid sample rate"))
    factory.return_value = invalid
    with pytest.raises(OSError, match="Invalid sample rate"):
        player.initialize(123)
    factory.assert_called_once_with()
    invalid.terminate.assert_called_once_with()
    assert player.pyaudio_instance is None and not player.is_playing


@pytest.mark.parametrize("capture_alive", [False, True])
@pytest.mark.parametrize("failure", ["transient", "shared_host_state"])
def test_retry_respects_shared_portaudio_owners(monkeypatch, capture_alive, failure):
    # Use real PyAudio wrapper lifetime methods, but no native audio device.
    # The simulated native reference count models PortAudio's shared host state.
    pyaudio = pytest.importorskip("pyaudio")
    from core.pyaudio_lifecycle import initialize_pyaudio, terminate_pyaudio

    references = attempts = host_resets = 0
    stale = False

    def native_initialize():
        nonlocal references, stale
        if references == 0:
            stale = False
        references += 1

    def native_terminate():
        nonlocal references, host_resets
        assert references > 0
        references -= 1
        if references == 0:
            host_resets += 1

    stream = SimpleNamespace(write=Mock(), close=Mock(), stop_stream=Mock(), is_active=lambda: True)

    def output_open(_owner, **_kwargs):
        nonlocal attempts
        attempts += 1
        if (failure == "transient" and attempts == 1) or (failure == "shared_host_state" and stale):
            raise OSError(-9986, "Synthetic internal output failure")
        return stream

    monkeypatch.setattr(pyaudio.pa, "initialize", native_initialize)
    monkeypatch.setattr(pyaudio.pa, "terminate", native_terminate)
    monkeypatch.setattr(pyaudio.PyAudio, "open", output_open)
    capture = initialize_pyaudio(pyaudio.PyAudio) if capture_alive else None
    player = StreamPlayer(SimpleNamespace(publish_mouth_value=Mock()))
    player.pyaudio_instance = initialize_pyaudio(pyaudio.PyAudio)
    stale = True
    try:
        if capture_alive and failure == "shared_host_state":
            with pytest.raises(OSError, match="Synthetic internal output failure"):
                player.initialize(44100)
            assert player.stream is None and player.pyaudio_instance is None
            assert not player.is_playing
            assert references == 1
        else:
            player.initialize(44100)
            assert player.stream is stream and player.is_playing
            assert references == (2 if capture_alive else 1)
        assert attempts == 2
        # Output recovery never releases the capture owner's reference.
        assert host_resets == (0 if capture_alive else 1)
    finally:
        player.cleanup()
        if capture is not None:
            terminate_pyaudio(capture)
    assert references == 0


class _ObservedRLock:
    """Report real lock contention so races need no sleep or scheduling guess."""

    def __init__(self, watched_thread):
        self.lock = threading.RLock()
        self.watched_thread = watched_thread
        self.attempted = threading.Event()
        self.blocked = None

    def __enter__(self):
        acquired = self.lock.acquire(blocking=False)
        if threading.current_thread().name == self.watched_thread:
            self.blocked = not acquired
            self.attempted.set()
        if not acquired:
            self.lock.acquire()
        return self

    def __exit__(self, *_args):
        self.lock.release()


def _named_call(name, call):
    threading.current_thread().name = name
    return call()


@pytest.fixture
def lifecycle_player(monkeypatch):
    streams = []

    def open_stream(**kwargs):
        stream = SimpleNamespace(rate=kwargs["rate"], write=Mock(), close=Mock(),
                                 stop_stream=Mock(), is_active=lambda: True)
        streams.append(stream)
        return stream

    owner = SimpleNamespace(open=Mock(side_effect=open_stream), terminate=Mock())
    monkeypatch.setitem(sys.modules, "pyaudio", SimpleNamespace(
        PyAudio=lambda: owner, paFloat32=1))
    player = StreamPlayer(SimpleNamespace(publish_mouth_value=Mock()))
    yield player, owner, streams
    player.cleanup()


def _write_samples(player, *, queued=False, is_current=None):
    audio = np.full(480, 0.1, dtype=np.float32)
    rate = player._current_rate
    if queued:
        asyncio.run(player.write_audio_async(
            audio, sample_rate=rate, mouth_envelope=True, is_current=is_current))
    else:
        player._write_audio_sync(audio, sample_rate=rate, is_current=is_current)


@pytest.mark.parametrize("rate", [24000, 48000])
@pytest.mark.parametrize("queued", [False, True])
def test_reinitialize_during_stop_keeps_the_replacement_stream(lifecycle_player, monkeypatch, rate, queued):
    player, _, streams = lifecycle_player
    player.initialize(24000)
    old_stream = player.stream
    lock = _ObservedRLock("reinitialize")
    player._stream_lock = lock
    draining, resume_stop = threading.Event(), threading.Event()
    original_drain = player._drain_audio_writer_queue

    def drain():
        if threading.current_thread().name == "stop":
            draining.set()
            assert resume_stop.wait(5), "stop was not released"
        original_drain()

    monkeypatch.setattr(player, "_drain_audio_writer_queue", drain)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stopping = pool.submit(_named_call, "stop", player.stop)
        try:
            assert draining.wait(5), "stop did not reach queue draining"
            reopening = pool.submit(_named_call, "reinitialize", lambda: player.initialize(rate))
            assert lock.attempted.wait(5), "initialization did not attempt the device lock"
            # On the broken implementation initialization can finish inside the
            # paused stop. Let it finish to reproduce the lost replacement exactly.
            if not lock.blocked:
                reopening.result(timeout=5)
        finally:
            resume_stop.set()
        stopping.result(timeout=5)
        reopening.result(timeout=5)

    assert len(streams) == 2
    assert player.is_playing and player.stream is streams[1]
    assert player._current_rate == rate
    old_stream.close.assert_called_once()
    player.stream.close.assert_not_called()
    _write_samples(player, queued=queued)
    written = b"".join(call.args[0] for call in player.stream.write.call_args_list)
    assert written == np.full(480, 0.1, dtype=np.float32).tobytes()


@pytest.mark.parametrize("warm", [False, True])
def test_stop_during_open_leaves_a_consistently_stopped_player(lifecycle_player, warm):
    player, owner, streams = lifecycle_player
    if warm:
        player.initialize(24000)
    lock = _ObservedRLock("stop")
    player._stream_lock = lock
    opening, resume_open = threading.Event(), threading.Event()
    original_open = owner.open.side_effect

    def open_stream(**kwargs):
        if threading.current_thread().name == "initialize":
            opening.set()
            assert resume_open.wait(5), "device open was not released"
        return original_open(**kwargs)

    owner.open.side_effect = open_stream
    with ThreadPoolExecutor(max_workers=2) as pool:
        initializing = pool.submit(_named_call, "initialize", lambda: player.initialize(48000))
        try:
            assert opening.wait(5), "initialization did not reach device open"
            stopping = pool.submit(_named_call, "stop", player.stop)
            assert lock.attempted.wait(5), "stop did not attempt the device lock"
        finally:
            resume_open.set()
        initializing.result(timeout=5)
        stopping.result(timeout=5)

    assert player.stream is None and not player.is_playing
    for stream in streams:
        stream.close.assert_called_once()
    _write_samples(player)  # A stopped player drops PCM without an empty-stream error.
    assert all(not stream.write.called for stream in streams)
    player.initialize(24000)
    _write_samples(player)
    assert player.stream is streams[-1] and player.stream.write.called


@pytest.mark.parametrize("queued", [False, True])
def test_stop_waits_for_inflight_write_and_stale_windows_do_not_resume(lifecycle_player, queued):
    player, _, _ = lifecycle_player
    player.initialize(24000)
    stream = player.stream
    lock = _ObservedRLock("stop")
    player._stream_lock = lock
    writing, resume_write, current = threading.Event(), threading.Event(), threading.Event()
    current.set()

    def write(_data):
        writing.set()
        assert resume_write.wait(5), "native write was not released"
        stream.close.assert_not_called()

    stream.write.side_effect = write
    with ThreadPoolExecutor(max_workers=2) as pool:
        playing = pool.submit(_write_samples, player, queued=queued, is_current=current.is_set)
        try:
            assert writing.wait(5), "playback did not reach native write"
            # The host invalidates the playback epoch before stopping the device.
            current.clear()
            stopping = pool.submit(_named_call, "stop", player.stop)
            assert lock.attempted.wait(5), "stop did not attempt the device lock"
            stream.close.assert_not_called()
        finally:
            resume_write.set()
        playing.result(timeout=5)
        stopping.result(timeout=5)

    stream.write.assert_called_once()
    assert len(stream.write.call_args.args[0]) == 240 * 4
    stream.close.assert_called_once()
    assert player.stream is None and not player.is_playing


def test_stop_from_window_callback_remains_reentrant(lifecycle_player):
    player, _, _ = lifecycle_player
    player.initialize(24000)
    stream = player.stream
    player._write_audio_sync(np.ones(480, dtype=np.float32),
                             before_window=lambda _window: player.stop())
    stream.write.assert_not_called()
    stream.close.assert_called_once()
    assert player.stream is None and not player.is_playing
