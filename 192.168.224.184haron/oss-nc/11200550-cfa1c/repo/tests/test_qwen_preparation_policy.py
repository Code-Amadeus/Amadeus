"""CPU-tier contracts for optional Qwen graph preparation and loader policy."""
from contextlib import contextmanager, nullcontext
import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from asr import qwen_cuda_graph as graph_policy
from asr.qwen_inference import qwen_loader_attention


class OutOfMemoryError(RuntimeError):
    pass


@pytest.fixture
def cuda_runtime(monkeypatch):
    runtime = ModuleType("torch")
    runtime.OutOfMemoryError = OutOfMemoryError
    runtime.version = SimpleNamespace(hip=None, cuda="synthetic")
    runtime.bfloat16, runtime.float32 = "bf16", "fp32"
    runtime.inference_mode = lambda *_: nullcontext()
    runtime.no_grad = nullcontext
    runtime.cuda = SimpleNamespace(
        is_available=Mock(return_value=True), get_device_capability=Mock(return_value=(8, 0)),
        synchronize=Mock(), memory_allocated=Mock(return_value=0),
        memory_reserved=Mock(return_value=0), empty_cache=Mock(),
    )
    monkeypatch.setitem(sys.modules, "torch", runtime)
    return runtime


@pytest.fixture
def cuda_model():
    return SimpleNamespace(device=SimpleNamespace(type="cuda"), thinker=SimpleNamespace(
        model=SimpleNamespace(config=SimpleNamespace(_attn_implementation="sdpa"))))


def state(capacity):
    return SimpleNamespace(statistics={"capacity": capacity}, capture=Mock(), close=Mock())


@pytest.mark.parametrize("device,major,hip,enabled,installed,expected", [
    ("cpu", 8, None, True, True, "eager"),
    ("cuda:0", 8, "synthetic-rocm", True, True, "eager"),
    ("cuda:0", 7, None, True, True, "sdpa"),
    ("cuda:0", 8, None, True, True, "flash_attention_2"),
    ("cuda:0", 9, None, True, True, "flash_attention_2"),
    ("cuda:0", 8, None, True, False, "sdpa"),
    ("cuda:0", 8, None, False, True, "sdpa"),
])
def test_fa2_requires_enabled_nvidia_ampere_and_an_importable_package(
    monkeypatch, cuda_runtime, device, major, hip, enabled, installed, expected,
):
    cuda_runtime.version.hip = hip
    cuda_runtime.cuda.get_device_capability.return_value = (major, 0)
    monkeypatch.setitem(sys.modules, "flash_attn", ModuleType("flash_attn") if installed else None)
    assert qwen_loader_attention(device, flash_attn_enabled=enabled) == expected
    if device.startswith("cuda") and not hip and enabled:
        cuda_runtime.cuda.get_device_capability.assert_called_once_with(device)
    else:
        cuda_runtime.cuda.get_device_capability.assert_not_called()


@pytest.mark.parametrize("graph_enabled", [False, True])
def test_default_engine_uses_parsed_config_and_explicit_engine_overrides_it(monkeypatch, graph_enabled):
    from asr.backends import qwen3_asr as backend_module
    monkeypatch.setattr(backend_module, "QWEN3_ASR_CUDA_GRAPH", graph_enabled)
    monkeypatch.setattr(backend_module, "QWEN3_ASR_FLASH_ATTN", False)
    monkeypatch.setenv("QWEN3_ASR_CUDA_GRAPH", str(not graph_enabled))
    backend = backend_module.Qwen3ASRBackend()
    assert backend._inference_engine == ("cuda_graph" if graph_enabled else "official")
    assert not backend._flash_attn_enabled
    explicit = backend_module.Qwen3ASRBackend(inference_engine="greedy")
    assert explicit._inference_engine == "greedy"


