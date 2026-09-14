"""llm/client.py — LLM 客户端层（同步非流式）

负责：
  - 客户端初始化（init_llm_client）
  - 远程 API 查询（remote_llm_query：DeepSeek / Gemini / AWS Bedrock）
  - 本地模型查询（local_llm_query：Ollama / LM Studio / llama-server / CLI）

依赖注入（configure()）：
  - llm_provider : str，覆盖默认 LLM_PROVIDER
  - local_llm_type : str，覆盖纯本地链路的 backend 类型
"""

import asyncio
import base64
import json
import logging
import re
from contextlib import ExitStack, closing
from typing import Any, Callable, Mapping

import requests
from openai import OpenAI

from config.settings import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL_NAME,
    OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL_NAME,
    GEMINI_API_KEY, GEMINI_MODEL_NAME,
    AWS_BEDROCK_BEARER_TOKEN, AWS_BEDROCK_AUTH_MODE, AWS_BEDROCK_REGION,
    AWS_BEDROCK_MODEL_ID, AWS_BEDROCK_USE_INFERENCE_PROFILE,
    AWS_BEDROCK_INFERENCE_PROFILE_ID, AWS_BEDROCK_ENDPOINT,
    AWS_BEDROCK_USE_CACHE, AWS_BEDROCK_CONNECTION_POOL_SIZE, AWS_BEDROCK_MAX_KEEPALIVE,
    AWS_BEDROCK_KEEPALIVE_EXPIRY,
    LOCAL_LLM_TYPE, LOCAL_LLM_MODEL,
    LOCAL_LLM_URL, LOCAL_LLM_LM_STUDIO_URL, LOCAL_LLM_OLLAMA_URL,
    LLM_PROVIDER as DEFAULT_LLM_PROVIDER,
)
from llm.gemini_client import create_gemini_client, generate_gemini_text
from llm.local_cli import local_llm_query_cli
from llm.local_backends import local_chat_url

logger = logging.getLogger(__name__)

# ===== 运行时状态 =====
LLM_PROVIDER: str = DEFAULT_LLM_PROVIDER
llm_client = None
gemini_model = None
bedrock_http_client = None
bedrock_runtime_client = None

def configure(llm_provider: str = None, local_llm_type: str = None):
    """设置当前进程的 LLM 路由选项。"""
    global LLM_PROVIDER, LOCAL_LLM_TYPE, llm_client
    if llm_provider is not None:
        previous_family = _openai_client_family(LLM_PROVIDER)
        next_provider = str(llm_provider).strip().lower()
        LLM_PROVIDER = next_provider
        if _openai_client_family(next_provider) != previous_family:
            # DeepSeek and OpenAI use the same SDK type but different endpoints.
            # Keeping the old object silently sends a newly selected model to the
            # previous service.
            llm_client = None
    if local_llm_type is not None:
        LOCAL_LLM_TYPE = local_llm_type


def _openai_client_family(provider: str) -> str:
    selected = str(provider or "").strip().lower()
    if selected in {"deepseek", "hybrid2"}:
        return "deepseek"
    if selected in {"openai", "hybrid3"}:
        return "openai"
    return ""


# =============================================================================
# 客户端初始化
# =============================================================================

