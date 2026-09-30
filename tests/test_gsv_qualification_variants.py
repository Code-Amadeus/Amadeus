"""Small functional qualification checks; no CPU performance assertions."""
from __future__ import annotations

from functools import partial
import shutil
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from tools.probes import gsv_backend_probe as probe
from tools.probes import gsv_qualification_variants as variants


MLX_CASES = [("audit", "float32"), ("unpadded", "float32"), ("unpadded", "float16"),
             ("current", "float32"), ("current", "float16")]


@pytest.mark.parametrize("backend,revision,dtype", [
    ("torch", "current", "float32"), ("torch", "current", "float16"),
    *(("mlx", revision, dtype) for revision, dtype in MLX_CASES),
])
def test_probe_cli_accepts_all_seven_fixed_variants(monkeypatch, backend, revision, dtype):
    captured = []
    monkeypatch.setattr(probe, "bench", lambda args: captured.append(args) or {"status": "passed"})
    probe.main(["bench", "--backend", backend, "--checkpoint", "local.ckpt", "--fixed-only",
                "--mlx-revision", revision, "--inference-dtype", dtype])
    assert (captured[0].backend, captured[0].mlx_revision, captured[0].inference_dtype) == (backend, revision, dtype)
    assert captured[0].fixed_only is True


@pytest.mark.parametrize("backend,revision,dtype", [
    ("mlx", "audit", "float16"), ("torch", "audit", "float32"),
])
def test_probe_rejects_unimplemented_variant_before_model_load(backend, revision, dtype):
    with pytest.raises(SystemExit) as raised:
        probe.main(["bench", "--backend", backend, "--checkpoint", "local.ckpt",
                    "--mlx-revision", revision, "--inference-dtype", dtype])
    assert raised.value.code == 2


def test_frozen_provenance_and_checksum_work_without_git(tmp_path, monkeypatch):
    target = tmp_path / "tools/probes/gsv_qualification_snapshots"
    shutil.copytree(variants.ROOT / "tools/probes/gsv_qualification_snapshots", target)
    monkeypatch.setattr(variants, "ROOT", tmp_path)
    monkeypatch.setattr(variants.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("Historical variant invoked Git"))
    for revision in ("audit", "unpadded"):
        identity = variants.source_identity("mlx", revision)
        assert identity["model_source_commit"] == variants.SNAPSHOTS[revision]["commit"]
        assert identity["source_sha256"] == variants.SNAPSHOTS[revision]["sha256"]
        assert identity["source_kind"] == "frozen_snapshot"
    with (target / "audit_model.py").open("ab") as stream:
        stream.write(b"\n# changed snapshot\n")
    with pytest.raises(ValueError, match="checksum mismatch"):
        variants.source_identity("mlx", "audit")


@pytest.mark.parametrize("revision", ["audit", "unpadded", "current"])
def test_fixed_loop_retains_revision_scheduling_and_all_work(monkeypatch, revision):
    events = []

    class Stop:
        def __or__(self, _other):
            return self

        def item(self):
            events.append("host_read")
            return True  # Fixed N must still execute every subsequent position.

    class ArgmaxToken:
        def __eq__(self, _other):
            return Stop()

    mx = ModuleType("mlx.core")
    mx.eval = lambda *_args: events.append("eval")
    mx.async_eval = lambda *_args: events.append("async")
    mx.argmax = lambda *_args, **_kwargs: [ArgmaxToken()]
    mx.concatenate = np.concatenate
    monkeypatch.setitem(sys.modules, "mlx", ModuleType("mlx"))
    monkeypatch.setitem(sys.modules, "mlx.core", mx)

    def sample(_probabilities):
        events.append("sample")
        return np.array([[1]], dtype=np.int32)

    monkeypatch.setattr(variants, "generation_module", lambda _revision: SimpleNamespace(
        logits_to_probs=lambda logits, *_args, **_kwargs: (logits, logits), sample=sample))

    class Model:
        config = SimpleNamespace(eos=2)

        def prefill(self, _phones, prompt, _bert):
            events.append("prefill")
            return np.array([[0., 1., 2.]]), SimpleNamespace(layers=[], audio_length=prompt.shape[1])

        def decode_step(self, _token, cache):
            events.append("decode")
            cache.audio_length += 1
            return np.array([[0., 1., 2.]]), cache

    inputs = {"phones": np.array([[1, 2]]), "prompt": np.array([[1, 1]]),
              "bert": np.zeros((1, 2, 2)), "history": np.array([[0, 1]])}
    args = SimpleNamespace(mlx_revision=revision, top_k=1, top_p=1., temperature=.6,
                           repetition_penalty=1.35)
    result = probe._fixed_mlx_work(Model(), inputs, args)
    step = (["sample", "eval", "host_read", "decode"] if revision == "audit" else
            ["sample", "async", "decode", "async", "host_read"])
    assert events == ["prefill", "eval", *step, *step, "eval"]
    assert result["decode_calls"] == result["sampler_calls"] == result["host_stop_reads"] == 2
    assert result["final_audio_length"] == 4


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    pytest.importorskip("torch")
    pytest.importorskip("mlx.core")
    from tools.probes.gsv_semantic_reference import create_tiny_checkpoint
    from tts.semantic_mlx.weights import export_checkpoint

    folder = tmp_path_factory.mktemp("qualification-variants")
    checkpoint = create_tiny_checkpoint(folder / "tiny.ckpt", layers=1)
    artifact, config = export_checkpoint(checkpoint, folder / "cache", return_config=True)
    return checkpoint, artifact, config


