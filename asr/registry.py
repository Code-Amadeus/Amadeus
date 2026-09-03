"""Public ASR backend registry and non-loading availability projection."""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asr.backend import BaseASRBackend


ASRFactory = Callable[[], BaseASRBackend]
ASRProbe = Callable[[], tuple[str, str]]


@dataclass(frozen=True)
class ASRBackendDescriptor:
    backend_id: str
    label: str
    deployment: str
    factory: ASRFactory
    probe: ASRProbe
    summary: str = ""

    def status(self, *, selected: bool = False) -> dict[str, Any]:
        state, detail = self.probe()
        return {
            "id": self.backend_id,
            "label": self.label,
            "deployment": self.deployment,
            "state": state,
            "available": state in {"installed", "remote"},
            "selected": bool(selected),
            "detail": detail,
            "summary": self.summary,
        }


_REGISTRY: dict[str, ASRBackendDescriptor] = {}
_LOCK = threading.Lock()
_BUILTINS_READY = False
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BUILTIN_IDS = frozenset({"qwen3_asr", "sense_voice", "openai_compatible"})


def register_asr_backend(
    descriptor: ASRBackendDescriptor,
    *,
    replace: bool = False,
) -> None:
    _ensure_builtins()
    backend_id = str(descriptor.backend_id or "").strip().lower()
    if not backend_id:
        raise ValueError("ASR backend id is required")
    with _LOCK:
        if backend_id in _REGISTRY and not replace:
            raise ValueError(f"ASR backend already registered: {backend_id}")
        _REGISTRY[backend_id] = descriptor


def _qwen_factory() -> BaseASRBackend:
    from asr.backends.qwen3_asr import Qwen3ASRBackend

    return Qwen3ASRBackend()


def _sense_voice_factory() -> BaseASRBackend:
    from asr.backends.sense_voice import SenseVoiceBackend

    return SenseVoiceBackend()


def _remote_factory() -> BaseASRBackend:
    from asr.backends.openai_compatible import OpenAICompatibleASRBackend

    return OpenAICompatibleASRBackend()


def _qwen_probe() -> tuple[str, str]:
    from asr.qwen_model import qwen_model_status

    configured_python = str(os.environ.get("QWEN3_ASR_PYTHON") or "").strip()
    runtime_available = False
    runtime_detail = ""
    if importlib.util.find_spec("qwen_asr") is not None:
        runtime_available = True
        runtime_detail = f"embedded runtime in {Path(sys.executable).name}"
    elif configured_python and Path(configured_python).is_file():
        runtime_available = True
        runtime_detail = "configured isolated runtime"
    else:
        from config.environment import venv_python as _venv_python

        for candidate in (
            _venv_python(_PROJECT_ROOT, ".venv"),
        ):
            if candidate.is_file():
                runtime_available = True
                runtime_detail = f"isolated runtime at {candidate.parent.parent.name}"
                break
    if not runtime_available:
        return "not_installed", "Qwen ASR runtime is not installed"
    model_ready, model_detail, _ = qwen_model_status()
    if not model_ready:
        return "not_installed", model_detail
    return "installed", f"{model_detail}; {runtime_detail}"


def _sense_voice_probe() -> tuple[str, str]:
    from config import settings

    if importlib.util.find_spec("funasr") is None:
        return "not_installed", "FunASR is not installed"
    configured = str(settings.SENSEVOICE_MODEL_PATH or "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend(
        [
            Path.home() / ".cache" / "modelscope" / "hub" / "models" / "iic" / "SenseVoiceSmall",
            Path.home() / ".cache" / "modelscope" / "hub" / "models" / "._____temp" / "iic" / "SenseVoiceSmall",
        ]
    )
    if any((path / "config.yaml").is_file() and (path / "model.pt").is_file() for path in candidates):
        return "installed", "SenseVoice model cache found"
    return "not_installed", "SenseVoice model cache is not installed"


def _remote_probe() -> tuple[str, str]:
    from config import settings

    if not str(settings.ASR_API_BASE_URL or "").strip():
        return "unavailable", "ASR API endpoint is not configured"
    if not str(settings.ASR_API_MODEL or "").strip():
        return "unavailable", "ASR API model is not configured"
    return "remote", "Remote endpoint configured; availability is checked on use"


def _ensure_builtins() -> None:
    global _BUILTINS_READY
    if _BUILTINS_READY:
        return
    with _LOCK:
        if _BUILTINS_READY:
            return
        _REGISTRY.update(
            {
                "qwen3_asr": ASRBackendDescriptor(
                    "qwen3_asr",
                    "Qwen3-ASR",
                    "embedded",
                    _qwen_factory,
                    _qwen_probe,
                    "Full conversation recognizer with context and speculative endpointing.",
                ),
                "sense_voice": ASRBackendDescriptor(
                    "sense_voice",
                    "SenseVoice",
                    "embedded",
                    _sense_voice_factory,
                    _sense_voice_probe,
                    "Lightweight local recognizer; also used independently by Wake.",
                ),
                "openai_compatible": ASRBackendDescriptor(
                    "openai_compatible",
                    "OpenAI-compatible API",
                    "remote",
                    _remote_factory,
                    _remote_probe,
                    "Remote conversation transcription; speculative requests are disabled.",
                ),
            }
        )
        _BUILTINS_READY = True


def asr_backend_ids() -> tuple[str, ...]:
    _ensure_builtins()
    return tuple(_REGISTRY)


def unregister_asr_backend(backend_id: str) -> None:
    _ensure_builtins()
    clean = str(backend_id or "").strip().lower()
    if clean in _BUILTIN_IDS:
        raise ValueError(f"cannot unregister built-in ASR backend: {clean}")
    with _LOCK:
        _REGISTRY.pop(clean, None)


def create_asr_backend(backend_id: str) -> BaseASRBackend:
    _ensure_builtins()
    clean = str(backend_id or "").strip().lower()
    descriptor = _REGISTRY.get(clean)
    if descriptor is None:
        raise ValueError(f"unknown ASR backend {clean!r}; available: {list(_REGISTRY)}")
    return descriptor.factory()


def asr_backend_statuses(selected: str = "") -> list[dict[str, Any]]:
    _ensure_builtins()
    clean = str(selected or "").strip().lower()
    return [item.status(selected=item.backend_id == clean) for item in _REGISTRY.values()]