def init_llm_client():
    """Initialize the appropriate LLM client based on LLM_PROVIDER."""
    global llm_client, gemini_model, bedrock_http_client, bedrock_runtime_client

    if LLM_PROVIDER in ("deepseek", "hybrid2"):
        logger.info("🚀 Initializing DeepSeek LLM client with connection pool")
        import httpx
        http_client = httpx.Client(
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=30.0,
            ),
            timeout=httpx.Timeout(30.0),
            http2=False,
        )
        llm_client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
            http_client=http_client,
        )
        logger.info("DeepSeek client configured with connection pooling; SSL handshake latency should be reduced")
        return llm_client

    elif LLM_PROVIDER in ("openai", "hybrid3"):
        logger.info(f"🚀 Initializing OpenAI LLM client: {OPENAI_MODEL_NAME}")
        import httpx
        http_client = httpx.Client(
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=30.0,
            ),
            timeout=httpx.Timeout(30.0),
            http2=False,
        )
        llm_client = OpenAI(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_BASE_URL,
            http_client=http_client,
        )
        logger.info("runtime log event at llm/client.py:97")
        return llm_client

    elif LLM_PROVIDER == "gemini":
        logger.info("Initializing Gemini LLM client")
        gemini_model = create_gemini_client(GEMINI_API_KEY)
        return gemini_model

    elif LLM_PROVIDER == "bedrock":
        logger.info("Initializing AWS Bedrock client")

        if AWS_BEDROCK_AUTH_MODE == "bearer" and not AWS_BEDROCK_BEARER_TOKEN:
            logger.error("runtime log event at llm/client.py:110")
            return None

        if AWS_BEDROCK_AUTH_MODE in ("auto", "bearer") and bedrock_http_client is None:
            try:
                import httpx
                bedrock_http_client = httpx.Client(
                    limits=httpx.Limits(
                        max_connections=AWS_BEDROCK_CONNECTION_POOL_SIZE,
                        max_keepalive_connections=AWS_BEDROCK_MAX_KEEPALIVE,
                        keepalive_expiry=AWS_BEDROCK_KEEPALIVE_EXPIRY,
                    ),
                    timeout=httpx.Timeout(30.0),
                    http2=False,
                )
                logger.info(
                    f"✅ AWS Bedrock连接池已初始化: "
                    f"最大连接数={AWS_BEDROCK_CONNECTION_POOL_SIZE}, "
                    f"保持连接数={AWS_BEDROCK_MAX_KEEPALIVE}"
                )
            except Exception:
                logger.warning("runtime log event at llm/client.py:131")
                bedrock_http_client = None

        if AWS_BEDROCK_AUTH_MODE in ("auto", "boto3") and bedrock_runtime_client is None:
            try:
                import boto3
                bedrock_runtime_client = boto3.client(
                    "bedrock-runtime", region_name=AWS_BEDROCK_REGION
                )
                logger.info("runtime log event at llm/client.py:140")
            except Exception:
                logger.warning("runtime log event at llm/client.py:142")
                bedrock_runtime_client = None

        if AWS_BEDROCK_USE_INFERENCE_PROFILE and AWS_BEDROCK_INFERENCE_PROFILE_ID:
            model_id = AWS_BEDROCK_INFERENCE_PROFILE_ID
            logger.info("runtime log event at llm/client.py:147")
            logger.info("runtime log event at llm/client.py:148")
            logger.info(f"   Inference Profile ID: {model_id}")
        else:
            model_id = AWS_BEDROCK_MODEL_ID
            logger.info("runtime log event at llm/client.py:152")
            logger.info("runtime log event at llm/client.py:153")
            logger.info("runtime log event at llm/client.py:154")
            if AWS_BEDROCK_USE_INFERENCE_PROFILE:
                logger.warning("runtime log event at llm/client.py:156")

        if AWS_BEDROCK_USE_CACHE:
            logger.info("runtime log event at llm/client.py:159")
        else:
            logger.info("runtime log event at llm/client.py:161")

        return "bedrock_client"

    elif LLM_PROVIDER == "hybrid":
        # Hybrid: 本地 9B 产首句 + Bedrock 续句
        # 预初始化 Bedrock boto3 客户端，避免首次对话时同步读取 AWS 凭证
        if bedrock_runtime_client is None:
            try:
                import boto3
                bedrock_runtime_client = boto3.client(
                    "bedrock-runtime", region_name=AWS_BEDROCK_REGION
                )
                logger.info("runtime log event at llm/client.py:174")
            except Exception:
                logger.warning("runtime log event at llm/client.py:176")
        return "hybrid_client"

    elif LLM_PROVIDER == "local":
        # Pure-local HTTP/CLI transports are opened lazily by ChatRuntime.
        return "local_client"

    else:
        logger.error(f"Unknown LLM provider: {LLM_PROVIDER}")
        return None


# =============================================================================
# 远程 API 查询（同步，非流式）
# =============================================================================

