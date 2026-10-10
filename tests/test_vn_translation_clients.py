"""VN translation's synchronous client boundary must leave audio work runnable."""

import asyncio
import threading
from types import SimpleNamespace

import httpx
import openai
import pytest

from server import vn_tts_bridge as bridge


# Deadlock watchdogs, not latency thresholds: ordering is asserted with events.
WATCHDOG = 15
BASE_URL = "https://example.invalid/v1"


@pytest.fixture(autouse=True)
def isolated_clients(monkeypatch):
    monkeypatch.setattr(bridge, "_CLIENTS", {})
    monkeypatch.setattr(bridge.settings, "DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.setattr(bridge.settings, "OPENAI_API_KEY", "synthetic-key")
    for prefix in ("VN_SUBTITLE_TRANSLATE", "VN_TTS_TRANSLATE"):
        monkeypatch.setenv(f"{prefix}_BASE_URL", BASE_URL)
        monkeypatch.setenv(f"{prefix}_MODEL", "synthetic-model")
        monkeypatch.setenv(f"{prefix}_MAX_TOKENS", "80")
        monkeypatch.setenv(f"{prefix}_TIMEOUT", "12")


def set_provider(monkeypatch, provider):
    for prefix in ("VN_SUBTITLE_TRANSLATE", "VN_TTS_TRANSLATE"):
        monkeypatch.setenv(f"{prefix}_PROVIDER", provider)


def chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


def response():
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=" 翻译结果 "))])


async def translate_stream(text):
    return [piece async for piece in bridge._stream_translate_zh_to_ja(text)]


@pytest.mark.parametrize("provider", ["deepseek", "openai"])
async def test_cold_translation_paths_share_client_without_blocking_audio(monkeypatch, provider):
    set_provider(monkeypatch, provider)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    initializing = asyncio.Event()
    release_initialization = threading.Event()
    concurrent_requests = threading.Barrier(2)
    http_clients, sdk_clients, requests = [], [], []

    def http_client(**_kwargs):
        loop.call_soon_threadsafe(initializing.set)
        assert threading.get_ident() != loop_thread
        client = object()
        http_clients.append(client)
        assert release_initialization.wait(WATCHDOG)
        return client

    def stream():
        assert threading.get_ident() != loop_thread
        yield chunk(None)
        yield chunk("一。")
        assert threading.get_ident() != loop_thread
        yield chunk("二。")

    def create(**kwargs):
        assert threading.get_ident() != loop_thread
        requests.append(kwargs)
        # Both paths must reach the provider before either request completes.
        concurrent_requests.wait(timeout=WATCHDOG)
        return stream() if kwargs["stream"] else response()

    def sdk_client(**kwargs):
        assert threading.get_ident() != loop_thread
        sdk_clients.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(httpx, "Client", http_client)
    monkeypatch.setattr(openai, "OpenAI", sdk_client)
    pending = [
        asyncio.create_task(bridge._translate_ja_to_zh("一。")),
        asyncio.create_task(translate_stream("二。")),
    ]
    try:
        await asyncio.wait_for(initializing.wait(), WATCHDOG)
        audio_queue = asyncio.Queue()
        await asyncio.create_task(audio_queue.put("ready audio"))
        assert audio_queue.get_nowait() == "ready audio"
        assert all(not task.done() for task in pending)
        release_initialization.set()
        assert await asyncio.gather(*pending) == ["翻译结果", ["一。", "二。"]]
        # Warm requests still share the same client and remain concurrent.
        assert await asyncio.gather(
            bridge._translate_ja_to_zh("三。"), translate_stream("四。")
        ) == ["翻译结果", ["一。", "二。"]]
        assert len(http_clients) == len(sdk_clients) == 1
        assert sdk_clients[0]["http_client"] is http_clients[0]
        assert sdk_clients[0]["api_key"] == "synthetic-key"
        assert sdk_clients[0]["base_url"] == BASE_URL
        assert len(requests) == 4
        for request in requests:
            assert request["model"] == "synthetic-model"
            assert request["timeout"] == 12
            assert request["max_tokens"] == 80
            assert request["temperature"] == (0.15 if request["stream"] else 0.1)
            if provider == "deepseek":
                assert request["extra_body"] == {"thinking": {"type": "disabled"}}
            else:
                assert "extra_body" not in request
    finally:
        release_initialization.set()
        concurrent_requests.abort()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("streaming", [False, True])
async def test_failed_initialization_can_be_retried(monkeypatch, streaming):
    set_provider(monkeypatch, "deepseek")
    attempts = []

    def http_client(**_kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("synthetic initialization failure")
        return object()

    def create(**kwargs):
        return iter([chunk("一。")]) if kwargs["stream"] else response()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(httpx, "Client", http_client)
    monkeypatch.setattr(openai, "OpenAI", lambda **_: client)
    translate = translate_stream if streaming else bridge._translate_ja_to_zh
    with pytest.raises(RuntimeError, match="synthetic initialization failure"):
        await translate("一。")
    assert bridge._CLIENTS == {}
    assert await translate("二。") == (["一。"] if streaming else "翻译结果")
    assert bridge._CLIENTS == {("deepseek", BASE_URL): client}
    assert len(attempts) == 2


@pytest.mark.parametrize("phase", ["initialization", "iteration"])
async def test_stream_cancellation_does_not_deliver_late_pieces(monkeypatch, phase):
    set_provider(monkeypatch, "deepseek")
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    blocked = asyncio.Event()
    worker_finished = asyncio.Event()
    release_worker = threading.Event()
    delivered, requests, clients = [], [], []

    def block_worker():
        loop.call_soon_threadsafe(blocked.set)
        assert threading.get_ident() != loop_thread
        assert release_worker.wait(WATCHDOG)

    def http_client(**_kwargs):
        if phase == "initialization":
            block_worker()
        clients.append(object())
        return clients[-1]

    def stream():
        yield chunk("一。")
        if phase == "iteration":
            try:
                block_worker()
            finally:
                loop.call_soon_threadsafe(worker_finished.set)
        yield chunk("二。")

    def create(**_kwargs):
        requests.append(True)
        if phase == "initialization":
            loop.call_soon_threadsafe(worker_finished.set)
        return stream()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(httpx, "Client", http_client)
    monkeypatch.setattr(openai, "OpenAI", lambda **_: client)

    async def consume():
        async for piece in bridge._stream_translate_zh_to_ja("合成文本"):
            delivered.append(piece)

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(blocked.wait(), WATCHDOG)
        if phase == "iteration":
            # The first piece is available before the rest of the stream.
            assert delivered == ["一。"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, WATCHDOG)
        assert not release_worker.is_set()
    finally:
        release_worker.set()
        await asyncio.gather(task, return_exceptions=True)
        if task.cancelled():
            await asyncio.wait_for(worker_finished.wait(), WATCHDOG)

    # to_thread cannot interrupt synchronous work, but its late result must
    # never resume the cancelled consumer. The shared client remains usable.
    assert await translate_stream("新文本") == ["一。", "二。"]
    assert delivered == (["一。"] if phase == "iteration" else [])
    assert len(clients) == 1
    assert len(requests) == 2
