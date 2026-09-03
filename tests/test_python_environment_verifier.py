from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from tools import verify_python_environment as verifier


def _torch(monkeypatch, version: str, audio_version: str, *, cuda=None, hip=None) -> None:
    monkeypatch.setattr(verifier.platform, "system", lambda: "Windows")
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        __version__=version, version=SimpleNamespace(cuda=cuda, hip=hip),
        cuda=SimpleNamespace(is_available=lambda: False),
    ))
    monkeypatch.setitem(sys.modules, "torchaudio", SimpleNamespace(__version__=audio_version))


def test_cpu_vad_requires_cpu_builds_of_both_torch_packages(monkeypatch) -> None:
    _torch(monkeypatch, "2.5.1+cpu", "2.5.1+cpu")
    verifier._verify_torch_build("cpu")
    _torch(monkeypatch, "2.5.1+cpu", "2.5.1+cu124")
    with pytest.raises(RuntimeError, match="torchaudio cpu"):
        verifier._verify_torch_build("cpu")


def test_cuda_build_check_does_not_claim_device_availability(monkeypatch) -> None:
    _torch(monkeypatch, "2.5.1+cu124", "2.5.1+cu124", cuda="12.4")
    verifier._verify_torch_build("cu124")
    with pytest.raises(RuntimeError, match="no usable CUDA device"):
        verifier._verify_torch_build("cu124", require_cuda_device=True)


def test_build_version_match_is_exact(monkeypatch) -> None:
    _torch(monkeypatch, "2.5.10+cu124", "2.5.1+cu124", cuda="12.4")
    with pytest.raises(RuntimeError, match="expected torch 2.5.1"):
        verifier._verify_torch_build("cu124")


def test_dependency_check_targets_the_requested_interpreter(monkeypatch) -> None:
    monkeypatch.setattr(verifier.shutil, "which", lambda _: "uv.exe")
    assert verifier.dependency_check_command("selected/python.exe") == [
        "uv.exe", "pip", "check", "--python", "selected/python.exe",
    ]
    monkeypatch.setattr(verifier.shutil, "which", lambda _: None)
    assert verifier.dependency_check_command("selected/python.exe") == [
        "selected/python.exe", "-m", "pip", "check",
    ]
