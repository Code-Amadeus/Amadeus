"""Optional upstream contextual reading selection, before native NJD prosody."""
from __future__ import annotations

import logging
from pathlib import Path

from config.asset_paths import PROJECT_ROOT
from config.japanese_reading import PACK_TREE, VERSION, load_reading_pack

logger = logging.getLogger(__name__)
_runtime_status = {"state": "not_loaded", "ready": False, "identity": None,
                   "version": "", "detail": "Japanese context reading has not loaded."}


def reading_runtime_status() -> dict:
    return dict(_runtime_status)


def configure_japanese_reading(jtalk, *, project_root: Path = PROJECT_ROOT, enabled: bool | None = None) -> dict:
    global _runtime_status
    was_ready = _runtime_status["ready"]
    _runtime_status = {"state": "invalid", "ready": False, "identity": None, "version": "", "detail": ""}
    state, identity, version = "not_installed", None, ""
    detail = "Optional context-reading model is not installed; using the base Japanese frontend."
    try:
        if enabled is None:
            from config.settings import ENABLE_JAPANESE_CONTEXT_READING
            enabled = ENABLE_JAPANESE_CONTEXT_READING
        root = Path(project_root) / "assets" / PACK_TREE
        # Never call the Hub-loading API. Startup uses only explicitly installed,
        # hash-checked local files, on CPU independently of the voice accelerator.
        if was_ready or enabled and root.exists():
            jtalk.tsqyomi.unload_model()
        if not enabled:
            state, detail = "disabled", "Japanese context reading is disabled."
        elif root.exists():
            pack = load_reading_pack(root)
            jtalk.tsqyomi.load_model(model_dir=pack.root, onnx_providers=["CPUExecutionProvider"],
                                     allow_provider_fallback=False)
            if not jtalk.tsqyomi.is_model_loaded():
                raise RuntimeError("Context-reading model did not become ready")
            state, identity, version = "ready", pack.identity, VERSION
            detail = "Installed tsqyomi v5 context-reading model loaded on CPU."
    except Exception as exc:
        state, detail = "invalid", str(exc)
        logger.warning("[JapaneseReading] Using the base frontend: %s", exc)
    _runtime_status = {"state": state, "ready": state == "ready", "identity": identity,
                       "version": version, "detail": detail}
    return reading_runtime_status()


def run_frontend(jtalk, text: str) -> list[dict]:
    if _runtime_status["ready"]:
        return jtalk.run_frontend(text, use_tsqyomi=True)
    return jtalk.run_frontend(text)
