"""Small JSON-oriented LLM client for VN Player lanes."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from llm.visual_context import attach_openai_chat_image, provider_supports_direct_image

from .schemas import VNProfile

logger = logging.getLogger(__name__)


class VNLLMClient:
    def __init__(self, profile: VNProfile) -> None:
        self.profile = profile

    def _provider_model(self) -> tuple[str, str]:
        from config import settings

        provider = (self.profile.provider or "deepseek").lower()
        if provider == "openai":
            model = self.profile.model or getattr(settings, "OPENAI_MODEL_NAME", "gpt-5.4-mini")
        else:
            model = self.profile.model or getattr(settings, "DEEPSEEK_MODEL_NAME", "deepseek-v4-flash")
        return provider, model

    def supports_visual(self) -> bool:
        provider, model = self._provider_model()
        return provider_supports_direct_image(provider, model)

    def configured(self) -> bool:
        from config import settings

        provider, _ = self._provider_model()
        if provider == "openai":
            return bool(getattr(settings, "OPENAI_API_KEY", ""))
        if provider == "deepseek":
            return bool(getattr(settings, "DEEPSEEK_API_KEY", ""))
        return False

    async def complete_json(
        self,
        messages: list[dict[str, Any]],
        *,
        lane: str,
        max_tokens: int = 700,
        temperature: float = 0.45,
        visual_context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        try:
            raw = await asyncio.to_thread(
                self._complete_sync,
                messages,
                lane=lane,
                max_tokens=max_tokens,
                temperature=temperature,
                visual_context=visual_context,
            )
        except Exception as exc:
            logger.warning("VN LLM %s call failed: %s", lane, exc)
            return None, str(exc)
        parsed = _parse_json_object(raw)
        if parsed is None:
            return None, raw
        return parsed, raw

    def _complete_sync(self, messages: list[dict[str, Any]], *, lane: str, max_tokens: int, temperature: float, visual_context: dict[str, Any] | None = None) -> str:
        provider, model = self._provider_model()
        if provider not in {"deepseek", "openai"}:
            raise RuntimeError(f"VN MVP only supports deepseek/openai-compatible providers, got {provider}")
        if visual_context and not self.supports_visual():
            raise RuntimeError("The configured VN model does not support direct image input")

        from openai import OpenAI
        from config import settings

        if provider == "openai":
            api_key = getattr(settings, "OPENAI_API_KEY", "")
            base_url = self.profile.base_url or getattr(settings, "OPENAI_BASE_URL", "")
        else:
            api_key = getattr(settings, "DEEPSEEK_API_KEY", "")
            base_url = self.profile.base_url or getattr(settings, "DEEPSEEK_BASE_URL", "")

        if not api_key:
            raise RuntimeError(f"{provider} API key is not configured")

        client = OpenAI(api_key=api_key, base_url=base_url)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": attach_openai_chat_image(messages, visual_context),
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
        choice = response.choices[0]
        usage = response.usage
        log = logger.warning if choice.finish_reason == "length" else logger.info
        log(
            "VN LLM %s completed: model=%s finish_reason=%s completion_tokens=%s max_tokens=%s",
            lane, response.model, choice.finish_reason,
            usage.completion_tokens if usage else None, max_tokens,
        )
        return choice.message.content or ""


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