@pytest.mark.mlx_cpu
@pytest.mark.parametrize("revision", ["audit", "unpadded"])
def test_bundled_snapshot_loader_uses_current_weight_reader_without_git(tiny, tmp_path, monkeypatch, revision):
    mx = pytest.importorskip("mlx.core")
    _, artifact, config = tiny
    target = tmp_path / "tools/probes/gsv_qualification_snapshots"
    shutil.copytree(variants.ROOT / "tools/probes/gsv_qualification_snapshots", target)
    monkeypatch.setattr(variants, "ROOT", tmp_path)
    monkeypatch.setattr(variants.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("Snapshot loader invoked Git"))
    variants._snapshot_module.cache_clear()
    decoder = variants.create_decoder(artifact, mlx_revision=revision, device=mx.cpu,
                                       validated_config=config)
    try:
        module = sys.modules[type(decoder.model).__module__]
        assert module.validate_artifact.__module__ == "tts.semantic_mlx.weights"
        assert module.__file__.startswith(str(tmp_path))
        assert decoder.info["source_kind"] == "frozen_snapshot"
    finally:
        decoder.close()
        variants._snapshot_module.cache_clear()


@pytest.mark.mlx_cpu
@pytest.mark.parametrize("revision,dtype", MLX_CASES)
def test_all_mlx_models_and_generation_run_through_existing_bridge(tiny, revision, dtype):
    import torch
    import mlx.core as mx
    from tools.probes.gsv_semantic_reference import synthetic_inputs, validate_numerics

    mx.set_default_device(mx.cpu)
    checkpoint, artifact, config = tiny
    data = synthetic_inputs(config, phone_length=2, prompt_length=2, steps=2)
    numerics = validate_numerics(
        checkpoint, artifact, data, atol=1e-4 if dtype == "float32" else 0.05,
        rtol=1e-4 if dtype == "float32" else 0.05, inference_dtype=dtype,
        model_loader=partial(variants.load_model, mlx_revision=revision))
    assert numerics["status"] == "passed", [row for row in numerics["rows"] if not row["passed"]]
    decoder = variants.create_decoder(artifact, mlx_revision=revision, inference_dtype=dtype,
                                       validated_config=config)
    try:
        arrays = [mx.array(data[key].astype(np.float32 if key == "bert" else np.int32))
                  for key in ("phones", "prompt", "bert")]
        generation = variants.generation_module(revision)
        assert decoder._generation_fn is generation.generate
        assert decoder.info["mlx_revision"] == revision
        assert decoder.info["dtype"] == dtype
        mx.random.seed(716)
        expected, expected_idx = generation.generate(decoder.model, *arrays, top_k=1, early_stop_num=1)
        mx.eval(expected)
        mx.random.seed(716)
        actual, idx = decoder.infer_panel(
            torch.from_numpy(data["phones"]), torch.tensor([2]),
            torch.from_numpy(data["prompt"]), torch.from_numpy(data["bert"]), top_k=1, early_stop_num=1)
        assert idx == expected_idx
        np.testing.assert_array_equal(actual.numpy(), np.asarray(expected))
    finally:
        decoder.close()