def remote_llm_messages_query(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.0,
    max_tokens: int = 900,
    timeout: float = 45.0,
    model: str | None = None,
    response_observer: Callable[[Mapping[str, Any]], None] | None = None,
    visual_context: dict[str, Any] | None = None,
    on_text: Callable[[str], None] | None = None,
    json_output: bool = True,
) -> str:
    """Query the selected Chat backend with the supplied role messages.

    ControlDecision needs the production system message and prior conversation
    as distinct roles. Flattening them into ``remote_llm_query(question)``
    silently removes the very history that resolves follow-ups and Project
    references. This narrow port therefore adapts the same message list to each
    existing Chat backend instead of restricting which model the user may pick.

    ``on_text`` consumes deltas from that same request. Exceptions propagate and
    close its stream. CLI retains its existing complete-only query because its
    shared process has no per-query cancellation owner.
    """

    global llm_client, gemini_model
    normalized = [
        {
            "role": str(message.get("role") or ""),
            "content": str(message.get("content") or ""),
        }
        for message in messages
        if str(message.get("role") or "") in {"system", "user", "assistant"}
    ]
    if not normalized or normalized[0]["role"] != "system":
        raise ValueError("message query requires a leading system message")
    if not any(message["role"] == "user" for message in normalized[1:]):
        raise ValueError("message query requires a user message")

    selected_provider = str(LLM_PROVIDER or "").strip().lower()
    if visual_context:
        from llm.visual_context import attach_openai_chat_image, visual_notice_text

        if selected_provider in {"openai", "hybrid3"} and not visual_context.get("error"):
            normalized = attach_openai_chat_image(normalized, visual_context)
        elif selected_provider != "gemini" or visual_context.get("error"):
            # Match ChatRuntime's text-only backends, including local. A visual
            # input does not change the selected model or add another model pass.
            for message in reversed(normalized):
                if message["role"] == "user":
                    message["content"] = visual_notice_text(
                        message["content"], visual_context, supported=False,
                    )
                    break
    requested_model = ""
    response_model = None
    response_id = None
    finish_reason = None

    if selected_provider in ("deepseek", "hybrid2"):
        if llm_client is None:
            llm_client = init_llm_client()
        requested_model = str(model or DEEPSEEK_MODEL_NAME)
        response = llm_client.chat.completions.create(
            model=requested_model,
            messages=normalized,
            temperature=float(temperature),
            max_tokens=max(1, int(max_tokens)),
            stream=on_text is not None,
            timeout=float(timeout),
            **({"response_format": {"type": "json_object"}} if json_output else {}),
            extra_body={"thinking": {"type": "disabled"}},
        )
        content, response_model, response_id, finish_reason = (
            _openai_response_details(response, on_text=on_text)
        )
    elif selected_provider in ("openai", "hybrid3"):
        if llm_client is None:
            llm_client = init_llm_client()
        requested_model = str(model or OPENAI_MODEL_NAME)
        response = llm_client.chat.completions.create(
            model=requested_model,
            messages=normalized,
            max_completion_tokens=max(1, int(max_tokens)),
            reasoning_effort="low",
            stream=on_text is not None,
            timeout=float(timeout),
            **({"response_format": {"type": "json_object"}} if json_output else {}),
        )
        content, response_model, response_id, finish_reason = (
            _openai_response_details(response, on_text=on_text)
        )
    elif selected_provider == "gemini":
        if gemini_model is None:
            gemini_model = init_llm_client()
        requested_model = str(model or GEMINI_MODEL_NAME)
        contents = [
            {
                "role": "model" if message["role"] == "assistant" else "user",
                "parts": [{"text": message["content"]}],
            }
            for message in normalized[1:]
        ]
        if visual_context and not visual_context.get("error"):
            from google.genai import types
            from llm.visual_context import gemini_contents

            for index in range(len(contents) - 1, -1, -1):
                if normalized[index + 1]["role"] == "user":
                    # The public SDK content adapter encodes the helper's PIL
                    # image while keeping this turn separate from prior roles.
                    contents[index] = types.UserContent(parts=gemini_contents(
                        normalized[index + 1]["content"], visual_context,
                    ))
                    break
        generation_config = {
            "system_instruction": normalized[0]["content"],
            "temperature": float(temperature),
            "max_output_tokens": max(1, int(max_tokens)),
            **({"response_mime_type": "application/json"} if json_output else {}),
        }
        if on_text is None:
            content = generate_gemini_text(
                gemini_model, model=requested_model, contents=contents, config=generation_config,
            )
        else:
            stream = gemini_model.models.generate_content_stream(
                model=requested_model, contents=contents, config=generation_config,
            )
            content = _collect_text_stream(stream, lambda chunk: chunk.text or "", on_text)
        response_model = requested_model
    elif selected_provider in {"bedrock", "hybrid"}:
        requested_model = str(model or _bedrock_model_id())
        content = _bedrock_messages_query(
            normalized,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            model=requested_model,
            **({"on_text": on_text} if on_text is not None else {}),
        )
        response_model = requested_model
    elif selected_provider == "local":
        requested_model = str(model or LOCAL_LLM_MODEL)
        content = _local_messages_query(
            normalized,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            model=requested_model,
            **({"on_text": on_text} if on_text is not None else {}),
            **({"json_output": False} if not json_output else {}),
        )
        response_model = requested_model
    else:
        raise RuntimeError(f"message query is unavailable for {selected_provider!r}")

    if response_observer is not None:
        # Explicit diagnostic probes may inspect the native response boundary.
        # No routine logging, new model call, or change to the text contract.
        try:
            response_observer({
                "provider": selected_provider,
                "requested_model": requested_model,
                "response_model": response_model,
                "response_id": response_id,
                "finish_reason": finish_reason,
                "content_type": type(content).__name__,
                "content": content,
            })
        except Exception:
            logger.debug("structured response observation failed", exc_info=True)
    return str(content or "")


