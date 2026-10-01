"""Qwen / DashScope 联网检索（Web Research）客户端。

主模型仍由 LLM_PROVIDER 决定（默认 deepseek）；本模块是独立的"联网检索链路"：
调用 DashScope OpenAI 兼容接口 + enable_search，联网搜索在阿里云服务端完成，
不经本地浏览器，因此不受反爬/被墙站点影响。配置 DASHSCOPE_API_KEY 后可用。
"""

from __future__ import annotations

import logging
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

RESEARCH_SYSTEM_PROMPT = (
    "You are Amadeus's web-research assistant. Use the live search results to "
    "answer the user's query directly and concisely in the same language as the "
    "query. If the search results do not answer the question, say so plainly "
    "instead of guessing. Keep the answer focused; cite the source site names "
    "when they are clearly relevant."
)


def qwen_web_research(
    query: str,
    *,
    model: str | None = None,
    max_tokens: int = 900,
    timeout: float = 60.0,
) -> dict[str, Any] | None:
    """Run one Qwen enable_search turn; return {answer, sources} or None on failure.

    ``sources`` is a best-effort list of {title, url} parsed from DashScope's
    ``search_info`` when the provider returns it. The answer text is the
    authoritative result.
    """
    api_key = str(getattr(settings, "DASHSCOPE_API_KEY", "") or "").strip()
    if not api_key:
        return None
    resolved_model = str(model or getattr(settings, "QWEN_MODEL_NAME", "") or "qwen-plus").strip()
    base_url = str(
        getattr(settings, "DASHSCOPE_BASE_URL", "")
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ).strip()
    prompt = str(query or "").strip()
    if not prompt:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=max(5.0, float(timeout)),
            max_retries=1,
        )
        response = client.chat.completions.create(
            model=resolved_model,
            messages=[
                {"role": "system", "content": RESEARCH_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            extra_body={"enable_search": True},
            max_tokens=max(64, int(max_tokens)),
        )
    except Exception as exc:  # noqa: BLE001 - surface as provider-level fallback
        logger.warning("[qwen] web research call failed: %s", exc)
        return None

    try:
        answer = str(response.choices[0].message.content or "").strip()
    except Exception:
        answer = ""
    if not answer:
        return None

    sources: list[dict[str, str]] = []
    search_info = getattr(response, "search_info", None)
    if search_info is None and hasattr(response, "model_extra"):
        search_info = (response.model_extra or {}).get("search_info")
    if isinstance(search_info, list):
        for item in search_info:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("link") or "").strip()
            if not url:
                continue
            title = str(item.get("title") or "").strip()
            sources.append({"title": title, "url": url})

    return {"answer": answer, "sources": sources}

VISION_SYSTEM_PROMPT = (
    "You are Amadeus's vision assistant. Describe the screenshot concisely in "
    "the same language as the user's request (Chinese by default). Cover: which "
    "application/window is visible, key UI elements, visible text, and anything "
    "notable. Do not invent details that are not visible."
)


def qwen_vision_describe(
    image_base64: str,
    *,
    prompt: str = "请描述这张截图：正在显示什么应用/窗口、关键界面元素、可见文字、值得注意的地方。",
    model: str | None = None,
    max_tokens: int = 700,
    timeout: float = 60.0,
) -> dict[str, Any] | None:
    """Describe one base64 JPEG screenshot with a Qwen-VL model (DashScope).

    Returns ``{"description": text}`` on success, or ``None`` when the API key
    is missing or the call fails. Used by the dsh-amadeus plugin via
    ``POST /vision/describe`` so a text-only agent can "see" the desktop.
    """
    api_key = str(getattr(settings, "DASHSCOPE_API_KEY", "") or "").strip()
    if not api_key:
        return None
    image = str(image_base64 or "").strip()
    if not image:
        return None
    resolved_model = str(
        model or getattr(settings, "QWEN_VL_MODEL_NAME", "") or "qwen-vl-max"
    ).strip()
    base_url = str(
        getattr(settings, "DASHSCOPE_BASE_URL", "")
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ).strip()
    user_prompt = str(prompt or "").strip() or VISION_SYSTEM_PROMPT
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=max(5.0, float(timeout)),
            max_retries=1,
        )
        response = client.chat.completions.create(
            model=resolved_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image}"},
                        },
                    ],
                }
            ],
            max_tokens=max(64, int(max_tokens)),
        )
    except Exception as exc:  # noqa: BLE001 - surface as provider-level fallback
        logger.warning("[qwen] vision describe call failed: %s", exc)
        return None

    try:
        description = str(response.choices[0].message.content or "").strip()
    except Exception:
        description = ""
    if not description:
        return None
    return {"description": description}
