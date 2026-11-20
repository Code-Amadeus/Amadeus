"""llm/client.py — LLM 客户端层（同步非流式）

负责：
  - 客户端初始化（init_llm_client）
  - 消息查询（remote_llm_messages_query：远程 API / 本地模型）
  - 本地 messages 查询（Ollama / LM Studio / llama-server）

依赖注入（configure()）：
  - llm_provider : str，覆盖默认 LLM_PROVIDER
  - local_llm_type : str，覆盖纯本地链路的 backend 类型
"""

import base64
import json
import logging
import re
import time
import uuid
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
    AWS_BEDROCK_USE_CACHE, AWS_BEDROCK_CACHE_TTL, AWS_BEDROCK_CONNECTION_POOL_SIZE, AWS_BEDROCK_MAX_KEEPALIVE,
    AWS_BEDROCK_KEEPALIVE_EXPIRY,
    LOCAL_LLM_TYPE, LOCAL_LLM_MODEL,
    LOCAL_LLM_URL, LOCAL_LLM_LM_STUDIO_URL, LOCAL_LLM_OLLAMA_URL,
    LLM_PROVIDER as DEFAULT_LLM_PROVIDER,
)
from llm.gemini_client import create_gemini_client, generate_gemini_text
from llm.local_backends import local_chat_url, require_llama_message_capacity

logger = logging.getLogger(__name__)

# ===== 运行时状态 =====
LLM_PROVIDER: str = DEFAULT_LLM_PROVIDER
llm_client = None
gemini_model = None
bedrock_http_client = None
bedrock_runtime_client = None

