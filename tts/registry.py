"""Public TTS backend registry and availability projection."""

from __future__ import annotations

import importlib.util
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tts.backend import BaseTTSBackend, TTSRuntimeAdapter


TTSFactory = Callable[[], BaseTTSBackend]
TTSProbe = Callable[[], tuple[str, str]]
TTSStreamingProbe = Callable[[], bool]


@dataclass(frozen=True)
class TTSBackendDescriptor:
    backend_id: str
    label: str
    deployment: str
    factory: TTSFactory
    probe: TTSProbe
    summary: str = ""
    supports_streaming: bool | TTSStreamingProbe = False
    supports_reference_conditioning: bool = False

    def status(self, *, selected: bool = False) -> dict[str, Any]:
        state, detail = self.probe()
        supports_streaming = (
            self.supports_streaming()
            if callable(self.supports_streaming)
            else self.supports_streaming
        )
        return {
            "id": self.backend_id,
            "label": self.label,
            "deployment": self.deployment,
            "state": state,
            "available": state in {"installed", "remote"},
            "selected": bool(selected),
            "detail": detail,
            "summary": self.summary,
            "supports_streaming": bool(supports_streaming),
            "supports_reference_conditioning": bool(
                self.supports_reference_conditioning
            ),
        }


_REGISTRY: dict[str, TTSBackendDescriptor] = {}
_LOCK = threading.Lock()
_BUILTINS_READY = False
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BUILTIN_IDS = frozenset({"gpt_sovits", "openai_compatible", "mimo"})


def register_tts_backend(
    descriptor: TTSBackendDescriptor,
    *,
    replace: bool = False,
) -> None:
    _ensure_builtins()
    backend_id = str(descriptor.backend_id or "").strip().lower()
    if not backend_id:
        raise ValueError("TTS backend id is required")
    with _LOCK:
        if backend_id in _REGISTRY and not replace:
            raise ValueError(f"TTS backend already registered: {backend_id}")
        _REGISTRY[backend_id] = descriptor


def _local_factory() -> BaseTTSBackend:
    from tts.backends.gpt_sovits import GPTSoVITSBackend

    return GPTSoVITSBackend()


def _remote_factory() -> BaseTTSBackend:
    from tts.backends.openai_compatible import OpenAICompatibleTTSBackend

    return OpenAICompatibleTTSBackend()


def _mimo_factory() -> BaseTTSBackend:
    from tts.backends.mimo import MiMoTTSBackend

    return MiMoTTSBackend()


def _mimo_probe() -> tuple[str, str]:
    from config import settings
    from tts.backends.mimo import MIMO_TTS_MODEL_ID

    if not str(settings.MIMO_TTS_BASE_URL or "").strip():
        return "unavailable", "MiMo TTS endpoint is not configured"
    if not str(settings.MIMO_TTS_API_KEY or "").strip():
        return "unavailable", "MiMo TTS API key is not configured"
    model = str(settings.MIMO_TTS_MODEL or "").strip()
    if model != MIMO_TTS_MODEL_ID:
        return "unavailable", f"MiMo TTS supports only {MIMO_TTS_MODEL_ID}"
    if not str(settings.MIMO_TTS_VOICE or "").strip():
        return "unavailable", "MiMo TTS voice is not configured"
    return "remote", "MiMo TTS endpoint configured for PCM16 SSE streaming"


def _local_probe() -> tuple[str, str]:
    from config import settings

    if importlib.util.find_spec("soundfile") is None:
        return "not_installed", "Local GPT-SoVITS v3 dependencies are not installed"
    model_root = _PROJECT_ROOT / "assets" / "models" / "gpt-sovits"

    def configured_path(raw: str, fallback: Path) -> Path:
        path = Path(str(raw or "")) if str(raw or "").strip() else fallback
        return path if path.is_absolute() else _PROJECT_ROOT / path

    gpt = configured_path(
        settings.TTS_GPT_MODEL_PATH,
        model_root / "weights" / "gpt" / "v3" / "xxx-e15.ckpt",
    )
    sovits = configured_path(
        settings.TTS_SOVITS_MODEL_PATH,
        model_root / "weights" / "sovits" / "v3" / "xxx_e2_s174_l32.pth",
    )
    if gpt.is_file() and sovits.is_file():
        return "installed", "Embedded GPT-SoVITS v3 checkpoint pair found"
    return "not_installed", "Embedded GPT-SoVITS v3 checkpoint pair is not installed"


