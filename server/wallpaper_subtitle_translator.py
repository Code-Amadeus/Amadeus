"""Provider-selectable Japanese -> Chinese presentation subtitle translator.

Wallpaper/Lively and GUI Chat may share this provider configuration and output
cleaning, but each surface owns its own enablement and render state.  This
module never writes chat memory, changes VN subtitle routing, or alters TTS
input.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

from config import settings

logger = logging.getLogger(__name__)

_CLIENTS: dict[tuple[str, str], Any] = {}

_SYSTEM_PROMPT = (
    "Translate Japanese assistant speech into concise, natural Simplified Chinese "
    "subtitles. Return only the Chinese translation. Preserve names, technical "
    "terms, URLs, commands, and code identifiers. Do not add explanations, "
    "markdown, quotes, labels, or new facts."
)


@dataclass(frozen=True)
class SubtitleTranslatorConfig:
    provider: str
    model: str
    base_url: str
    timeout_s: float
    max_tokens: int


def get_translation_runtime_info() -> dict[str, str | float | int]:
    config = _resolve_config()
    return {
        "provider": config.provider,
        "model": config.model,
        "base_url": _redact_url(config.base_url),
        "timeout_s": config.timeout_s,
        "max_tokens": config.max_tokens,
    }


async def translate_presentation_subtitle(japanese_text: str) -> str:
    text = str(japanese_text or "").strip()
    if not text:
        return ""

    config = _resolve_config()
    providers = _provider_order(config.provider)
    last_error: Exception | None = None
    started = asyncio.get_running_loop().time()

    for provider in providers:
        try:
            attempt = _config_for_provider(config, provider)
            result = await _translate_with_config(text, attempt)
            result = _clean_translation(result)
            if _looks_like_valid_translation(result):
                elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000)
                logger.info(
                    "presentation subtitle translated provider=%s model=%s chars_in=%d chars_out=%d elapsed_ms=%d",
                    attempt.provider,
                    attempt.model,
                    len(text),
                    len(result),
                    elapsed_ms,
                )
                return result
            logger.warning(
                "presentation subtitle provider returned unusable text provider=%s model=%s chars_in=%d raw_len=%d",
                attempt.provider,
                attempt.model,
                len(text),
                len(result or ""),
            )
        except Exception as exc:
            last_error = exc
            logger.warning(
                "presentation subtitle translation attempt failed provider=%s: %s",
                provider,
                exc,
            )

    if last_error is not None:
        logger.error("presentation subtitle translation failed after fallbacks: %s", last_error)
    return ""


async def translate_wallpaper_subtitle(japanese_text: str) -> str:
    """Compatibility entry point for the established Wallpaper pipeline."""

    return await translate_presentation_subtitle(japanese_text)


def _resolve_config() -> SubtitleTranslatorConfig:
    provider = (
        os.environ.get("AMADEUS_SUBTITLE_TRANSLATE_PROVIDER")
        or os.environ.get("AMADEUS_WALLPAPER_SUBTITLE_TRANSLATE_PROVIDER")
        or "auto"
    ).strip().lower()
    if provider not in {"auto", "deepseek", "gemini", "openai"}:
        provider = "auto"

    timeout_s = _env_float("AMADEUS_SUBTITLE_TRANSLATE_TIMEOUT", 8.0)
    max_tokens = _env_int("AMADEUS_SUBTITLE_TRANSLATE_MAX_TOKENS", 220)
    return _config_for_provider(
        SubtitleTranslatorConfig(
            provider=provider,
            model="",
            base_url="",
            timeout_s=max(3.0, timeout_s),
            max_tokens=max(80, max_tokens),
        ),
        provider,
    )


def _config_for_provider(base: SubtitleTranslatorConfig, provider: str) -> SubtitleTranslatorConfig:
    if provider == "auto":
        provider = _provider_order("auto")[0]

    model_override = os.environ.get("AMADEUS_SUBTITLE_TRANSLATE_MODEL", "").strip()
    base_url_override = os.environ.get("AMADEUS_SUBTITLE_TRANSLATE_BASE_URL", "").strip()

    if provider == "openai":
        model = model_override or getattr(settings, "OPENAI_MODEL_NAME", "gpt-5.4-mini")
        base_url = base_url_override or getattr(settings, "OPENAI_BASE_URL", "")
    elif provider == "gemini":
        model = model_override or getattr(settings, "GEMINI_MODEL_NAME", "gemini-3.6-flash")
        base_url = base_url_override or "https://generativelanguage.googleapis.com/v1beta"
    else:
        provider = "deepseek"
        model = model_override or getattr(settings, "DEEPSEEK_MODEL_NAME", "deepseek-v4-flash")
        base_url = base_url_override or getattr(settings, "DEEPSEEK_BASE_URL", "")

    return SubtitleTranslatorConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        timeout_s=base.timeout_s,
        max_tokens=base.max_tokens,
    )


def _provider_order(provider: str) -> list[str]:
    if provider in {"deepseek", "gemini", "openai"}:
        return [provider]
    order: list[str] = []
    if getattr(settings, "DEEPSEEK_API_KEY", ""):
        order.append("deepseek")
    if getattr(settings, "GEMINI_API_KEY", ""):
        order.append("gemini")
    if getattr(settings, "OPENAI_API_KEY", ""):
        order.append("openai")
    return order or ["deepseek"]


async def _translate_with_config(text: str, config: SubtitleTranslatorConfig) -> str:
    if config.provider == "gemini":
        return await _translate_gemini(text, config)
    return await _translate_openai_compatible(text, config)


async def _translate_openai_compatible(text: str, config: SubtitleTranslatorConfig) -> str:
    if config.provider == "openai":
        api_key = getattr(settings, "OPENAI_API_KEY", "")
    else:
        api_key = getattr(settings, "DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError(f"{config.provider} API key is not configured")

    client = _get_openai_client(config.provider, api_key, config.base_url)
    kwargs: dict[str, Any] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "timeout": config.timeout_s,
    }
    if config.provider == "openai":
        kwargs["max_completion_tokens"] = config.max_tokens
        kwargs["reasoning_effort"] = "low"
    else:
        kwargs["temperature"] = 0.1
        kwargs["max_tokens"] = config.max_tokens
    if config.provider == "deepseek":
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    response = await asyncio.to_thread(lambda: client.chat.completions.create(**kwargs))
    try:
        return str(response.choices[0].message.content or "")
    except Exception:
        return ""


async def _translate_gemini(text: str, config: SubtitleTranslatorConfig) -> str:
    api_key = getattr(settings, "GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("gemini API key is not configured")
    url = f"{config.base_url.rstrip('/')}/models/{config.model}:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            f"{_SYSTEM_PROMPT}\n\nJapanese text:\n{text}"
                        )
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": config.max_tokens,
        },
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=config.timeout_s),
        ) as resp:
            if resp.status != 200:
                raw = await resp.text()
                raise RuntimeError(f"gemini HTTP {resp.status}: {raw[:180]}")
            data = await resp.json()
    try:
        return str(data["candidates"][0]["content"]["parts"][0]["text"] or "")
    except Exception:
        return ""


def _get_openai_client(provider: str, api_key: str, base_url: str):
    key = (provider, base_url)
    cached = _CLIENTS.get(key)
    if cached is not None:
        return cached

    import httpx
    from openai import OpenAI

    http_client = httpx.Client(
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=60.0),
        timeout=httpx.Timeout(30.0),
        http2=False,
    )
    client = OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)
    _CLIENTS[key] = client
    return client


def _clean_translation(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", text, flags=re.S)
    text = re.sub(r"^(?:中文|简体中文|翻译|translation)\s*[:：]\s*", "", text, flags=re.I)
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
    return text.strip()


def _looks_like_valid_translation(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if re.fullmatch(r"[\s.。…!?！？,，、;；:：-]+", text):
        return False
    # At least one CJK ideograph keeps short subtitle fragments such as
    # "嗯。" valid, while rejecting pure punctuation or echoed metadata.
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return default


def _redact_url(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    return text.split("?key=", 1)[0]
