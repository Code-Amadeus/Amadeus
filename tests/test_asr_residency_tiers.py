"""Qwen3-ASR stays hot in VRAM near conversation, waits in RAM, and comes back on wake."""

import asyncio
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from weakref import WeakSet

import numpy as np
import pytest

from asr.backends.qwen3_asr import Qwen3ASRBackend
from asr.backend import ASRBackendFatalError
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
    for name in ("_shared_model", "_shared_device", "_shared_attn_impl", "_shared_parked",
                 "_last_used", "_inprocess_disabled_until", "_inprocess_disabled_reason"):
        monkeypatch.setattr(Qwen3ASRBackend, name, getattr(Qwen3ASRBackend, name))
    monkeypatch.setattr(Qwen3ASRBackend, "_model_users", WeakSet())
    monkeypatch.setattr(Qwen3ASRBackend, "_shared_model", None)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        _C=SimpleNamespace(), version=SimpleNamespace(hip=None),
        bfloat16="bf16", float32="fp32",
        cuda=SimpleNamespace(is_available=lambda: True, empty_cache=lambda: None,
                             get_device_capability=lambda _device: (8, 0))))
    monkeypatch.setitem(sys.modules, "flash_attn", None)
    monkeypatch.setitem(sys.modules, "qwen_asr", SimpleNamespace(
        Qwen3ASRModel=SimpleNamespace(from_pretrained=lambda *_args, **_kwargs: FakeQwen())))
    monkeypatch.setattr("asr.backends.qwen3_asr.resolve_qwen_model_source", lambda: "synthetic-model")
    monkeypatch.setattr("asr.backends.qwen3_asr.qwen_attention_configs", lambda _model: {"model": "synthetic"})
    # Lifecycle tests own the recognizer boundary, not Qwen/Transformers internals.
    def fake_inference(model, audio, sample_rate, context, language, max_tokens, **_kwargs):
        results = model.transcribe(audio=(audio, sample_rate), context=context, language=language)
        return results, {"engine": "official", "wall_ms": {}, "gpu_ms": {}}
    monkeypatch.setattr("asr.backends.qwen3_asr.transcribe_qwen", fake_inference)
    engine = Qwen3ASRBackend(inference_engine="official")
    engine._load_inprocess("cuda")
    Qwen3ASRBackend._last_used = time.monotonic() - 1000
    yield engine
    for user in list(Qwen3ASRBackend._model_users):
        user.close()


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


def test_park_releases_model_owned_graph_before_weights_move(backend, monkeypatch):
    closed = []
    class Graph:
        def close(self):
            assert backend._model.model.device == "cuda:0"
            closed.append(True)
    backend._model.model._amadeus_qwen_cuda_graphs = {256: Graph()}
    assert backend.release_vram()
    assert closed == [True]
    assert not hasattr(backend._model.model, "_amadeus_qwen_cuda_graphs")


def test_default_prepares_on_shared_load_and_speech_prepare_but_explicit_official_does_not(backend, monkeypatch):
    from asr.qwen_cuda_graph import DEFAULT_CAPACITIES
    monkeypatch.setattr("asr.backends.qwen3_asr.QWEN3_ASR_CUDA_GRAPH", True)
    prepared = []
    monkeypatch.setattr("asr.backends.qwen3_asr.qwen_cuda_graph_support_reason", lambda _model: None)
    monkeypatch.setattr("asr.backends.qwen3_asr.prepare_qwen_cuda_graphs",
                        lambda model, capacities, **kwargs: prepared.append((model, kwargs, capacities)) or {"deferred": True})
    backend.prepare()
    assert prepared == []
    graph_user = Qwen3ASRBackend()
    graph_user._load_inprocess("cuda")
    assert len(prepared) == 1 and prepared[0][1]["capture_allowed"]
    graph_user.prepare()
    assert len(prepared) == 2
    assert all(model is backend._model.model and capacities == DEFAULT_CAPACITIES
               and options["capture_allowed"] for model, options, capacities in prepared)
    graph_user.close()


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
        asr._awake_until = time.monotonic() - 1
        asr._active = calls < 2
        return None

    manager.listen_for_speech = listen_for_speech
    await asr.start_listening({"source": "wake", "awake_seconds": 180, "continuous": True})
    await asyncio.wait_for(asr._listen_task, 2)
    manager.release_vram.assert_any_call(handler_module.ASR_IDLE_UNLOAD_SECONDS)
    await asr.stop_listening()
    asr._unload_task.cancel()


