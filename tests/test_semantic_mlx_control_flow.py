"""Controlled source-versus-MLX generation boundaries; math is tested separately."""
from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

if os.environ.get("AMADEUS_MLX_TESTS") == "1":
    import torch
    import mlx.core as mx
else:
    torch = pytest.importorskip("torch")
    mx = pytest.importorskip("mlx.core")

from tools.probes.gsv_semantic_reference import create_tiny_checkpoint, load_reference
from tts.semantic_mlx import generation
from tts.semantic_mlx.weights import export_checkpoint, validate_artifact


pytestmark = pytest.mark.mlx_cpu
mx.set_default_device(mx.cpu)


def synchronous_reference(model, phones, prompt, bert, *, budget, steps):
    """Pre-optimization scheduling oracle, kept only in tests."""
    history = prompt
    logits, cache = model.prefill(phones, prompt, bert)
    for idx in range(steps):
        if idx < 11:
            logits = logits[:, :-1]
        probs, penalized = generation.logits_to_probs(
            logits, history, top_k=3, top_p=1.0, temperature=0.6, repetition_penalty=1.35)
        token = generation.sample(probs)
        stop = (mx.argmax(penalized, axis=-1)[0] == model.config.eos) | (token[0, 0] == model.config.eos)
        history = mx.concatenate((history, token), axis=1)
        mx.eval(history, stop, cache.layers)
        if (budget != -1 and history.shape[1] - prompt.shape[1] > budget) or bool(stop.item()):
            break
        if idx + 1 < steps:
            logits, cache = model.decode_step(token, cache)
    return history[:, :-1], idx


@pytest.mark.parametrize("budget,steps,eos_at", [(0, 15, None), (2, 15, None),
                                                 (-1, 3, None), (-1, 15, 11)])
def test_forward_lookahead_preserves_tokens_stop_and_rng(budget, steps, eos_at):
    class Model:
        config = SimpleNamespace(eos=5)

        def prefill(self, *_):
            self.step = 0
            return self.value(), SimpleNamespace(layers=[])

        def value(self):
            return mx.array([[1.0, 2.0, 2.5, 1.4, 2.1,
                              30.0 if eos_at is not None and self.step >= eos_at else -30.0]])

        def decode_step(self, _token, cache):
            self.step += 1
            return self.value(), cache

    args = (mx.array([[1, 2]]), mx.array([[2, 3]]), mx.zeros((1, 1024, 2)))
    mx.random.seed(924)
    expected, expected_idx = synchronous_reference(Model(), *args, budget=budget, steps=steps)
    expected_rng = mx.random.uniform(shape=(10,))
    mx.eval(expected, expected_rng)
    mx.random.seed(924)
    actual, idx = generation.generate(Model(), *args, top_k=3, top_p=1.0,
                                      temperature=0.6, early_stop_num=budget, max_steps=steps)
    actual_rng = mx.random.uniform(shape=(10,))
    mx.eval(actual, actual_rng)
    assert idx == expected_idx
    np.testing.assert_array_equal(np.array(actual), np.array(expected))
    np.testing.assert_array_equal(np.array(actual_rng), np.array(expected_rng))


def test_partition_topk_keeps_all_pivot_ties_and_promotes_half_sampler():
    probabilities, penalized = generation.logits_to_probs(
        mx.array([[2.0, 2.0, 0.0]], dtype=mx.float16), top_k=1)
    np.testing.assert_allclose(np.array(probabilities), [[0.5, 0.5, 0.0]])
    assert probabilities.dtype == penalized.dtype == mx.float32


@pytest.mark.parametrize("dtype", ["float32", "float16"])
def test_real_model_lookahead_matches_sync_across_cache_growth(tiny, dtype):
    from tools.probes.gsv_semantic_reference import synthetic_inputs
    from tts.semantic_mlx.model import T2SModel

    _, artifact = tiny
    model = T2SModel.from_artifact(artifact, inference_dtype=dtype)
    data = synthetic_inputs(model.config, phone_length=127, prompt_length=128, steps=12)
    args = (mx.array(data["phones"].astype(np.int32)),
            mx.array(data["prompt"].astype(np.int32)), mx.array(data["bert"]))
    mx.random.seed(791)
    expected, expected_idx = synchronous_reference(model, *args, budget=12, steps=20)
    expected_rng = mx.random.uniform(shape=(10,))
    mx.eval(expected, expected_rng)
    mx.random.seed(791)
    actual, idx = generation.generate(model, *args, top_k=3, top_p=1.0,
                                      temperature=0.6, early_stop_num=12, max_steps=20)
    actual_rng = mx.random.uniform(shape=(10,))
    mx.eval(actual, actual_rng)
    assert idx == expected_idx
    np.testing.assert_array_equal(np.array(actual), np.array(expected))
    np.testing.assert_array_equal(np.array(actual_rng), np.array(expected_rng))


@pytest.fixture
def tiny(tmp_path):
    source = create_tiny_checkpoint(tmp_path / "tiny.ckpt")
    artifact = export_checkpoint(source, tmp_path / "cache")
    return source, artifact


