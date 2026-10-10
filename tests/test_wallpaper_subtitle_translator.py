"""Subtitle client startup must not occupy the shared audio event loop."""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import openai
import pytest

from server import wallpaper_subtitle_translator as translator


@pytest.fixture(autouse=True)
def isolated_clients(monkeypatch):
    monkeypatch.setattr(translator, "_CLIENTS", {})
    monkeypatch.setattr(translator.settings, "DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.setattr(translator.settings, "OPENAI_API_KEY", "synthetic-key")


def config(provider):
    return translator.SubtitleTranslatorConfig(
        provider=provider, model="synthetic-model", base_url="https://example.invalid/v1",
        timeout_s=3, max_tokens=80)


@pytest.mark.parametrize("provider", ["deepseek", "openai"])
async def test_cold_subtitles_leave_audio_runnable_and_share_one_concurrent_client(monkeypatch, provider):
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    initializing = asyncio.Event()
    release_initialization = threading.Event()
    concurrent_requests = threading.Barrier(2)
    http_clients, sdk_clients, requests = [], [], []
    requests_lock = threading.Lock()

    def http_client(**_kwargs):
        assert threading.get_ident() != loop_thread
        client = object()
        http_clients.append(client)
        loop.call_soon_threadsafe(initializing.set)
        assert release_initialization.wait(3), "audio loop did not release initialization"
        return client

    def create(**kwargs):
        assert threading.get_ident() != loop_thread
        with requests_lock:
            requests.append(kwargs)
            index = len(requests)
        if index <= 2:
            # Client ownership must not serialize the actual provider requests.
            concurrent_requests.wait(timeout=3)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="翻译结果"))])

    def sdk_client(**kwargs):
        sdk_clients.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(httpx, "Client", http_client)
    monkeypatch.setattr(openai, "OpenAI", sdk_client)
    pending = [asyncio.create_task(translator._translate_openai_compatible(text, config(provider)))
        for text in ("一。", "二。")]
    try:
        await asyncio.wait_for(initializing.wait(), 1)
        audio_queue = asyncio.Queue()

        async def ready_audio():
            await audio_queue.put("first audio")

        await asyncio.create_task(ready_audio())
        assert audio_queue.get_nowait() == "first audio"
        assert all(not task.done() for task in pending)
        release_initialization.set()
        assert await asyncio.gather(*pending) == ["翻译结果", "翻译结果"]
        assert await translator._translate_openai_compatible("三。", config(provider)) == "翻译结果"
        assert len(http_clients) == len(sdk_clients) == 1
        assert sdk_clients[0]["http_client"] is http_clients[0]
        assert len(requests) == 3
        for request in requests:
            assert request["model"] == "synthetic-model"
            assert request["timeout"] == 3 and request["stream"] is False
            if provider == "openai":
                assert request["max_completion_tokens"] == 80
                assert request["reasoning_effort"] == "low"
            else:
                assert request["max_tokens"] == 80
                assert request["extra_body"] == {"thinking":{"type":"disabled"}}
    finally:
        release_initialization.set()
        await asyncio.gather(*pending, return_exceptions=True)


async def test_failed_initialization_does_not_poison_client_cache(monkeypatch):
    attempts = []
    http_client = object()

    def construct(**_kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("synthetic client initialization failure")
        return http_client

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **_:SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="翻译结果"))]))))
    monkeypatch.setattr(httpx, "Client", construct)
    monkeypatch.setattr(openai, "OpenAI", lambda **_:client)
    with pytest.raises(RuntimeError, match="synthetic client initialization failure"):
        await translator._translate_openai_compatible("一。", config("deepseek"))
    assert translator._CLIENTS == {}
    assert await translator._translate_openai_compatible("二。", config("deepseek")) == "翻译结果"
    assert translator._CLIENTS == {("deepseek", "https://example.invalid/v1"):client}
    assert len(attempts) == 2