@pytest.mark.parametrize("stage,error_factory,reason", [
    ("allocation", lambda: OutOfMemoryError("allocation too large"), "out_of_memory"),
    ("capture", lambda: OutOfMemoryError("capture allocation too large"), "out_of_memory"),
    ("capture", lambda: RuntimeError("CUDA error: operation not permitted when stream is capturing"), "capture_unavailable"),
    ("capture", lambda: RuntimeError("CUDA error: operation failed due to a previous error during capture"), "capture_unavailable"),
    ("capture", lambda: RuntimeError("capture unavailable in this runtime"), "capture_failed"),
    ("capture", RuntimeError, "capture_failed"),
    ("capture", lambda: RuntimeError("CUDA error: unknown device failure"), None),
    ("capture", lambda: RuntimeError("illegal memory access"), None),
    ("capture", lambda: RuntimeError("device-side assert triggered"), None),
    ("capture", lambda: RuntimeError("CUBLAS_STATUS_EXECUTION_FAILED"), None),
    ("allocation", lambda: RuntimeError("constructor contract is broken"), None),
    ("capture", lambda: ValueError("model configuration is broken"), None),
])
def test_preparation_releases_partial_graphs_and_memoizes_only_recoverable_failures(
    monkeypatch, cuda_runtime, cuda_model, stage, error_factory, reason,
):
    previous, partial = state(256), state(384)
    cuda_model._amadeus_qwen_cuda_graphs = {256: previous}
    error = error_factory()
    factory = Mock(side_effect=error) if stage == "allocation" else Mock(return_value=partial)
    if stage == "capture":
        partial.capture.side_effect = error
    monkeypatch.setattr(graph_policy, "QwenStaticDecode", factory)
    if reason is None:
        with pytest.raises(type(error)) as caught:
            graph_policy.prepare_qwen_cuda_graphs(cuda_model, (256, 384), capture_allowed=True)
        assert caught.value is error
        assert not hasattr(cuda_model, "_amadeus_qwen_cuda_graph_failure")
        cuda_runtime.cuda.empty_cache.assert_not_called()
    else:
        evidence = graph_policy.prepare_qwen_cuda_graphs(cuda_model, (256, 384), capture_allowed=True)
        assert evidence["failed"] and evidence["reason"] == reason and evidence["stage"] == stage
        assert evidence["error"] == (str(error) or type(error).__name__)
        cuda_runtime.cuda.empty_cache.assert_called_once_with()
        chosen, fallback = graph_policy.select_qwen_cuda_graph(cuda_model, 200)
        assert chosen is None and fallback == "graph_preparation_failed:" + reason
        with monkeypatch.context() as hot:
            hot.setattr(graph_policy.speech_cuda, "try_capture", Mock(side_effect=AssertionError("failed retry")))
            repeated = graph_policy.prepare_qwen_cuda_graphs(cuda_model)
            assert repeated["failed"] and repeated["cache_hit"]
        factory.assert_called_once()
    previous.close.assert_called_once_with()
    if stage == "capture":
        partial.close.assert_called_once_with()
    assert not hasattr(cuda_model, "_amadeus_qwen_cuda_graphs")


def test_constructor_oom_releases_buffers_before_the_factory_can_return(monkeypatch, cuda_runtime):
    partial = []
    def initialize(owner, *_):
        owner.cache, owner.token = object(), object()
        partial.append(owner)
        raise OutOfMemoryError("static cache allocation")
    monkeypatch.setattr(graph_policy.QwenStaticDecode, "_initialize", initialize)
    with pytest.raises(OutOfMemoryError):
        graph_policy.QwenStaticDecode(object(), 384)
    assert partial[0].closed and partial[0].cache is None and partial[0].token is None
    assert partial[0].thinker is None and partial[0].graph is None


def test_lifecycle_invalidation_allows_a_new_preparation_after_failure(monkeypatch, cuda_runtime, cuda_model):
    factory = Mock(side_effect=OutOfMemoryError("first residency"))
    monkeypatch.setattr(graph_policy, "QwenStaticDecode", factory)
    assert graph_policy.prepare_qwen_cuda_graphs(cuda_model, capture_allowed=True)["failed"]
    graph_policy.invalidate_qwen_cuda_graphs(cuda_model)
    ready = state(384)
    factory.side_effect, factory.return_value = None, ready
    result = graph_policy.prepare_qwen_cuda_graphs(cuda_model, capture_allowed=True)
    assert result["prepared_capacities"] == [384]
    assert graph_policy.select_qwen_cuda_graph(cuda_model, 200) == (ready, None)
    assert not hasattr(cuda_model, "_amadeus_qwen_cuda_graph_failure")


def test_competing_work_defers_without_failure_and_later_prepares(monkeypatch, cuda_runtime, cuda_model):
    admissions = iter((False, True))
    @contextmanager
    def admission():
        yield next(admissions)
    monkeypatch.setattr(graph_policy.speech_cuda, "try_capture", admission)
    ready = state(384)
    factory = Mock(return_value=ready)
    monkeypatch.setattr(graph_policy, "QwenStaticDecode", factory)
    deferred = graph_policy.prepare_qwen_cuda_graphs(cuda_model, capture_allowed=True)
    assert deferred["deferred"] and "failed" not in deferred
    factory.assert_not_called()
    cuda_runtime.cuda.synchronize.assert_not_called()
    result = graph_policy.prepare_qwen_cuda_graphs(cuda_model, capture_allowed=True)
    assert result["prepared_capacities"] == [384]
    assert not hasattr(cuda_model, "_amadeus_qwen_cuda_graph_failure")


