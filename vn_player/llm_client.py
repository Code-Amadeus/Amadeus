"""Small JSON-oriented LLM client for VN Player lanes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
import json
import logging
import math
import time
from typing import Any

from llm.visual_context import attach_openai_chat_image, provider_supports_direct_image

from .schemas import VNProfile

logger = logging.getLogger(__name__)


class VNLLMClient:
    def __init__(self, profile: VNProfile) -> None:
        self.profile = profile
        self._clients = {}

    async def aclose(self) -> None:
        clients, self._clients = self._clients, {}
        for client in clients.values():
            await client.close()

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
        on_ready: Callable[[dict[str, Any], bool], Awaitable[None]] | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        timing = metrics if metrics is not None else {}
        timing["started_at_ms"] = round(time.time() * 1000)
        raw, committed, text_complete = "", None, False
        try:
            from openai import AsyncOpenAI

            options, kwargs = self._request_options(messages, max_tokens=max_tokens,
                                                    temperature=temperature, visual_context=visual_context)
            client_key = (options["base_url"], options["api_key"])
            if client_key not in self._clients:
                self._clients[client_key] = AsyncOpenAI(**options)
            pooled_client = self._clients[client_key]
            stream = lane in {"immediate", "immediate_context_retry"}
            kwargs["stream"] = stream
            if stream:
                kwargs["stream_options"] = {"include_usage": True}
            # Once speech is delivered, a transport retry cannot repeat the turn.
            client = pooled_client.with_options(max_retries=0) if stream else pooled_client
            response = await client.chat.completions.create(**kwargs)
            if stream:
                async with response:
                    async for chunk in response:
                        usage = getattr(chunk, "usage", None)
                        if usage is not None:
                            timing["completion_tokens"] = usage.completion_tokens
                        if not chunk.choices:
                            continue
                        choice = chunk.choices[0]
                        delta = choice.delta.content or ""
                        if delta:
                            timing.setdefault("first_token_at_ms", round(time.time() * 1000))
                            raw += delta
                        timing["model"] = chunk.model
                        if choice.finish_reason:
                            timing["finish_reason"] = choice.finish_reason
                        if on_ready is not None:
                            prefix = _speech_prefix(raw)
                            if prefix is not None:
                                header, closed = prefix
                                if committed is not None and not _extends_speech(committed, header):
                                    raise ValueError("Stream revised committed speech")
                                if header != committed or closed != text_complete:
                                    committed, text_complete = header, closed
                                    timing.setdefault("speech_prefix_at_ms", round(time.time() * 1000))
                                    await on_ready(deepcopy(header), closed)
            else:
                if not response.choices:
                    raise RuntimeError("empty LLM response")
                choice = response.choices[0]
                raw = choice.message.content or ""
                timing.update(model=response.model, finish_reason=choice.finish_reason,
                              completion_tokens=response.usage.completion_tokens if response.usage else None)
        except Exception as exc:
            logger.warning("VN LLM %s call failed: %s", lane, exc)
            timing["error"] = type(exc).__name__
            return None, raw or str(exc)
        finally:
            timing["completed_at_ms"] = round(time.time() * 1000)
            log = logger.warning if timing.get("finish_reason") == "length" else logger.info
            log("VN LLM %s completed: model=%s finish_reason=%s completion_tokens=%s max_tokens=%s elapsed_ms=%s",
                lane, timing.get("model"), timing.get("finish_reason"), timing.get("completion_tokens"),
                max_tokens, timing["completed_at_ms"] - timing["started_at_ms"])
        parsed = _parse_json_object(raw)
        if committed is not None and (parsed is None or not _extends_speech(committed, parsed)):
            # Already delivered speech is immutable; a corrupt/revised tail has
            # no authority to rewrite it or contribute memory patches.
            return None, raw
        return parsed, raw

    def _request_options(self, messages: list[dict[str, Any]], *, max_tokens: int, temperature: float,
                         visual_context: dict[str, Any] | None = None) -> tuple[dict, dict]:
        provider, model = self._provider_model()
        if provider not in {"deepseek", "openai"}:
            raise RuntimeError(f"VN MVP only supports deepseek/openai-compatible providers, got {provider}")
        if visual_context and not self.supports_visual():
            raise RuntimeError("The configured VN model does not support direct image input")

        from config import settings

        if provider == "openai":
            api_key = getattr(settings, "OPENAI_API_KEY", "")
            base_url = self.profile.base_url or getattr(settings, "OPENAI_BASE_URL", "")
        else:
            api_key = getattr(settings, "DEEPSEEK_API_KEY", "")
            base_url = self.profile.base_url or getattr(settings, "DEEPSEEK_BASE_URL", "")

        if not api_key:
            raise RuntimeError(f"{provider} API key is not configured")

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": attach_openai_chat_image(messages, visual_context),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": 12,
        }
        if provider == "deepseek":
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        return {"api_key": api_key, "base_url": base_url}, kwargs


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


_PLAYBACK_FIELDS = {"priority", "interrupt", "expires_after_lines", "target_line_id", "target_script_id", "emotion_intent"}


def _extends_speech(previous: dict, current: dict) -> bool:
    for key, value in previous.items():
        if key != "speak":
            if current.get(key) != value:
                return False
            continue
        speak = current.get("speak")
        if not isinstance(speak, dict):
            return False
        if not isinstance(speak.get("text"), str) or not speak["text"].startswith(value["text"]):
            return False
        if any(speak.get(field) != item for field, item in value.items() if field != "text"):
            return False
    return True


def _string_prefix(text: str, pos: int) -> tuple[str, int, bool]:
    """Decode only complete JSON characters; never repair escapes or split a surrogate pair."""
    if pos >= len(text) or text[pos] != '"':
        raise ValueError("Speech text must be a JSON string")
    chars, pos = [], pos + 1
    escapes = {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}
    while pos < len(text):
        char = text[pos]
        if char == '"':
            return "".join(chars), pos + 1, True
        if char == '\\':
            if pos + 1 >= len(text):
                break
            escape = text[pos + 1]
            if escape == 'u':
                if pos + 6 > len(text):
                    break
                code = int(text[pos + 2:pos + 6], 16)
                consumed = 6
                if 0xD800 <= code <= 0xDBFF:
                    if pos + 12 > len(text):
                        break
                    low = int(text[pos + 8:pos + 12], 16)
                    if text[pos + 6:pos + 8] != '\\u' or not 0xDC00 <= low <= 0xDFFF:
                        raise ValueError("Invalid JSON surrogate pair")
                    code = 0x10000 + ((code - 0xD800) << 10) + low - 0xDC00
                    consumed = 12
                elif 0xDC00 <= code <= 0xDFFF:
                    raise ValueError("Unpaired JSON surrogate")
                chars.append(chr(code))
                pos += consumed
                continue
            if escape not in escapes:
                raise ValueError("Invalid JSON escape")
            chars.append(escapes[escape])
            pos += 2
            continue
        if ord(char) < 0x20 or 0xD800 <= ord(char) <= 0xDFFF:
            raise ValueError("Invalid character in JSON speech")
        chars.append(char)
        pos += 1
    return "".join(chars), pos, False


def _partial_speak(text: str, pos: int, decoder) -> tuple[dict, bool]:
    fields, closed = {}, False
    if pos >= len(text) or text[pos] != "{":
        return fields, closed
    pos += 1
    while pos < len(text):
        try:
            while pos < len(text) and text[pos].isspace():
                pos += 1
            key, pos = decoder.raw_decode(text, pos)
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if not isinstance(key, str) or pos >= len(text) or text[pos] != ":":
                break
            if key in fields:
                raise ValueError(f"duplicate JSON field: {key}")
            pos += 1
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos >= len(text):
                break
            if key == "text":
                value, pos, closed = _string_prefix(text, pos)
                fields[key] = value
                if not closed:
                    break
            else:
                value, pos = decoder.raw_decode(text, pos)
                fields[key] = value
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos >= len(text) or text[pos] != ",":
                break
            pos += 1
        except json.JSONDecodeError:
            break
    return fields, closed


def _speech_prefix(text: str) -> tuple[dict[str, Any], bool] | None:
    """Complete decision/playback fields plus an append-only decoded speech prefix."""
    text = text.lstrip()
    if text.startswith("```json"):
        text = text[7:].lstrip()
    elif text.startswith("```"):
        text = text[3:].lstrip()
    if not text.startswith("{"):
        return None
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object)
    fields, pos = {}, 1
    while True:
        try:
            while pos < len(text) and text[pos].isspace():
                pos += 1
            key, pos = decoder.raw_decode(text, pos)
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if not isinstance(key, str) or pos >= len(text) or text[pos] != ":":
                return None
            pos += 1
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if key in fields:
                raise ValueError(f"duplicate JSON field: {key}")
            complete_object, text_closed = True, True
            try:
                value, end = decoder.raw_decode(text, pos)
            except json.JSONDecodeError:
                if key != "speak":
                    return None
                value, text_closed = _partial_speak(text, pos, decoder)
                if not _PLAYBACK_FIELDS.issubset(value):
                    return None
                end, complete_object = len(text), False
            pos = end
            fields[key] = value
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if complete_object and ((pos >= len(text) and key != "speak")
                                    or (pos < len(text) and text[pos] not in ",}")):
                return None
        except json.JSONDecodeError:
            return None
        if all(key in fields for key in ("decision", "importance", "confidence", "speak")):
            if (fields["decision"] != "speak" or not isinstance(fields["speak"], dict)
                    or not isinstance(fields["speak"].get("text"), str) or not fields["speak"]["text"].strip()
                    or any(0xD800 <= ord(char) <= 0xDFFF for char in fields["speak"]["text"])
                    or any(type(fields[k]) not in {int, float} or not math.isfinite(fields[k])
                           or not 0 <= fields[k] <= 1 for k in ("importance", "confidence"))):
                return None
            return fields, text_closed
        if not complete_object:
            return None
        if pos >= len(text) or text[pos] == "}":
            return None
        pos += 1


def _parse_json_object(text: str) -> dict[str, Any] | None:
    value = str(text or "").strip()
    if not value:
        return None
    if value.startswith("```"):
        value = value.strip("`")
        if value.lower().startswith("json"):
            value = value[4:].strip()
    try:
        data = json.loads(value, object_pairs_hook=_unique_object)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(value[start : end + 1], object_pairs_hook=_unique_object)
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None