def _remote_probe() -> tuple[str, str]:
    from config import settings

    if not str(settings.TTS_API_BASE_URL or "").strip():
        return "unavailable", "TTS API endpoint is not configured"
    if not str(settings.TTS_API_MODEL or "").strip():
        return "unavailable", "TTS API model is not configured"
    if not str(settings.TTS_API_VOICE or "").strip():
        return "unavailable", "TTS API voice is not configured"
    protocol = str(settings.TTS_API_STREAM_PROTOCOL or "buffered").strip().lower()
    if protocol not in {"buffered", "openai_sse"}:
        return "unavailable", f"Unsupported remote TTS stream protocol: {protocol}"
    if protocol == "openai_sse":
        return "remote", "Remote endpoint configured for OpenAI SSE PCM streaming"
    return "remote", "Remote endpoint configured for buffered WAV responses"


def _remote_streaming_enabled() -> bool:
    from config import settings

    return str(settings.TTS_API_STREAM_PROTOCOL or "buffered").strip().lower() == "openai_sse"


def _ensure_builtins() -> None:
    global _BUILTINS_READY
    if _BUILTINS_READY:
        return
    with _LOCK:
        if _BUILTINS_READY:
            return
        _REGISTRY.update(
            {
                "gpt_sovits": TTSBackendDescriptor(
                    "gpt_sovits",
                    "GPT-SoVITS v3 · Amadeus",
                    "embedded",
                    _local_factory,
                    _local_probe,
                    "Amadeus low-latency rewrite; only GPT-SoVITS v3 checkpoints are supported.",
                    supports_streaming=True,
                    supports_reference_conditioning=True,
                ),
                "openai_compatible": TTSBackendDescriptor(
                    "openai_compatible",
                    "OpenAI-compatible API",
                    "remote",
                    _remote_factory,
                    _remote_probe,
                    "Buffered WAV compatibility or explicit OpenAI SSE first-packet playback.",
                    supports_streaming=_remote_streaming_enabled,
                ),
                "mimo": TTSBackendDescriptor(
                    "mimo",
                    "MiMo TTS (Xiaomi)",
                    "remote",
                    _mimo_factory,
                    _mimo_probe,
                    "MiMo chat-completions speech synthesis; PCM16 SSE streaming on mimo-v2.5-tts.",
                    supports_streaming=True,
                ),
            }
        )
        _BUILTINS_READY = True


def tts_backend_ids() -> tuple[str, ...]:
    _ensure_builtins()
    return (*tuple(_REGISTRY), "disabled")


def unregister_tts_backend(backend_id: str) -> None:
    _ensure_builtins()
    clean = str(backend_id or "").strip().lower()
    if clean in _BUILTIN_IDS or clean == "disabled":
        raise ValueError(f"cannot unregister built-in TTS backend: {clean}")
    with _LOCK:
        _REGISTRY.pop(clean, None)


def create_tts_runtime(backend_id: str) -> TTSRuntimeAdapter | None:
    _ensure_builtins()
    clean = str(backend_id or "").strip().lower()
    if clean == "disabled":
        return None
    descriptor = _REGISTRY.get(clean)
    if descriptor is None:
        raise ValueError(f"unknown TTS backend {clean!r}; available: {list(tts_backend_ids())}")
    backend = descriptor.factory()
    backend.load()
    return TTSRuntimeAdapter(backend)


def tts_backend_statuses(selected: str = "") -> list[dict[str, Any]]:
    _ensure_builtins()
    clean = str(selected or "").strip().lower()
    statuses = [item.status(selected=item.backend_id == clean) for item in _REGISTRY.values()]
    statuses.append(
        {
            "id": "disabled",
            "label": "Disabled",
            "deployment": "disabled",
            "state": "disabled",
            "available": True,
            "selected": clean == "disabled",
            "detail": "Text interaction remains available without speech synthesis",
            "summary": "Do not synthesize assistant speech.",
        }
    )
    return statuses
