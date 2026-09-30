"""A/B identity and quality accounting without loading private voice assets."""
from __future__ import annotations

import numpy as np
import pytest
from types import SimpleNamespace

from tools.probes.gsv_ab_acceptance import (
    audio_metrics, backend_order, controlled_comparison, verify_pair_identity,
)
from tools.probes.gsv_backend_probe import _validate_probe_mlx_cpu


def _identity(backend, *, controlled=True):
    return {
        "backend": backend,
        "semantic": {"semantic_backend": backend},
        "semantic_dtype": "float32",
        "acoustic_device": "cpu",
        "acoustic_dtype": "float32",
        "candidate_sha": "commit",
        "working_tree_dirty": True,
        "code_sha256": "code",
        "controlled_acoustic_rng": controlled,
        "source_checkpoint_sha256": "gpt",
        "sovits_checkpoint_sha256": "sovits",
        "reference_audio_sha256": "reference",
        "text_sha256": "text",
        "prompt_text_sha256": "prompt",
        "parameters": {"top_k": 1, "sample_steps": 4, "speed": 1.1},
    }


def test_backend_order_alternates_within_each_case():
    assert backend_order(0) == ("torch", "mlx")
    assert backend_order(1) == ("mlx", "torch")
    assert backend_order(2) == ("torch", "mlx")


def test_probe_only_mlx_cpu_requires_explicit_backend_and_device():
    _validate_probe_mlx_cpu(SimpleNamespace(probe_mlx_cpu=True, backend="mlx", acoustic_device="cpu"))
    for backend, device in (("torch", "cpu"), ("mlx", "mps"), ("mlx", "")):
        with pytest.raises(ValueError, match="requires --backend mlx"):
            _validate_probe_mlx_cpu(SimpleNamespace(
                probe_mlx_cpu=True, backend=backend, acoustic_device=device))
    _validate_probe_mlx_cpu(SimpleNamespace(probe_mlx_cpu=False, backend="mlx", acoustic_device="mps"))


def test_pair_rejects_changed_reference_or_acoustic_settings():
    torch = _identity("torch")
    mlx = _identity("mlx")
    expected = {key: torch[key] for key in (
        "source_checkpoint_sha256", "sovits_checkpoint_sha256", "reference_audio_sha256",
        "acoustic_device", "acoustic_dtype", "candidate_sha", "working_tree_dirty", "code_sha256")}
    verify_pair_identity(torch, mlx, expected)
    mlx["reference_audio_sha256"] = "another reference"
    with pytest.raises(ValueError, match="different source"):
        verify_pair_identity(torch, mlx, expected)
    mlx["reference_audio_sha256"] = "reference"
    mlx["parameters"]["sample_steps"] = 16
    with pytest.raises(ValueError, match="different generation"):
        verify_pair_identity(torch, mlx, expected)


def test_pair_rejects_mislabeled_backend_and_changed_code():
    torch, mlx = _identity("torch"), _identity("mlx")
    expected = {key: torch[key] for key in (
        "source_checkpoint_sha256", "sovits_checkpoint_sha256", "reference_audio_sha256",
        "acoustic_device", "acoustic_dtype", "candidate_sha", "working_tree_dirty", "code_sha256")}
    mlx["semantic"]["semantic_backend"] = "torch"
    with pytest.raises(ValueError, match="different semantic backend"):
        verify_pair_identity(torch, mlx, expected)
    mlx["semantic"]["semantic_backend"] = "mlx"
    mlx["code_sha256"] = "changed"
    with pytest.raises(ValueError, match="code_sha256"):
        verify_pair_identity(torch, mlx, expected)
    mlx["code_sha256"] = "code"
    mlx["acoustic_dtype"] = "float16"
    with pytest.raises(ValueError, match="acoustic dtype"):
        verify_pair_identity(torch, mlx, expected)


def test_pair_accepts_requested_fp16_candidate_and_rejects_variant_mislabel():
    torch, mlx = _identity("torch"), _identity("mlx")
    expected = {key: torch[key] for key in (
        "source_checkpoint_sha256", "sovits_checkpoint_sha256", "reference_audio_sha256",
        "acoustic_device", "acoustic_dtype", "candidate_sha", "working_tree_dirty", "code_sha256")}
    expected.update(mlx_dtype="float16")
    mlx["semantic_dtype"] = "float16"
    verify_pair_identity(torch, mlx, expected)
    mlx["semantic_dtype"] = "float32"
    with pytest.raises(ValueError, match="semantic dtype differs"):
        verify_pair_identity(torch, mlx, expected)


def test_controlled_audio_only_compares_waveforms_after_semantic_and_rng_match(tmp_path):
    sf = pytest.importorskip("soundfile")
    signal = np.sin(np.arange(512) * 0.04).astype(np.float32) * 0.2
    torch_wav, mlx_wav = tmp_path / "torch.wav", tmp_path / "mlx.wav"
    sf.write(torch_wav, signal, 24000)
    sf.write(mlx_wav, signal, 24000)
    segments = [{"tokens": [1, 2, 3], "budget_boundary_reached": False}]
    torch, mlx = _identity("torch"), _identity("mlx")
    matched = controlled_comparison(torch_wav, mlx_wav, segments, segments, torch, mlx)
    assert matched["semantic_ids_match"] is True
    assert matched["bit_identical"] is True and matched["max_abs"] == 0
    divergent = controlled_comparison(
        torch_wav, mlx_wav, segments, [{"tokens": [1, 2, 4]}], torch, mlx)
    assert divergent["waveform_comparison"] == "not_applicable_semantic_divergence"
    mlx["controlled_acoustic_rng"] = False
    with pytest.raises(ValueError, match="isolated acoustic RNG"):
        controlled_comparison(torch_wav, mlx_wav, segments, segments, torch, mlx)


def test_quality_checks_report_silence_clip_budget_and_repetition(tmp_path):
    sf = pytest.importorskip("soundfile")
    wav = tmp_path / "pathological.wav"
    samples = np.zeros(24000, dtype=np.float32)
    samples[100:200] = 1.0
    sf.write(wav, samples, 24000)
    metrics = audio_metrics(wav, [{"tokens": [7] * 25, "budget_boundary_reached": True}])
    assert set(metrics["quality_flags"]) >= {
        "clipping", "semantic_budget_boundary", "semantic_repetition_run"
    }
    assert metrics["leading_silence_s"] > 0
