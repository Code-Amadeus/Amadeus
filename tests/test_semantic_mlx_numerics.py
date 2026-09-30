"""Optional in core; the required numerical lane cannot silently skip MLX."""
import os
import json
import math
from types import SimpleNamespace
import numpy as np
import pytest

if os.environ.get("AMADEUS_MLX_TESTS") == "1":
    import torch
    import mlx.core as mx
else:
    torch = pytest.importorskip("torch")
    mx = pytest.importorskip("mlx.core")

from tools.probes.gsv_semantic_reference import (
    create_tiny_checkpoint, decoder_type, synthetic_inputs, validate_numerics,
)
from tts.semantic_mlx.weights import export_checkpoint, validate_artifact
from tts.semantic_mlx.generation import generate, logits_to_probs, sample
from tts.semantic_mlx.model import T2SModel
from tts.semantic_mlx.runtime import MLXSemanticDecoder, require_metal


pytestmark = pytest.mark.mlx_cpu
mx.set_default_device(mx.cpu)


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    root = tmp_path_factory.mktemp("semantic")
    checkpoint = create_tiny_checkpoint(root / "tiny.ckpt")
    artifact = export_checkpoint(checkpoint, root / "cache")
    config, _ = validate_artifact(artifact)
    return checkpoint, artifact, config


@pytest.fixture(scope="module")
def head32(tmp_path_factory):
    root = tmp_path_factory.mktemp("semantic-head32")
    source_config = {
        "model": {"hidden_dim": 64, "embedding_dim": 64, "head": 2,
                  "n_layer": 2, "phoneme_vocab_size": 23, "vocab_size": 17,
                  "EOS": 16, "dropout": 0},
        "data": {"max_sec": 20},
    }
    torch.manual_seed(721)
    model = decoder_type()(source_config).eval()
    with torch.no_grad():
        model.ar_text_position.alpha.fill_(0.7)
        model.ar_audio_position.alpha.fill_(1.3)
    checkpoint = root / "head32.ckpt"
    torch.save({"config": source_config, "weight": {"model." + key: value
                for key, value in model.state_dict().items()}}, checkpoint)
    artifact = export_checkpoint(checkpoint, root / "cache")
    config, _ = validate_artifact(artifact)
    return checkpoint, artifact, config


def test_head32_padding_matches_source_across_cache_growth(head32):
    checkpoint, artifact, config = head32
    inputs = synthetic_inputs(config, phone_length=127, prompt_length=128, steps=2)
    report = validate_numerics(checkpoint, artifact, inputs, atol=1e-4, rtol=1e-4)
    assert report["status"] == "passed", [row for row in report["rows"] if not row["passed"]]


@pytest.mark.parametrize("dtype", ["float32", "float16"])
def test_head32_padding_preserves_unpadded_attention_math(head32, monkeypatch, dtype):
    _, artifact, config = head32
    model = T2SModel.from_artifact(artifact, inference_dtype=dtype)
    sdpa = mx.fast.scaled_dot_product_attention
    tolerance = 1e-4 if dtype == "float32" else 1e-3

    def checked_attention(q, k, v, *, scale, mask):
        assert q.shape[-1] == k.shape[-1] == v.shape[-1] == 64
        assert scale == 1 / math.sqrt(32)
        for item in (q, k, v):
            assert np.count_nonzero(np.array(item[..., 32:])) == 0
        actual = sdpa(q, k, v, scale=scale, mask=mask)
        expected = sdpa(q[..., :32], k[..., :32], v[..., :32], scale=scale, mask=mask)
        np.testing.assert_allclose(np.array(actual[..., :32]), np.array(expected),
                                   atol=tolerance, rtol=tolerance)
        assert np.count_nonzero(np.array(actual[..., 32:])) == 0
        return actual

    monkeypatch.setattr(mx.fast, "scaled_dot_product_attention", checked_attention)
    inputs = synthetic_inputs(config, phone_length=2, prompt_length=3, steps=1)
    _, cache = model.prefill(mx.array(inputs["phones"].astype(np.int32)),
                             mx.array(inputs["prompt"].astype(np.int32)), mx.array(inputs["bert"]))
    logits, _ = model.decode_step(mx.array(inputs["history"].astype(np.int32)), cache)
    assert np.isfinite(np.array(logits)).all()