def test_known_capture_error_does_not_hide_a_fatal_exception_chain(monkeypatch, cuda_runtime, cuda_model):
    error = RuntimeError("CUDA error: operation failed due to a previous error during capture")
    error.__cause__ = RuntimeError("illegal memory access")
    partial = state(384)
    partial.capture.side_effect = error
    monkeypatch.setattr(graph_policy, "QwenStaticDecode", Mock(return_value=partial))
    with pytest.raises(RuntimeError) as caught:
        graph_policy.prepare_qwen_cuda_graphs(cuda_model, capture_allowed=True)
    assert caught.value is error and error.__cause__ is not None
    assert not hasattr(cuda_model, "_amadeus_qwen_cuda_graph_failure")


def test_cleanup_cuda_error_propagates_after_recoverable_capture(monkeypatch, cuda_runtime, cuda_model):
    partial = state(384)
    partial.capture.side_effect = OutOfMemoryError("capture allocation")
    monkeypatch.setattr(graph_policy, "QwenStaticDecode", Mock(return_value=partial))
    cuda_runtime.cuda.empty_cache.side_effect = RuntimeError("CUDA error: unknown cleanup failure")
    with pytest.raises(RuntimeError, match="unknown cleanup failure"):
        graph_policy.prepare_qwen_cuda_graphs(cuda_model, capture_allowed=True)
    partial.close.assert_called_once_with()


def test_unsupported_configuration_is_not_memoized_as_a_capture_failure(monkeypatch, cuda_runtime, cuda_model):
    cuda_model.thinker.model.config._attn_implementation = "eager"
    factory = Mock()
    monkeypatch.setattr(graph_policy, "QwenStaticDecode", factory)
    with pytest.raises(ValueError, match="SDPA text"):
        graph_policy.prepare_qwen_cuda_graphs(cuda_model, capture_allowed=True)
    factory.assert_not_called()
    assert not hasattr(cuda_model, "_amadeus_qwen_cuda_graph_failure")


@pytest.mark.parametrize("device,major,hip,disabled,expected", [
    ("cpu", 8, None, False, "eager"),
    ("cuda", 7, None, False, "sdpa"),
    ("cuda", 8, None, False, "flash_attention_2"),
    ("cuda", 8, None, True, "sdpa"),
    ("cuda", 8, "synthetic-rocm", False, "eager"),
])
def test_sidecar_uses_the_same_hardware_and_parsed_flash_policy(
    monkeypatch, cuda_runtime, device, major, hip, disabled, expected,
):
    from asr import qwen_inference, qwen_model
    cuda_runtime.version.hip = hip
    cuda_runtime.cuda.get_device_capability.return_value = (major, 0)
    monkeypatch.setitem(sys.modules, "flash_attn", ModuleType("flash_attn"))
    loaded = SimpleNamespace(model=SimpleNamespace(set_attn_implementation=Mock()))
    factory = Mock(return_value=loaded)
    monkeypatch.setitem(sys.modules, "qwen_asr", SimpleNamespace(Qwen3ASRModel=SimpleNamespace(from_pretrained=factory)))
    monkeypatch.setattr(qwen_model, "resolve_qwen_model_source", lambda: "synthetic-model")
    monkeypatch.setattr(qwen_inference, "apply_qwen_attention_policy", lambda *_: None)
    monkeypatch.setattr(qwen_inference, "qwen_attention_configs", lambda *_: {})
    monkeypatch.setenv("QWEN3_ASR_DEVICE", device)
    monkeypatch.setenv("QWEN3_ASR_REQUIRE_CUDA", "false")
    monkeypatch.setenv("QWEN3_ASR_WARMUP", "0")
    monkeypatch.setattr(sys, "stdin", iter(()))
    sidecar = runpy.run_path(str(Path(__file__).parents[1] / "asr/qwen3_asr_sidecar.py"))
    emitted = []
    monkeypatch.setitem(sidecar["main"].__globals__, "_emit", emitted.append)
    sidecar["main"](["--disable-flash-attn"] if disabled else [])
    assert factory.call_args.kwargs.get("attn_implementation", "eager") == expected
    assert emitted[0]["type"] == "ready" and emitted[0]["loader_attention_requested"] == expected
    if hip:
        loaded.model.set_attn_implementation.assert_called_once_with("eager")
