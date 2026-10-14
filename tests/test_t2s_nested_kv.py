"""Storage ownership, request lifetime and bucket-shape contracts for T2S KV."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import logging
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


@pytest.mark.parametrize("layout,owners", [("bucket", 2), ("nested", 1)])
def test_layout_shares_storage_without_enlarging_attention_shape(layout, owners):
    model = decoder(layout)
    keys = [48, 48, 96]
    views = [model._graph_kv_views(key, "cpu", torch.float16) for key in keys]
    assert len(model._graph_kv_roots) == owners
    for key, (k, v) in zip(keys, views):
        assert len(k) == len(v) == 2
        assert all(item.shape == (1, key, 32) for item in k + v)
        # FA2 can split hidden into heads without a contiguous copy.
        assert all(item.view(1, key, 2, 16).data_ptr() == item.data_ptr() for item in k + v)
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
    model._graph_kv_views(48, "cpu", torch.float16)
    with pytest.raises(RuntimeError, match="new decoder"):
        model._graph_kv_views(48, "cpu", torch.float32)
    with pytest.raises(RuntimeError, match="new decoder"):
        model._graph_kv_views(256, "cpu", torch.float16)


@pytest.mark.parametrize("value", ["typo", "graph"])
def test_invalid_layout_is_observable(monkeypatch, value):
    monkeypatch.setenv("TTS_KV_CACHE_LAYOUT", value)
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


@pytest.mark.parametrize("layout,keys", [("bucket", [48, 96])])
def test_independent_storage_owners_can_progress_concurrently(layout, keys):
    model = decoder(layout)
    barrier = threading.Barrier(2, timeout=2)

    def work(key):
        with model._use_graph_workspace(key, "cpu"):
            barrier.wait()

    with ThreadPoolExecutor(2) as pool:
        for future in [pool.submit(work, key) for key in keys]:
            future.result(timeout=3)


@pytest.mark.parametrize("layout", ["bucket", "nested"])
def test_capture_disables_grad_and_keeps_normal_workspace_tensors(monkeypatch, layout):
    model = decoder(layout)
    observed_modes = []

    def decode(x, k, v, *_args):
        observed_modes.append((torch.is_grad_enabled(), torch.is_inference_mode_enabled()))
        return x, k, v

    monkeypatch.setattr(model, "t2s_transformer_static", SimpleNamespace(
        decode_next_token_with_static_cache=decode))
    monkeypatch.setattr(torch.cuda, "device", lambda _device: nullcontext())
    monkeypatch.setattr(torch.cuda, "CUDAGraph", lambda: object())
    monkeypatch.setattr(torch.cuda, "graph", lambda *_args, **_kwargs: nullcontext())
    # Product initialization calls capture with grad enabled and trainable
    # parameters. Inference tensors would reject a later no_grad prompt write.
    with torch.enable_grad():
        assert all(parameter.requires_grad for parameter in model.parameters())
        assert model._capture_bucket(48, "cpu")
        assert torch.is_grad_enabled()
    assert observed_modes == [(False, False)] * (model.cuda_graph_warmup_steps + 1)
    for roots in model._graph_kv_roots.values():
        for root in roots:
            assert not root.requires_grad and root.grad_fn is None
            assert not torch.is_inference(root)
    with torch.no_grad():
        for view in model.bucket_static_inputs[48]['k_cache']:
            view[:, :16].fill_(1)


@pytest.mark.parametrize("layout", ["bucket", "nested"])
def test_precapture_prepares_one_graph_per_bucket_without_length_ranges(monkeypatch, layout):
    model = decoder(layout)
    model.cuda_graph_enabled = True
    calls = []

    def capture(bucket, device):
        calls.append(bucket)
        model.bucket_graphs[bucket] = object()
        return True

    monkeypatch.setattr(model, "_warmup_and_capture_bucket", capture)
    assert model.precapture_cuda_graph([48, 48, 96]) == {48: True, 96: True}
    assert model.precapture_cuda_graph() == {48: True, 96: True, 128: True}
    assert calls == [48, 96, 128]
    assert model.precapture_cuda_graph([999]) == {999: False}


@pytest.mark.parametrize("layout", ["bucket", "nested"])
@pytest.mark.parametrize("poison", [float("nan"), float("inf")], ids=["nan", "inf"])
def test_promotion_preserves_prefix_and_clears_newly_exposed_prepared_view(monkeypatch, layout, poison):
    model = decoder(layout)
    old_k, old_v = model._graph_kv_views(48, "cpu", torch.float32)
    new_k, new_v = model._graph_kv_views(96, "cpu", torch.float32)
    for roots in model._graph_kv_roots.values():
        for root in roots:
            root.fill_(7)
    for tensor in old_k + old_v:
        tensor.fill_(17)
    for tensor in new_k + new_v:
        tensor[:, 48:].fill_(poison)
    roots_before = [[root.clone() for root in roots] for roots in model._graph_kv_roots.values()]
    mask = torch.ones(1, 96, dtype=torch.bool)
    position = torch.full((1, 1, 32), -1, dtype=torch.long)
    model.bucket_graphs = {48: object(), 96: object()}
    model.bucket_static_inputs[96] = {'k_cache': new_k, 'v_cache': new_v,
                                      'key_valid_mask': mask, 'pos_idx': position}
    monkeypatch.setattr(model, "_capture_bucket", lambda *_: pytest.fail("promotion captured live KV"))
    promoted = model._promote_graph_workspace(48, 48)
    assert promoted == (96 if layout == "nested" else None)
    if layout == "nested":
        assert mask[:, :48].all() and not mask[:, 48:].any()
        assert position.eq(48).all()
        assert all(old.data_ptr() == new.data_ptr() for old, new in zip(old_k, new_k))
        assert all(tensor[:, :48].eq(17).all() and tensor[:, 48:].eq(0).all()
                   for tensor in new_k + new_v)
        for expected in roots_before[0]:
            expected[:, :, 48:96, :] = 0
    else:
        assert mask.all() and position.eq(-1).all()
    for before, roots in zip(roots_before, model._graph_kv_roots.values()):
        for expected, actual in zip(before, roots):
            torch.testing.assert_close(expected, actual, rtol=0, atol=0, equal_nan=True)
    assert model._promote_graph_workspace(96, 96) is None


@pytest.mark.skipif(not torch.cuda.is_available(), reason="requires CUDA precapture and replay")
@pytest.mark.parametrize("layout", ["bucket", "nested"])
@pytest.mark.parametrize("dtype", [torch.float16, torch.float32])
def test_sdpa_startup_precapture_reuses_graphs_across_request_modes(monkeypatch, layout, dtype):
    monkeypatch.setattr(module, "tqdm", lambda values: values)
    monkeypatch.setattr(module, "_should_stop_on_eos", lambda *_: False)
    trace = []

    def sample(logits, history, **_kwargs):
        trace.append(logits.clone())
        return torch.full((1, 1), (history.shape[1] + 3) % 62,
                          device=logits.device, dtype=torch.int32), None

    monkeypatch.setattr(module, "sample", sample)
    device = torch.device("cuda:0")
    with torch.cuda.device(device), torch.enable_grad():
        model = decoder(layout, hidden=64).to(device=device, dtype=dtype)
        model.kv_cache_buckets = [128, 256, 448, 512, 768, 1024]
        model._BUCKET_MIN_GEN_SLOTS = 96
        model.use_static_kv_cache = model.cuda_graph_enabled = True
        assert not model.use_flash_attn_kvcache
        assert all(parameter.requires_grad for parameter in model.parameters())
        assert all(model.precapture_cuda_graph(model.kv_cache_buckets).values())
        assert torch.is_grad_enabled()
        keys = set(model.bucket_graphs)
        assert keys == set(model.kv_cache_buckets)
        for roots in model._graph_kv_roots.values():
            assert all(not torch.is_inference(root) and not root.requires_grad
                       and root.grad_fn is None for root in roots)
        # Exercise every precaptured computation bucket using ordinary tensors.
        for text_len, prompt_len in [(32, 256), (32, 303), (128, 304), (400, 304),
                                     (128, 480), (128, 672)]:
            x = torch.arange(1, text_len + 1, device=device).remainder(62).unsqueeze(0)
            prompt = torch.arange(prompt_len, device=device).remainder(62).unsqueeze(0)
            bert = torch.zeros(1, 1024, text_len, device=device, dtype=dtype)
            results = []
            for context in (torch.no_grad, torch.inference_mode):
                trace.clear()
                with context():
                    tokens, index = model.infer_panel_naive(
                        x, None, prompt, bert, top_k=1, early_stop_num=15)
                    torch.cuda.synchronize(device)
                    results.append((tokens.cpu(), index, [value.cpu() for value in trace]))
                assert set(model.bucket_graphs) == keys
                assert model.cuda_graph_stats['graph_replay_steps'] == 15
            assert results[0][1] == results[1][1]
            torch.testing.assert_close(results[0][0], results[1][0], rtol=0, atol=0)
            assert len(results[0][2]) == len(results[1][2]) == 16
            for expected, actual in zip(results[0][2], results[1][2]):
                torch.testing.assert_close(expected, actual, rtol=2e-3, atol=2e-3)


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
        models = [decoder(layout, hidden=64).half().to(device) for layout in ("bucket", "nested")]
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
        k, _ = model._graph_kv_views(48, device, torch.float16)
        first, second = [torch.cuda.Stream(device=device) for _ in range(2)]
        first.wait_stream(torch.cuda.current_stream(device))
        second.wait_stream(torch.cuda.current_stream(device))
        with torch.cuda.stream(first), model._use_graph_workspace(48, device):
            torch.cuda._sleep(5_000_000)
            k[0].fill_(17)
        # No host synchronization between requests. The second lease's wait
        # must order this mutation after the first stream's delayed write.
        with torch.cuda.stream(second), model._use_graph_workspace(96, device):
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


@pytest.mark.skipif(not torch.cuda.is_available(), reason="requires CUDA replay fallback")
def test_replay_failure_continues_from_active_workspace(monkeypatch):
    monkeypatch.setattr(module, "tqdm", lambda values: values)
    monkeypatch.setattr(module, "_should_stop_on_eos", lambda *_: False)
    trace = []

    def sample(logits, history, **_kwargs):
        trace.append(logits.clone())
        return torch.full((1, 1), (history.shape[1] + 3) % 62,
                          device=logits.device, dtype=torch.int32), None

    monkeypatch.setattr(module, "sample", sample)
    device = torch.device("cuda:0")
    with torch.cuda.device(device):
        model = decoder(hidden=64).half().to(device)
        model.kv_cache_buckets = [512]
        model.use_static_kv_cache = model.cuda_graph_enabled = True
        assert model.precapture_cuda_graph([512]) == {512: True}
        keys = set(model.bucket_graphs)
        x = torch.arange(1, 97, device=device).remainder(62).unsqueeze(0)
        prompt = torch.arange(96, device=device).remainder(62).unsqueeze(0)
        bert = torch.zeros(1, 1024, 96, device=device, dtype=torch.float16)
        results = []
        calls = []

        def fail_before_launch(*_args):
            calls.append(True)
            raise RuntimeError("synthetic replay failure before launch")

        for fail in (False, True):
            trace.clear()
            if fail:
                monkeypatch.setattr(model, "_replay_cuda_graph", fail_before_launch)
            with torch.no_grad():
                tokens, index = model.infer_panel_naive(
                    x, None, prompt, bert, top_k=1, early_stop_num=15)
                torch.cuda.synchronize(device)
                results.append((tokens.cpu(), index, [value.cpu() for value in trace]))
            assert set(model.bucket_graphs) == keys
        assert len(calls) == 1 and model.cuda_graph_stats['graph_replay_steps'] == 0
        assert results[0][1] == results[1][1]
        torch.testing.assert_close(results[0][0], results[1][0], rtol=0, atol=0)
        assert len(results[0][2]) == len(results[1][2]) == 16
        for expected, actual in zip(results[0][2], results[1][2]):
            torch.testing.assert_close(expected, actual, rtol=2e-3, atol=2e-3)


@pytest.mark.skipif(not torch.cuda.is_available(), reason="requires CUDA graph promotion")
@pytest.mark.parametrize("attention", ["sdpa", "flash"])
@pytest.mark.parametrize("prepared,steps", [([256, 448, 512, 768, 1024], 700),
                                           ([256], 160), ([256, 512], 350),
                                           ([256, 512, 1024], 700)])
def test_nested_promotion_and_missing_or_maximum_fallback_match_full_bucket(
        monkeypatch, caplog, attention, prepared, steps):
    from AR.models.t2s_flash_attn import apply_flash_attn_patch, is_flash_attn_available
    if attention == "flash" and not is_flash_attn_available():
        pytest.skip("FlashAttention extension unavailable")
    monkeypatch.setattr(module, "tqdm", lambda values: values)
    monkeypatch.setattr(module, "_should_stop_on_eos", lambda *_: False)
    trace = []

    def sample(logits, _history, **_kwargs):
        trace.append(logits.clone())
        return logits.argmax(-1, keepdim=True).to(torch.int32), None

    monkeypatch.setattr(module, "sample", sample)
    device = torch.device("cuda:0")
    with torch.cuda.device(device), torch.no_grad():
        candidate = decoder(hidden=64).half().to(device)
        reference = decoder(hidden=64).half().to(device)
        candidate.kv_cache_buckets = [256, 448, 512, 768, 1024] if 1024 in prepared else [256, 448, 512]
        reference.kv_cache_buckets = [max(candidate.kv_cache_buckets)]
        for model in (candidate, reference):
            model.use_static_kv_cache = model.cuda_graph_enabled = True
            if attention == "flash":
                assert apply_flash_attn_patch(model)
                for block in model.t2s_transformer_static.blocks:
                    def strict_decode(x, k, v, pos, mask, valid=-1, sdpa=True, block=block):
                        return block._finish_attention(x, block._flash_valid_len(x, k, v, pos)), k, v
                    block.decode_next_token_with_static_cache = strict_decode
        assert all(candidate.precapture_cuda_graph(prepared).values())
        assert all(reference.precapture_cuda_graph().values())
        roots = [root.data_ptr() for root in candidate._graph_kv_roots[None]]
        keys = set(candidate.bucket_graphs)
        monkeypatch.setattr(candidate, "_warmup_and_capture_bucket",
                            lambda *_: pytest.fail("request tried to capture over live KV"))
        x = torch.arange(1, 97, device=device).remainder(62).unsqueeze(0)
        prompt = torch.arange(96, device=device).remainder(62).unsqueeze(0)
        bert = torch.zeros(1, 1024, 96, dtype=torch.float16, device=device)
        results = []
        with caplog.at_level(logging.INFO, logger=module.__name__):
            for model in (reference, candidate):
                trace.clear()
                tokens, index = model.infer_panel_naive(x, None, prompt, bert,
                                                      top_k=1, early_stop_num=steps - 1)
                torch.cuda.synchronize(device)
                results.append((tokens.cpu(), index, [value.cpu() for value in trace]))
        torch.testing.assert_close(results[0][0], results[1][0], rtol=0, atol=0)
        assert results[0][1] == results[1][1]
        assert len(results[0][2]) == len(results[1][2]) == steps
        for expected, actual in zip(results[0][2], results[1][2]):
            torch.testing.assert_close(expected, actual, rtol=2e-3, atol=2e-3)
        assert roots == [root.data_ptr() for root in candidate._graph_kv_roots[None]]
        assert keys == set(candidate.bucket_graphs)
        stats = candidate.cuda_graph_stats
        assert stats['start_bucket'] == 256 and stats['capture_attempts'] == 0
        assert stats['generated_tokens'] == steps - 1
        assert stats['total_steps'] == 1 + stats['graph_replay_steps'] + stats['static_steps'] + stats['dynamic_steps']
        if len(prepared) == 5:
            assert stats['promotions'] == 4 and stats['final_bucket'] == 1024
            assert stats['dynamic_steps'] == stats['static_steps'] == 0
        elif 1024 in prepared:
            assert stats['promotions'] == 2 and stats['final_bucket'] == 1024
            assert stats['dynamic_steps'] == stats['static_steps'] == 0
        elif len(prepared) == 1:
            assert stats['promotions'] == 0 and stats['dynamic_steps'] > 0
        else:
            assert stats['promotions'] == 1 and stats['final_bucket'] == 512
            assert stats['dynamic_steps'] > 0
        messages = [record.getMessage() for record in caplog.records if '[T2S request]' in record.getMessage()]
        assert len(messages) == 2
        assert f"promotions={stats['promotions']}" in messages[-1]
        assert f"generated_tokens={steps - 1}" in messages[-1]
        assert f"dynamic_steps={stats['dynamic_steps']}" in messages[-1]


@pytest.mark.skipif(not torch.cuda.is_available(), reason="requires CUDA graph promotion")
@pytest.mark.parametrize("attention", ["sdpa", "flash"])
@pytest.mark.parametrize("poison", [float("nan"), float("inf")], ids=["nan", "inf"])
def test_promotion_discards_nonfinite_previous_request_tail(monkeypatch, attention, poison):
    from AR.models.t2s_flash_attn import apply_flash_attn_patch, is_flash_attn_available
    if attention == "flash" and not is_flash_attn_available():
        pytest.skip("FlashAttention extension unavailable")
    monkeypatch.setattr(module, "tqdm", lambda values: values)
    monkeypatch.setattr(module, "_should_stop_on_eos", lambda *_: False)
    trace = []

    def sample(logits, _history, **_kwargs):
        trace.append(logits.clone())
        return logits.argmax(-1, keepdim=True).to(torch.int32), None

    monkeypatch.setattr(module, "sample", sample)
    device = torch.device("cuda:0")
    with torch.cuda.device(device), torch.no_grad():
        candidate = decoder(hidden=64).half().to(device)
        reference = decoder(hidden=64).half().to(device)
        candidate.kv_cache_buckets = [256, 512]
        reference.kv_cache_buckets = [512]
        for model in (candidate, reference):
            model.use_static_kv_cache = model.cuda_graph_enabled = True
            if attention == "flash":
                assert apply_flash_attn_patch(model)
                for block in model.t2s_transformer_static.blocks:
                    def strict_decode(x, k, v, pos, mask, valid=-1, sdpa=True, block=block):
                        return block._finish_attention(x, block._flash_valid_len(x, k, v, pos)), k, v
                    block.decode_next_token_with_static_cache = strict_decode
            assert all(model.precapture_cuda_graph().values())
        keys = set(candidate.bucket_graphs)
        roots = [root.data_ptr() for root in candidate._graph_kv_roots[None]]
        # Simulate a previous request's nonfinite value outside the new
        # request's initial view and final valid prefix. A bool SDPA mask
        # cannot make a NaN/Inf value safe when it reads the whole larger view.
        for root in candidate._graph_kv_roots[None]:
            root[:, :, 400, :] = poison
        monkeypatch.setattr(candidate, "_warmup_and_capture_bucket",
                            lambda *_: pytest.fail("request tried to capture over live KV"))
        x = torch.arange(1, 51, device=device).remainder(62).unsqueeze(0)
        prompt = torch.arange(50, device=device).remainder(62).unsqueeze(0)
        bert = torch.zeros(1, 1024, 50, dtype=torch.float16, device=device)
        results = []
        for model in (reference, candidate):
            trace.clear()
            tokens, index = model.infer_panel_naive(x, None, prompt, bert,
                                                  top_k=1, early_stop_num=199)
            torch.cuda.synchronize(device)
            results.append((tokens.cpu(), index, [logits.cpu() for logits in trace]))
        bad_steps = [index for index, logits in enumerate(results[1][2])
                     if not torch.isfinite(logits).all()]
        assert not bad_steps, f"nonfinite logits at zero-based decode steps {bad_steps}"
        torch.testing.assert_close(results[0][0], results[1][0], rtol=0, atol=0)
        assert results[0][1] == results[1][1] == 199
        assert len(results[0][2]) == len(results[1][2]) == 200
        for expected, actual in zip(results[0][2], results[1][2]):
            torch.testing.assert_close(expected, actual, rtol=2e-3, atol=2e-3)
        assert keys == set(candidate.bucket_graphs)
        assert roots == [root.data_ptr() for root in candidate._graph_kv_roots[None]]
        stats = candidate.cuda_graph_stats
        assert stats['start_bucket'] == 256 and stats['final_bucket'] == 512
        assert stats['promotions'] == 1 and stats['graph_replay_steps'] == 199
        assert stats['capture_attempts'] == stats['static_steps'] == stats['dynamic_steps'] == 0
        assert stats['final_kv'] == 299