@pytest.mark.parametrize("phones,prompt", [(1, 1), (7, 9), (31, 33), (32, 32), (33, 31)])
def test_original_torch_blocks_and_cached_full_agree(tiny, phones, prompt):
    checkpoint, artifact, config = tiny
    inputs = synthetic_inputs(config, phone_length=phones, prompt_length=prompt)
    report = validate_numerics(checkpoint, artifact, inputs, atol=1e-4, rtol=1e-4)
    assert report["status"] == "passed", [row for row in report["rows"] if not row["passed"]]


@pytest.mark.parametrize("phones,prompt,steps", [(2, 3, 3), (7, 9, 5), (127, 128, 3)])
def test_fast_fp32_matches_source_across_cache_growth(tiny, phones, prompt, steps):
    checkpoint, artifact, config = tiny
    inputs = synthetic_inputs(config, phone_length=phones, prompt_length=prompt, steps=steps)
    report = validate_numerics(checkpoint, artifact, inputs,
                               atol=1e-4, rtol=1e-4)
    assert report["status"] == "passed", [row for row in report["rows"] if not row["passed"]]
    model = T2SModel.from_artifact(artifact)
    _, cache = model.prefill(mx.array(inputs["phones"].astype(np.int32)),
                             mx.array(inputs["prompt"].astype(np.int32)), mx.array(inputs["bert"]))
    assert cache.layers[0][0].shape[2] == 256
    for step in range(steps):
        _, cache = model.decode_step(mx.array(inputs["history"][:, step:step + 1].astype(np.int32)), cache)
    assert cache.text_length + cache.audio_length == phones + prompt + steps
    assert cache.layers[0][0].shape[2] == (512 if phones + prompt + steps > 256 else 256)


def test_fast_mask_and_dtype_are_explicit(tiny):
    _, artifact, config = tiny
    allowed = np.array(T2SModel.attention_mask(2, 3))[0, 0]
    np.testing.assert_array_equal(allowed, [
        [1, 1, 0, 0, 0], [1, 1, 0, 0, 0], [1, 1, 1, 0, 0],
        [1, 1, 1, 1, 0], [1, 1, 1, 1, 1],
    ])
    decoder = MLXSemanticDecoder.for_numerical_test(
        artifact, inference_dtype="float16")
    assert decoder.info["dtype"] == "float16"
    assert decoder.info["cache_impl"] == "mlx_chunked_valid_prefix"
    assert decoder.info["head_dim"] == decoder.info["cache_head_dim"] == 8
    assert all(weight.dtype == mx.float16 for weight in decoder.model.weights.values())
    assert decoder.model.positions.dtype == mx.float16
    inputs = synthetic_inputs(config)
    logits, cache = decoder.model.prefill(mx.array(inputs["phones"].astype(np.int32)),
                                           mx.array(inputs["prompt"].astype(np.int32)),
                                           mx.array(inputs["bert"]))
    mx.eval(logits, [item for layer in cache.layers for item in layer])
    assert logits.dtype == mx.float16
    assert np.isfinite(np.array(logits)).all()
    decoder.close()
    with pytest.raises(ValueError, match="dtype"):
        T2SModel.from_artifact(artifact, inference_dtype="float64")


def test_fast_float16_rejects_overflow_on_finite_float32_source(tiny, tmp_path):
    checkpoint, _, _ = tiny
    source = torch.load(checkpoint, map_location="cpu", weights_only=True)
    source["weight"]["model.bert_proj.weight"][0, 0] = 1_000_000.0
    changed = tmp_path / "large-finite.ckpt"
    torch.save(source, changed)
    artifact = export_checkpoint(changed, tmp_path / "cache")
    with pytest.raises(ValueError, match="float16 inference overflows"):
        T2SModel.from_artifact(artifact, inference_dtype="float16")