def _collect_text_stream(stream, extract, on_text: Callable[[str], None]) -> str:
    pieces = []
    with closing(stream):
        for chunk in stream:
            text = extract(chunk)
            if text:
                pieces.append(text)
                on_text(text)
    return "".join(pieces)


def _openai_response_details(response: Any, *, on_text=None) -> tuple[Any, Any, Any, Any]:
    if on_text is not None:
        model = response_id = finish_reason = None

        def extract(chunk):
            nonlocal model, response_id, finish_reason
            model = getattr(chunk, "model", None) or model
            response_id = getattr(chunk, "id", None) or response_id
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                return ""
            finish_reason = getattr(choices[0], "finish_reason", None) or finish_reason
            return getattr(getattr(choices[0], "delta", None), "content", "") or ""

        content = _collect_text_stream(response, extract, on_text)
        return content, model, response_id, finish_reason
    if not response or not getattr(response, "choices", None):
        raise RuntimeError("structured control backend returned no choices")
    choice = response.choices[0]
    return (
        getattr(getattr(choice, "message", None), "content", "") or "",
        getattr(response, "model", None),
        getattr(response, "id", None),
        getattr(choice, "finish_reason", None),
    )


def _bedrock_model_id() -> str:
    if AWS_BEDROCK_USE_INFERENCE_PROFILE and AWS_BEDROCK_INFERENCE_PROFILE_ID:
        return str(AWS_BEDROCK_INFERENCE_PROFILE_ID)
    return str(AWS_BEDROCK_MODEL_ID)


def _bedrock_messages_query(
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
    timeout: float,
    model: str,
    on_text: Callable[[str], None] | None = None,
) -> str:
    global bedrock_http_client, bedrock_runtime_client

    init_llm_client()
    payload = {
        "max_tokens": max(1, int(max_tokens)),
        "temperature": float(temperature),
        "messages": messages,
    }
    if on_text is not None:
        return _bedrock_messages_stream(payload, model=model, timeout=timeout, on_text=on_text)
    boto3_error: Exception | None = None
    if AWS_BEDROCK_AUTH_MODE != "bearer":
        try:
            if bedrock_runtime_client is None:
                import boto3

                bedrock_runtime_client = boto3.client(
                    "bedrock-runtime", region_name=AWS_BEDROCK_REGION
                )
            response = bedrock_runtime_client.invoke_model(
                modelId=model,
                body=json.dumps(payload),
            )
            return _bedrock_response_text(json.loads(response["body"].read()))
        except Exception as exc:
            boto3_error = exc
            if AWS_BEDROCK_AUTH_MODE == "boto3":
                raise RuntimeError(f"Bedrock boto3 request failed: {exc}") from exc

    if not AWS_BEDROCK_BEARER_TOKEN:
        detail = f": {boto3_error}" if boto3_error else ""
        raise RuntimeError("Bedrock bearer token is unavailable" + detail)
    url = f"{AWS_BEDROCK_ENDPOINT}/model/{model}/invoke"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AWS_BEDROCK_BEARER_TOKEN}",
    }
    client = bedrock_http_client
    if client is not None:
        response = client.post(url, headers=headers, json=payload, timeout=float(timeout))
    else:
        response = requests.post(
            url, headers=headers, json=payload, timeout=float(timeout)
        )
    response.raise_for_status()
    return _bedrock_response_text(response.json())


