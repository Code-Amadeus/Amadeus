"""Runtime authority for non-conversational presentation choices.

The assistant/voice language remains owned by ``assistant_language``.  This
module controls Slice process presentation (including Provider reporting copy)
and how the wallpaper caption surface presents an already-authored role line.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping


VALID_PRESENTATION_LOCALES = {"zh-CN", "en-US", "ja-JP"}
VALID_CAPTION_MODES = {"translated", "source", "bilingual", "off"}
DEFAULT_PRESENTATION_LOCALE = "en-US"


def normalize_presentation_locale(value: object) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "zh": "zh-CN",
        "zh-cn": "zh-CN",
        "chinese": "zh-CN",
        "simplified-chinese": "zh-CN",
        "en": "en-US",
        "en-us": "en-US",
        "english": "en-US",
        "ja": "ja-JP",
        "jp": "ja-JP",
        "ja-jp": "ja-JP",
        "japanese": "ja-JP",
    }
    normalized = aliases.get(raw, str(value or "").strip())
    return normalized if normalized in VALID_PRESENTATION_LOCALES else DEFAULT_PRESENTATION_LOCALE


def normalize_caption_mode(value: object) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "zh": "translated",
        "chinese": "translated",
        "translated": "translated",
        "translation": "translated",
        "ja": "source",
        "jp": "source",
        "japanese": "source",
        "source": "source",
        "both": "bilingual",
        "dual": "bilingual",
        "bilingual": "bilingual",
        "none": "off",
        "disabled": "off",
        "off": "off",
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in VALID_CAPTION_MODES else "translated"


def _legacy_profile(value: object) -> tuple[str, str]:
    raw = str(value or "").strip().lower().replace("_", "-")
    if raw in {"ja", "jp", "ja-jp", "japanese"}:
        return "ja-JP", "source"
    if raw in {"both", "dual", "bilingual"}:
        return "zh-CN", "bilingual"
    if raw in {"none", "disabled", "off"}:
        return "zh-CN", "off"
    return "zh-CN", "translated"


_legacy_initial = (
    os.environ.get("AMADEUS_WALLPAPER_SUBTITLE_LANG")
    or os.environ.get("AMADEUS_WALLPAPER_SUBTITLE_MODE")
    or "zh"
)
_, _legacy_mode = _legacy_profile(_legacy_initial)
_presentation_locale = normalize_presentation_locale(
    os.environ.get("AMADEUS_PRESENTATION_LOCALE") or DEFAULT_PRESENTATION_LOCALE
)
_caption_mode = normalize_caption_mode(
    os.environ.get("AMADEUS_WALLPAPER_CAPTION_MODE") or _legacy_mode
)
_renderer: Callable[[dict[str, str]], None] | None = None


def get_presentation_locale() -> str:
    return _presentation_locale


def get_caption_mode() -> str:
    return _caption_mode


def get_config() -> dict[str, str]:
    return {
        "presentation_locale": _presentation_locale,
        "wallpaper_caption_mode": _caption_mode,
    }


def set_renderer(renderer: Callable[[dict[str, str]], None] | None) -> None:
    global _renderer
    _renderer = renderer
    _render()


def set_config(values: Mapping[str, object], *, render_current: bool = True) -> list[str]:
    """Apply one profile update, accepting the retired combined key at input."""

    global _presentation_locale, _caption_mode
    changed: list[str] = []
    has_locale = "presentation_locale" in values
    has_mode = "wallpaper_caption_mode" in values

    if "wallpaper_subtitle_language" in values and not (has_locale or has_mode):
        locale, mode = _legacy_profile(values["wallpaper_subtitle_language"])
        if locale != _presentation_locale:
            _presentation_locale = locale
            changed.append("presentation_locale")
        if mode != _caption_mode:
            _caption_mode = mode
            changed.append("wallpaper_caption_mode")

    if has_locale:
        locale = normalize_presentation_locale(values["presentation_locale"])
        if locale != _presentation_locale:
            _presentation_locale = locale
            changed.append("presentation_locale")
    if has_mode:
        mode = normalize_caption_mode(values["wallpaper_caption_mode"])
        if mode != _caption_mode:
            _caption_mode = mode
            changed.append("wallpaper_caption_mode")

    if changed and render_current:
        _render()
    return changed


def set_legacy_caption_setting(value: object, *, render_current: bool = True) -> list[str]:
    return set_config(
        {"wallpaper_subtitle_language": value},
        render_current=render_current,
    )


def legacy_caption_setting() -> str:
    return {
        "translated": "zh",
        "source": "ja",
        "bilingual": "bilingual",
        "off": "off",
    }[_caption_mode]


def _render() -> None:
    if _renderer is not None:
        _renderer(get_config())
