"""llm/client.py — LLM 客户端层（同步非流式）

负责：
  - 客户端初始化（init_llm_client）
  - 远程 API 查询（remote_llm_query：DeepSeek / Gemini / AWS Bedrock）
  - 本地模型查询（local_llm_query：Ollama / LM Studio / llama-server / CLI）

依赖注入（configure()）：
  - llm_provider : str，覆盖默认 LLM_PROVIDER
  - local_llm_type : str，覆盖纯本地链路的 backend 类型
"""

import json
import logging

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
    global LLM_PROVIDER, LOCAL_LLM_TYPE, llm_client, gemini_model
    if llm_provider is not None:
        if llm_provider != LLM_PROVIDER:
            llm_client = None
            gemini_model = None
        LLM_PROVIDER = llm_provider
    if local_llm_type is not None:
        LOCAL_LLM_TYPE = local_llm_type


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
) -> str:
    """Query the configured remote backend with an exact history.

    ControlDecision needs the production system message and prior conversation
    as distinct roles. Flattening them into ``remote_llm_query(question)``
    silently removes the very history that resolves follow-ups and Project
    references, so this narrow port preserves the supplied message topology.
    Unsupported backends fail visibly; the caller's shadow contract converts
    that to ``unavailable`` and never touches dispatch.
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
    if LLM_PROVIDER in ("deepseek", "hybrid2"):
        if llm_client is None:
            llm_client = init_llm_client()
        response = llm_client.chat.completions.create(
            model=str(model or DEEPSEEK_MODEL_NAME),
            messages=normalized,
            temperature=float(temperature),
            max_tokens=max(1, int(max_tokens)),
            stream=False,
            timeout=float(timeout),
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )
    elif LLM_PROVIDER in ("openai", "hybrid3"):
        if llm_client is None:
            llm_client = init_llm_client()
        response = llm_client.chat.completions.create(
            model=str(model or OPENAI_MODEL_NAME),
            messages=normalized,
            max_completion_tokens=max(1, int(max_tokens)),
            reasoning_effort="low",
            stream=False,
            timeout=float(timeout),
            response_format={"type": "json_object"},
        )
    elif LLM_PROVIDER == "gemini":
        if gemini_model is None:
            gemini_model = init_llm_client()
        reply = generate_gemini_text(
            gemini_model,
            model=str(model or GEMINI_MODEL_NAME),
            contents=[
                {
                    "role": "model" if message["role"] == "assistant" else "user",
                    "parts": [{"text": message["content"]}],
                }
                for message in normalized[1:]
                if message["role"] != "system"
            ],
            config={
                "system_instruction": "\n\n".join(
                    message["content"]
                    for message in normalized
                    if message["role"] == "system"
                ),
                "temperature": float(temperature),
                "max_output_tokens": max(1, int(max_tokens)),
                "response_mime_type": "application/json",
                "http_options": {"timeout": max(1, round(float(timeout) * 1000))},
            },
        )
        if not reply:
            raise RuntimeError("structured control backend returned no content")
        return reply
    else:
        raise RuntimeError(
            f"structured control message query is unavailable for {LLM_PROVIDER!r}"
        )
    if not response or not getattr(response, "choices", None):
        raise RuntimeError("structured control backend returned no choices")
    return str(response.choices[0].message.content or "")

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

def local_llm_query(question: str) -> str:
    """调用本地模型(Ollama / LM Studio / llama-server / CLI) - 非流式版本"""
    try:
        _system = _get_system_prompt("local_fallback")

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
            reply = loop.run_until_complete(local_llm_query_cli(question, stream=False))

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
