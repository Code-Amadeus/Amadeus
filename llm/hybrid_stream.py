"""Hybrid LLM dual stream.

Local LLM produces the first short Japanese sentence for fast first voice.
The remote tail model produces the full answer in parallel; its first sentence
is skipped and the remainder is streamed after the local first sentence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import AsyncGenerator

import aiohttp

from config.settings import (
    AWS_BEDROCK_BEARER_TOKEN,
    AWS_BEDROCK_AUTH_MODE,
    AWS_BEDROCK_ENDPOINT,
    AWS_BEDROCK_INFERENCE_PROFILE_ID,
    AWS_BEDROCK_MODEL_ID,
    AWS_BEDROCK_REGION,
    AWS_BEDROCK_USE_INFERENCE_PROFILE,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL_NAME,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL_NAME,
    HYBRID_LOCAL_LLM_MODEL,
    HYBRID_LOCAL_LLM_URL,
)
from llm.local_backends import openai_chat_url
logger = logging.getLogger(__name__)

_SENTENCE_ENDINGS = frozenset("。！？!?\n、；;")
_SENTINEL = object()


async def hybrid_llm_stream(
    messages_local: list[dict],
    messages_remote: list[dict],
    bedrock_model_id: str | None = None,
    tail_provider: str = "bedrock",
) -> AsyncGenerator[tuple[str, str], None]:
    """Yield ``(source, text_chunk)`` from local first sentence + remote tail."""
    local_q: asyncio.Queue = asyncio.Queue()
    remote_q: asyncio.Queue = asyncio.Queue()

    async def _run_local() -> None:
        try:
            payload = {
                "model": HYBRID_LOCAL_LLM_MODEL,
                "messages": messages_local,
                "stream": True,
                "temperature": 0.35,
                "top_p": 0.9,
                "cache_prompt": True,
                "max_tokens": 80,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    openai_chat_url(HYBRID_LOCAL_LLM_URL),
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30, sock_read=20),
                ) as resp:
                    resp.raise_for_status()
                    try:
                        from tts.latency_clock import log_latency_marker

                        # 与 local_first_token 的差值 = llama-server prefill 时间
                        log_latency_marker(logger, "local_post_opened", status=resp.status)
                    except Exception:
                        pass
                    async for line_bytes in resp.content:
                        line = line_bytes.decode("utf-8").strip()
                        if not line or not line.startswith("data: "):
                            continue
                        json_str = line[6:]
                        if json_str == "[DONE]":
                            break
                        try:
                            data = json.loads(json_str)
                            delta = (data.get("choices") or [{}])[0].get("delta", {})
                            token: str = delta.get("content") or delta.get("text") or ""
                        except (json.JSONDecodeError, IndexError):
                            continue
                        if token:
                            token = re.sub(r"<think>.*?</think>", "", token, flags=re.DOTALL)
                            if token:
                                await local_q.put(token)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("[Hybrid] local stream error: %s", exc)
        finally:
            await local_q.put(_SENTINEL)

    async def _run_bedrock() -> None:
        try:
            if bedrock_model_id:
                model_id = bedrock_model_id
            elif AWS_BEDROCK_USE_INFERENCE_PROFILE and AWS_BEDROCK_INFERENCE_PROFILE_ID:
                model_id = AWS_BEDROCK_INFERENCE_PROFILE_ID
            else:
                model_id = AWS_BEDROCK_MODEL_ID

            payload_dict = {
                "model": model_id,
                "max_tokens": 500,
                "temperature": 0.7,
                "messages": messages_remote,
                "stream": True,
            }

            boto3_error = None
            try:
                if AWS_BEDROCK_AUTH_MODE == "bearer":
                    raise ImportError("BEDROCK_AUTH_MODE=bearer")
                import boto3
                from llm.client import bedrock_runtime_client as _brc

                brc = _brc
                if brc is None:
                    brc = boto3.client("bedrock-runtime", region_name=AWS_BEDROCK_REGION)

                loop = asyncio.get_event_loop()

                def _boto3_call():
                    return brc.invoke_model_with_response_stream(
                        modelId=model_id,
                        body=json.dumps(payload_dict),
                    ).get("body")

                stream = await loop.run_in_executor(None, _boto3_call)
                if stream:
                    def _boto3_iter():
                        for event in stream:
                            if "chunk" not in event:
                                continue
                            try:
                                chunk_data = json.loads(event["chunk"]["bytes"].decode("utf-8"))
                            except Exception:
                                continue
                            token = _extract_token(chunk_data)
                            if token is _SENTINEL:
                                return
                            if token:
                                loop.call_soon_threadsafe(remote_q.put_nowait, token)

                    await loop.run_in_executor(None, _boto3_iter)
                    return
            except ImportError as exc:
                boto3_error = exc
                if AWS_BEDROCK_AUTH_MODE == "bearer":
                    logger.debug("[Hybrid] BEDROCK_AUTH_MODE=bearer, skipping boto3")
                else:
                    logger.debug("[Hybrid] boto3 not installed, using HTTP")
            except Exception as boto_err:
                boto3_error = boto_err
                logger.debug("[Hybrid] boto3 error: %s", boto_err)

            if AWS_BEDROCK_AUTH_MODE == "boto3":
                raise RuntimeError(f"Bedrock boto3 auth failed and fallback is disabled: {boto3_error}")
            if boto3_error and AWS_BEDROCK_AUTH_MODE == "auto":
                logger.debug("[Hybrid] BEDROCK_AUTH_MODE=auto, falling back to HTTP")
            if not AWS_BEDROCK_BEARER_TOKEN:
                raise RuntimeError("AWS_BEARER_TOKEN_BEDROCK is not set; HTTP Bearer fallback unavailable")

            url = f"{AWS_BEDROCK_ENDPOINT}/model/{model_id}/invoke-with-response-stream"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {AWS_BEDROCK_BEARER_TOKEN}",
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    headers=headers,
                    json=payload_dict,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    resp.raise_for_status()
                    buf = b""
                    async for raw_chunk in resp.content.iter_chunked(8192):
                        buf += raw_chunk
                        while len(buf) >= 12:
                            total_len = int.from_bytes(buf[:4], "big")
                            hdr_len = int.from_bytes(buf[4:8], "big")
                            if total_len < 16 or hdr_len > total_len - 16:
                                logger.warning(
                                    "[Hybrid] invalid Bedrock event frame: total=%s headers=%s head=%r",
                                    total_len,
                                    hdr_len,
                                    buf[:32],
                                )
                                buf = b""
                                break
                            if len(buf) < total_len:
                                break
                            evt = buf[:total_len]
                            buf = buf[total_len:]
                            if len(evt) < 12 + hdr_len + 4:
                                continue
                            payload_bytes = evt[12 + hdr_len : total_len - 4]
                            try:
                                payload_text = payload_bytes.decode("utf-8", errors="ignore")
                                json_start = payload_text.find("{")
                                if json_start > 0:
                                    payload_text = payload_text[json_start:]
                                data = json.loads(payload_text)
                            except Exception:
                                continue
                            if "bytes" in data:
                                import base64

                                try:
                                    data = json.loads(base64.b64decode(data["bytes"]))
                                except Exception:
                                    continue
                            token = _extract_token(data)
                            if token is _SENTINEL:
                                return
                            if token:
                                await remote_q.put(token)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("[Hybrid] bedrock stream error: %s", exc)
        finally:
            await remote_q.put(_SENTINEL)

    async def _run_deepseek() -> None:
        try:
            if not DEEPSEEK_API_KEY:
                raise RuntimeError("DEEPSEEK_API_KEY is not configured")
            loop = asyncio.get_event_loop()

            def _deepseek_iter() -> None:
                from llm.client import llm_client as _client

                client = _client
                if client is None:
                    import httpx
                    from openai import OpenAI

                    client = OpenAI(
                        api_key=DEEPSEEK_API_KEY,
                        base_url=DEEPSEEK_BASE_URL,
                        http_client=httpx.Client(
                            limits=httpx.Limits(
                                max_connections=4,
                                max_keepalive_connections=2,
                                keepalive_expiry=30.0,
                            ),
                            timeout=httpx.Timeout(30.0),
                            http2=False,
                        ),
                    )

                stream = client.chat.completions.create(
                    model=DEEPSEEK_MODEL_NAME,
                    messages=messages_remote,
                    stream=True,
                    temperature=0.7,
                    max_tokens=500,
                    timeout=20,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                for chunk in stream:
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    delta = getattr(choices[0], "delta", None)
                    token = (
                        getattr(delta, "content", None)
                        or getattr(delta, "text", None)
                        or ""
                    )
                    if token:
                        loop.call_soon_threadsafe(remote_q.put_nowait, token)

            await loop.run_in_executor(None, _deepseek_iter)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("[Hybrid2] deepseek stream error: %s", exc)
        finally:
            await remote_q.put(_SENTINEL)

    async def _run_openai() -> None:
        try:
            if not OPENAI_API_KEY:
                raise RuntimeError("OPENAI_API_KEY is not configured")
            loop = asyncio.get_event_loop()

            def _openai_iter() -> None:
                from llm.client import llm_client as _client

                client = _client
                if client is None:
                    import httpx
                    from openai import OpenAI

                    client = OpenAI(
                        api_key=OPENAI_API_KEY,
                        base_url=OPENAI_BASE_URL,
                        http_client=httpx.Client(
                            limits=httpx.Limits(
                                max_connections=4,
                                max_keepalive_connections=2,
                                keepalive_expiry=30.0,
                            ),
                            timeout=httpx.Timeout(30.0),
                            http2=False,
                        ),
                    )

                stream = client.chat.completions.create(
                    model=OPENAI_MODEL_NAME,
                    messages=messages_remote,
                    stream=True,
                    timeout=20,
                    max_completion_tokens=500,
                    reasoning_effort="low",
                )
                for chunk in stream:
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    delta = getattr(choices[0], "delta", None)
                    token = (
                        getattr(delta, "content", None)
                        or getattr(delta, "text", None)
                        or ""
                    )
                    if token:
                        loop.call_soon_threadsafe(remote_q.put_nowait, token)

            await loop.run_in_executor(None, _openai_iter)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("[Hybrid3] openai stream error: %s", exc)
        finally:
            await remote_q.put(_SENTINEL)

    t_local = asyncio.create_task(_run_local())
    normalized_tail_provider = (tail_provider or "bedrock").strip().lower()
    if normalized_tail_provider == "deepseek":
        t_remote = asyncio.create_task(_run_deepseek())
    elif normalized_tail_provider == "openai":
        t_remote = asyncio.create_task(_run_openai())
    else:
        normalized_tail_provider = "bedrock"
        t_remote = asyncio.create_task(_run_bedrock())

    try:
        local_buf = ""
        while True:
            token = await local_q.get()
            if token is _SENTINEL:
                break
            yield ("local", token)
            local_buf += token
            if any(c in local_buf for c in _SENTENCE_ENDINGS):
                t_local.cancel()
                break

        remote_buf = ""
        remote_first_done = False
        while True:
            token = await remote_q.get()
            if token is _SENTINEL:
                break
            if not remote_first_done:
                remote_buf += token
                for i, ch in enumerate(remote_buf):
                    if ch in _SENTENCE_ENDINGS:
                        remote_first_done = True
                        remainder = remote_buf[i + 1 :]
                        remote_buf = ""
                        if remainder.strip():
                            yield (normalized_tail_provider, remainder)
                        break
            else:
                yield (normalized_tail_provider, token)
    finally:
        for task in (t_local, t_remote):
            if task is not None and not task.done():
                task.cancel()


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
