from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from tools import verify_python_environment as verifier


def _torch(
    monkeypatch,
    version: str,
    audio_version: str,
    *,
    cuda=None,
    hip=None,
    available: bool = False,
) -> None:
    monkeypatch.setattr(verifier.platform, "system", lambda: "Windows")
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        __version__=version, version=SimpleNamespace(cuda=cuda, hip=hip),
        cuda=SimpleNamespace(is_available=lambda: available),
    ))
    monkeypatch.setitem(sys.modules, "torchaudio", SimpleNamespace(__version__=audio_version))


def test_cpu_vad_requires_cpu_builds_of_both_torch_packages(monkeypatch) -> None:
    _torch(monkeypatch, "2.7.0+cpu", "2.7.0+cpu")
    verifier._verify_torch_build("cpu")
    _torch(monkeypatch, "2.7.0+cpu", "2.7.0+cu128")
    with pytest.raises(RuntimeError, match="torchaudio cpu"):
        verifier._verify_torch_build("cpu")


def test_cuda_build_check_does_not_claim_device_availability(monkeypatch) -> None:
    _torch(monkeypatch, "2.6.0+cu124", "2.6.0+cu124", cuda="12.4")
    verifier._verify_torch_build("cu124")
    with pytest.raises(RuntimeError, match="no usable CUDA device"):
        verifier._verify_torch_build("cu124", require_cuda_device=True)


def test_build_version_match_is_exact(monkeypatch) -> None:
    _torch(monkeypatch, "2.6.10+cu124", "2.6.0+cu124", cuda="12.4")
    with pytest.raises(RuntimeError, match="expected torch 2.6.0"):
        verifier._verify_torch_build("cu124")


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_cu128_candidate_checks_pair_and_cuda_runtime(monkeypatch, system) -> None:
    _torch(monkeypatch, "2.7.0+cu128", "2.7.0+cu128", cuda="12.8")
    monkeypatch.setattr(verifier.platform, "system", lambda: system)
    verifier._verify_torch_build("cu128")
    with pytest.raises(RuntimeError, match="no usable CUDA device"):
        verifier._verify_torch_build("cu128", require_cuda_device=True)
    sys.modules["torch"].version.cuda = "12.6"
    with pytest.raises(RuntimeError, match="CUDA 12.8"):
        verifier._verify_torch_build("cu128")


def test_mps_build_and_device_are_separate_evidence(monkeypatch) -> None:
    _torch(monkeypatch, "2.7.0", "2.7.0")
    monkeypatch.setattr(verifier.platform, "system", lambda: "Darwin")
    sys.modules["torch"].backends = SimpleNamespace(mps=SimpleNamespace(
        is_built=lambda: True, is_available=lambda: False,
    ))
    verifier._verify_torch_build("mps")
    with pytest.raises(RuntimeError, match="no usable MPS device"):
        verifier._verify_torch_build("mps", require_mps_device=True)
    sys.modules["torch"].backends.mps.is_built = lambda: False
    with pytest.raises(RuntimeError, match="include MPS support"):
        verifier._verify_torch_build("mps")


@pytest.mark.parametrize("profile,system,machine", [
    ("mps", "Windows", "AMD64"), ("mps", "Darwin", "x86_64"),
    ("cu128", "Darwin", "arm64"), ("cu128", "Linux", "aarch64"),
])
def test_candidate_rejects_wrong_platform_before_imports(monkeypatch, profile, system, machine):
    monkeypatch.setattr(verifier.platform, "system", lambda: system)
    monkeypatch.setattr(verifier.platform, "machine", lambda: machine)
    with pytest.raises(RuntimeError, match="candidate targets"):
        verifier.verify(profile)


def test_device_check_flags_cannot_be_used_with_unrelated_profile():
    with pytest.raises(RuntimeError, match="--require-mps-device requires"):
        verifier.verify("cpu", require_mps_device=True)
    with pytest.raises(RuntimeError, match="--require-cuda-device requires"):
        verifier.verify("voice", require_cuda_device=True)


def test_rocm_build_requires_the_fixed_hip_pair(monkeypatch) -> None:
    _torch(
        monkeypatch,
        "2.9.1+rocm7.2.1",
        "2.9.1+rocm7.2.1",
        hip="7.2.53211",
        available=True,
    )
    verifier._verify_torch_build("rocm", require_cuda_device=True)

    _torch(monkeypatch, "2.9.1+rocm7.2.1", "2.9.1+rocm7.2.1", cuda="12.8")
    with pytest.raises(RuntimeError, match="ROCm/HIP 7.2"):
        verifier._verify_torch_build("rocm")


def test_dependency_check_targets_the_requested_interpreter(monkeypatch) -> None:
    monkeypatch.setattr(verifier.shutil, "which", lambda _: "uv.exe")
    assert verifier.dependency_check_command("selected/python.exe") == [
        "uv.exe", "pip", "check", "--python", "selected/python.exe",
    ]
    monkeypatch.setattr(verifier.shutil, "which", lambda _: None)
    assert verifier.dependency_check_command("selected/python.exe") == [
        "selected/python.exe", "-m", "pip", "check",
    ]