@pytest.mark.mlx_cpu
def test_default_generator_resolves_at_call_and_explicit_generator_isolated(monkeypatch):
    torch = pytest.importorskip("torch")
    mx = pytest.importorskip("mlx.core")
    from tts.semantic_mlx import runtime

    model = SimpleNamespace(inference_dtype="float32", head_dim=8, cache_head_dim=8,
                            config=SimpleNamespace(bert_dim=2, phoneme_vocab_size=4, eos=4))
    default = runtime.MLXSemanticDecoder(model, device=mx.cpu, purpose="numerical_test")
    custom = runtime.MLXSemanticDecoder(model, device=mx.cpu, purpose="numerical_test",
                                        generation_fn=lambda *_args, **_kwargs: (mx.array([[1, 1]]), 1))
    monkeypatch.setattr(runtime, "generate", lambda *_args, **_kwargs: (mx.array([[1, 2]]), 2))
    args = (torch.tensor([[1]]), torch.tensor([1]), torch.tensor([[1]]), torch.zeros((1, 2, 1)))
    try:
        result, idx = default.infer_panel(*args)
        assert idx == 2 and result.tolist() == [[1, 2]]
        result, idx = custom.infer_panel(*args)
        assert idx == 1 and result.tolist() == [[1, 1]]
    finally:
        default.close()
        custom.close()


def test_fixed_only_skips_free_generation_in_all_bench_backends(monkeypatch):
    pytest.importorskip("torch")
    pytest.importorskip("mlx.core")
    from tools.probes import gsv_semantic_reference as reference

    def forbidden(*_args, **_kwargs):
        pytest.fail("Fixed-only probe invoked free generation")

    decoder = SimpleNamespace(model=object(), info={"cache_impl": "test"},
                              infer_panel=forbidden, close=lambda: None)
    monkeypatch.setattr(variants, "create_decoder", lambda *_args, **_kwargs: decoder)
    monkeypatch.setattr(reference, "load_reference", lambda *_args, **_kwargs: SimpleNamespace(infer_panel=forbidden))
    monkeypatch.setattr(reference, "TorchTrace", lambda model, **_kwargs: SimpleNamespace(model=model))
    monkeypatch.setattr(probe, "_qualify_device", lambda _device: None)
    manifest = {"source_checkpoint_sha256": "source", "weights_sha256": "weights"}
    monkeypatch.setattr(probe, "_artifact", lambda _args: ("artifact", object(), manifest))
    inputs = {"phones": np.array([[1]]), "prompt": np.array([[1]]),
              "bert": np.zeros((1, 2, 1), dtype=np.float32), "history": np.array([[1, 2]])}
    monkeypatch.setattr(probe, "_inputs", lambda *_args: (inputs, {}))
    monkeypatch.setattr(probe, "_set_sampling_seed", lambda *_args, **_kwargs: None)
    fixed = {"prefill_ms": 1., "fixed_ar_work_ms": 2., "fixed_step_ms": [1., 1.],
             "decode_calls": 2, "sampler_calls": 2, "host_stop_reads": 2,
             "final_audio_length": 3, "final_drain_ms": 0., "sampled_ids_sha256": "sampled"}
    monkeypatch.setattr(probe, "_fixed_torch_work", lambda *_args: fixed)
    monkeypatch.setattr(probe, "_fixed_mlx_work", lambda *_args: fixed)
    monkeypatch.setattr(probe, "_save", lambda _args, data: data)
    for backend in ("torch", "mlx"):
        args = SimpleNamespace(backend=backend, device="cpu", inference_dtype="float32",
                               checkpoint="local.ckpt", mlx_revision="current", fixed_only=True,
                               runs=1, warmup=1, seed=10, top_k=1, top_p=1., temperature=.6,
                               repetition_penalty=1.35, budget=2)
        result = probe.bench(args)
        assert result["free_generation_outputs"] == []
        assert "free_generation_ms" not in result["timings_ms"]
        assert "free_generation_ms" not in result["raw_fixed_runs"][0]
        assert result["execution"]["fixed_only"] is True
        assert result["raw_fixed_runs"][0]["seed"] == 10