def _bedrock_messages_stream(payload, *, model, timeout, on_text) -> str:
    """Use the existing Bedrock stream protocol; never retry after submission."""
    from llm.hybrid_stream import _extract_token, _SENTINEL

    payload = {**payload, "model": model, "stream": True}
    pieces = []

    def accept(data):
        if data.get("type") == "error" or data.get("error"):
            raise RuntimeError(f"Bedrock stream error: {data}")
        token = _extract_token(data)
        if token is _SENTINEL:
            return False
        if token:
            pieces.append(token)
            on_text(token)
        return True

    if AWS_BEDROCK_AUTH_MODE != "bearer" and bedrock_runtime_client is not None:
        response = bedrock_runtime_client.invoke_model_with_response_stream(
            modelId=model, body=json.dumps(payload),
        )
        with closing(response["body"]) as stream:
            for event in stream:
                if "chunk" not in event:
                    raise RuntimeError(f"Bedrock stream error: {event}")
                if not accept(json.loads(event["chunk"]["bytes"])):
                    break
    else:
        if AWS_BEDROCK_AUTH_MODE == "boto3" or not AWS_BEDROCK_BEARER_TOKEN:
            raise RuntimeError("Bedrock streaming authentication is unavailable")
        from botocore.eventstream import EventStreamBuffer

        url = f"{AWS_BEDROCK_ENDPOINT}/model/{model}/invoke-with-response-stream"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {AWS_BEDROCK_BEARER_TOKEN}",
        }
        with ExitStack() as stack:
            if bedrock_http_client is not None:
                response = stack.enter_context(bedrock_http_client.stream(
                    "POST", url, headers=headers, json=payload, timeout=float(timeout),
                ))
                chunks = response.iter_bytes()
            else:
                response = stack.enter_context(closing(requests.post(
                    url, headers=headers, json=payload, timeout=float(timeout), stream=True,
                )))
                chunks = response.iter_content(chunk_size=8192)
            response.raise_for_status()
            buffer = EventStreamBuffer()
            for chunk in chunks:
                buffer.add_data(chunk)
                for event in buffer:
                    if event.headers.get(":message-type") in {"exception", "error"}:
                        raise RuntimeError(f"Bedrock stream error: {event.payload!r}")
                    data = json.loads(event.payload)
                    if "bytes" in data:
                        data = json.loads(base64.b64decode(data["bytes"]))
                    if not accept(data):
                        return "".join(pieces)
    return "".join(pieces)


