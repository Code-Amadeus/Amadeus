from __future__ import annotations

from pathlib import Path

import pytest


torch = pytest.importorskip("torch", reason="test requires the local-model tier")

from GPT_SoVITS.process_ckpt import HParams, load_sovits_new


def _write_checkpoint(path: Path) -> bytes:
    payload = {
        "weight": {"fixture": torch.tensor([1.0])},
        "config": HParams(data={"sampling_rate": 24000}),
    }
    torch.save(payload, path)
    return path.read_bytes()


def test_safe_loader_accepts_standard_checkpoint(tmp_path: Path) -> None:
    checkpoint = tmp_path / "standard.pth"
    _write_checkpoint(checkpoint)

    loaded = load_sovits_new(checkpoint)

    assert loaded["config"].data.sampling_rate == 24000
    assert loaded["weight"]["fixture"].item() == 1.0


def test_safe_loader_restores_gpt_sovits_version_prefix(tmp_path: Path) -> None:
    standard = tmp_path / "standard.pth"
    data = _write_checkpoint(standard)
    prefixed = tmp_path / "prefixed.pth"
    prefixed.write_bytes(b"02" + data[2:])
    loaded = load_sovits_new(prefixed)
    assert loaded["config"].data.sampling_rate == 24000


def test_version_detection_supports_v2pro_and_v2proplus(tmp_path: Path) -> None:
    from GPT_SoVITS.process_ckpt import get_sovits_version_from_path_fast

    v2pro = tmp_path / "model_v2pro.pth"
    v2pro.write_bytes(b"05" + b"\x00" * 20)
    assert get_sovits_version_from_path_fast(v2pro) == ["v2", "v2Pro", False]

    v2proplus = tmp_path / "model_v2proplus.pth"
    v2proplus.write_bytes(b"06" + b"\x00" * 20)
    assert get_sovits_version_from_path_fast(v2proplus) == ["v2", "v2ProPlus", False]


def test_synthesizer_trn_initializes_v2proplus_projection_layers() -> None:
    from GPT_SoVITS.module.models import SynthesizerTrn

    model = SynthesizerTrn(
        spec_channels=1025,
        segment_size=32,
        inter_channels=192,
        hidden_channels=192,
        filter_channels=768,
        n_heads=2,
        n_layers=6,
        kernel_size=3,
        p_dropout=0.0,
        resblock="1",
        resblock_kernel_sizes=[3, 7, 11],
        resblock_dilation_sizes=[[1, 3, 5], [1, 3, 5], [1, 3, 5]],
        upsample_rates=[10, 8, 2, 2, 2],
        upsample_initial_channel=768,
        upsample_kernel_sizes=[20, 16, 8, 2, 2],
        gin_channels=1024,
        semantic_frame_rate="25hz",
        version="v2ProPlus",
    )
    assert model.is_v2pro is True
    assert hasattr(model, "ge_to512")
    assert hasattr(model, "sv_emb")
    assert hasattr(model, "prelu")
    assert model.ge_to512.in_features == 1024
    assert model.ge_to512.out_features == 512


def test_synthesizer_trn_v2proplus_decode_accepts_sv_emb() -> None:
    from GPT_SoVITS.module.models import SynthesizerTrn

    model = SynthesizerTrn(
        spec_channels=1025,
        segment_size=32,
        inter_channels=192,
        hidden_channels=192,
        filter_channels=768,
        n_heads=2,
        n_layers=6,
        kernel_size=3,
        p_dropout=0.0,
        resblock="1",
        resblock_kernel_sizes=[3, 7, 11],
        resblock_dilation_sizes=[[1, 3, 5], [1, 3, 5], [1, 3, 5]],
        upsample_rates=[10, 8, 2, 2, 2],
        upsample_initial_channel=768,
        upsample_kernel_sizes=[20, 16, 8, 2, 2],
        gin_channels=1024,
        semantic_frame_rate="25hz",
        version="v2ProPlus",
    )
    codes = torch.randint(0, 1024, (1, 1, 10))
    text = torch.randint(0, 100, (1, 5))
    refer = torch.randn(1, 1025, 20)
    sv_emb = [torch.randn(1, 20480)]

    audio = model.decode(codes, text, refer, sv_emb=sv_emb)
    assert audio.shape[0] == 1
    assert audio.ndim == 3


