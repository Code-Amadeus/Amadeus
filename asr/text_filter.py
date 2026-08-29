"""Small guards for ASR text before it becomes chat input."""

from __future__ import annotations

import re


_PROMPT_LEAK_PHRASES = (
    "the speaker uses mixed chinese and english",
    "preserve english technical terms",
    "do not transliterate",
    "key terms:",
)


def _normalize(text: str) -> str:
    text = str(text or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def is_asr_prompt_leak(text: str, *, context: str = "") -> bool:
    """Return True when ASR appears to have transcribed its own prompt.

    Qwen-ASR sometimes echoes the context/system hint as recognized speech.
    That text is useful as a recognizer bias, but it must never be emitted as a
    user utterance or sent to the LLM conversation.
    """
    normalized = _normalize(text)
    if not normalized:
        return False
    if any(phrase in normalized for phrase in _PROMPT_LEAK_PHRASES):
        return True

    context_norm = _normalize(context)
    if not context_norm:
        return False
    if normalized == context_norm:
        return True
    if len(normalized) >= 32 and normalized in context_norm:
        return True
    if len(context_norm) >= 32 and context_norm in normalized:
        return True
    return False
