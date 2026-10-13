"""CPU contracts for static Qwen continuation and graph lifecycle ownership."""
import threading
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
torch = pytest.importorskip("torch")
pytest.importorskip("qwen_asr")

from test_qwen_inference import model as model, SyntheticProcessor, generate
from qwen_asr.inference.qwen3_asr import Qwen3ASRModel
from asr.qwen_cuda_graph import DEFAULT_CAPACITIES, QwenStaticDecode, invalidate_qwen_cuda_graphs, prepare_qwen_cuda_graphs
from asr.qwen_inference import _greedy_decode, qwen_attention_configs, set_qwen_attention_configs, transcribe_qwen
from core.cuda_execution import speech_cuda


@pytest.mark.parametrize("attention", ["eager", "sdpa"])
def test_static_kv_matches_dynamic_continuation_padding_rope_and_request_reuse(model, attention):
    set_qwen_attention_configs(model, dict.fromkeys(qwen_attention_configs(model), attention))
    with torch.inference_mode():
        state = QwenStaticDecode(model.thinker, 32)
        addresses = [layer.keys.data_ptr() for layer in state.cache.layers]
        for length in (3, 12, 4):
            ids = torch.tensor([[0] + list(range(3, length + 3))])
            mask = (ids != 0).long()
            prefill = model.thinker(input_ids=ids, attention_mask=mask, use_cache=True,
                                    cache_position=torch.arange(ids.shape[1]), return_dict=True)
            dynamic = prefill.past_key_values
            state.load_prefill(dynamic, mask, model.thinker.rope_deltas)
            for index, token in enumerate((6, 9, 10)):
                position = ids.shape[1] + index
                mask = torch.cat((mask, torch.ones((1, 1), dtype=torch.long)), dim=-1)
                expected = model.thinker(input_ids=torch.tensor([[token]]), attention_mask=mask,
                                         past_key_values=dynamic, cache_position=torch.tensor([position]),
                                         use_cache=True, return_dict=True)
                actual = state.step(torch.tensor([token]), replay=False)
                torch.testing.assert_close(actual, expected.logits, rtol=1e-5, atol=1e-6)
                assert state.mask[0, 0, 0, 0] == torch.finfo(state.dtype).min
                assert torch.all(state.mask[0, 0, 0, position + 1:] == torch.finfo(state.dtype).min)
                assert state.positions[0, 0, 0] == position + model.thinker.rope_deltas[0, 0]
                assert [layer.keys.data_ptr() for layer in state.cache.layers] == addresses
        state.close()
        with pytest.raises(RuntimeError, match="released"):
            state.step(torch.tensor([3]), replay=False)


@pytest.mark.parametrize("eos", [None, 1, 2])
def test_static_custom_loop_preserves_processors_eos_budget_and_alternating_requests(model, monkeypatch, eos):
    model.thinker.generation_config.min_new_tokens = 3
    if eos is None:
        model.thinker.generation_config.suppress_tokens = [1, 2]
    else:
        model.thinker.generation_config.forced_eos_token_id = eos
    with torch.inference_mode():
        state = QwenStaticDecode(model.thinker, 32)
        original_step = state.step
        # Exercise the graph loop's static-cache semantics on CPU, while making
        # no claim about capture/replay kernels or bitwise CUDA equivalence.
        monkeypatch.setattr(state, "step", lambda token, replay: original_step(token, replay=False))
        for length in (3, 9, 4):
            ids = torch.tensor([[0] + list(range(3, length + 3))])
            expected = generate(model, ids, "official", budget=5)
            statistics = {"replay_steps": 0}
            actual = model.generate(
                input_ids=ids, attention_mask=(ids != 0).long(), max_new_tokens=5, eos_token_id=[1, 2],
                custom_generate=_greedy_decode, graph_state=state, graph_statistics=statistics).sequences
            assert torch.equal(actual, expected)
            assert statistics["replay_steps"] == 4
            if eos is not None:
                assert actual[0, -1] == eos
        state.close()


def test_graph_cpu_selection_uses_official_before_inference_and_reports_reason(model):
    wrapper = Qwen3ASRModel("transformers", model, SyntheticProcessor(), max_inference_batch_size=1)
    pcm = np.zeros(1600, dtype=np.float32)
    official, _ = transcribe_qwen(wrapper, pcm, 16000, "context", None, 4)
    actual, metrics = transcribe_qwen(wrapper, pcm, 16000, "context", None, 4, engine="cuda_graph")
    assert actual == official
    assert metrics["requested_engine"] == "cuda_graph" and metrics["engine"] == "official"
    assert metrics["graph"][0]["fallback_reason"] == "not_cuda"
    assert metrics["graph"][0]["replay_steps"] == 0


def test_attention_match_retains_graph_but_a_change_releases_it(model):
    state = SimpleNamespace(close=Mock())
    model._amadeus_qwen_cuda_graphs = {32: state}
    targets = qwen_attention_configs(model)
    set_qwen_attention_configs(model, targets)
    state.close.assert_not_called()
    assert model._amadeus_qwen_cuda_graphs[32] is state
    set_qwen_attention_configs(model, dict.fromkeys(targets, "sdpa"))
    state.close.assert_called_once_with()
    assert not hasattr(model, "_amadeus_qwen_cuda_graphs")
    invalidate_qwen_cuda_graphs(model)  # Cleanup is repeatable.


def test_capture_admission_fails_closed_or_defers_before_gpu_work():
    model = object()
    with pytest.raises(RuntimeError, match="quiet CUDA window"):
        prepare_qwen_cuda_graphs(model)
    entered, release = threading.Event(), threading.Event()

    def other_owner():
        with speech_cuda.work():
            entered.set()
            assert release.wait(3)
    worker = threading.Thread(target=other_owner)
    worker.start()
    try:
        assert entered.wait(3)
        result = prepare_qwen_cuda_graphs(model, capture_allowed=True)
        assert result["deferred"] and result["reason"] == "other_cuda_work_active"
    finally:
        release.set()
        worker.join(3)


