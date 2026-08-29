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
    "onnxruntime",
    "openai",
    "PIL",
    "playwright",
    "pyaudio",
    "starlette",
    "torch",
    "torchaudio",
    "uvicorn",
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
    if profile == "cu124":
        for module_name in CU124_IMPORTS:
            importlib.import_module(module_name)

    import torch
    import torchaudio

    _require(
        not _distribution_installed("google-generativeai"),
        "deprecated google-generativeai is installed; use google-genai only",
    )
    _require(
        str(torch.__version__).startswith("2.5.1"),
        f"expected torch 2.5.1, found {torch.__version__}",
    )
    _require(
        str(torchaudio.__version__).startswith("2.5.1"),
        f"expected torchaudio 2.5.1, found {torchaudio.__version__}",
    )

    if profile in {"cpu", "ci"}:
        _require(str(torch.__version__).endswith("+cpu"), "CPU torch wheel is required")
        _require(torch.version.cuda is None, "CPU profile unexpectedly exposes CUDA")
    else:
        _require(
            str(torch.__version__).endswith("+cu124"),
            "cu124 torch wheel is required",
        )
        _require(str(torch.version.cuda) == "12.4", "torch must target CUDA 12.4")
        if require_cuda_device:
            _require(torch.cuda.is_available(), "no usable CUDA device was detected")

    subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        text=True,
        check=True,
    )
    print(
        "environment ok: "
        f"profile={profile} python={platform.python_version()} "
        f"torch={torch.__version__} torchaudio={torchaudio.__version__}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("cpu", "ci", "cu124"), required=True)
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
