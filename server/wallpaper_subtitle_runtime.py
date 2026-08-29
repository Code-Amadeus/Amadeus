"""Wallpaper-only subtitle display mode.

This module deliberately controls only the Wallpaper/Lively subtitle surface.
It does not change chat history, chat bubbles, TTS language, or the legacy
floating subtitle window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from server import presentation_runtime


@dataclass
class _SubtitleState:
    japanese_text: str = ""
    chinese_text: str = ""


_state = _SubtitleState()
_render_fn: Callable[[str], None] | None = None


def get_mode() -> str:
    """Compatibility view for callers that still use zh/ja/bilingual/off."""

    return presentation_runtime.legacy_caption_setting()


def get_config() -> dict[str, str]:
    return presentation_runtime.get_config()


def set_mode(value: object, *, render_current: bool = True) -> str:
    presentation_runtime.set_legacy_caption_setting(
        value,
        render_current=render_current,
    )
    if render_current:
        _render()
    return get_mode()


def needs_translation() -> bool:
    return presentation_runtime.get_caption_mode() in {"translated", "bilingual"}


def set_renderer(render_fn: Callable[[str], None] | None) -> None:
    global _render_fn
    _render_fn = render_fn
    _render()


def update(japanese_text: str, chinese_text: str = "") -> str:
    _state.japanese_text = str(japanese_text or "")
    _state.chinese_text = str(chinese_text or "")
    return _render()


def current_text() -> str:
    ja = _state.japanese_text
    zh = _state.chinese_text
    mode = presentation_runtime.get_caption_mode()
    if mode == "off":
        return ""
    if mode == "source":
        return ja
    if mode == "bilingual":
        if zh and ja and zh != ja:
            return f"{zh}\n{ja}"
        return zh or ja
    # Chinese mode is intentionally strict: playback often sends the Japanese
    # sentence first and fills the translated subtitle later. Falling back to
    # Japanese here makes the CRT flicker between languages.
    return zh


def refresh() -> str:
    return _render()


def _render() -> str:
    text = current_text()
    if _render_fn is not None:
        _render_fn(text)
    return text
