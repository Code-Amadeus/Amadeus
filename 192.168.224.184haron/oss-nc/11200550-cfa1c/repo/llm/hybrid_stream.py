"""Hybrid LLM dual stream.

Local LLM produces a complete first sentence over its independent HTTP endpoint.
Cooperative owns the remote reply and keeps it complete. The token extractor
is also shared by the live Bedrock messages transport.
"""
from __future__ import annotations

import json
import logging
from contextlib import aclosing
from typing import AsyncGenerator

import aiohttp

from config.settings import HYBRID_LOCAL_LLM_MODEL, HYBRID_LOCAL_LLM_URL
from llm.local_backends import openai_chat_url
from tools.text_utils import STRONG_SENTENCE_ENDINGS
logger = logging.getLogger(__name__)

_SENTENCE_ENDINGS = STRONG_SENTENCE_ENDINGS
_SENTINEL = object()


async def local_head_tokens(messages: list[dict]) -> AsyncGenerator[str, None]:
    """The existing HTTP head producer; it has no Chat/Work authority."""
    payload = {
        "model": HYBRID_LOCAL_LLM_MODEL, "messages": messages, "stream": True,
        "temperature": 0.35, "top_p": 0.9, "cache_prompt": True, "max_tokens": 80,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(openai_chat_url(HYBRID_LOCAL_LLM_URL), json=payload,
                timeout=aiohttp.ClientTimeout(total=30, sock_read=20)) as resp:
            resp.raise_for_status()
            from tts.latency_clock import log_latency_marker

            log_latency_marker(logger, "local_post_opened", status=resp.status)
            async for line_bytes in resp.content:
                line = line_bytes.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    data = json.loads(raw)
                    delta = (data.get("choices") or [{}])[0].get("delta", {})
                    token = delta.get("content") or delta.get("text") or ""
                except (json.JSONDecodeError, IndexError):
                    continue
                if token:
                    yield token


def hybrid_local_head(text: str, visual_context=None):
    """Select the independent HTTP head only for a Hybrid Chat provider."""
    from llm import client
    from llm.prompts import (
        get_system_prompt, finalize_system_prompt_language, wrap_user_message_for_language_lock,
    )
    from llm.visual_context import local_visual_ack_text

    if client.LLM_PROVIDER not in {"hybrid", "hybrid2", "hybrid3"}:
        return None
    source = local_visual_ack_text(text, visual_context) if visual_context else text
    messages = [
        {"role": "system", "content": finalize_system_prompt_language(get_system_prompt("hybrid_local"))},
        {"role": "user", "content": wrap_user_message_for_language_lock(source)},
    ]
    return _first_local_sentence(messages)


async def _first_local_sentence(messages):
    from llm.stream_parser import StreamTagParser, presentation_parts

    parser = StreamTagParser(control_envelope_enabled=True, stop_after_control=False)
    try:
        async with aclosing(local_head_tokens(messages)) as tokens:
            async for token in tokens:
                _cleaned, parts = presentation_parts(parser, token)
                for kind, value in parts:
                    if kind == "action":
                        yield value["raw"]
                        continue
                    for index, char in enumerate(value):
                        if char in _SENTENCE_ENDINGS:
                            yield value[:index + 1]
                            return
                    if value:
                        yield value
    except Exception as exc:
        logger.warning("[Hybrid] local head unavailable: %s", type(exc).__name__)


def _extract_token(data: dict):
    event_type = data.get("type")
    if event_type == "content_block_delta":
        return data.get("delta", {}).get("text") or ""
    if event_type == "message_stop":
        return _SENTINEL
    if event_type in ("content_block_start", "content_block_stop", "message_start"):
        return ""
    if "choices" in data:
        choices = data.get("choices") or []
        if choices:
            delta = (choices[0] or {}).get("delta") or {}
            token = delta.get("content") or delta.get("text") or ""
            finish = (choices[0] or {}).get("finish_reason")
            if finish and not token:
                return _SENTINEL
            return token
    if "delta" in data:
        delta = data.get("delta") or {}
        if isinstance(delta, dict):
            return delta.get("text") or delta.get("content") or ""
    if "completion" in data:
        return data.get("completion") or ""
    return ""