def test_fixed_benchmark_executes_every_decode_and_sampler_step(tiny):
    from tools.probes.gsv_backend_probe import _fixed_torch_work, _fixed_mlx_work
    from tools.probes.gsv_semantic_reference import TorchTrace, load_reference

    checkpoint, artifact, config = tiny
    inputs = synthetic_inputs(config, phone_length=7, prompt_length=9, steps=4)
    args = SimpleNamespace(top_k=1, top_p=1.0, temperature=0.6,
                           repetition_penalty=1.35)
    torch_inputs = {key: torch.from_numpy(value) for key, value in inputs.items()}
    oracle = TorchTrace(load_reference(checkpoint), capture_trace=False)
    torch_result = _fixed_torch_work(oracle, torch_inputs, args)
    mlx_inputs = {key: mx.array(value.astype(np.float32 if key == "bert" else np.int32))
                  for key, value in inputs.items()}
    model = T2SModel.from_artifact(artifact)
    mlx_result = _fixed_mlx_work(model, mlx_inputs, args)
    for result in (torch_result, mlx_result):
        assert result["decode_calls"] == result["sampler_calls"] == inputs["history"].shape[1]
        assert result["final_audio_length"] == inputs["prompt"].shape[1] + inputs["history"].shape[1]
        assert result["fixed_ar_work_ms"] >= sum(result["fixed_step_ms"])
    assert torch_result["sampled_ids_sha256"] == mlx_result["sampled_ids_sha256"]


@pytest.mark.parametrize("prompt_length", [337, 345, 351])
def test_cache_matches_source_at_real_prompt_lengths(tiny, prompt_length):
    checkpoint, artifact, config = tiny
    inputs = synthetic_inputs(config, phone_length=48, prompt_length=prompt_length, steps=3)
    report = validate_numerics(checkpoint, artifact, inputs, atol=1e-4, rtol=1e-4)
    assert report["status"] == "passed", [r for r in report["rows"] if not r["passed"]]


@pytest.mark.parametrize("fixture_name,dtype", [
    ("tiny", "float32"), ("head32", "float32"), ("head32", "float16"),
])
def test_fast_attention_ignores_poisoned_unused_cache_capacity(request, fixture_name, dtype):
    _, artifact, config = request.getfixturevalue(fixture_name)
    short = synthetic_inputs(config, phone_length=2, prompt_length=3, steps=1)
    short_model = T2SModel.from_artifact(artifact, inference_dtype=dtype)
    short_inputs = [mx.array(short[key].astype(np.int32)) for key in ("phones", "prompt")]
    _, short_clean = short_model.prefill(*short_inputs, mx.array(short["bert"]))
    short_poisoned_layers = []
    nan = mx.full((1, config.num_heads, 1, short_model.cache_head_dim), float("nan"),
                  dtype=short_model.dtype)
    for key, value in short_clean.layers:
        short_poisoned_layers.append((mx.slice_update(key, nan, mx.array([100]), axes=(2,)),
                                      mx.slice_update(value, nan, mx.array([100]), axes=(2,))))
    short_poisoned = type(short_clean)(short_poisoned_layers,
                                       short_clean.text_length, short_clean.audio_length)
    short_token = mx.array(short["history"][:, :1].astype(np.int32))
    short_expected, _ = short_model.decode_step(short_token, short_clean)
    short_actual, _ = short_model.decode_step(short_token, short_poisoned)
    np.testing.assert_allclose(np.array(short_actual), np.array(short_expected), atol=1e-4, rtol=1e-4)

    inputs = synthetic_inputs(config, phone_length=127, prompt_length=128, steps=2)
    model = T2SModel.from_artifact(artifact, inference_dtype=dtype)

    def assert_cache_padding(cache):
        for layer in cache.layers:
            for item in layer:
                assert item.dtype == model.dtype
                assert item.shape[-1] == model.cache_head_dim
                assert np.count_nonzero(np.array(item[..., model.head_dim:])) == 0

    phones = mx.array(inputs["phones"].astype(np.int32))
    prompt = mx.array(inputs["prompt"].astype(np.int32))
    bert = mx.array(inputs["bert"])
    _, clean = model.prefill(phones, prompt, bert)
    assert clean.text_length + clean.audio_length == 255
    assert_cache_padding(clean)
    poisoned_layers = []
    for key, value in clean.layers:
        poisoned_layers.append((mx.slice_update(key, nan, mx.array([255]), axes=(2,)),
                                mx.slice_update(value, nan, mx.array([255]), axes=(2,))))
    poisoned = type(clean)(poisoned_layers, clean.text_length, clean.audio_length)
    token = mx.array(inputs["history"][:, :1].astype(np.int32))
    first_clean, clean = model.decode_step(token, clean)
    first_poisoned, poisoned = model.decode_step(token, poisoned)
    np.testing.assert_allclose(np.array(first_poisoned), np.array(first_clean), atol=1e-4, rtol=1e-4)
    assert clean.text_length + clean.audio_length == 256
    assert clean.layers[0][0].shape[2] == 256
    assert_cache_padding(clean)
    token = mx.array(inputs["history"][:, 1:2].astype(np.int32))
    second_clean, clean = model.decode_step(token, clean)
    second_poisoned, poisoned = model.decode_step(token, poisoned)
    np.testing.assert_allclose(np.array(second_poisoned), np.array(second_clean), atol=1e-4, rtol=1e-4)
    assert clean.text_length + clean.audio_length == 257
    assert clean.layers[0][0].shape[2] == 512
    assert_cache_padding(clean)


