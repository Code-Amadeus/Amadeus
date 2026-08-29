"""Logging helpers for Windows console mojibake repair."""

from __future__ import annotations

import logging
import sys


_MOJIBAKE_MARKERS = (
    "馃",
    "鎾",
    "鐩",
    "璋",
    "鈿",
    "鉁",
    "銆",
    "锛",
    "绂",
    "妯",
    "浣",
    "寮",
    "濮",
    "姝",
    "鍚",
    "瀹",
    "灏",
    "彞",
    "绠",
    "悊",
    "搴",
    "鍙",
    "笍",
    "€",
)


def _badness(text: str) -> int:
    score = sum(text.count(marker) * 3 for marker in _MOJIBAKE_MARKERS)
    score += sum(2 for ch in text if "\ue000" <= ch <= "\uf8ff")
    score += text.count("\ufffd") * 8
    return score


def repair_mojibake_text(value: str) -> str:
    """Repair common UTF-8 text that was accidentally decoded as GBK/CP936."""
    if not value or _badness(value) < 3:
        return value

    original_score = _badness(value)
    best = value
    best_score = original_score

    error_modes = ("ignore",) if "\ufffd" in value else ("strict", "ignore")
    for encoding in ("gb18030", "gbk", "cp936"):
        for errors in error_modes:
            try:
                candidate = value.encode(encoding, errors=errors).decode("utf-8", errors=errors)
            except UnicodeError:
                continue
            candidate_score = _badness(candidate)
            if candidate and candidate_score < best_score:
                best = candidate
                best_score = candidate_score

    # Avoid surprising rewrites for legitimate Chinese text that only happens
    # to contain one rare marker character.
    if best is not value and best_score + 2 < original_score:
        return best
    return value


def _repair_args(args):
    if isinstance(args, tuple):
        return tuple(repair_mojibake_text(arg) if isinstance(arg, str) else arg for arg in args)
    if isinstance(args, dict):
        return {key: repair_mojibake_text(arg) if isinstance(arg, str) else arg for key, arg in args.items()}
    if isinstance(args, str):
        return repair_mojibake_text(args)
    return args


class MojibakeRepairFilter(logging.Filter):
    """Repairs legacy mojibake log records before handlers format them."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = repair_mojibake_text(record.msg)
        if record.args:
            record.args = _repair_args(record.args)
        return True


class MojibakeRepairStream:
    """Text stream wrapper that repairs mojibake before writing to console."""

    def __init__(self, wrapped):
        self._wrapped = wrapped

    def write(self, text):
        if isinstance(text, str):
            text = repair_mojibake_text(text)
        return self._wrapped.write(text)

    def writelines(self, lines):
        return self._wrapped.writelines(
            repair_mojibake_text(line) if isinstance(line, str) else line
            for line in lines
        )

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def install_mojibake_repair_filter(logger: logging.Logger | None = None) -> None:
    target = logger or logging.getLogger()
    filt = MojibakeRepairFilter()

    if not any(isinstance(existing, MojibakeRepairFilter) for existing in target.filters):
        target.addFilter(filt)

    for handler in target.handlers:
        if not any(isinstance(existing, MojibakeRepairFilter) for existing in handler.filters):
            handler.addFilter(filt)


def install_stdio_mojibake_repair() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or isinstance(stream, MojibakeRepairStream):
            continue
        setattr(sys, stream_name, MojibakeRepairStream(stream))