def _bedrock_response_text(result: Mapping[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        text = "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, Mapping)
        )
        if text:
            return text
    output = result.get("output")
    if isinstance(output, Mapping):
        message = output.get("message")
        if isinstance(message, Mapping):
            return _bedrock_response_text(message)
    raise RuntimeError("Bedrock structured message query returned no text")


def _local_messages_query(
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
    timeout: float,
    model: str,
    on_text: Callable[[str], None] | None = None,
    json_output: bool = True,
) -> str:
    backend = str(LOCAL_LLM_TYPE or "").strip().lower()
    if backend == "cli":
        transcript = "\n".join(
            f"{message['role'].capitalize()}: {message['content']}"
            for message in messages[1:]
        )
        content = asyncio.run(
            local_llm_query_cli(
                transcript,
                stream=False,
                system_prompt=messages[0]["content"],
            )
        )
        # The reusable CLI process has no per-query cancellation owner. Keep its
        # existing single complete query; do not claim native token streaming.
        if on_text is not None:
            on_text(content)
        return content

    url = local_chat_url(
        backend,
        llama_server_url=LOCAL_LLM_URL,
        lmstudio_url=LOCAL_LLM_LM_STUDIO_URL,
        ollama_url=LOCAL_LLM_OLLAMA_URL,
    )
    if backend == "ollama":
        payload = {
            "model": model,
            "messages": messages,
            "stream": on_text is not None,
            **({"format": "json"} if json_output else {}),
            "options": {
                "temperature": float(temperature),
                "num_predict": max(1, int(max_tokens)),
            },
        }
        if on_text is not None:
            return _local_messages_stream(url, payload, timeout=timeout, on_text=on_text, ollama=True)
        response = requests.post(url, json=payload, timeout=float(timeout))
        response.raise_for_status()
        result = response.json()
        message = result.get("message") if isinstance(result, Mapping) else None
        if not isinstance(message, Mapping):
            raise RuntimeError("Ollama message query returned no message")
        return str(message.get("content") or "")

    if backend not in {"llama_server", "lmstudio"}:
        raise RuntimeError(f"unsupported local LLM type: {backend!r}")
    payload = {
        "model": model,
        "messages": messages,
        "stream": on_text is not None,
        "temperature": float(temperature),
        "max_tokens": max(1, int(max_tokens)),
    }
    if backend == "llama_server":
        payload["cache_prompt"] = True
    if on_text is not None:
        return _local_messages_stream(url, payload, timeout=timeout, on_text=on_text)
    response = requests.post(url, json=payload, timeout=float(timeout))
    response.raise_for_status()
    result = response.json()
    choices = result.get("choices") if isinstance(result, Mapping) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("local message query returned no choices")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    if not isinstance(message, Mapping):
        raise RuntimeError("local message query returned no message")
    content = str(message.get("content") or "")
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def _local_messages_stream(url, payload, *, timeout, on_text, ollama=False) -> str:
    pieces = []
    with closing(requests.post(
        url, json=payload, timeout=float(timeout), stream=True,
    )) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            line = line.decode("utf-8") if isinstance(line, bytes) else line
            if not line:
                continue
            if not ollama:
                if not line.startswith("data:"):
                    continue
                line = line[5:].strip()
                if line == "[DONE]":
                    break
            data = json.loads(line)
            if data.get("error"):
                raise RuntimeError(f"local stream error: {data['error']}")
            if ollama:
                text = (data.get("message") or {}).get("content") or ""
            else:
                choices = data.get("choices") or []
                text = ((choices[0].get("delta") or {}).get("content") or "") if choices else ""
            if text:
                pieces.append(text)
                on_text(text)
            if ollama and data.get("done"):
                break
    return "".join(pieces)

from llm.prompts import get_system_prompt as _get_system_prompt

# 保留模块级别名，供外部直接引用（动态求值，每次调用都读当前语言）
def _SYSTEM_PROMPT_BASE():         return _get_system_prompt("base")
def _SYSTEM_PROMPT_WITH_DELEGATE(): return _get_system_prompt("with_delegate")


def remote_llm_query(
    question: str,
    system_prompt: str | None = None,
    *,
    temperature: float = 0.7,
) -> str:
    """Call online API (DeepSeek, Gemini, or AWS Bedrock), with enhanced error handling.

    `system_prompt` overrides the default for callers that need a different
    contract in force — asking the model to re-emit a delegate it omitted needs
    the variant that documents the tag, which the base prompt deliberately does
    not.
    """

    global llm_client, gemini_model
    _system = system_prompt if system_prompt else None

    try:
        if LLM_PROVIDER in ("deepseek", "hybrid2", "openai", "hybrid3") and llm_client is None:
            llm_client = init_llm_client()
        elif LLM_PROVIDER == "gemini" and gemini_model is None:
            gemini_model = init_llm_client()
        elif LLM_PROVIDER == "bedrock":
            init_llm_client()

        logger.info(f"Sending API request to {LLM_PROVIDER}...")

        # ── DeepSeek ──────────────────────────────────────────────────────────
        if LLM_PROVIDER in ("deepseek", "hybrid2"):
            response = llm_client.chat.completions.create(
                model=DEEPSEEK_MODEL_NAME,
                messages=[
                    {"role": "system", "content": _system or _SYSTEM_PROMPT_BASE()},
                    {"role": "user", "content": question},
                ],
                temperature=temperature,
                max_tokens=500,
                stream=False,
                timeout=5,
                extra_body={"thinking": {"type": "disabled"}},
            )
            if not response or not hasattr(response, "choices") or not response.choices:
                logger.warning("⚠️ DeepSeek API returned invalid response")
                return "APIからの応答が無効です."
            reply = response.choices[0].message.content

        # ── OpenAI / GPT ─────────────────────────────────────────────────────
        elif LLM_PROVIDER in ("openai", "hybrid3"):
            response = llm_client.chat.completions.create(
                model=OPENAI_MODEL_NAME,
                messages=[
                    {"role": "system", "content": _system or _SYSTEM_PROMPT_BASE()},
                    {"role": "user", "content": question},
                ],
                max_completion_tokens=500,
                reasoning_effort="low",
                stream=False,
                timeout=10,
            )
            if not response or not hasattr(response, "choices") or not response.choices:
                logger.warning("⚠️ OpenAI API returned invalid response")
                return "OpenAI APIからの応答が無効です."
            reply = response.choices[0].message.content

        # ── Gemini ────────────────────────────────────────────────────────────
        elif LLM_PROVIDER == "gemini":
            if gemini_model is None:
                logger.info("Initializing Gemini LLM client")
                gemini_model = create_gemini_client(GEMINI_API_KEY)
            full_prompt = f"{_SYSTEM_PROMPT_WITH_DELEGATE()}\n\n質問:{question}"
            generation_config = {
                "temperature": temperature,
                "top_p": 0.95,
                "top_k": 64,
                "max_output_tokens": 1000,
            }
            try:
                reply = generate_gemini_text(
                    gemini_model,
                    model=GEMINI_MODEL_NAME,
                    contents=full_prompt,
                    config=generation_config,
                )
                if not reply:
                    logger.warning("⚠️ Gemini API returned invalid response")
                    return "Gemini APIからの応答が無効です."
                logger.info(f"✓ Gemini API response successful, length: {len(reply)}")
                return reply
            except Exception as e:
                logger.error(f"❌ Gemini API error: {str(e)}")
                return f"Gemini APIエラー:{str(e)}"

        # ── AWS Bedrock ───────────────────────────────────────────────────────
        elif LLM_PROVIDER == "bedrock":
            system_prompt = _get_system_prompt("bedrock")
            if AWS_BEDROCK_USE_INFERENCE_PROFILE and AWS_BEDROCK_INFERENCE_PROFILE_ID:
                model_id = AWS_BEDROCK_INFERENCE_PROFILE_ID
            else:
                model_id = AWS_BEDROCK_MODEL_ID
            try:
                boto3_error = None
                try:
                    if AWS_BEDROCK_AUTH_MODE == "bearer":
                        raise ImportError("BEDROCK_AUTH_MODE=bearer")
                    import boto3
                    global bedrock_runtime_client
                    if bedrock_runtime_client is None:
                        bedrock_runtime_client = boto3.client(
                            "bedrock-runtime", region_name=AWS_BEDROCK_REGION,
                            aws_access_key_id=None, aws_secret_access_key=None,
                        )
                    payload = {
                        "max_tokens": 500,
                        "temperature": temperature,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": question},
                        ],
                    }
                    response = bedrock_runtime_client.invoke_model(
                        modelId=model_id, body=json.dumps(payload)
                    )
                    result = json.loads(response["body"].read())
                    if "content" in result and len(result["content"]) > 0:
                        reply = result["content"][0]["text"]
                    else:
                        logger.warning(f"⚠️ Bedrock API returned invalid response: {result}")
                        return "Bedrock APIからの応答が無効です."
                    logger.info(f"✓ Bedrock API response successful (boto3), reply length: {len(reply)}")
                    return reply
                except ImportError as exc:
                    boto3_error = exc
                    if AWS_BEDROCK_AUTH_MODE == "bearer":
                        logger.info("runtime log event at llm/client.py:314")
                    else:
                        logger.warning("runtime log event at llm/client.py:316")
                except Exception as boto_error:
                    boto3_error = boto_error
                    logger.warning("runtime log event at llm/client.py:319")

                if AWS_BEDROCK_AUTH_MODE == "boto3":
                    raise RuntimeError(f"Bedrock boto3 auth failed and fallback is disabled: {boto3_error}")
                if boto3_error and AWS_BEDROCK_AUTH_MODE == "auto":
                    logger.info("runtime log event at llm/client.py:324")
                if not AWS_BEDROCK_BEARER_TOKEN:
                    raise RuntimeError("AWS_BEARER_TOKEN_BEDROCK未设置，无法使用HTTP Bearer fallback")

                # HTTP 降级
                url = f"{AWS_BEDROCK_ENDPOINT}/model/{model_id}/invoke"
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {AWS_BEDROCK_BEARER_TOKEN}",
                }
                payload = {
                    "max_tokens": 500,
                    "temperature": temperature,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": question},
                    ],
                }
                global bedrock_http_client
                if bedrock_http_client is not None:
                    response = bedrock_http_client.post(url, headers=headers, json=payload, timeout=30)
                else:
                    response = requests.post(url, headers=headers, json=payload, timeout=30)
                if response.status_code != 200:
                    error_detail = response.text
                    logger.error("runtime log event at llm/client.py:349")
                    return f"Bedrock APIエラー: {response.status_code} - {error_detail[:200]}"
                result = response.json()
                if "content" in result and len(result["content"]) > 0:
                    reply = result["content"][0]["text"]
                else:
                    logger.warning(f"⚠️ Bedrock API returned invalid response: {result}")
                    return "Bedrock APIからの応答が無効です."
                logger.info(f"✓ Bedrock API response successful (HTTP), reply length: {len(reply)}")
                return reply
            except Exception as e:
                logger.error(f"❌ Bedrock API error: {str(e)}")
                logger.error("runtime log event at llm/client.py:361")
                return f"Bedrock APIエラー:{str(e)}"

        logger.info(f"✓ {LLM_PROVIDER} API response successful, reply length: {len(reply)}")
        return reply

    except Exception as e:
        logger.error(f"❌ Failed to call online LLM ({LLM_PROVIDER}): {str(e)}")
        return "すみません,今ちょっと調子が悪いです……."


