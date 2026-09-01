"""Check that an installed environment matches an Amadeus release profile."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import os
import platform
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
# T2b local-model layer (pyproject `[local-cu124]` extra).
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
# Verification ladder: each release profile is a strict superset of the one
# below it, mirroring the install tiers L1→L4 (base → voice → vad → local-cu124).
PROFILE_TIER_IMPORTS: dict[str, tuple[str, ...]] = {
    "cpu": (),
    "ci": (),
    "voice": VOICE_IMPORTS,
    "vad": (*VOICE_IMPORTS, *VAD_IMPORTS),
    "cu124": (*VOICE_IMPORTS, *VAD_IMPORTS, *LOCAL_MODEL_IMPORTS, *CU124_IMPORTS),
}


def _distribution_installed(name: str) -> bool:
    try:
        importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def verify(profile: str, *, require_cuda_device: bool = False) -> None:
    _require(sys.version_info[:2] == (3, 12), "CPython 3.12 is required")
    _require(platform.system() == "Windows", "the release profiles target Windows")

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
        import torch
        import torchaudio

        _require(
            str(torch.__version__).startswith("2.5.1"),
            f"expected torch 2.5.1, found {torch.__version__}",
        )
        _require(
            str(torchaudio.__version__).startswith("2.5.1"),
            f"expected torchaudio 2.5.1, found {torchaudio.__version__}",
        )
        _require(
            str(torch.__version__).endswith("+cu124"),
            "cu124 torch wheel is required",
        )
        _require(str(torch.version.cuda) == "12.4", "torch must target CUDA 12.4")
        if require_cuda_device:
            _require(torch.cuda.is_available(), "no usable CUDA device was detected")
        torch_summary = f" torch={torch.__version__} torchaudio={torchaudio.__version__}"
    else:
        torch_summary = ""

    subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        text=True,
        check=True,
    )
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
        help="also require torch.cuda.is_available() for the cu124 profile",
    )
    args = parser.parse_args()
    verify(args.profile, require_cuda_device=args.require_cuda_device)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
