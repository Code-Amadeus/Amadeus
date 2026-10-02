"""Qwen3-ASR stays hot in VRAM near conversation, waits in RAM, and comes back on wake."""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from asr.backends.qwen3_asr import Qwen3ASRBackend
from asr.manager import ASRManager
from server.event_bus import EventBus
from server.handlers import asr_handler as handler_module
from server.handlers.asr_handler import AsrHandler


class FakeWeights:
    def __init__(self):
        self.device = "cuda:0"
        self.moves = []

    def to(self, device):
        self.device = device
        self.moves.append(device)
        return self


class FakeQwen:
    def __init__(self, delay=0.0):
        self.model = FakeWeights()
        self.max_new_tokens = 256
        self.delay = delay
        self.devices_seen = []

    def transcribe(self, **_kwargs):
        self.devices_seen.append(self.model.device)
        time.sleep(self.delay)
        return [SimpleNamespace(text="こんにちは")]


@pytest.fixture
def backend(monkeypatch):
    for name in ("_shared_model", "_shared_device", "_shared_parked", "_last_used"):
        monkeypatch.setattr(Qwen3ASRBackend, name, getattr(Qwen3ASRBackend, name))
    engine = Qwen3ASRBackend()
    engine._model = FakeQwen()
    engine._device = "cuda:0"
    engine._mode = "inprocess"
    engine._owns_model = True
    Qwen3ASRBackend._shared_model = engine._model
    Qwen3ASRBackend._shared_device = "cuda:0"
    Qwen3ASRBackend._shared_parked = False
    Qwen3ASRBackend._last_used = time.monotonic() - 1000
    return engine


def test_an_idle_model_moves_to_ram_and_returns_before_recognition(backend):
    assert backend.release_vram(180)
    assert backend.parked and backend._model.model.device == "cpu"
    assert backend.transcribe(np.zeros(1600, dtype=np.float32)) == "こんにちは"
    assert backend._model.devices_seen == ["cuda:0"]
    assert not backend.parked


def test_a_recently_used_model_stays_in_vram(backend):
    Qwen3ASRBackend._last_used = time.monotonic()
    assert not backend.release_vram(180)
    assert backend._model.model.moves == []


def test_a_transcription_in_progress_keeps_the_model_hot(backend):
    backend._model.delay = 0.2
    worker = threading.Thread(target=backend.transcribe, args=(np.zeros(1600, dtype=np.float32),))
    worker.start()
    time.sleep(0.05)
    # Waits for the transcription, then sees it was just used.
    assert not backend.release_vram(180)
    worker.join()
    assert backend._model.model.moves == []


def test_prepare_returns_the_model_and_close_clears_the_parked_state(backend):
    backend.release_vram()
    backend.prepare()
    assert backend._model.model.device == "cuda:0" and not backend.parked
    backend.release_vram()
    backend.close()
    assert not Qwen3ASRBackend._shared_parked and Qwen3ASRBackend._shared_model is None


@pytest.mark.parametrize("mode,device", [("sidecar", "cuda:0"), ("inprocess", "cpu")])
def test_backends_that_cannot_park_report_it(backend, mode, device):
    backend._mode, backend._device = mode, device
    assert not backend.release_vram()
    assert backend._model.model.moves == []


def test_speech_start_moves_a_parked_model_back_in_the_background():
    prepared = threading.Event()
    manager = ASRManager.__new__(ASRManager)
    manager._backend = SimpleNamespace(parked=True, prepare=prepared.set)
    manager._on_speech_start_fn = Mock()
    manager._speech_started()
    assert prepared.wait(2)
    manager._on_speech_start_fn.assert_called_once_with()


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setattr(handler_module, "bus", EventBus())
    asr = AsrHandler()
    manager = SimpleNamespace(is_ready=True, release_vram=Mock(return_value=True),
                              prepare_backend_async=Mock())
    asr.configure(asr_manager=manager)
    asr.unload = AsyncMock(return_value={"status": "unloaded"})
    return asr, manager


async def test_an_expired_hot_window_moves_the_model_to_ram_at_once(handler):
    asr, manager = handler
    await asr._finish_listening("awake_timeout")
    await asyncio.wait_for(asr._unload_task, 2)
    manager.release_vram.assert_called_once_with()
    asr.unload.assert_not_awaited()


async def test_other_stops_keep_the_model_hot_for_the_idle_window(handler):
    asr, manager = handler
    await asr._finish_listening("manual_stop")
    await asyncio.sleep(0.05)
    manager.release_vram.assert_not_called()
    asr._unload_task.cancel()


async def test_ram_tier_unloads_only_when_configured(handler, monkeypatch):
    asr, manager = handler
    monkeypatch.setattr(handler_module, "ASR_RAM_UNLOAD_SECONDS", 0.01)
    await asr._unload_after(0)
    manager.release_vram.assert_called_once_with()
    asr.unload.assert_awaited_once()


async def test_backends_that_cannot_park_still_unload(handler):
    asr, manager = handler
    manager.release_vram.return_value = False
    await asr._unload_after(0)
    asr.unload.assert_awaited_once()


async def test_a_wake_start_begins_moving_the_model_back(handler, monkeypatch):
    asr, manager = handler
    monkeypatch.setattr(asr, "_listen_loop", AsyncMock())
    await asr.start_listening({"source": "wake", "awake_seconds": 180})
    manager.prepare_backend_async.assert_called_once_with()


async def test_idle_listening_without_a_wake_fallback_still_leaves_vram(handler, monkeypatch):
    asr, manager = handler
    calls = 0

    def listen_for_speech(**_kwargs):
        nonlocal calls
        calls += 1
        asr._active = calls < 2
        return None

    manager.listen_for_speech = listen_for_speech
    await asr.start_listening({"source": "wake", "awake_seconds": 180, "continuous": True})
    await asyncio.wait_for(asr._listen_task, 2)
    manager.release_vram.assert_any_call(handler_module.ASR_IDLE_UNLOAD_SECONDS)
    await asr.stop_listening()
    asr._unload_task.cancel()