@pytest.mark.parametrize("top_p", [0.6, 0.8, 1.0])
@pytest.mark.parametrize("top_k", [1, 3, None])
@pytest.mark.parametrize("temperature", [0.0, 0.6, 1.0])
def test_sampler_matches_active_torch_and_inplace_eos_view(tiny, top_p, top_k, temperature):
    from AR.models.utils import logits_to_probs as torch_probs

    values = np.array([[2.1, -0.3, 1.1, -2.4, 0.8]], dtype=np.float32)
    history = np.array([[0, 1, 0, 4]], dtype=np.int64)
    kwargs = dict(top_p=top_p, top_k=top_k, temperature=temperature, repetition_penalty=1.35)
    reference = torch.tensor(values)
    expected = torch_probs(reference, torch.tensor(history), **kwargs)
    actual, modified = logits_to_probs(mx.array(values), mx.array(history.astype(np.int32)), **kwargs)
    np.testing.assert_allclose(np.array(actual), expected.numpy(), atol=1e-6, rtol=1e-5)
    np.testing.assert_allclose(np.array(modified), reference.numpy(), atol=1e-6, rtol=1e-6)


def test_top_p_retains_maximum_and_top_k_retains_ties():
    probs, _ = logits_to_probs(mx.log(mx.array([[0.8, 0.15, 0.05]])), top_p=0.6)
    np.testing.assert_array_equal(np.array(probs), [[1, 0, 0]])
    probs, _ = logits_to_probs(mx.array([[3.0, 2.0, 2.0, 1.0]]), top_k=2)
    assert np.count_nonzero(np.array(probs)) == 3


def test_sampler_shared_noise():
    probs = mx.array([[0.2, 0.7, 0.1]])
    noise = mx.array([[0.1, 0.2, 0.3]])
    np.testing.assert_array_equal(np.array(sample(probs, noise=noise)), [[1]])


def test_attention_mask_and_cache_requests_are_independent(tiny):
    _, artifact, config = tiny
    model = T2SModel.from_artifact(artifact)
    mask = np.array(model.attention_mask(2, 3))[0, 0]
    np.testing.assert_array_equal(~mask, [
        [0, 0, 1, 1, 1], [0, 0, 1, 1, 1], [0, 0, 0, 1, 1],
        [0, 0, 0, 0, 1], [0, 0, 0, 0, 0],
    ])
    def prefill(length):
        inputs = synthetic_inputs(config, prompt_length=length)
        return model.prefill(mx.array(inputs["phones"].astype(np.int32)),
                             mx.array(inputs["prompt"].astype(np.int32)), mx.array(inputs["bert"]))
    first, cache = prefill(3)
    prefill(35)
    again, again_cache = prefill(3)
    np.testing.assert_array_equal(np.array(first), np.array(again))
    assert cache.audio_length == again_cache.audio_length == 3


def test_existing_artifact_rejects_config_mutation(tiny):
    checkpoint, artifact, _ = tiny
    manifest_path = artifact / "manifest.json"
    original = manifest_path.read_bytes()
    try:
        manifest = json.loads(original)
        manifest["config"]["max_sec"] = 21
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ValueError, match="manifest checksum"):
            export_checkpoint(checkpoint, artifact.parent.parent)
        manifest["config"]["norm_eps"] = 1e-3
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ValueError, match="manifest checksum"):
            validate_artifact(artifact)
    finally:
        manifest_path.write_bytes(original)