def test_hot_preparation_is_pure_cpu_without_capture_admission(monkeypatch):
    model = SimpleNamespace(_amadeus_qwen_cuda_graphs={
        size: SimpleNamespace(statistics={"capacity": size, "capture_ms": 3.0}) for size in DEFAULT_CAPACITIES})
    monkeypatch.setattr(speech_cuda, "try_capture", Mock(side_effect=AssertionError("hot capture admission")))
    monkeypatch.setattr(torch.cuda, "synchronize", Mock(side_effect=AssertionError("hot CUDA sync")))
    evidence = prepare_qwen_cuda_graphs(model)
    assert not evidence["deferred"] and all(item["cache_hit"] for item in evidence["buckets"])


def test_graph_initialization_creates_normal_mutable_buffers_inside_inference_caller(model):
    with torch.inference_mode():
        state = QwenStaticDecode(model.thinker, 32)
        assert torch.is_inference_mode_enabled()
    tensors = [state.token, state.cache_position, state.rope_delta, state.positions, state.valid, state.mask]
    tensors.extend(layer.keys for layer in state.cache.layers)
    tensors.extend(layer.values for layer in state.cache.layers)
    assert all(not tensor.is_inference() and not tensor.requires_grad for tensor in tensors)
    with torch.no_grad():
        state.reset()  # Must remain mutable outside inference mode.
    state.close()


def test_capture_disables_outer_inference_mode_and_grad_without_cuda(monkeypatch):
    calls = []
    state = object.__new__(QwenStaticDecode)
    state.device = SimpleNamespace(type="cuda")
    state.statistics = {}
    state.closed = False
    state.reset = lambda: None
    state.buffer_bytes = lambda: 0

    def forward():
        assert not torch.is_inference_mode_enabled() and not torch.is_grad_enabled()
        calls.append("forward")
        return torch.zeros(1)
    state.forward = forward

    class Stream:
        def wait_stream(self, _other): pass
        def synchronize(self): pass
    class Graph:
        def __init__(self):
            assert not torch.is_inference_mode_enabled() and not torch.is_grad_enabled()
            calls.append("graph_state")
    @contextmanager
    def capture(_graph, **_kwargs):
        assert not torch.is_inference_mode_enabled() and not torch.is_grad_enabled()
        calls.append("capture_begin")
        yield
    monkeypatch.setattr(torch.cuda, "memory_allocated", lambda *_: 0)
    monkeypatch.setattr(torch.cuda, "memory_reserved", lambda *_: 0)
    monkeypatch.setattr(torch.cuda, "device", lambda *_: nullcontext())
    monkeypatch.setattr(torch.cuda, "Stream", lambda **_: Stream())
    monkeypatch.setattr(torch.cuda, "current_stream", lambda *_: Stream())
    monkeypatch.setattr(torch.cuda, "stream", lambda *_: nullcontext())
    monkeypatch.setattr(torch.cuda, "CUDAGraph", Graph)
    monkeypatch.setattr(torch.cuda, "graph", capture)
    with torch.inference_mode():
        state.capture()
        assert torch.is_inference_mode_enabled()
    assert calls.count("forward") == 4 and "graph_state" in calls and "capture_begin" in calls
    assert not state.logits.is_inference()


def test_default_capacity_shared_with_backend_and_explicit_larger_bucket_is_retained(monkeypatch):
    from asr.backends.qwen3_asr import Qwen3ASRBackend
    assert DEFAULT_CAPACITIES == (384,)
    backend = Qwen3ASRBackend()
    backend._mode = "inprocess"
    backend._model = SimpleNamespace(model=SimpleNamespace(_amadeus_qwen_cuda_graphs={
        384: SimpleNamespace(statistics={"capacity": 384})}))
    monkeypatch.setattr(backend, "_restore_device_locked", lambda: None)
    try:
        assert backend.prepare_cuda_graphs()["prepared_capacities"] == [384]
    finally:
        backend._model, backend._mode = None, "unloaded"
    model = SimpleNamespace(_amadeus_qwen_cuda_graphs={
        size: SimpleNamespace(statistics={"capacity": size}) for size in (256, 512)})
    evidence = prepare_qwen_cuda_graphs(model, capacities=(512,))
    assert [item["capacity"] for item in evidence["buckets"]] == [512]


def test_production_backend_default_explicitly_selects_official_on_cpu(model, monkeypatch):
    from asr.backends.qwen3_asr import Qwen3ASRBackend
    wrapper = Qwen3ASRModel("transformers", model, SyntheticProcessor(), max_inference_batch_size=1)
    pcm = np.zeros(1600, dtype=np.float32)
    expected, _ = transcribe_qwen(wrapper, pcm, 16000, "context", None, 32, engine="official")
    backend = Qwen3ASRBackend()
    backend._model, backend._mode, backend._device = wrapper, "inprocess", "cpu"
    monkeypatch.setattr(Qwen3ASRBackend, "_shared_parked", False)
    monkeypatch.setattr(Qwen3ASRBackend, "_last_used", 0.0)
    try:
        assert backend.transcribe(pcm, context="context") == expected[0].text.strip()
        assert backend.last_inference_metrics["requested_engine"] == "cuda_graph"
        assert backend.last_inference_metrics["engine"] == "official"
        assert backend.last_inference_metrics["graph"][0]["fallback_reason"] == "not_cuda"
    finally:
        # A CPU-only unit fixture does not exercise device cleanup on real CUDA.
        backend._model, backend._mode = None, "unloaded"