@pytest.mark.parametrize("wake_word", [True, False])
async def test_reply_completion_keeps_model_hot_for_the_renewed_window(backend, monkeypatch, wake_word):
    monkeypatch.setattr(handler_module, "bus", EventBus())
    monkeypatch.setattr(handler_module, "ASR_IDLE_UNLOAD_SECONDS", 180)
    captures = 0

    def listen_for_speech(**_kwargs):
        nonlocal captures
        captures += 1
        if captures == 2:
            raise asyncio.CancelledError
        return None

    asr = AsrHandler()
    asr.configure(asr_manager=SimpleNamespace(is_ready=True,
        release_vram=backend.release_vram, listen_for_speech=listen_for_speech),
        wake_resumable_fn=lambda: wake_word)
    asr._active, asr._source, asr._continuous_awake = True, "wake", True
    asr._awake_seconds = 180
    asr._waiting_turn_complete = True
    # ASR last ran before a long reply, but reply completion renews the hot window.
    await asr.notify_turn_complete("playback")
    await asr._listen_loop()
    assert asr._awake_until > time.monotonic()
    assert backend._model.model.device == "cuda:0"
    assert backend._model.model.moves == []


@pytest.mark.parametrize("first_closes", ["conversation", "wake"])
def test_shared_model_survives_either_users_close_until_the_last_release(backend, first_closes):
    wake = Qwen3ASRBackend(inference_engine="official")
    wake._load_inprocess("cuda")
    model = backend._model
    assert wake._model is model
    assert backend.release_vram()
    first, remaining = (backend, wake) if first_closes == "conversation" else (wake, backend)
    first.close()
    first.close()  # Repeated cleanup cannot detach the remaining user.
    assert Qwen3ASRBackend._shared_model is model
    assert remaining.parked
    assert remaining.transcribe(np.zeros(1600, dtype=np.float32)) == "こんにちは"
    assert model.devices_seen == ["cuda:0"]
    # Restarting Conversation while Wake holds the model must reuse its residency facts.
    rejoined = Qwen3ASRBackend(inference_engine="official")
    rejoined._load_inprocess("cuda")
    rejoined._load_inprocess("cuda")
    assert rejoined._model is model
    remaining.close()
    assert rejoined.release_vram() and model.model.device == "cpu"
    rejoined.close()
    assert Qwen3ASRBackend._shared_model is None and not Qwen3ASRBackend._shared_parked


