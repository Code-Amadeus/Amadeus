"""Optional in core; the required numerical lane cannot silently skip MLX."""
import os
import json
import numpy as np
import pytest

if os.environ.get("AMADEUS_MLX_TESTS") == "1":
    import torch
    import mlx.core as mx
else:
    torch = pytest.importorskip("torch")
    mx = pytest.importorskip("mlx.core")

from tools.probes.gsv_semantic_reference import (
    create_tiny_checkpoint, synthetic_inputs, validate_numerics,
)
from tts.semantic_mlx.weights import export_checkpoint, validate_artifact
from tts.semantic_mlx.generation import generate, logits_to_probs, sample
from tts.semantic_mlx.model import T2SModel
from tts.semantic_mlx.runtime import MLXSemanticDecoder


pytestmark = pytest.mark.mlx_cpu
mx.set_default_device(mx.cpu)


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    root = tmp_path_factory.mktemp("semantic")
    checkpoint = create_tiny_checkpoint(root / "tiny.ckpt")
    artifact = export_checkpoint(checkpoint, root / "cache")
    config, _ = validate_artifact(artifact)
    return checkpoint, artifact, config


@pytest.mark.parametrize("phones,prompt", [(1, 1), (7, 9), (31, 33), (32, 32), (33, 31)])
def test_original_torch_blocks_and_cached_full_agree(tiny, phones, prompt):
    checkpoint, artifact, config = tiny
    inputs = synthetic_inputs(config, phone_length=phones, prompt_length=prompt)
    report = validate_numerics(checkpoint, artifact, inputs, atol=1e-4, rtol=1e-4)
    assert report["status"] == "passed", [row for row in report["rows"] if not row["passed"]]


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
    np.testing.assert_array_equal(np.isneginf(mask), [
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
        with pytest.raises(ValueError, match="differs from source"):
            export_checkpoint(checkpoint, artifact.parent.parent)
        manifest["config"]["norm_eps"] = 1e-3
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ValueError, match="invalid"):
            validate_artifact(artifact)
    finally:
        manifest_path.write_bytes(original)


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
