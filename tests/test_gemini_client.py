from __future__ import annotations

import asyncio
from types import SimpleNamespace

from llm.gemini_client import generate_gemini_text, stream_gemini_text


class _SyncModels:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text="sync reply")


class _AsyncModels:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def generate_content_stream(self, **kwargs):
        self.calls.append(kwargs)

        async def chunks():
            yield SimpleNamespace(text="first")
            yield SimpleNamespace(text="")
            yield SimpleNamespace(text=" second")

        return chunks()


def test_sync_gemini_adapter_uses_client_models_contract() -> None:
    models = _SyncModels()
    client = SimpleNamespace(models=models)

    result = generate_gemini_text(
        client,
        model="gemini-test",
        contents="hello",
        config={"temperature": 0.5},
    )

    assert result == "sync reply"
    assert models.calls == [
        {
            "model": "gemini-test",
            "contents": "hello",
            "config": {"temperature": 0.5},
        }
    ]


def test_async_gemini_adapter_uses_aio_stream_contract() -> None:
    async def run() -> None:
        models = _AsyncModels()
        client = SimpleNamespace(aio=SimpleNamespace(models=models))

        chunks = [
            text
            async for text in stream_gemini_text(
                client,
                model="gemini-test",
                contents=["hello", object()],
                config={"max_output_tokens": 20},
            )
        ]

        assert chunks == ["first", " second"]
        assert models.calls[0]["model"] == "gemini-test"
        assert models.calls[0]["config"] == {"max_output_tokens": 20}

    asyncio.run(run())
