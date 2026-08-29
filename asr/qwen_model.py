"""Resolve the local Qwen3-ASR model without network access at runtime."""

from __future__ import annotations

import os
from pathlib import Path


MODEL_ID = "Qwen/Qwen3-ASR-0.6B"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = PROJECT_ROOT / "assets" / "models" / "asr" / "qwen3-asr-0.6b"
REQUIRED_MODEL_FILES = (
    "chat_template.json",
    "config.json",
    "generation_config.json",
    "merges.txt",
    "model.safetensors",
    "preprocessor_config.json",
    "tokenizer_config.json",
    "vocab.json",
)


def _resolved_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def model_directory_ready(path: Path) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in REQUIRED_MODEL_FILES)


def configured_model_directory() -> tuple[Path, bool]:
    """Return the configured/canonical directory and whether it was explicit."""

    from config import settings

    configured = str(settings.QWEN3_ASR_MODEL_PATH or "").strip()
    if configured:
        return _resolved_path(configured), True
    return DEFAULT_MODEL_DIR, False


def legacy_huggingface_snapshot() -> Path | None:
    """Locate a complete pre-existing HF cache for backwards compatibility."""

    configured_cache = str(os.environ.get("HF_HUB_CACHE") or "").strip()
    if configured_cache:
        hub_root = Path(configured_cache).expanduser()
    else:
        hf_home = str(os.environ.get("HF_HOME") or "").strip()
        home_root = Path(hf_home).expanduser() if hf_home else Path.home() / ".cache" / "huggingface"
        hub_root = home_root / "hub"

    repository = hub_root / "models--Qwen--Qwen3-ASR-0.6B"
    ref = repository / "refs" / "main"
    candidates: list[Path] = []
    try:
        revision = ref.read_text(encoding="utf-8").strip()
    except OSError:
        revision = ""
    if revision:
        candidates.append(repository / "snapshots" / revision)
    snapshots = repository / "snapshots"
    if snapshots.is_dir():
        candidates.extend(path for path in snapshots.iterdir() if path.is_dir())
    for candidate in candidates:
        if model_directory_ready(candidate):
            return candidate
    return None


def qwen_model_status() -> tuple[bool, str, Path | None]:
    configured, explicit = configured_model_directory()
    if model_directory_ready(configured):
        return True, "Qwen3-ASR model installed in the Amadeus asset directory", configured
    if explicit:
        return False, f"Qwen3-ASR model directory is incomplete: {configured}", None
    legacy = legacy_huggingface_snapshot()
    if legacy is not None:
        return True, "Qwen3-ASR model found in the legacy Hugging Face cache", legacy
    return (
        False,
        "Qwen3-ASR model is not installed; install external pack asr-qwen3-0.6b",
        None,
    )


def resolve_qwen_model_source() -> str:
    ready, detail, path = qwen_model_status()
    if ready and path is not None:
        return str(path)
    raise FileNotFoundError(detail)
