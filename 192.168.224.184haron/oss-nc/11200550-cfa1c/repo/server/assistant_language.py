"""Single runtime authority for the assistant's primary output language.

Wallpaper subtitle mode is a presentation choice: it may show the primary
line, a Chinese translation, both, or neither.  It must not decide the
language used by Chat, host-authored reports, or speech.
"""

from __future__ import annotations

import re


def normalize_assistant_language(value: object) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    if raw in {"en", "en_us", "english", "英文"}:
        return "english"
    return "japanese"


def current_assistant_language() -> str:
    """Return the language currently used by the role model and TTS."""

    try:
        import tts.pipeline as tts_pipeline

        value = getattr(tts_pipeline, "TTS_OUTPUT_LANGUAGE", "日文")
    except Exception:
        from config import settings

        value = getattr(settings, "TTS_OUTPUT_LANGUAGE", "日文")
    return normalize_assistant_language(value)


def text_matches_assistant_language(text: object, language: object) -> bool:
    """Check a user-facing sentence against the declared primary language.

    This is a contract check, not language detection for arbitrary content.
    Host narration in Japanese must contain kana; English narration must have
    Latin prose and no kana.  Page titles and filenames may still contain any
    script inside an otherwise valid sentence.
    """

    value = str(text or "").strip()
    if not value:
        return False
    raw_language = str(language or "").strip().lower().replace("-", "_")
    has_kana = bool(re.search(r"[\u3040-\u30ff\u31f0-\u31ff]", value))
    if raw_language in {
        "zh",
        "zh_cn",
        "chinese",
        "simplified_chinese",
        "中文",
        "简体中文",
    }:
        # Chinese remains a supported presentation language for legacy
        # Observer callers, but it is not a third primary role-language axis.
        return bool(re.search(r"[\u3400-\u9fff]", value)) and not has_kana
    normalized = normalize_assistant_language(language)
    if normalized == "japanese":
        return has_kana
    return bool(re.search(r"[A-Za-z]", value)) and not has_kana
