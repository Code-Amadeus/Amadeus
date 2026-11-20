"""Pure sentence-boundary helpers for the streaming chat pipeline.

Extracted from main.py. Stateless functions only; per-turn accumulation
state lives in core.chat_runtime.
"""

from __future__ import annotations

_JA_EARLY_CUT_MARKS = {"。", "！", "？", "!", "?", "、", "，", ",", "；", ";", "…"}
_JA_EARLY_CUT_TAILS = (
    "ですね",
    "ますね",
    "だね",
    "よね",
    "のね",
    "かね",
    "ね",
    "よ",
    "わ",
)


def split_stream_buffer_at_last_whitespace(text: str) -> tuple[str, str]:
    """流式缓冲按「词边界」切开：在最后一个空白符处分为 (prefix, suffix)。

    prefix 含该空白符之前的全部内容（含尾部空白）；suffix 为末尾未完结片段。
    若 buffer 以空白结尾，视为已处于词边界，整段作为 prefix。
    若全文无空白（单个超长 token），返回 (text, '')，由调用方整段送出。
    """
    if not text:
        return "", ""
    if text[-1].isspace():
        return text, ""
    for i in range(len(text) - 1, -1, -1):
        if text[i].isspace():
            return text[: i + 1], text[i + 1 :]
    return text, ""


def contains_japanese_text(text: str) -> bool:
    return any(
        "぀" <= ch <= "ヿ"
        or "㐀" <= ch <= "鿿"
        or ch == "ー"
        for ch in text
    )


def split_stream_buffer_for_first_sentence(
    text: str,
    min_chars: int,
    output_language: str,
) -> tuple[str, str, str]:
    """Choose a low-latency first-sentence boundary without cutting Japanese words.

    Returns (head, tail, reason). Empty head means "wait for more text".
    """
    if not text:
        return "", "", "empty"

    head, tail = split_stream_buffer_at_last_whitespace(text)
    if tail and head.strip():
        return head, tail, "whitespace"

    if output_language == "英文" or not contains_japanese_text(text):
        return text, "", "no_whitespace"

    stripped_len = len(text.strip())
    min_chars = max(1, min_chars)

    # Prefer real Japanese pause marks already emitted by the LLM. This catches
    # phrases like "パクソス理論ね…" while leaving the following word intact.
    min_japanese_boundary = max(4, min_chars // 2)
    for idx in range(len(text) - 1, min_japanese_boundary - 2, -1):
        ch = text[idx]
        if ch in _JA_EARLY_CUT_MARKS:
            head = text[: idx + 1]
            tail = text[idx + 1 :]
            if head.strip():
                return head, tail, "japanese_punctuation"

    # If there is no punctuation, allow a small set of sentence-final particles.
    # We only cut when more text has arrived after the particle, so "分散システム"
    # will not be split just because the first sentence hit the latency budget.
    for tail_word in _JA_EARLY_CUT_TAILS:
        pos = text.rfind(tail_word)
        if pos >= min_chars - len(tail_word):
            end = pos + len(tail_word)
            if end < len(text):
                return text[:end], text[end:], "japanese_particle"

    # Safety valve: if the first utterance is getting long and still has no
    # natural break, send it rather than stalling forever. The multiplier keeps
    # the current latency benefit for short English-like chunks but protects
    # Japanese compounds from the 11-character hard cut.
    if stripped_len >= max(min_chars * 3, min_chars + 24):
        return text, "", "japanese_safety_limit"

    return "", text, "japanese_wait_boundary"
