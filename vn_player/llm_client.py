"""Small JSON-oriented LLM client for VN Player lanes."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .schemas import VNProfile

logger = logging.getLogger(__name__)


class VNLLMClient:
    def __init__(self, profile: VNProfile) -> None:
        self.profile = profile

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        lane: str,
        max_tokens: int = 700,
        temperature: float = 0.45,
    ) -> tuple[dict[str, Any] | None, str]:
        try:
            raw = await asyncio.to_thread(
                self._complete_sync,
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as exc:
            logger.warning("VN LLM %s call failed: %s", lane, exc)
            return None, str(exc)
        parsed = _parse_json_object(raw)
        if parsed is None:
            return None, raw
        return parsed, raw

    def _complete_sync(self, messages: list[dict[str, str]], *, max_tokens: int, temperature: float) -> str:
        provider = (self.profile.provider or "deepseek").lower()
        if provider not in {"deepseek", "openai"}:
            raise RuntimeError(f"VN MVP only supports deepseek/openai-compatible providers, got {provider}")

        from openai import OpenAI
        from config import settings

        if provider == "openai":
            api_key = getattr(settings, "OPENAI_API_KEY", "")
            base_url = self.profile.base_url or getattr(settings, "OPENAI_BASE_URL", "")
            model = self.profile.model or getattr(settings, "OPENAI_MODEL_NAME", "gpt-5.4-mini")
        else:
            api_key = getattr(settings, "DEEPSEEK_API_KEY", "")
            base_url = self.profile.base_url or getattr(settings, "DEEPSEEK_BASE_URL", "")
            model = self.profile.model or "deepseek-v4-flash"

        if not api_key:
            raise RuntimeError(f"{provider} API key is not configured")

        client = OpenAI(api_key=api_key, base_url=base_url)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "timeout": 12,
        }
        if provider == "deepseek":
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        response = client.chat.completions.create(**kwargs)
        if not response.choices:
            raise RuntimeError("empty LLM response")
        return response.choices[0].message.content or ""


def _parse_json_object(text: str) -> dict[str, Any] | None:
    value = str(text or "").strip()
    if not value:
        return None
    if value.startswith("```"):
        value = value.strip("`")
        if value.lower().startswith("json"):
            value = value[4:].strip()
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(value[start : end + 1])
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None