@pytest.mark.parametrize(
    ("case", "budget", "steps", "expected_idx"), [
        ("suppressed_greedy_eos", 2, 15, 2),
        ("sampled_eos", -1, 15, 11),
        ("greedy_eos", -1, 15, 11),
        ("budget_zero", 0, 15, 0),
        ("strict_budget", 2, 15, 2),
        ("loop_exit", -1, 3, 2),
    ],
)
def test_controlled_generation_matches_actual_source(tiny, monkeypatch, case, budget, steps, expected_idx):
    source, _ = tiny
    oracle = load_reference(source)
    eos = oracle.EOS
    source_module = sys.modules[type(oracle).__module__]

    def logits(index):
        values = np.full((1, eos + 1), -8.0, dtype=np.float32)
        values[0, 2] = 2.0
        if case in {"suppressed_greedy_eos", "greedy_eos"}:
            values[0, eos] = 8.0
        return values

    class Projection(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def forward(self, hidden):
            result = torch.from_numpy(logits(self.calls)).to(hidden.device)
            self.calls += 1
            return result

    oracle.ar_predict_layer = Projection()
    sampled = eos if case == "sampled_eos" else 2
    source_calls = 0

    def torch_sample(_logits, _history, **_kwargs):
        nonlocal source_calls
        token = sampled if source_calls == 11 else 2
        source_calls += 1
        return (torch.tensor([[token]], dtype=torch.long), None)

    monkeypatch.setattr(source_module, "sample", torch_sample)
    monkeypatch.setattr(source_module, "tqdm", lambda values: range(steps))

    class ControlledMLX:
        config = SimpleNamespace(eos=eos)

        def __init__(self):
            self.calls = 0

        def prefill(self, *_):
            result = mx.array(logits(self.calls))
            self.calls += 1
            return result, SimpleNamespace(layers=[])

        def decode_step(self, *_):
            result = mx.array(logits(self.calls))
            self.calls += 1
            return result, SimpleNamespace(layers=[])

    mlx_calls = 0

    def mlx_sample(_probs):
        nonlocal mlx_calls
        token = sampled if mlx_calls == 11 else 2
        mlx_calls += 1
        return mx.array([[token]], dtype=mx.int32)

    monkeypatch.setattr(generation, "sample", mlx_sample)
    phones = torch.tensor([[1, 2]], dtype=torch.long)
    prompt = torch.tensor([[3, 4]], dtype=torch.long)
    bert = torch.zeros((1, 1024, 2))
    with torch.inference_mode():
        expected, source_idx = oracle.infer_panel(
            phones, torch.tensor([2]), prompt, bert, top_k=1, top_p=1.0,
            early_stop_num=budget, temperature=1.0, repetition_penalty=1.35,
            enable_cuda_graph=False, enable_static_kv=False)
    actual, mlx_idx = generation.generate(
        ControlledMLX(), mx.array(np.array(phones, dtype=np.int32)),
        mx.array(np.array(prompt, dtype=np.int32)),
        mx.array(np.array(bert)), top_k=1, top_p=1.0, early_stop_num=budget,
        max_steps=steps)
    assert source_idx == mlx_idx == expected_idx
    np.testing.assert_array_equal(np.array(actual), expected.numpy())
    assert actual.shape[1] == prompt.shape[1] + expected_idx


@pytest.mark.parametrize("mutation,expected", [
    ("missing", "missing"), ("unexpected", "unexpected"), ("shape", "shape mismatch"),
])
def test_converter_rejects_incompatible_source(tiny, tmp_path, mutation, expected):
    source, _ = tiny
    checkpoint = torch.load(source, weights_only=True, map_location="cpu")
    weights = checkpoint["weight"]
    key = "model.bert_proj.weight"
    if mutation == "missing":
        del weights[key]
    elif mutation == "unexpected":
        weights["model.unexpected.weight"] = torch.zeros(1)
    else:
        weights[key] = weights[key][:-1]
    changed = tmp_path / f"{mutation}.ckpt"
    torch.save(checkpoint, changed)
    with pytest.raises(ValueError, match=expected):
        export_checkpoint(changed, tmp_path / "changed-cache")


def test_corrupted_converted_weights_are_rejected(tiny):
    _, artifact = tiny
    weights = artifact / "model.safetensors"
    original = weights.read_bytes()
    try:
        weights.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with pytest.raises(ValueError, match="checksum mismatch"):
            validate_artifact(artifact)
    finally:
        weights.write_bytes(original)


def test_semantic_seam_imports_without_torch_or_mlx():
    script = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'torch' or fullname.startswith('torch.') or fullname == 'mlx' or fullname.startswith('mlx.'):
            raise AssertionError('heavy runtime imported: ' + fullname)
sys.meta_path.insert(0, Block())
import tts.semantic_runtime
assert not any(name == 'torch' or name.startswith('torch.') or name == 'mlx' or name.startswith('mlx.') for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_native_mlx_import_error_is_actionable(monkeypatch):
    import tts.semantic_runtime as seam

    real_import = seam.importlib.import_module

    def broken_import(name):
        if name == "mlx.core":
            raise ImportError("DLL load failed while importing core")
        return real_import(name)

    monkeypatch.setattr(seam.importlib, "import_module", broken_import)
    with pytest.raises(RuntimeError, match="optional mlx-t2s dependencies"):
        seam.load_mlx_decoder("unused", acoustic_device="mps", cache_root="unused")
