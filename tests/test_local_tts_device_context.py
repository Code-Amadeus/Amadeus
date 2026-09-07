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


def test_nvidia_cuda_device_allows_nvidia_extensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.version, "hip", None)

    uses_torch_cuda_api = _uses_torch_cuda_device_api("cuda:0")

    assert uses_torch_cuda_api is True
    assert _allows_nvidia_cuda_extensions(uses_torch_cuda_api) is True
