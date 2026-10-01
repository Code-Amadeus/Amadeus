"""Resolve the effective GPT-SoVITS weights for the active character."""

from __future__ import annotations

from pathlib import Path

from config.asset_paths import PROJECT_ROOT


_DEFAULT_GPT = PROJECT_ROOT / "assets" / "models" / "gpt-sovits" / "weights" / "gpt" / "v3" / "xxx-e15.ckpt"
_DEFAULT_SOVITS = PROJECT_ROOT / "assets" / "models" / "gpt-sovits" / "weights" / "sovits" / "v3" / "xxx_e2_s174_l32.pth"


def _absolute(value: str | Path | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path.resolve())


def resolve_active_tts_model_paths() -> tuple[str, str]:
    """Return absolute GPT/SoVITS paths, with per-character overrides first.

    Empty character fields intentionally fall back to the global Amadeus
    settings.  Empty global fields use the same stock checkpoint defaults as
    ``local_tts_infer.TTSInferencer`` so switching back to Kurisu is stable
    even when the normal installation config uses implicit paths.
    """

    from config import settings

    try:
        from core.character_profile import active_voice

        voice = active_voice() or {}
    except Exception:
        voice = {}

    gpt = voice.get("gpt_model") or settings.TTS_GPT_MODEL_PATH or _DEFAULT_GPT
    sovits = voice.get("sovits_model") or settings.TTS_SOVITS_MODEL_PATH or _DEFAULT_SOVITS
    return _absolute(gpt), _absolute(sovits)