# =============================================================================
# 本地模型查询（同步，非流式）
# =============================================================================

def local_llm_query(question: str, *, system_prompt: str | None = None) -> str:
    """调用本地模型(Ollama / LM Studio / llama-server / CLI) - 非流式版本"""
    try:
        _system = system_prompt or _get_system_prompt("local_fallback")

        if LOCAL_LLM_TYPE == "ollama":
            payload = {
                "model": LOCAL_LLM_MODEL,
                "messages": [
                    {"role": "system", "content": _system},
                    {"role": "user", "content": question},
                ],
                "stream": False,
                "temperature": 0.7,
            }
            response = requests.post(
                local_chat_url(
                    "ollama",
                    llama_server_url=LOCAL_LLM_URL,
                    lmstudio_url=LOCAL_LLM_LM_STUDIO_URL,
                    ollama_url=LOCAL_LLM_OLLAMA_URL,
                ),
                json=payload,
                timeout=20,
            )
            response.raise_for_status()
            reply = response.json()["message"]["content"]

        elif LOCAL_LLM_TYPE == "lmstudio":
            payload = {
                "model": LOCAL_LLM_MODEL,
                "messages": [
                    {"role": "system", "content": _system},
                    {"role": "user", "content": question},
                ],
                "stream": False,
                "temperature": 0.7,
            }
            response = requests.post(
                local_chat_url(
                    "lmstudio",
                    llama_server_url=LOCAL_LLM_URL,
                    lmstudio_url=LOCAL_LLM_LM_STUDIO_URL,
                    ollama_url=LOCAL_LLM_OLLAMA_URL,
                ),
                json=payload,
                timeout=20,
            )
            response.raise_for_status()
            reply = response.json()["choices"][0]["message"]["content"]

        elif LOCAL_LLM_TYPE == "cli":
            import asyncio
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            reply = loop.run_until_complete(
                local_llm_query_cli(
                    question, stream=False,
                    **({"system_prompt": system_prompt} if system_prompt else {}),
                )
            )

        elif LOCAL_LLM_TYPE == "llama_server":
            payload = {
                "model": LOCAL_LLM_MODEL,
                "messages": [
                    {"role": "system", "content": _system},
                    {"role": "user", "content": question},
                ],
                "stream": False,
                "temperature": 0.7,
                "cache_prompt": True,
            }
            response = requests.post(
                local_chat_url(
                    "llama_server",
                    llama_server_url=LOCAL_LLM_URL,
                    lmstudio_url=LOCAL_LLM_LM_STUDIO_URL,
                    ollama_url=LOCAL_LLM_OLLAMA_URL,
                ),
                json=payload,
                timeout=20,
            )
            response.raise_for_status()
            raw_reply = response.json()["choices"][0]["message"]["content"]
            import re as _re
            reply = _re.sub(r"<think>.*?</think>", "", raw_reply, flags=_re.DOTALL).strip()

        else:
            raise ValueError(f"未知的本地LLM类型: {LOCAL_LLM_TYPE}")

        logger.info("runtime log event at llm/client.py:441")
        return reply

    except Exception:
        logger.error("runtime log event at llm/client.py:445")
        return "(ローカルモデルの応答に失敗しました……)"
