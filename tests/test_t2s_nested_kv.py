"""Storage ownership, request lifetime and bucket-shape contracts for T2S KV."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import sys
import threading

import pytest

torch = pytest.importorskip("torch", reason="local-model tier requires torch")
pytest.importorskip("torchmetrics", reason="local-model tier requires torchmetrics")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "GPT_SoVITS"))
import AR.models.t2s_model as module
from core.cuda_execution import speech_cuda


def decoder(layout="nested", hidden=32):
    config = {"model": {"hidden_dim": hidden, "embedding_dim": hidden, "head": 2,
                        "n_layer": 2, "vocab_size": 64, "phoneme_vocab_size": 64,
                        "dropout": 0.0, "EOS": 63}}
    torch.manual_seed(29)
    result = module.Text2SemanticDecoder(config).eval()
    result.kv_cache_layout = layout
    result.kv_cache_buckets = [48, 96, 128]
    result._BUCKET_MIN_GEN_SLOTS = 0
    return result


@pytest.mark.parametrize("layout,owners", [("graph", 3), ("bucket", 2), ("nested", 1)])
def test_layout_shares_storage_without_enlarging_attention_shape(layout, owners):
    model = decoder(layout)
    keys = [(48, 16), (48, 32), (96, 64)]
    views = [model._graph_kv_views(key, "cpu", torch.float16) for key in keys]
    assert len(model._graph_kv_roots) == owners
    for key, (k, v) in zip(keys, views):
        assert len(k) == len(v) == 2
        assert all(item.shape == (1, key[0], 32) for item in k + v)
        # FA2 can split hidden into heads without a contiguous copy.
        assert all(item.view(1, key[0], 2, 16).data_ptr() == item.data_ptr() for item in k + v)
    pointers = [k[0].untyped_storage().data_ptr() for k, _ in views]
    assert len(set(pointers)) == owners
    if layout == "nested":
        views[0][0][0].fill_(17)
        assert views[2][0][0][:, :48].eq(17).all()
        assert model._graph_kv_roots[None][0].shape == (2, 1, 128, 32)
    locks = [model._get_bucket_lock(key) for key in keys]
    assert len({id(lock) for lock in locks}) == owners


def test_captured_storage_cannot_be_replaced_on_dtype_or_capacity_change():
    model = decoder()
    model._graph_kv_views((48, 32), "cpu", torch.float16)
    with pytest.raises(RuntimeError, match="new decoder"):
        model._graph_kv_views((48, 32), "cpu", torch.float32)
    with pytest.raises(RuntimeError, match="new decoder"):
        model._graph_kv_views((256, 192), "cpu", torch.float16)


def test_invalid_layout_is_observable(monkeypatch):
    monkeypatch.setenv("TTS_KV_CACHE_LAYOUT", "typo")
    with pytest.raises(ValueError, match="TTS_KV_CACHE_LAYOUT"):
        decoder()


def test_entire_request_is_exclusive_and_exception_releases_it(monkeypatch):
    model = decoder()
    model.use_static_kv_cache = model.cuda_graph_enabled = True
    entered, attempted, release, second = [threading.Event() for _ in range(4)]
    # The real wrapper selects the graph lease. A CPU fake avoids needing CUDA
    # to test the concurrency boundary through prefill, sampling and return.
    x = SimpleNamespace(shape=(1, 17), is_cuda=True, device="cpu")
    prompts = SimpleNamespace(shape=(1, 1))

    def infer(*args, **kwargs):
        if kwargs.get("fail"):
            entered.set()
            assert release.wait(3)
            raise RuntimeError("synthetic request failure")
        second.set()
        return "second request"

    monkeypatch.setattr(model, "_infer_panel_naive", infer)

    def next_request():
        attempted.set()
        return model.infer_panel_naive(x, None, prompts, None)

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(model.infer_panel_naive, x, None, prompts, None, fail=True)
        assert entered.wait(2)
        future = pool.submit(next_request)
        assert attempted.wait(2)
        try:
            assert not second.wait(.05)
            # Ordinary ASR work is admitted during the TTS request lease.
            with speech_cuda.work():
                assert not second.is_set()
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="synthetic request"):
            first.result(timeout=3)
        assert future.result(timeout=3) == "second request"


@pytest.mark.parametrize("layout,keys", [
    ("graph", [(48, 16), (48, 32)]),
    ("bucket", [(48, 32), (96, 64)]),
])
def test_independent_storage_owners_can_progress_concurrently(layout, keys):
    model = decoder(layout)
    barrier = threading.Barrier(2, timeout=2)

    def work(key):
        with model._use_graph_workspace(key, "cpu"):
            barrier.wait()

    with ThreadPoolExecutor(2) as pool:
        for future in [pool.submit(work, key) for key in keys]:
            future.result(timeout=3)


@pytest.mark.skipif(not torch.cuda.is_available(), reason="requires CUDA Graph execution")
@pytest.mark.parametrize("attention", ["sdpa", "flash"])
def test_cuda_views_match_independent_graphs_across_requests_and_overflow(monkeypatch, attention):
    from AR.models.t2s_flash_attn import apply_flash_attn_patch, is_flash_attn_available
    if attention == "flash" and not is_flash_attn_available():
        pytest.skip("FlashAttention extension unavailable")
    monkeypatch.setattr(module, "tqdm", lambda value: value)
    monkeypatch.setattr(module, "_should_stop_on_eos", lambda *_: False)
    device = torch.device("cuda:0")
    with torch.cuda.device(device), torch.inference_mode():
        # Match the production head dimension (32) and aligned CUDA buckets.
        # Tiny head_dim=16/48-slot FA2 graphs are not numerically repeatable
        # even between two untouched original decoders on this runtime.
        models = [decoder(layout, hidden=64).half().to(device) for layout in ("graph", "nested")]
        for name, value in models[0].state_dict().items():
            torch.testing.assert_close(value, models[1].state_dict()[name], rtol=0, atol=0)
        for model in models:
            model.kv_cache_buckets = [128, 256, 512]
            model.use_static_kv_cache = model.cuda_graph_enabled = True
            if attention == "flash":
                assert apply_flash_attn_patch(model)
                for block in model.t2s_transformer_static.blocks:
                    def strict_decode(x, k, v, pos, mask, valid=-1, sdpa=True, block=block):
                        return block._finish_attention(x, block._flash_valid_len(x, k, v, pos)), k, v
                    block.decode_next_token_with_static_cache = strict_decode
        # Aligned/unaligned prompts, different buckets, return to the short
        # bucket, full-context dynamic overflow, reference-free and early exit.
        for text_len, prompt_len, steps in [(8, 8, 16), (65, 78, 16), (8, 9, 16),
                                            (8, 8, 140), (8, 0, 16), (8, 8, 2)]:
            x = torch.arange(1, text_len + 1, device=device).remainder(62).unsqueeze(0)
            prompts = torch.arange(prompt_len, device=device).remainder(62).unsqueeze(0) if prompt_len else None
            bert = torch.zeros(1, 1024, text_len, dtype=torch.float16, device=device)
            outputs, traces = [], []
            for model in models:
                trace = []

                def sample(logits, history, **_kwargs):
                    # Teacher-force identical next inputs and compare every
                    # decoder logit. The real-checkpoint probes separately
                    # verify actual sampling and full waveform equality.
                    trace.append(logits.clone())
                    return torch.full((1, 1), (history.shape[1] + 3) % 62,
                                      device=logits.device, dtype=torch.int32), None

                monkeypatch.setattr(module, "sample", sample)
                result = model.infer_panel_naive(x, None, prompts, bert, top_k=1,
                                                early_stop_num=steps - 1)
                torch.cuda.synchronize(device)
                outputs.append(result)
                traces.append(trace)
            assert outputs[0][1] == outputs[1][1]
            torch.testing.assert_close(outputs[0][0], outputs[1][0], rtol=0, atol=0)
            assert len(traces[0]) == len(traces[1]) == steps
            for expected, actual in zip(traces[0], traces[1]):
                torch.testing.assert_close(expected, actual, rtol=2e-3, atol=2e-3)
        nested = models[1]
        pointers = {item.untyped_storage().data_ptr() for inputs in nested.bucket_static_inputs.values()
                    for item in inputs['k_cache']}
        assert len(pointers) == 1
        assert len(nested.bucket_graphs) >= 2


@pytest.mark.skipif(not torch.cuda.is_available(), reason="requires CUDA stream execution")
def test_workspace_handoff_orders_pending_gpu_writes():
    device = torch.device("cuda:0")
    with torch.cuda.device(device), torch.inference_mode():
        model = decoder()
        k, _ = model._graph_kv_views((48, 32), device, torch.float16)
        first, second = [torch.cuda.Stream(device=device) for _ in range(2)]
        first.wait_stream(torch.cuda.current_stream(device))
        second.wait_stream(torch.cuda.current_stream(device))
        with torch.cuda.stream(first), model._use_graph_workspace((48, 32), device):
            torch.cuda._sleep(5_000_000)
            k[0].fill_(17)
        # No host synchronization between requests. The second lease's wait
        # must order this mutation after the first stream's delayed write.
        with torch.cuda.stream(second), model._use_graph_workspace((96, 64), device):
            k[0].mul_(2)
        second.synchronize()
        assert k[0].eq(34).all()


@pytest.mark.skipif(not torch.cuda.is_available(), reason="requires CUDA execution")
def test_batch_two_uses_private_kv_without_touching_graph_workspace(monkeypatch):
    monkeypatch.setattr(module, "tqdm", lambda value: value)
    monkeypatch.setattr(module, "_should_stop_on_eos", lambda *_: False)
    device = torch.device("cuda:0")
    with torch.cuda.device(device), torch.inference_mode():
        model = decoder().half().to(device)
        model.use_static_kv_cache = model.cuda_graph_enabled = True
        x = torch.tensor([[1, 2], [3, 4]], device=device)
        prompts = torch.tensor([[5, 6], [7, 8]], device=device)
        output, index = model.infer_panel_naive(x, None, prompts,
                                              torch.zeros(2, 1024, 2, dtype=torch.float16, device=device),
                                              top_k=1, early_stop_num=15)
        assert output.shape == (2, 17) and index == 15
        assert not model.bucket_graphs and not model._graph_kv_roots


@pytest.mark.skipif(not torch.cuda.is_available(), reason="requires CUDA request execution")
@pytest.mark.parametrize("attention", ["sdpa", "flash"])
@pytest.mark.parametrize("different_key", [False, True])
def test_concurrent_requests_match_serial_logits_on_shared_workspace(monkeypatch, attention, different_key):
    from AR.models.t2s_flash_attn import apply_flash_attn_patch, is_flash_attn_available
    if attention == "flash" and not is_flash_attn_available():
        pytest.skip("FlashAttention extension unavailable")
    monkeypatch.setattr(module, "tqdm", lambda values: values)
    monkeypatch.setattr(module, "_should_stop_on_eos", lambda *_: False)
    local = threading.local()

    def sample(logits, history, **_kwargs):
        local.trace.append(logits.clone())
        return torch.full((1, 1), (history.shape[1] + 3) % 62,
                          device=logits.device, dtype=torch.int32), None

    monkeypatch.setattr(module, "sample", sample)
    device = torch.device("cuda:0")
    with torch.cuda.device(device), torch.inference_mode():
        model = decoder(hidden=64).half().to(device)
        model.kv_cache_buckets = [128, 256, 512]
        model.use_static_kv_cache = model.cuda_graph_enabled = True
        if attention == "flash":
            assert apply_flash_attn_patch(model)
        shapes = [(8, 8), (65, 78) if different_key else (8, 9)]
        requests = []
        for text_len, prompt_len in shapes:
            x = torch.arange(1, text_len + 1, device=device).remainder(62).unsqueeze(0)
            prompt = torch.arange(prompt_len, device=device).remainder(62).unsqueeze(0)
            bert = torch.zeros(1, 1024, text_len, device=device, dtype=torch.float16)
            requests.append((x, prompt, bert))
        torch.cuda.synchronize(device)

    def run(request, gate=None):
        with torch.cuda.device(device), torch.inference_mode():
            stream = torch.cuda.Stream(device=device)
            local.trace = []
            if gate is not None:
                gate.wait(timeout=3)
            with torch.cuda.stream(stream):
                x, prompt, bert = request
                tokens, index = model.infer_panel_naive(x, None, prompt, bert, top_k=1, early_stop_num=15)
            stream.synchronize()
            return tokens.cpu(), index, [value.cpu() for value in local.trace]

    # Capture both keys and establish per-request numerical references before
    # threads contend for one root. Each contender uses its own CUDA stream.
    expected = [run(request) for request in requests]
    keys = set(model.bucket_graphs)
    gate = threading.Barrier(2)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run, request, gate) for request in requests]
        actual = [future.result(timeout=10) for future in futures]
    assert set(model.bucket_graphs) == keys
    for (tokens, index, trace), (want_tokens, want_index, want_trace) in zip(actual, expected):
        assert index == want_index and len(trace) == len(want_trace) == 16
        torch.testing.assert_close(tokens, want_tokens, rtol=0, atol=0)
        for value, want in zip(trace, want_trace):
            torch.testing.assert_close(value, want, rtol=2e-3, atol=2e-3)
