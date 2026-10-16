"""Check that an installed environment matches an Amadeus release profile."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import os
import platform
import shutil
import subprocess
import sys


BASE_IMPORTS = (
    "aiohttp",
    "fastapi",
    "google.genai",
    "mcp",
    "numpy",
    "openai",
    "PIL",
    "playwright",
    "starlette",
    "uvicorn",
)
# T2 voice common layer (pyproject `[voice]` extra).
VOICE_IMPORTS = (
    "pyaudio",
    "aec_audio_processing",
    "av",
    "ffmpeg",
    "scipy",
    "soundfile",
)
# T3 realtime interruption layer (pyproject `[vad]` extra); pulls torch.
VAD_IMPORTS = (
    "silero_vad",
)
# Shared local-model capability, independent of the selected Torch build.
LOCAL_MODEL_IMPORTS = (
    "onnxruntime",
    "torch",
    "torchaudio",
)
PROJECT_IMPORTS = (
    "llm.gemini_client",
    "llm.client",
    "core.chat_runtime",
    "server.app",
)
MODEL_BACKEND_IMPORTS = (
    "ffmpeg",
    "qwen_asr",
    "local_tts_infer",
)
ROCM_IMPORTS = (*MODEL_BACKEND_IMPORTS, "torchvision")
# Verification ladder: each release profile is a strict superset of the one
# below it, mirroring the install tiers T1→T4 (core → voice → vad → embedded).
# A profile names a (tier, build) pair; see TIER_TAXONOMY.md for the contract.
PROFILE_TIER_IMPORTS: dict[str, tuple[str, ...]] = {
    "cpu": (),
    "ci": (),
    "voice": VOICE_IMPORTS,
    "vad": (*VOICE_IMPORTS, *VAD_IMPORTS),
    "vad-cpu": (*VOICE_IMPORTS, *VAD_IMPORTS),
    "cu124": (*VOICE_IMPORTS, *VAD_IMPORTS, *LOCAL_MODEL_IMPORTS, *MODEL_BACKEND_IMPORTS),
    "cu128": (*VOICE_IMPORTS, *VAD_IMPORTS, *LOCAL_MODEL_IMPORTS, *MODEL_BACKEND_IMPORTS),
    "mps": (*VOICE_IMPORTS, *VAD_IMPORTS, *LOCAL_MODEL_IMPORTS, *MODEL_BACKEND_IMPORTS),
    "t4-cpu": (*VOICE_IMPORTS, *VAD_IMPORTS, *LOCAL_MODEL_IMPORTS, *MODEL_BACKEND_IMPORTS),
    "rocm": (*VOICE_IMPORTS, *VAD_IMPORTS, *LOCAL_MODEL_IMPORTS, *ROCM_IMPORTS),
}


def _distribution_installed(name: str) -> bool:
    try:
        importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


# The cu124 local-model profile is qualified on Windows; ROCm remains an
# experimental Windows candidate. Capability imports and T3 vad checks do not
# imply a GPU device or platform qualification.
_WINDOWS_ONLY_PROFILES = frozenset({"cu124", "rocm"})


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def dependency_check_command(python: str) -> list[str]:
    """Check the selected interpreter, including existing pip-created runtimes."""
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "check", "--python", python]
    return [python, "-m", "pip", "check"]


def _verify_torch_build(
    build: str, *, require_cuda_device: bool = False, require_mps_device: bool = False
) -> str:
    import torch
    import torchaudio

    expected = {
        "cpu": "2.7.0", "mps": "2.7.0", "cu128": "2.7.0",
        "cu124": "2.6.0", "rocm": "2.9.1",
    }[build]
    expected_suffix = "rocm7.2.1" if build == "rocm" else build
    for name, module in (("torch", torch), ("torchaudio", torchaudio)):
        version = str(module.__version__)
        _require(version.split("+", 1)[0] == expected, f"expected {name} {expected}, found {version}")
        if platform.system() in {"Windows", "Linux"}:
            _require(
                version.endswith(f"+{expected_suffix}"),
                f"{name} {expected_suffix} wheel is required",
            )
    hip_version = getattr(torch.version, "hip", None)
    if build == "rocm":
        _require(str(hip_version or "").startswith("7.2"), "torch must target ROCm/HIP 7.2")
        _require(torch.version.cuda is None, "ROCm profile cannot use a CUDA build")
        if require_cuda_device:
            _require(torch.cuda.is_available(), "no usable ROCm/HIP device was detected")
    elif build in {"cpu", "mps"}:
        _require(not hip_version, f"{build} profile cannot use a ROCm build")
        _require(torch.version.cuda is None, f"{build} profile cannot use a CUDA build")
        if build == "mps":
            _require(torch.backends.mps.is_built(), "torch must include MPS support")
            if require_mps_device:
                _require(torch.backends.mps.is_available(), "no usable MPS device was detected")
    else:
        _require(not hip_version, f"{build} profile cannot use a ROCm build")
        cuda_version = {"cu124": "12.4", "cu128": "12.8"}[build]
        _require(str(torch.version.cuda) == cuda_version, f"torch must target CUDA {cuda_version}")
        if require_cuda_device:
            _require(torch.cuda.is_available(), "no usable CUDA device was detected")
    return f" torch={torch.__version__} torchaudio={torchaudio.__version__}"


def _detect_torch_build() -> str:
    """Name the locked build of the installed Torch; fail on an unlocked one.

    Torch only enters through a build extra; a Torch resolved some other way
    (for example `--extra vad` without a build) belongs to no tier.
    """
    import torch

    if getattr(torch.version, "hip", None):
        return "rocm"
    cuda_build = {"12.4": "cu124", "12.8": "cu128"}.get(str(torch.version.cuda))
    if cuda_build:
        return cuda_build
    _require(torch.version.cuda is None, f"unlocked CUDA {torch.version.cuda} torch build")
    # macOS has one wheel for both torch-cpu and local-mps; its version carries no suffix.
    if platform.system() == "Darwin" or str(torch.__version__).endswith("+cpu"):
        return "cpu"
    raise RuntimeError(
        f"torch {torch.__version__} is not a locked build; select torch-cpu or local-<build>"
    )


def verify(
    profile: str, *, require_cuda_device: bool = False, require_mps_device: bool = False
) -> None:
    _require(sys.version_info[:2] == (3, 12), "CPython 3.12 is required")
    if profile in _WINDOWS_ONLY_PROFILES:
        _require(platform.system() == "Windows", f"the {profile} local-model profile targets Windows")
    if profile == "cu128":
        _require(
            platform.system() in {"Windows", "Linux"}
            and platform.machine().lower() in {"amd64", "x86_64"},
            "the cu128 candidate targets Windows/Linux x86_64",
        )
    if profile == "mps":
        _require(
            platform.system() == "Darwin" and platform.machine() == "arm64",
            "the mps candidate targets Apple Silicon macOS",
        )
    _require(
        not require_cuda_device or profile in {"cu124", "cu128", "rocm"},
        "--require-cuda-device requires --profile cu124, cu128 or rocm",
    )
    _require(
        not require_mps_device or profile == "mps",
        "--require-mps-device requires --profile mps",
    )

    if profile in {"cpu", "ci"}:
        os.environ.setdefault("TTS_DEVICE", "cpu")

    for module_name in (*BASE_IMPORTS, *PROJECT_IMPORTS):
        importlib.import_module(module_name)
    for module_name in PROFILE_TIER_IMPORTS[profile]:
        importlib.import_module(module_name)

    _require(
        not _distribution_installed("google-generativeai"),
        "deprecated google-generativeai is installed; use google-genai only",
    )

    if profile in {"cu124", "cu128", "rocm", "mps", "vad", "vad-cpu", "t4-cpu"}:
        build = {"vad": None, "vad-cpu": "cpu", "t4-cpu": "cpu"}.get(profile, profile)
        torch_summary = _verify_torch_build(
            build or _detect_torch_build(),
            require_cuda_device=require_cuda_device,
            require_mps_device=require_mps_device,
        )
    else:
        torch_summary = ""

    subprocess.run(dependency_check_command(sys.executable), text=True, check=True)
    print(
        "environment ok: "
        f"profile={profile} python={platform.python_version()}"
        + torch_summary
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(PROFILE_TIER_IMPORTS), required=True)
    parser.add_argument(
        "--require-cuda-device",
        action="store_true",
        help="also require torch.cuda.is_available() for a CUDA or ROCm profile",
    )
    parser.add_argument(
        "--require-mps-device", action="store_true",
        help="also require torch.backends.mps.is_available() for the MPS candidate",
    )
    args = parser.parse_args()
    verify(
        args.profile, require_cuda_device=args.require_cuda_device,
        require_mps_device=args.require_mps_device,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