@pytest.mark.parametrize("operation", ["park", "restore", "transcribe"])
def test_close_waits_for_model_use_before_replacement(backend, monkeypatch, operation):
    model = backend._model
    if operation == "restore":
        backend.release_vram()
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()
    original = model.transcribe if operation == "transcribe" else model.model.to

    def hold(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test did not release model operation"
        return original(*args, **kwargs)

    if operation == "transcribe":
        monkeypatch.setattr(model, "transcribe", hold)
        act = lambda: backend.transcribe(np.zeros(1600, dtype=np.float32))
    else:
        monkeypatch.setattr(model.model, "to", hold)
        act = backend.release_vram if operation == "park" else backend.prepare

    def close():
        closing.set()
        backend.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        moving = workers.submit(act)
        try:
            assert entered.wait(2)
            closed = workers.submit(close)
            assert closing.wait(2)
            assert not closed.done()
        finally:
            release.set()
        moving.result(timeout=3)
        closed.result(timeout=3)
    assert backend._model is None and Qwen3ASRBackend._shared_model is None
    assert not Qwen3ASRBackend._shared_parked
    replacement = Qwen3ASRBackend(inference_engine="official")
    replacement._load_inprocess("cuda")
    assert replacement._model is not model and not replacement.parked
    backend.prepare()  # A late prepare from the retired manager cannot touch the new model.
    assert not backend.release_vram()
    assert replacement.release_vram()
    assert replacement._model.model.device == "cpu"


@pytest.mark.parametrize("failure_at", ["restore", "transcribe"])
def test_fatal_model_failure_invalidates_all_shared_users(backend, monkeypatch, failure_at):
    wake = Qwen3ASRBackend(inference_engine="official")
    wake._load_inprocess("cuda")

    def fail(*_args, **_kwargs):
        raise RuntimeError("CUDA error: synthetic failure")

    if failure_at == "restore":
        backend.release_vram()
        monkeypatch.setattr(backend._model.model, "to", fail)
    else:
        monkeypatch.setattr(backend._model, "transcribe", fail)
    with pytest.raises(ASRBackendFatalError):
        backend.transcribe(np.zeros(1600, dtype=np.float32))
    assert backend._model is None and wake._model is None
    assert Qwen3ASRBackend._shared_model is None and not Qwen3ASRBackend._shared_parked
    assert Qwen3ASRBackend._inprocess_disabled_until > time.time()


def test_close_waits_for_an_in_progress_load_before_releasing_it(backend, monkeypatch):
    backend.close()
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()

    def load_model(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return FakeQwen()

    monkeypatch.setattr(sys.modules["qwen_asr"].Qwen3ASRModel, "from_pretrained", load_model)

    def close():
        closing.set()
        backend.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        loading = workers.submit(backend._load_inprocess, "cuda")
        try:
            assert entered.wait(2)
            closed = workers.submit(close)
            assert closing.wait(2)
            assert not closed.done()
        finally:
            release.set()
        loading.result(timeout=3)
        closed.result(timeout=3)
    assert backend._model is None and Qwen3ASRBackend._shared_model is None


def test_sidecar_shutdown_does_not_wait_for_blocked_pipe_io(backend, monkeypatch):
    backend.close()
    sidecar = Qwen3ASRBackend()
    sidecar._mode, sidecar._owns_proc = "sidecar", True
    sidecar._proc = process = object()
    terminate = Mock()
    monkeypatch.setattr(sidecar, "_terminate_process_tree", terminate)
    entered, release = threading.Event(), threading.Event()

    def pipe_read():
        with sidecar._lock:
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as workers:
        reading = workers.submit(pipe_read)
        try:
            assert entered.wait(2)
            workers.submit(sidecar.close).result(timeout=1)
            terminate.assert_called_once_with(process)
            assert sidecar._proc is None
        finally:
            release.set()
        reading.result(timeout=3)


def test_hot_graph_prepare_keeps_idle_time_and_does_not_take_a_cuda_work_lease(backend, monkeypatch, caplog):
    from asr.backends import qwen3_asr as module
    import logging
    monkeypatch.setattr(module, "qwen_cuda_graph_support_reason", lambda _: None)
    monkeypatch.setattr(module, "prepare_qwen_cuda_graphs", lambda *_, **__: {
        "buckets": [{"cache_hit": True}], "prepared_capacities": [384]})
    graph_user = Qwen3ASRBackend(inference_engine="cuda_graph")
    graph_user._load_inprocess("cuda")
    used = Qwen3ASRBackend._last_used
    caplog.clear()
    with monkeypatch.context() as hot, caplog.at_level(logging.INFO):
        hot.setattr(module.speech_cuda, "work", Mock(side_effect=AssertionError("hot CUDA work lease")))
        graph_user.prepare()
        graph_user.prepare()
    assert Qwen3ASRBackend._last_used == used
    assert not any("graph preparation" in record.message for record in caplog.records)
    assert backend.release_vram(180)


def test_default_graph_borrower_preserves_parked_residency_until_speech_prepare(backend, monkeypatch):
    from asr.backends import qwen3_asr as module
    monkeypatch.setattr(module, "QWEN3_ASR_CUDA_GRAPH", True)
    monkeypatch.setattr(module, "qwen_cuda_graph_support_reason", lambda _: None)
    prepare = Mock(return_value={"buckets": [{"cache_hit": True}]})
    monkeypatch.setattr(module, "prepare_qwen_cuda_graphs", prepare)
    assert backend.release_vram()
    used = Qwen3ASRBackend._last_used
    borrower = Qwen3ASRBackend()
    borrower._load_inprocess("cuda")
    assert borrower.parked and borrower._model is backend._model
    assert borrower._model.model.moves == ["cpu"]
    assert Qwen3ASRBackend._last_used == used
    prepare.assert_not_called()
    borrower.prepare()
    assert not borrower.parked and borrower._model.model.moves == ["cpu", "cuda:0"]
    assert Qwen3ASRBackend._last_used > used
    prepare.assert_called_once()


def test_unknown_cuda_prepare_failure_keeps_the_existing_fatal_drop_contract(backend, monkeypatch):
    from asr.backends import qwen3_asr as module
    monkeypatch.setattr(module, "prepare_qwen_cuda_graphs", Mock(side_effect=RuntimeError("CUDA error: unknown capture failure")))
    other = Qwen3ASRBackend(inference_engine="official")
    other._load_inprocess("cuda")
    with pytest.raises(ASRBackendFatalError, match="unknown capture failure"):
        backend.prepare_cuda_graphs(capture_allowed=True)
    assert backend._model is None and other._model is None
    assert Qwen3ASRBackend._shared_model is None
    assert Qwen3ASRBackend._inprocess_disabled_until > time.time()


@pytest.mark.parametrize("message", ["operation failed due to a previous error during capture", ""])
def test_capture_end_wrapped_fatal_error_drops_all_shared_users(backend, monkeypatch, message):
    from asr import qwen_cuda_graph as graph
    from asr.backends import qwen3_asr as module
    runtime = sys.modules["torch"]
    monkeypatch.setattr(runtime, "OutOfMemoryError", type("OutOfMemoryError", (RuntimeError,), {}), raising=False)
    for name in ("synchronize", "memory_allocated", "memory_reserved"):
        monkeypatch.setattr(runtime.cuda, name, Mock(return_value=0), raising=False)
    other = Qwen3ASRBackend(inference_engine="official")
    other._load_inprocess("cuda")
    weights = backend._model.model
    weights.device = SimpleNamespace(type="cuda")
    weights.thinker = SimpleNamespace(model=SimpleNamespace(config=SimpleNamespace(_attn_implementation="sdpa")))
    error = RuntimeError(message)
    error.__cause__ = RuntimeError("illegal memory access")
    partial = SimpleNamespace(statistics={"capacity": 384}, capture=Mock(side_effect=error), close=Mock())
    monkeypatch.setattr(graph, "QwenStaticDecode", Mock(return_value=partial))
    # Normal inference still applies its pre-existing top-level string policy.
    assert not module.Qwen3ASRBackend._is_fatal_cuda_error(error)
    with pytest.raises(ASRBackendFatalError, match=message or "RuntimeError"):
        backend.prepare_cuda_graphs(capture_allowed=True)
    partial.close.assert_called_once_with()
    assert backend._model is None and other._model is None
    assert Qwen3ASRBackend._shared_model is None and not Qwen3ASRBackend._model_users
    assert Qwen3ASRBackend._inprocess_disabled_until > time.time()
