"""
OpenClaw API 客户端
- _get_openclaw_client：懒加载 OpenAI 兼容客户端
- ask_openclaw：单次同步调用（via asyncio.to_thread）
- ask_openclaw_stream：流式调用
- _classify_openclaw_result：结果类型判断（ok / question / error / partial）
"""
from __future__ import annotations

import asyncio
import logging
import os

from openai import OpenAI

from config.settings import OPENCLAW_BASE_URL, OPENCLAW_TOKEN

logger = logging.getLogger(__name__)

_openclaw_client: OpenAI | None = None


OPENCLAW_EXECUTION_SYSTEM_PROMPT = (
    "You are an execution assistant working behind Amadeus. Execute the user's task directly and concisely. "
    "Do not roleplay as Amadeus or Kurisu. Do not narrate raw tool calls. "
    "When you learn something meaningful about the task content during a longer task, emit a short standalone "
    "progress sentence. Use the user's language for progress and final results. "
    "A useful progress sentence says what was found, confirmed, filtered, compared, summarized, or blocked; "
    "it does not merely say that a tool was opened or a request is running. "
    "When the task contains a progress reporting contract, follow its exact marker forms and do not invent a "
    "different marker. Never use a progress marker as the final completion claim."
)


def _get_openclaw_client() -> OpenAI:
    """懒加载 OpenClaw 客户端（复用连接池）。"""
    global _openclaw_client
    if _openclaw_client is None:
        _openclaw_client = OpenAI(
            api_key=OPENCLAW_TOKEN,
            base_url=f"{OPENCLAW_BASE_URL}/v1",
            max_retries=0,
        )
        logger.info(f"[openclaw] client initialized, Gateway: {OPENCLAW_BASE_URL}")
    return _openclaw_client


async def ask_openclaw(
    task: str,
    timeout: float = 120.0,
    image_path: str | None = None,
) -> str:
    """
    向 OpenClaw 发送任务指令，返回执行结果文本。

    参数：
        image_path: 可选本地图片路径；支持 vision 时以 base64 附加，否则降级为路径文本。
    """
    try:
        client = _get_openclaw_client()
        if image_path and os.path.exists(image_path):
            try:
                import base64 as _b64
                ext = os.path.splitext(image_path)[1].lower().lstrip(".")
                mime = {
                    "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "png": "image/png", "webp": "image/webp",
                    "gif": "image/gif",
                }.get(ext, "image/png")
                with open(image_path, "rb") as f:
                    b64_data = _b64.b64encode(f.read()).decode()
                user_content = [
                    {"type": "text", "text": task},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64_data}"}},
                ]
                logger.info(f"[openclaw] attached image: {image_path} ({mime})")
            except Exception as img_e:
                logger.warning(f"[openclaw] image encoding failed ({img_e}); falling back to path text")
                user_content = f"{task}\n[参考图片路径: {image_path}]"
        else:
            user_content = task

        response = await asyncio.to_thread(
            lambda: client.chat.completions.create(
                model="openclaw",
                messages=[
                    {"role": "system", "content": OPENCLAW_EXECUTION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                timeout=timeout,
            )
        )
        result = response.choices[0].message.content or ""
        logger.info(f"[openclaw] task completed: {task[:40]}... -> {result[:60]}...")
        return result
    except Exception as e:
        logger.warning(f"[openclaw] call failed: {e}")
        return f"[OpenClaw 暂时不可用: {e}]"


async def ask_openclaw_stream(
    task: str,
    chunk_callback=None,
    tool_event_callback=None,
    run_started_callback=None,
    timeout: float = 60.0,
    image_path: str | None = None,
    session_key: str | None = None,
) -> str:
    """流式调用 OpenClaw，通过 raw SSE 解析同时处理文本块和工具事件。

    参数：
        chunk_callback(text: str)        — 每个助手文本块到达时调用（可选，向后兼容）
        tool_event_callback(event: dict) — 每个工具事件到达时调用（可选）
        run_started_callback(run_id: str) — 首个上游 run id 到达时调用（可选）
        image_path                       — 可选本地图片路径；以 base64 附加到消息
        session_key                      — 可选 Provider 原生 Session；由宿主账本管理
    """
    import httpx
    import json as _json

    # 构造用户消息内容（支持 vision）
    if image_path and os.path.exists(image_path):
        try:
            import base64 as _b64
            ext = os.path.splitext(image_path)[1].lower().lstrip(".")
            mime = {
                "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png", "webp": "image/webp",
                "gif": "image/gif",
            }.get(ext, "image/png")
            with open(image_path, "rb") as f:
                b64_data = _b64.b64encode(f.read()).decode()
            user_content = [
                {"type": "text", "text": task},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64_data}"}},
            ]
            logger.info(f"[openclaw] attached image (stream): {image_path} ({mime})")
        except Exception as img_e:
            logger.warning(f"[openclaw] image encoding failed ({img_e}); falling back to path text")
            user_content = f"{task}\n[参考图片路径: {image_path}]"
    else:
        user_content = task

    full_text = ""
    url = f"{OPENCLAW_BASE_URL}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENCLAW_TOKEN}",
        "Content-Type": "application/json",
    }
    if str(session_key or "").strip():
        headers["X-OpenClaw-Session-Key"] = str(session_key).strip()
    body = {
        "model": "openclaw",
        "messages": [
            {"role": "system", "content": OPENCLAW_EXECUTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "stream": True,
    }

    reported_run_id = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as http:
            async with http.stream("POST", url, headers=headers, json=body) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = _json.loads(raw)
                    except Exception:
                        continue

                    native_run_id = str(chunk.get("id") or "").strip()
                    if native_run_id and not reported_run_id:
                        reported_run_id = native_run_id
                        if run_started_callback:
                            run_started_callback(native_run_id)

                    obj = chunk.get("object", "")

                    if obj == "tool_event":
                        if tool_event_callback:
                            tool_event_callback(chunk.get("tool_event"))
                        continue

                    # standard chat.completion.chunk
                    choices = chunk.get("choices") or []
                    if choices:
                        delta = (choices[0].get("delta") or {}).get("content") or ""
                        if delta:
                            full_text += delta
                            if chunk_callback:
                                chunk_callback(delta)

        logger.info(f"[openclaw] streaming task completed: {task[:40]}...")
    except Exception as e:
        logger.warning(f"[openclaw] streaming call failed: {e}")
        full_text = f"[OpenClaw 暂时不可用: {e}]"

    return full_text


def _classify_openclaw_result(result: str) -> str:
    """
    判断 OpenClaw 返回结果的类型：
    - "question"：OpenClaw 在反问，需要 Kurisu 转达给用户
    - "error"：执行出错，需要 Kurisu 说明情况并请求用户协助
    - "partial"：工具能力受限，部分完成
    - "ok"：正常完成
    """
    lowered = result.lower()
    question_signals = [
        "?", "？",
        "which", "what", "would you", "do you want", "please specify", "please clarify",
        "どうし", "どれ", "どちら", "何を", "教えてください", "確認", "選んで",
    ]
    error_signals = [
        "error", "failed", "permission denied", "not found", "exception", "traceback",
        "エラー", "失敗", "見つかりません", "権限", "アクセス拒否",
    ]
    capability_limit_signals = [
        "設定されていない", "api key", "apiキー", "利用できません", "アクセスできません",
        "not configured", "not available", "not enabled", "not supported",
        "できません", "できなかった", "サポートされていません",
    ]
    if any(s in lowered for s in error_signals):
        return "error"
    if any(s in lowered for s in capability_limit_signals):
        return "partial"
    if any(s in result for s in question_signals):
        return "question"
    return "ok"
