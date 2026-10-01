"""Host LLM adapter for extracting long-term memory candidates."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import json
import re
from typing import Any

from .extractor import parse_extractor_payload
from .models import MemoryRecord


_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def extract_json_payload(raw: str) -> object:
    text = _JSON_FENCE.sub("", str(raw or "").strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
        if not starts:
            raise
        start = min(starts)
        decoder = json.JSONDecoder()
        payload, _end = decoder.raw_decode(text[start:])
        return payload


class HostMemoryExtractor:
    """Turns one conversation excerpt into validated memory records."""

    def __init__(
        self,
        *,
        default_scope: str = "user",
        default_namespace: str = "general",
        source_ids: Iterable[str] = (),
        query: Callable[[list[dict[str, str]]], str] | None = None,
    ):
        self.default_scope = default_scope
        self.default_namespace = default_namespace
        self.source_ids = tuple(str(item) for item in source_ids if str(item))
        self._query = query

    def __call__(self, conversation_text: str) -> list[MemoryRecord]:
        text = str(conversation_text or "").strip()
        if not text:
            return []
        payload = extract_json_payload(self._request(text))
        return parse_extractor_payload(
            payload,
            default_scope=self.default_scope,
            default_namespace=self.default_namespace,
            default_source="conversation",
            default_source_ids=self.source_ids,
        )

    def _request(self, conversation_text: str) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "Extract only durable user preferences, constraints, stable facts, "
                    "and ongoing activity state. Do not store temporary dialogue, "
                    "assistant claims, or uncertain guesses. Return JSON only: "
                    '{"memories":[{"text":"...","kind":"preference|constraint|fact|episode|story_state",'
                    '"tags":[],"importance":1-5,"confidence":0.0-1.0}]}'
                ),
            },
            {"role": "user", "content": conversation_text[:16000]},
        ]
        if self._query is not None:
            return str(self._query(messages))
        from llm.client import remote_llm_messages_query

        return str(
            remote_llm_messages_query(
                messages,
                temperature=0.0,
                max_tokens=700,
                timeout=30.0,
            )
        )