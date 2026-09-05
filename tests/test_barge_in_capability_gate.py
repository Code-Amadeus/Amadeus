"""VAD preparation must not block playback or outlive a cancelled start."""
from __future__ import annotations

import asyncio
import builtins
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from asr.barge_in_detector import BargeInDetector

ROOT = Path(__file__).resolve().parents[1]


async def _noop() -> None:
    pass


def _detector() -> BargeInDetector:
    return BargeInDetector(tts_playing_fn=lambda: True, on_barge_in=_noop)


async def _wait(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 5), "background operation did not complete"


def test_missing_vad_is_probed_once_without_starting_listener() -> None:
    probe = '''
import asyncio
import sys
from unittest.mock import patch

class Blocker:
    calls = 0
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'silero_vad', 'torch', 'torchaudio'}:
            self.calls += 1
            raise ModuleNotFoundError('L2 package absent', name=fullname)

blocker = Blocker()
sys.meta_path.insert(0, blocker)
import server.app
from asr.barge_in_detector import BargeInDetector

async def noop(): pass
async def run():
    detector = BargeInDetector(tts_playing_fn=lambda: True, on_barge_in=noop)
    assert detector._vad_capability is None
    with patch.object(detector, '_run') as listen:
        detector.start(asyncio.get_running_loop())
        preparation = detector._preparation_thread
        if preparation is not None:
            await asyncio.to_thread(preparation.join, 5)
            assert not preparation.is_alive()
        assert detector._vad_capability == ('fallback', '')
        calls = blocker.calls
        assert calls > 0
        for _ in range(5):
            detector.start(asyncio.get_running_loop())
        assert blocker.calls == calls
        assert detector._preparation_thread is None
        assert not detector.running
        listen.assert_not_called()
        assert server.app.asr_manager is None
        detector.stop()

asyncio.run(run())
print('L2_BARGE_IN_OK')
'''
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, capture_output=True,
        text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "L2_BARGE_IN_OK" in result.stdout


@pytest.mark.parametrize("error", [
    ModuleNotFoundError("missing torch", name="torch"),
    OSError("torch DLL cannot load"),
])
@pytest.mark.asyncio
async def test_broken_dependency_is_cached_as_degraded(monkeypatch, error, caplog) -> None:
    original_import = builtins.__import__
    calls = []

    def broken_import(name, *args, **kwargs):
        if name == "silero_vad":
            calls.append(name)
            raise error
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    detector = _detector()
    listeners = []
    monkeypatch.setattr(detector, "_run", lambda: listeners.append(True))
    with caplog.at_level("INFO"):
        detector.start(asyncio.get_running_loop())
        preparation = detector._preparation_thread
        if preparation is not None:
            await asyncio.to_thread(preparation.join, 5)
            assert not preparation.is_alive()
        assert detector._vad_capability[0] == "degraded"
        assert "torch" in detector._vad_capability[1]
        detector.start(asyncio.get_running_loop())
        detector.stop()
    assert calls == ["silero_vad"]
    assert not listeners
    assert len([r for r in caplog.records if "detector disabled" in r.message]) == 1
    assert not any("detector failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_failed_model_load_is_cached(monkeypatch) -> None:
    calls = []

    def load():
        calls.append(True)
        raise RuntimeError("invalid model")

    monkeypatch.setitem(sys.modules, "silero_vad", SimpleNamespace(load_silero_vad=load))
    detector = _detector()
    detector.start(asyncio.get_running_loop())
    preparation = detector._preparation_thread
    if preparation is not None:
        await asyncio.to_thread(preparation.join, 5)
        assert not preparation.is_alive()
    assert detector._vad_capability == ("degraded", "RuntimeError: invalid model")
    detector.start(asyncio.get_running_loop())
    detector.stop()
    assert calls == [True]
    assert not detector.running


@pytest.mark.parametrize("during_load", ["playing", "stopped", "restarted"])
@pytest.mark.asyncio
async def test_slow_preparation_keeps_loop_responsive_and_honors_stop(monkeypatch, during_load) -> None:
    entered, release, listening = threading.Event(), threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    loads = []
    model = SimpleNamespace(eval=lambda: None)

    def load():
        loads.append(threading.get_ident())
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test never released slow load")
        return model

    monkeypatch.setitem(sys.modules, "silero_vad", SimpleNamespace(load_silero_vad=load))
    detector = _detector()

    def listen():
        assert detector._vad_model is model
        listening.set()

    monkeypatch.setattr(detector, "_run", listen)
    detector.start(loop)
    preparation = detector._preparation_thread
    assert preparation is not None
    try:
        await _wait(entered)
        tick = asyncio.Event()
        loop.call_soon(tick.set)
        await asyncio.wait_for(tick.wait(), 1)
        assert not release.is_set() and not listening.is_set()
        for _ in range(5):
            detector.start(loop)
        if during_load != "playing":
            detector.stop()
            assert preparation.is_alive() and not release.is_set()
        if during_load == "restarted":
            detector.start(loop)
        release.set()
        await asyncio.to_thread(preparation.join, 5)
        assert not preparation.is_alive()
        if during_load == "stopped":
            assert not listening.is_set()
            # A later sentence after idle may reuse the prepared model.
            detector.start(loop)
        await _wait(listening)
        assert len(loads) == 1 and loads[0] != loop_thread
    finally:
        release.set()
        await asyncio.to_thread(preparation.join, 5)
        detector.stop()


@pytest.mark.asyncio
async def test_prepared_model_can_interrupt_first_keyboard_reply(monkeypatch) -> None:
    from asr import barge_in_detector as module
    from asr import mic_input_service
    from server import app

    monkeypatch.setattr(app, "asr_manager", None)

    model = SimpleNamespace(eval=lambda: None)
    loads, microphones = [], []
    detected = asyncio.Event()

    def load():
        loads.append(True)
        return model

    class Iterator:
        def __init__(self, prepared, **kwargs):
            assert prepared is model

        def __call__(self, audio, **kwargs):
            return {"start": 0}

    frame = SimpleNamespace(
        audio=np.full(512, 0.1, dtype=np.float32), raw_audio=None,
        rms=0.1, timestamp=0.0, seq=10,
    )
    mic = SimpleNamespace(
        start=lambda: microphones.append(True), mic_index=7,
        cursor=lambda: SimpleNamespace(read=lambda **kwargs: frame),
        mark_handoff=lambda *args, **kwargs: None,
    )
    monkeypatch.setitem(sys.modules, "silero_vad", SimpleNamespace(load_silero_vad=load, VADIterator=Iterator))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(from_numpy=lambda value: value))
    monkeypatch.setattr(mic_input_service, "get_mic_input_service", lambda: mic)
    monkeypatch.setattr(module, "BARGE_IN_START_DELAY_MS", 0)

    async def interrupt():
        detected.set()

    detector = BargeInDetector(tts_playing_fn=lambda: True, on_barge_in=interrupt)
    monkeypatch.setattr(detector, "_collect_confirm_frames", lambda cursor, first: [first])
    monkeypatch.setattr(detector, "_is_self_echo_candidate", lambda frames: False)
    try:
        for _ in range(2):
            detector.start(asyncio.get_running_loop())
            await asyncio.wait_for(detected.wait(), 5)
            detector.stop()
            detected.clear()
        assert len(loads) == 1
        assert len(microphones) == 2
        assert app.asr_manager is None
    finally:
        detector.stop()
