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
    "scipy",
)
# L3 realtime interruption layer (pyproject `[vad]` extra); pulls torch.
VAD_IMPORTS = (
    "silero_vad",
)
# T2b local-model layer (pyproject `[local-cu124]` / `[local-rocm]` extras).
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
CU124_IMPORTS = (
    "ffmpeg",
    "qwen_asr",
    "local_tts_infer",
)
ROCM_IMPORTS = (*CU124_IMPORTS, "torchvision")
# Verification ladder: each release profile is a strict superset of the one
# below it, mirroring the install tiers L1→L4 (base → voice → vad → local-cu124).
PROFILE_TIER_IMPORTS: dict[str, tuple[str, ...]] = {
    "cpu": (),
    "ci": (),
    "voice": VOICE_IMPORTS,
    "vad": (*VOICE_IMPORTS, *VAD_IMPORTS),
    "vad-cpu": (*VOICE_IMPORTS, *VAD_IMPORTS),
    "cu124": (*VOICE_IMPORTS, *VAD_IMPORTS, *LOCAL_MODEL_IMPORTS, *CU124_IMPORTS),
    "rocm": (*VOICE_IMPORTS, *VAD_IMPORTS, *LOCAL_MODEL_IMPORTS, *ROCM_IMPORTS),
}


def _distribution_installed(name: str) -> bool:
    try:
        importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


# The cu124 local-model profile is qualified on Windows; ROCm remains an
# experimental Windows candidate. Capability imports and CPU VAD checks do not
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


def _verify_torch_build(build: str, *, require_cuda_device: bool = False) -> str:
    import torch
    import torchaudio

    expected = "2.9.1" if build == "rocm" else "2.5.1"
    expected_suffix = "rocm7.2.1" if build == "rocm" else build
    for name, module in (("torch", torch), ("torchaudio", torchaudio)):
        version = str(module.__version__)
        _require(version.split("+", 1)[0] == expected, f"expected {name} {expected}, found {version}")
        if platform.system() == "Windows":
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
    elif build == "cpu":
        _require(not hip_version, "CPU VAD profile cannot use a ROCm build")
        _require(torch.version.cuda is None, "CPU VAD requires a CPU-only torch build")
    else:
        _require(not hip_version, f"{build} profile cannot use a ROCm build")
        _require(str(torch.version.cuda) == "12.4", "torch must target CUDA 12.4")
        if require_cuda_device:
            _require(torch.cuda.is_available(), "no usable CUDA device was detected")
    return f" torch={torch.__version__} torchaudio={torchaudio.__version__}"


def verify(profile: str, *, require_cuda_device: bool = False) -> None:
    _require(sys.version_info[:2] == (3, 12), "CPython 3.12 is required")
    if profile in _WINDOWS_ONLY_PROFILES:
        _require(platform.system() == "Windows", f"the {profile} local-model profile targets Windows")
    _require(
        not require_cuda_device or profile in {"cu124", "rocm"},
        "--require-cuda-device requires --profile cu124 or rocm",
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

    if profile == "cu124":
        torch_summary = _verify_torch_build("cu124", require_cuda_device=require_cuda_device)
    elif profile == "rocm":
        torch_summary = _verify_torch_build("rocm", require_cuda_device=require_cuda_device)
    elif profile == "vad-cpu":
        torch_summary = _verify_torch_build("cpu")
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
        help="also require torch.cuda.is_available() for the cu124 or ROCm profile",
    )
    args = parser.parse_args()
    verify(args.profile, require_cuda_device=args.require_cuda_device)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