def test_warm_artifact_load_avoids_checkpoint_deserialization_and_duplicate_validation(tiny, monkeypatch):
    import tts.semantic_mlx.weights as weights
    import tts.semantic_mlx.model as model_module

    checkpoint, artifact, _ = tiny
    calls = []
    original = weights.validate_artifact

    def checked(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    def unexpected_load(*_args, **_kwargs):
        pytest.fail("Warm artifact loading must not deserialize the source checkpoint")

    monkeypatch.setattr(weights, "read_checkpoint", unexpected_load)
    monkeypatch.setattr(weights, "validate_artifact", checked)
    monkeypatch.setattr(model_module, "validate_artifact", checked)
    directory, config = weights.export_checkpoint(checkpoint, artifact.parent.parent, return_config=True)
    model_module.T2SModel.from_artifact(directory, validated_config=config)
    assert len(calls) == 1


def test_mlx_bridge_matches_actual_torch_greedy_generation(tiny):
    from tools.probes.gsv_semantic_reference import load_reference

    checkpoint, artifact, config = tiny
    inputs = synthetic_inputs(config, phone_length=9, prompt_length=13, seed=481)
    phones = torch.from_numpy(inputs["phones"])
    prompt = torch.from_numpy(inputs["prompt"])
    bert = torch.from_numpy(inputs["bert"])
    lengths = torch.tensor([phones.shape[1]])
    kwargs = dict(top_k=1, top_p=0.7, temperature=0.6,
                  repetition_penalty=1.35, early_stop_num=3,
                  enable_cuda_graph=False, enable_static_kv=False)
    with torch.inference_mode():
        expected, expected_idx = load_reference(checkpoint).infer_panel(
            phones, lengths, prompt, bert, **kwargs)
    decoder = MLXSemanticDecoder.for_numerical_test(artifact)
    actual, idx = decoder.infer_panel(phones, lengths, prompt, bert, **kwargs)
    assert (idx, actual.dtype, actual.device.type) == (expected_idx, torch.int64, "cpu")
    torch.testing.assert_close(actual, expected, rtol=0, atol=0)
    assert decoder.last_timings["bridge_in_ms"] >= 0
    decoder.close()
    with pytest.raises(RuntimeError, match="closed"):
        decoder.infer_panel(phones, lengths, prompt, bert, **kwargs)


def test_mlx_requires_reference_and_integer_ids(tiny):
    _, artifact, config = tiny
    decoder = MLXSemanticDecoder.for_numerical_test(artifact)
    inputs = synthetic_inputs(config)
    phones = torch.from_numpy(inputs["phones"])
    prompt = torch.from_numpy(inputs["prompt"])
    bert = torch.from_numpy(inputs["bert"])
    lengths = torch.tensor([phones.shape[1]])
    with pytest.raises(ValueError, match="reference"):
        decoder.infer_panel(phones, lengths, None, bert)
    with pytest.raises(ValueError, match="integer"):
        decoder.infer_panel(phones.float(), lengths, prompt, bert)
    with pytest.raises(ValueError, match="BERT input"):
        decoder.infer_panel(phones, lengths, prompt, bert.transpose(1, 2))
    decoder.close()


def test_generation_budget_rejects_zero_steps(tiny):
    _, artifact, config = tiny
    model = T2SModel.from_artifact(artifact)
    inputs = synthetic_inputs(config)
    with pytest.raises(ValueError, match="max_steps"):
        generate(model, mx.array(inputs["phones"].astype(np.int32)),
                 mx.array(inputs["prompt"].astype(np.int32)),
                 mx.array(inputs["bert"]), max_steps=0)


def test_production_constructor_rejects_cpu_even_with_mlx_installed(tiny):
    checkpoint, artifact, _ = tiny
    with pytest.raises(RuntimeError, match="Apple Silicon|MLX Metal"):
        MLXSemanticDecoder.from_checkpoint(
            checkpoint, acoustic_device=torch.device("cpu"), cache_root=artifact.parent.parent)


@pytest.mark.parametrize("device", ["mps", "mps:0", torch.device("mps:0")])
def test_metal_device_normalization_accepts_mps_index(monkeypatch, device):
    import tts.semantic_mlx.runtime as runtime

    monkeypatch.setattr(runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(runtime.platform, "machine", lambda: "arm64")
    checked = []
    monkeypatch.setattr(mx.metal, "is_available", lambda: checked.append(True) or False)
    with pytest.raises(RuntimeError, match="MLX Metal"):
        require_metal(device)
    assert checked == [True]
