"""Selected install capabilities and Torch builds must agree with the uv lock."""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
UV = shutil.which("uv")


def _names(requirements: list[str]) -> set[str]:
    return {Requirement(value).name.lower() for value in requirements}


def test_capability_declarations_do_not_choose_a_gpu_build() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    assert not _names(project["dependencies"]) & {
        "torch", "torchaudio", "pyaudio", "silero-vad", "onnxruntime",
    }
    assert "pyaudio" in _names(extras["voice"])
    assert not _names(extras["voice"]) & {"torch", "torchaudio", "silero-vad"}
    assert "silero-vad" in _names(extras["vad"])
    assert "torch" not in _names(extras["vad"])
    for build in ("torch-cpu", "local-cu124", "local-rocm"):
        assert {"torch", "torchaudio"} <= _names(extras[build])
    assert {"torchvision", "rocm", "rocm-sdk-core"} <= _names(extras["local-rocm"])
    assert all("sys_platform == 'win32'" in item for item in extras["local-rocm"])


def _export(*extras: str) -> subprocess.CompletedProcess[str]:
    command = [UV, "export", "--locked", "--no-hashes", "--no-emit-project"]
    for extra in extras:
        command.extend(("--extra", extra))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)


def _selected_requirements(output: str, platform: str) -> dict[str, Requirement]:
    environment = {**default_environment(), "sys_platform": platform}
    requirements = {}
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "--")):
            continue
        requirement = Requirement(line)
        if requirement.marker is None or requirement.marker.evaluate(environment):
            requirements[requirement.name] = requirement
    return requirements


@pytest.mark.skipif(UV is None, reason="uv is required to select lock branches")
@pytest.mark.parametrize("extras", [(), ("voice",), ("dev",), ("voice", "dev")])
def test_core_and_voice_resolutions_remain_model_free(extras: tuple[str, ...]) -> None:
    result = _export(*extras)
    assert result.returncode == 0, result.stderr
    for platform in ("win32", "darwin"):
        selected = _selected_requirements(result.stdout, platform)
        assert "aiohttp" in selected
        assert not selected.keys() & {"torch", "torchaudio", "silero-vad", "onnxruntime"}
        assert ("pyaudio" in selected) == ("voice" in extras)


@pytest.mark.skipif(UV is None, reason="uv is required to select lock branches")
@pytest.mark.parametrize(
    "build,version",
    [("torch-cpu", "2.5.1+cpu"), ("local-cu124", "2.5.1+cu124")],
)
def test_windows_index_torch_selection_matches_the_requested_build(
    build: str, version: str
) -> None:
    result = _export("voice", "vad", build)
    assert result.returncode == 0, result.stderr
    selected = _selected_requirements(result.stdout, "win32")
    assert "silero-vad" in selected
    for name in ("torch", "torchaudio"):
        assert str(selected[name].specifier) == f"=={version}"
    macos = _selected_requirements(result.stdout, "darwin")
    assert "+cu124" not in str(macos["torch"].specifier)


@pytest.mark.skipif(UV is None, reason="uv is required to select lock branches")
def test_windows_rocm_selection_uses_only_the_fixed_amd_wheels() -> None:
    result = _export("voice", "vad", "local-rocm")
    assert result.returncode == 0, result.stderr
    selected = _selected_requirements(result.stdout, "win32")
    expected = {
        "torch": "torch-2.9.1%2Brocm7.2.1",
        "torchaudio": "torchaudio-2.9.1%2Brocm7.2.1",
        "torchvision": "torchvision-0.24.1%2Brocm7.2.1",
    }
    for name, wheel in expected.items():
        assert selected[name].url is not None
        assert selected[name].url.startswith("https://repo.radeon.com/rocm/windows/")
        assert wheel in selected[name].url
    assert selected["rocm"].url is not None
    assert "rocm-7.2.1" in selected["rocm"].url


@pytest.mark.skipif(UV is None, reason="uv is required to check conflicting selections")
@pytest.mark.parametrize(
    "left,right",
    [
        ("torch-cpu", "local-cu124"),
        ("torch-cpu", "local-rocm"),
        ("local-cu124", "local-rocm"),
    ],
)
def test_torch_builds_cannot_be_selected_together(left: str, right: str) -> None:
    result = _export(left, right)
    assert result.returncode != 0
    assert left in result.stderr and right in result.stderr


def test_verify_profiles_cover_the_capability_ladder() -> None:
    from tools import verify_python_environment as vpe

    ladder = vpe.PROFILE_TIER_IMPORTS
    chain = [set(ladder[name]) for name in ("cpu", "voice", "vad", "cu124")]
    assert all(lower < upper for lower, upper in zip(chain, chain[1:]))
    assert ladder["vad-cpu"] == ladder["vad"]
    assert set(vpe.LOCAL_MODEL_IMPORTS) <= set(ladder["cu124"]) - set(ladder["vad"])
    assert set(ladder["rocm"]) == set(ladder["cu124"]) | {"torchvision"}


@pytest.mark.skipif(UV is None, reason="uv is required for lock consistency")
def test_uv_lock_is_consistent_with_pyproject() -> None:
    result = subprocess.run([UV, "lock", "--check"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
