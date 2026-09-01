from __future__ import annotations

import unicodedata


_EXIT_COMMANDS = frozenset({"停止对话", "结束对话", "退出对话"})
_TERMINAL_PUNCTUATION = "。！？!?，,；;：:"


def normalize_desktop_voice_command(text: object) -> str:
    return str(text or "").strip().rstrip(_TERMINAL_PUNCTUATION).strip().casefold()


def is_desktop_voice_exit_command(text: object) -> bool:
    return normalize_desktop_voice_command(text) in _EXIT_COMMANDS


def _normalize_manual_wake_phrase(text: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(
        char
        for char in normalized
        if not char.isspace()
        and not unicodedata.category(char).startswith(("P", "Z"))
    )


def is_manual_wake_command(text: object, phrases: object) -> bool:
    """Return true only when the whole input is one configured wake phrase."""

    candidate = _normalize_manual_wake_phrase(text)
    if not candidate:
        return False
    configured = phrases.split(",") if isinstance(phrases, str) else phrases or ()
    return candidate in {
        normalized
        for phrase in configured
        if (normalized := _normalize_manual_wake_phrase(phrase))
    }
