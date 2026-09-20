"""Bounded compression for Continuity excerpts.

Two implementations share one contract:

- ``DeterministicTextCompressor`` — pure whole-span window sampling.  It is
  the offline/test fallback that keeps the CPU/model-less deterministic
  baseline green and never needs a model.
- ``ModelTextCompressor`` — faithful *semantic* compression through the same
  LLM family Main Chat is configured with.  It runs only when a text exceeds
  its character budget; shorter text keeps its verbatim wording.  Any model
  failure, timeout, empty or overlong output degrades to the deterministic
  sampler within the same bound.

A compressor shapes how an already-accepted text is bounded for storage or
quotation; it never decides what becomes durable truth.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

_ELISION = "…"

# Providers this build can drive for semantic compression.  Anything else
# (e.g. Bedrock in this version) degrades to the deterministic fallback.
_SUPPORTED_PROVIDERS = (
    "deepseek",
    "hybrid2",
    "openai",
    "hybrid3",
    "gemini",
    "local",
    "hybrid",
)


def compress_excerpt(text: str, max_chars: int) -> str:
    """Deterministic fallback: return ``text`` unchanged when it fits, else a
    bounded whole-span excerpt (head, two sampled middle windows, tail)."""

    value = str(text or "")
    limit = int(max_chars)
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    # Budget: head 40%, tail 25%; the remainder feeds two middle windows so the
    # middle of a long turn stays represented instead of being dropped whole.
    head_len = max(1, limit * 2 // 5)
    tail_len = max(1, limit // 4)
    mid_budget = limit - head_len - tail_len - 3 * len(_ELISION)
    if mid_budget < 4:
        # Extremely small budgets: keep both ends, still bounded.
        head_len = max(1, (limit - len(_ELISION)) // 2)
        tail_len = max(1, limit - head_len - len(_ELISION))
        return (value[:head_len] + _ELISION + value[-tail_len:])[:limit]
    window = mid_budget // 2
    interior_start = head_len
    interior_end = len(value) - tail_len
    middle = interior_start + (interior_end - interior_start) // 2
    first_center = interior_start + (middle - interior_start) // 2
    second_center = middle + (interior_end - middle) // 2
    first_start = max(interior_start, first_center - window // 2)
    second_start = max(middle, second_center - window // 2)
    segments = (
        value[:head_len],
        value[first_start : first_start + window],
        value[second_start : second_start + window],
        value[-tail_len:],
    )
    return _ELISION.join(segments)[:limit]


class TextCompressor(Protocol):
    """Bounded compression contract shared by the model and fallback paths."""

    def compress(self, text: str, max_chars: int, *, speaker: str = "") -> str:
        """Return ``text`` unchanged when it fits; otherwise a bounded
        whole-content compression of at most ``max_chars`` characters."""
        ...


class DeterministicTextCompressor:
    """Offline fallback used by tests, tools without a model, and failures."""

    requires_model = False

    def compress(self, text: str, max_chars: int, *, speaker: str = "") -> str:
        return compress_excerpt(text, max_chars)


_COMPRESSION_SYSTEM_PROMPT = (
    "你是对话记录的语意压缩器。忠实压缩给定文本，覆盖全文范围，保留人名、"
    "称呼、数字、日期、地点、决定、承诺、约定与偏好；不得添加原文中没有的"
    "信息；不得评论或解释；直接输出压缩后的正文。"
)


def _compression_user_prompt(text: str, max_chars: int, speaker: str) -> str:
    who = f"{speaker}的发言" if speaker else "一段对话文本"
    return (
        f"把下面{who}压缩到 {max_chars} 个字符以内。"
        "要求：覆盖全文范围，不要只保留开头或结尾；保持原意与关键细节。\n\n"
        f"{text}"
    )


class ModelTextCompressor:
    """Faithful semantic compression through the configured LLM.

    ``complete`` is an injected ``(system_prompt, user_prompt) -> str`` callable
    (the production wiring uses ``llm.client.text_completion``); results are
    cached in-process so repeated recalls of the same text reuse one call.
    """

    requires_model = True

    def __init__(
        self,
        complete: Callable[[str, str], str],
        *,
        fallback: TextCompressor | None = None,
        cache_size: int = 128,
    ) -> None:
        self._complete = complete
        self._fallback = fallback or DeterministicTextCompressor()
        self._cache: OrderedDict[str, str] = OrderedDict()
        # The capture worker and the archive-recall worker share one compressor
        # instance, so cache access is guarded against their concurrency.
        self._cache_lock = threading.Lock()
        self._cache_size = max(0, int(cache_size))

    def compress(self, text: str, max_chars: int, *, speaker: str = "") -> str:
        value = str(text or "")
        limit = int(max_chars)
        if limit <= 0:
            return ""
        if len(value) <= limit:
            return value
        cache_key = hashlib.sha1(
            f"{limit}\x1f{speaker}\x1f{value}".encode("utf-8")
        ).hexdigest()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache.move_to_end(cache_key)
                return cached
        result = ""
        try:
            model_text = str(
                self._complete(
                    _COMPRESSION_SYSTEM_PROMPT,
                    _compression_user_prompt(value, limit, speaker),
                )
                or ""
            ).strip()
            if model_text:
                result = (
                    model_text
                    if len(model_text) <= limit
                    else compress_excerpt(model_text, limit)
                )
        except Exception:
            logger.warning(
                "semantic compression unavailable; using deterministic fallback",
                exc_info=True,
            )
        if not result:
            result = self._fallback.compress(value, limit, speaker=speaker)
        if self._cache_size:
            with self._cache_lock:
                self._cache[cache_key] = result
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        return result


def default_text_compressor() -> TextCompressor:
    """Build the production compressor for the configured Main Chat provider.

    Falls back to the deterministic sampler when the provider has no plain
    text-completion port or when the LLM layer cannot be imported, so the
    model-less baseline keeps working unchanged.
    """

    try:
        import llm.client as llm_client_module

        provider = str(getattr(llm_client_module, "LLM_PROVIDER", "") or "")
        if provider not in _SUPPORTED_PROVIDERS:
            return DeterministicTextCompressor()
        return ModelTextCompressor(llm_client_module.text_completion)
    except Exception:
        logger.warning(
            "semantic compression wiring unavailable; using deterministic fallback",
            exc_info=True,
        )
        return DeterministicTextCompressor()


__all__ = [
    "DeterministicTextCompressor",
    "ModelTextCompressor",
    "TextCompressor",
    "compress_excerpt",
    "default_text_compressor",
]