def configure(llm_provider: str = None, local_llm_type: str = None):
    """Own process routing changes and publish them to settings readers."""
    global LLM_PROVIDER, LOCAL_LLM_TYPE, llm_client
    from config import settings

    if local_llm_type is not None:
        local_llm_type = str(local_llm_type or "").strip().lower()
        if local_llm_type not in {"llama_server", "lmstudio", "ollama", "cli"}:
            raise ValueError(f"unsupported local LLM type: {local_llm_type!r}")
    if llm_provider is not None:
        previous_family = _openai_client_family(LLM_PROVIDER)
        next_provider = str(llm_provider).strip().lower()
        LLM_PROVIDER = next_provider
        # Local lifecycle, Work observer and AUIP narration read this projection.
        settings.LLM_PROVIDER = next_provider
        if _openai_client_family(next_provider) != previous_family:
            # DeepSeek and OpenAI use the same SDK type but different endpoints.
            # Keeping the old object silently sends a newly selected model to the
            # previous service.
            llm_client = None
    if local_llm_type is not None:
        LOCAL_LLM_TYPE = local_llm_type
        settings.LOCAL_LLM_TYPE = local_llm_type


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
        logger.info("OpenAI-compatible client initialized with connection pooling")
        return llm_client

    elif LLM_PROVIDER == "gemini":
        logger.info("Initializing Gemini LLM client")
        gemini_model = create_gemini_client(GEMINI_API_KEY)
        return gemini_model

    elif LLM_PROVIDER == "bedrock":
        logger.info("Initializing AWS Bedrock client")

        if AWS_BEDROCK_AUTH_MODE == "bearer" and not AWS_BEDROCK_BEARER_TOKEN:
            logger.error("Bedrock bearer authentication selected but no token is configured")
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
            except Exception as exc:
                logger.warning("Bedrock HTTP client initialization failed (%s)", type(exc).__name__)
                bedrock_http_client = None

        if AWS_BEDROCK_AUTH_MODE in ("auto", "boto3") and bedrock_runtime_client is None:
            try:
                import boto3
                bedrock_runtime_client = boto3.client(
                    "bedrock-runtime", region_name=AWS_BEDROCK_REGION
                )
                logger.info("Bedrock SDK client initialized")
            except Exception as exc:
                logger.warning("Bedrock SDK client initialization failed (%s)", type(exc).__name__)
                bedrock_runtime_client = None

        if AWS_BEDROCK_USE_INFERENCE_PROFILE and AWS_BEDROCK_INFERENCE_PROFILE_ID:
            model_id = AWS_BEDROCK_INFERENCE_PROFILE_ID
            route = "inference_profile"
        else:
            model_id = AWS_BEDROCK_MODEL_ID
            route = "model"
            if AWS_BEDROCK_USE_INFERENCE_PROFILE:
                logger.warning("Bedrock inference profile requested without an ID; using the model ID")
        logger.info("Bedrock default configuration: region=%s model=%s route=%s auth_mode=%s",
                    AWS_BEDROCK_REGION, model_id, route, AWS_BEDROCK_AUTH_MODE)
        # These legacy settings are not applied by the current request builder.
        # Report configured facts without promising cache behavior to operators.
        logger.info("Bedrock prompt-cache settings: enabled=%s configured_ttl_seconds=%s; "
                    "this client does not send cache controls",
                    AWS_BEDROCK_USE_CACHE, AWS_BEDROCK_CACHE_TTL)

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
                logger.info("Hybrid Bedrock SDK client initialized")
            except Exception as exc:
                logger.warning("Hybrid Bedrock SDK client initialization failed (%s)", type(exc).__name__)
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
    turn_id: str = "",
) -> str:
    """Query the selected Chat backend with the supplied role messages.

    ControlDecision needs the production system message and prior conversation
    as distinct roles to resolve follow-ups and Project references. This port
    adapts the same supplied message list to each existing Chat backend.

    The current Gemini adapter does not forward ``timeout``. A caller's request
    budget therefore does not bound that backend's request duration.

    ``on_text`` consumes deltas from that same request. Exceptions propagate and
    close its stream. The CLI backend is rejected before query or delivery
    because its shared process has no per-query cancellation owner.

    Streaming diagnostics measure provider-call entry to the first nonempty
    text delta, before consumer backpressure. Client/message preparation is
    separate. ``turn_id`` only correlates logs and is never sent as an SDK option.
    """

    global llm_client, gemini_model
    query_started = time.perf_counter()
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
        from llm.visual_context import (
            attach_openai_chat_image, provider_supports_direct_image, visual_notice_text,
        )

        if (
            selected_provider != "gemini"
            and provider_supports_direct_image(selected_provider, model or DEEPSEEK_MODEL_NAME)
            and not visual_context.get("error")
        ):
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
    request_id = uuid.uuid4().hex
    request_started = None
    first_text_seen = False
    text_consumer = on_text

    def mark_request_started():
        nonlocal request_started
        request_started = time.perf_counter()
        if text_consumer is not None:
            logger.info(
                "[MODEL-LATENCY] stage=request_start request_id=%s turn_id=%s "
                "provider=%s model=%s prepare_ms=%.1f",
                request_id, turn_id or "-", selected_provider, requested_model,
                (request_started - query_started) * 1000,
            )

    def timed_text(text):
        nonlocal first_text_seen
        if text and not first_text_seen:
            first_text_seen = True
            logger.info(
                "[MODEL-LATENCY] stage=first_text request_id=%s turn_id=%s "
                "provider=%s model=%s ms=%.1f",
                request_id, turn_id or "-", selected_provider, requested_model,
                (time.perf_counter() - request_started) * 1000,
            )
        return text_consumer(text)

    if text_consumer is not None:
        on_text = timed_text

    if selected_provider in ("deepseek", "hybrid2"):
        if llm_client is None:
            llm_client = init_llm_client()
        requested_model = str(model or DEEPSEEK_MODEL_NAME)
        mark_request_started()
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
        mark_request_started()
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
        mark_request_started()
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
        mark_request_started()
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
        mark_request_started()
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
    # Bedrock's Qwen native InvokeModel response uses the OpenAI chat shape;
    # Anthropic and Converse responses retain their existing content blocks.
    choices = result.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        message = choices[0].get("message")
        if isinstance(message, Mapping):
            text = message.get("content")
            if isinstance(text, str) and text:
                return text
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
        raise RuntimeError(
            "Persistent llama-cli cannot provide isolated Chat/Planner message requests. "
            "Select LOCAL_LLM_TYPE=llama_server, point LOCAL_LLM_CLI_PATH to llama-server, "
            "and configure a context large enough for the complete conversation."
        )

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
    if json_output:
        # Explicit root type is portable across local grammar implementations;
        # an empty json_object schema can leave native template output unconstrained.
        payload["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "response", "schema": {"type": "object"},
        }}
    if backend == "llama_server":
        payload["cache_prompt"] = True
        require_llama_message_capacity(LOCAL_LLM_URL, payload, timeout=float(timeout))
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
