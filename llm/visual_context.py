"""Helpers for attaching optional visual context to LLM requests."""

from __future__ import annotations

import base64
import io
from typing import Any


def provider_supports_direct_image(provider: str) -> bool:
    return str(provider or "").strip().lower() in {"openai", "gemini", "hybrid3"}


def visual_notice_text(question: str, visual_context: dict[str, Any] | None, *, supported: bool) -> str:
    """Return user text with a compact hidden visual-system note."""

    question = str(question or "")
    if not visual_context:
        return question
    if visual_context.get("error"):
        return (
            f"{question}\n\n"
            "[VISUAL_CONTEXT_ERROR]\n"
            f"Vision was requested, but capture failed: {visual_context.get('error')}\n"
            "Do not pretend you can see the screen. Briefly explain that visual input failed if relevant."
        )
    if supported:
        scope = visual_context.get("actualScope") or visual_context.get("scope") or "screen"
        if visual_context.get("reason") == "attachment" or scope == "user_image":
            attachment = visual_context.get("attachment") or {}
            name = attachment.get("name") or "image"
            return (
                f"{question}\n\n"
                "[VISUAL_CONTEXT]\n"
                f"The user attached an image for this turn: {name}. "
                "Use it only for this reply and do not claim persistent vision."
            )
        return (
            f"{question}\n\n"
            "[VISUAL_CONTEXT]\n"
            f"A current visual frame is attached for this turn. Scope: {scope}. "
            "Use it only for this reply and do not claim persistent vision."
        )
    provider = visual_context.get("provider") or "auto"
    return (
        f"{question}\n\n"
        "[VISUAL_CONTEXT_UNAVAILABLE]\n"
        f"Vision was requested and a frame may have been captured, but the current LLM provider cannot receive image input directly (vision provider setting: {provider}). "
        "Do not pretend you can see the screen. Tell the user that a multimodal provider must be selected for visual grounding."
    )


def local_visual_ack_text(question: str, visual_context: dict[str, Any] | None) -> str:
    """Hint the local first-sentence model that vision exists without asking it to inspect it."""

    question = str(question or "")
    if not visual_context:
        return question
    if visual_context.get("error"):
        return (
            f"{question}\n\n"
            "[LOCAL_FIRST_SENTENCE_VISUAL_HINT]\n"
            "A visual input was requested, but capture failed. If you acknowledge this turn, do not claim that you can see the image."
        )
    scope = visual_context.get("actualScope") or visual_context.get("scope") or "screen"
    if visual_context.get("reason") == "attachment" or scope == "user_image":
        attachment = visual_context.get("attachment") or {}
        name = attachment.get("name") or "image"
        visual_label = f"the user's attached image ({name})"
    else:
        visual_label = f"a current visual frame ({scope})"
    return (
        f"{question}\n\n"
        "[LOCAL_FIRST_SENTENCE_VISUAL_HINT]\n"
        f"This turn includes {visual_label}. The remote multimodal model will inspect it. "
        "You are only producing the first short spoken acknowledgement. "
        "Do not describe visual details or pretend you inspected the image."
    )


def attach_openai_chat_image(messages: list[dict[str, Any]], visual_context: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Attach a screenshot to the last user message in OpenAI Chat Completions format."""

    if not visual_context or visual_context.get("error"):
        return messages
    frame = visual_context.get("frame") or {}
    data_url = frame.get("dataUrl")
    if not data_url and frame.get("dataBase64"):
        mime = frame.get("mime") or "image/jpeg"
        data_url = f"data:{mime};base64,{frame['dataBase64']}"
    if not data_url:
        return messages

    output = [dict(message) for message in messages]
    for message in reversed(output):
        if message.get("role") != "user":
            continue
        text = visual_notice_text(str(message.get("content") or ""), visual_context, supported=True)
        message["content"] = [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": data_url, "detail": "auto"}},
        ]
        break
    return output


def gemini_contents(full_prompt: str, visual_context: dict[str, Any] | None):
    """Return Gemini generate_content contents for text plus optional image."""

    if not visual_context or visual_context.get("error"):
        return full_prompt
    frame = visual_context.get("frame") or {}
    data = frame.get("dataBase64")
    if not data:
        return full_prompt
    try:
        from PIL import Image

        raw = base64.b64decode(data)
        image = Image.open(io.BytesIO(raw))
        return [visual_notice_text(full_prompt, visual_context, supported=True), image]
    except Exception:
        return full_prompt
