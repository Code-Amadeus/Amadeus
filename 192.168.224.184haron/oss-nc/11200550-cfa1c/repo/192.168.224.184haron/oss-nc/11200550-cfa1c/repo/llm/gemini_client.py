"""Small compatibility boundary around the maintained Google GenAI SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from google import genai


def create_gemini_client(api_key: str):
    return genai.Client(api_key=str(api_key or ""))


def generate_gemini_text(
    client: Any,
    *,
    model: str,
    contents: Any,
    config: dict[str, Any] | None = None,
) -> str:
    response = client.models.generate_content(
        model=str(model),
        contents=contents,
        config=config or None,
    )
    return str(getattr(response, "text", "") or "")


async def stream_gemini_text(
    client: Any,
    *,
    model: str,
    contents: Any,
    config: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    stream = await client.aio.models.generate_content_stream(
        model=str(model),
        contents=contents,
        config=config or None,
    )
    async for chunk in stream:
        text = str(getattr(chunk, "text", "") or "")
        if text:
            yield text
