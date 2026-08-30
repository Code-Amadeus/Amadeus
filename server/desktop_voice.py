from __future__ import annotations


_EXIT_COMMANDS = frozenset({"停止对话", "结束对话", "退出对话"})
_TERMINAL_PUNCTUATION = "。！？!?，,；;：:"


def normalize_desktop_voice_command(text: object) -> str:
    return str(text or "").strip().rstrip(_TERMINAL_PUNCTUATION).strip().casefold()


def is_desktop_voice_exit_command(text: object) -> bool:
    return normalize_desktop_voice_command(text) in _EXIT_COMMANDS
