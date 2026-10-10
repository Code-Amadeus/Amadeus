"""Public TTS backend registry and availability projection."""

from __future__ import annotations

import importlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from config.catalog import voice_backend_groups
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


def _call_entrypoint(reference: str):
    # References come only from the packaged builtin catalog. Resolving lazily
    # keeps registry discovery independent of optional voice/model dependencies.
    module, name = reference.split(":", 1)
    return getattr(importlib.import_module(module), name)()


def _ensure_builtins() -> None:
    global _BUILTINS_READY
    if _BUILTINS_READY:
        return
    with _LOCK:
        if _BUILTINS_READY:
            return
        descriptors = {}
        for group in voice_backend_groups():
            backend = group["voice_backend"]
            backend_id = backend["id"]
            if backend_id in descriptors or backend_id == "disabled":
                raise ValueError(f"duplicate/reserved builtin TTS backend: {backend_id}")
            streaming = backend["streaming"]
            descriptors[backend_id] = TTSBackendDescriptor(
                backend_id, backend["label"]["en-US"], backend["deployment"],
                partial(_call_entrypoint, backend["factory"]),
                partial(_call_entrypoint, backend["probe"]),
                backend["summary"],
                supports_streaming=partial(_call_entrypoint, streaming) if isinstance(streaming, str) else streaming,
                supports_reference_conditioning=backend["reference_conditioning"],
            )
        _REGISTRY.update(descriptors)
        _BUILTINS_READY = True


def tts_backend_ids() -> tuple[str, ...]:
    _ensure_builtins()
    return (*tuple(_REGISTRY), "disabled")


def unregister_tts_backend(backend_id: str) -> None:
    _ensure_builtins()
    clean = str(backend_id or "").strip().lower()
    if clean in {group["voice_backend"]["id"] for group in voice_backend_groups()} or clean == "disabled":
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
