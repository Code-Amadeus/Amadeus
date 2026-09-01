"""Render-only translated subtitles for completed GUI Chat messages.

The source text remains the clean Main Chat response.  Translations are
requested on demand by the trusted GUI and are never projected into
conversation history, session storage, TTS, or Provider context.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from server.assistant_language import text_matches_assistant_language

SETTING_KEY = "chat_translation_subtitles_enabled"
MAX_SOURCE_CHARS = 16_000


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


_enabled = _env_enabled("AMADEUS_CHAT_TRANSLATION_SUBTITLES_ENABLED", False)


def is_enabled() -> bool:
    return _enabled


def get_config() -> dict[str, bool]:
    return {SETTING_KEY: _enabled}


def set_config(values: Mapping[str, object]) -> list[str]:
    global _enabled
    if SETTING_KEY not in values:
        return []
    enabled = values[SETTING_KEY]
    if not isinstance(enabled, bool):
        raise ValueError(f"{SETTING_KEY} must be a boolean")
    if enabled == _enabled:
        return []
    _enabled = enabled
    return [SETTING_KEY]


def _looks_like_japanese_source(text: str) -> bool:
    # Use the existing presentation-language contract instead of guessing
    # that an all-Han line is Japanese; that avoids retranslating a Chinese
    # Observer entry while Japanese remains the primary voice language.
    return text_matches_assistant_language(text, "japanese")


async def translate_completed_message(value: object) -> dict[str, Any]:
    """Translate one clean completed assistant message for GUI display only."""

    if not _enabled:
        return {"status": "disabled", "translation": ""}

    text = str(value or "").strip()
    if not text:
        return {"status": "empty", "translation": ""}
    if len(text) > MAX_SOURCE_CHARS:
        raise ValueError(f"chat translation source exceeds {MAX_SOURCE_CHARS} characters")
    if not _looks_like_japanese_source(text):
        return {"status": "not_japanese", "translation": ""}

    from server.wallpaper_subtitle_translator import (
        translate_presentation_subtitle,
    )

    translation = await translate_presentation_subtitle(text)
    return {
        "status": "translated" if translation else "unavailable",
        "translation": str(translation or ""),
    }
