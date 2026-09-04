from __future__ import annotations

from contextlib import AbstractContextManager

import pytest


torch = pytest.importorskip("torch", reason="test requires the local-model tier")
pytest.importorskip("librosa", reason="test requires the local-model tier")

from local_tts_infer import TTSInferencer


def _inferencer(device: str, *, is_cuda: bool) -> TTSInferencer:
    inferencer = TTSInferencer.__new__(TTSInferencer)
    inferencer.device = device
    inferencer._is_cuda = is_cuda
    inferencer._tts_device_idx = 0
    return inferencer


def test_unavailable_cuda_device_fails_before_model_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    with pytest.raises(RuntimeError, match="requires CUDA"):
        TTSInferencer(device="cuda:0")


def test_unavailable_mps_device_fails_before_model_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)

    with pytest.raises(RuntimeError, match="requires an available PyTorch MPS backend"):
        TTSInferencer(device="mps")


def test_cpu_device_context_never_enters_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    inferencer = _inferencer("cpu", is_cuda=False)
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
    inferencer = _inferencer("cpu", is_cuda=False)
    monkeypatch.setattr(
        torch.cuda,
        "synchronize",
        lambda *_args, **_kwargs: pytest.fail("CPU inference synchronized CUDA"),
    )

    inferencer._synchronize_device()


def test_mps_synchronization_uses_mps(monkeypatch: pytest.MonkeyPatch) -> None:
    inferencer = _inferencer("mps", is_cuda=False)
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
    inferencer = _inferencer("cuda:2", is_cuda=True)
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
