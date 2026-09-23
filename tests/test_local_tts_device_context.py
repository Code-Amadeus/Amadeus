from __future__ import annotations

from contextlib import AbstractContextManager

import pytest


torch = pytest.importorskip("torch", reason="test requires the local-model tier")
pytest.importorskip("librosa", reason="test requires the local-model tier")

from local_tts_infer import (
    TTSInferencer,
    _allows_nvidia_cuda_extensions,
    _uses_torch_cuda_device_api,
)


def _inferencer(device: str, *, uses_torch_cuda_api: bool) -> TTSInferencer:
    inferencer = TTSInferencer.__new__(TTSInferencer)
    inferencer.device = device
    inferencer._uses_torch_cuda_api = uses_torch_cuda_api
    inferencer._allows_nvidia_cuda_extensions = uses_torch_cuda_api
    inferencer._tts_device_idx = 0
    return inferencer


def test_unavailable_cuda_device_fails_before_model_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    with pytest.raises(RuntimeError, match="requires a usable PyTorch CUDA/HIP device"):
        TTSInferencer(device="cuda:0")


def test_unavailable_mps_device_fails_before_model_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)

    with pytest.raises(RuntimeError, match="requires an available PyTorch MPS backend"):
        TTSInferencer(device="mps")


def test_cpu_device_context_never_enters_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    inferencer = _inferencer("cpu", uses_torch_cuda_api=False)
    monkeypatch.setattr(
        torch.cuda,
        "device",
        lambda *_args, **_kwargs: pytest.fail("CPU inference entered a CUDA context"),
    )

    context = inferencer._device_context()
    assert isinstance(context, AbstractContextManager)
    with context:
        pass


def test_cpu_synchronization_never_calls_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    inferencer = _inferencer("cpu", uses_torch_cuda_api=False)
    monkeypatch.setattr(
        torch.cuda,
        "synchronize",
        lambda *_args, **_kwargs: pytest.fail("CPU inference synchronized CUDA"),
    )

    inferencer._synchronize_device()


def test_mps_synchronization_uses_mps(monkeypatch: pytest.MonkeyPatch) -> None:
    inferencer = _inferencer("mps", uses_torch_cuda_api=False)
    calls: list[str] = []
    monkeypatch.setattr(torch.mps, "synchronize", lambda: calls.append("mps"))
    monkeypatch.setattr(
        torch.cuda,
        "synchronize",
        lambda *_args, **_kwargs: pytest.fail("MPS inference synchronized CUDA"),
    )

    inferencer._synchronize_device()

    assert calls == ["mps"]


def test_cuda_context_preserves_selected_device(monkeypatch: pytest.MonkeyPatch) -> None:
    inferencer = _inferencer("cuda:2", uses_torch_cuda_api=True)
    inferencer._tts_device_idx = 2
    entered: list[int] = []

    class _FakeContext:
        def __enter__(self):
            entered.append(2)

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(torch.cuda, "device", lambda index: _FakeContext())

    with inferencer._device_context():
        pass

    assert entered == [2]


def test_rocm_uses_torch_cuda_api_without_enabling_nvidia_extensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.version, "hip", "6.2")

    uses_torch_cuda_api = _uses_torch_cuda_device_api("cuda:0")

    assert uses_torch_cuda_api is True
    assert _allows_nvidia_cuda_extensions(uses_torch_cuda_api) is False


def test_rocm_stream_vocoder_reuses_final_chunk_shape() -> None:
    inferencer = _inferencer("cpu", uses_torch_cuda_api=False)
    shapes = []

    class Vocoder:
        def __call__(self, mel):
            shapes.append(tuple(mel.shape))
            return torch.ones((1, 1, mel.shape[-1] * 4)),

    inferencer.bigvgan_model = Vocoder()
    first = inferencer._run_bigvgan_stream_chunk(torch.ones((1, 100, 8)), target_frames=8)
    last = inferencer._run_bigvgan_stream_chunk(torch.ones((1, 100, 3)), target_frames=8)

    assert shapes == [(1, 100, 8), (1, 100, 8)]
    assert first.shape[-1] == 32
    assert last.shape[-1] == 12


def test_rocm_cfm_omits_only_unneeded_padding_masks(monkeypatch: pytest.MonkeyPatch) -> None:
    from GPT_SoVITS.module.models import CFM

    observed = []

    class Estimator(torch.nn.Module):
        def forward(self, x, *_args, use_padding_mask=True, **_kwargs):
            observed.append(use_padding_mask)
            return x.transpose(2, 1)

    monkeypatch.setattr(torch.version, "hip", "7.2")
    cfm = CFM(2, Estimator())
    mu = torch.zeros((1, 4, 2))
    prompt = torch.zeros((1, 2, 1))
    for length in (4, 3):
        cfm.inference(mu, torch.tensor([length]), prompt, n_timesteps=2)

    monkeypatch.setattr(torch.version, "hip", None)
    cfm.inference(mu, torch.tensor([4]), prompt, n_timesteps=2)

    assert observed == [False, False, True, True, True, True]


def test_unpadded_dit_output_matches_full_mask() -> None:
    from GPT_SoVITS.f5_tts.model.backbones.dit import DiT

    torch.manual_seed(1)
    model = DiT(
        dim=16, depth=1, heads=2, dim_head=8,
        mel_dim=4, text_dim=4, dropout=0,
    ).eval()
    noise = torch.randn(1, 4, 5)
    condition = torch.randn(1, 4, 5)
    text = torch.randn(1, 4, 5)
    arguments = (
        noise, condition, torch.tensor([5]),
        torch.tensor([0.5]), torch.tensor([0.1]), text,
    )
    with torch.inference_mode():
        masked = model(*arguments, use_padding_mask=True)
        unmasked = model(*arguments, use_padding_mask=False)

    torch.testing.assert_close(unmasked, masked)


def test_nvidia_cuda_device_allows_nvidia_extensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.version, "hip", None)

    uses_torch_cuda_api = _uses_torch_cuda_device_api("cuda:0")

    assert uses_torch_cuda_api is True
    assert _allows_nvidia_cuda_extensions(uses_torch_cuda_api) is True
