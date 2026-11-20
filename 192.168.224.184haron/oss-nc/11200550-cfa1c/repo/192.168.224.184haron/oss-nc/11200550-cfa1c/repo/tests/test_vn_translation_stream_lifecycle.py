"""The bridge owns every stream, including results delivered after cancellation."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace

import httpx
from openai import OpenAI
import pytest

from server import vn_tts_bridge as bridge


WATCHDOG = 15


@pytest.fixture(autouse=True)
def translation_config(monkeypatch):
    monkeypatch.setattr(bridge.settings, "DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.setenv("VN_TTS_TRANSLATE_PROVIDER", "deepseek")
    monkeypatch.setenv("VN_TTS_TRANSLATE_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("VN_TTS_TRANSLATE_MODEL", "synthetic-model")


def install_request(monkeypatch, create):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(bridge, "_get_client", lambda *_: client)


async def consume():
    return [piece async for piece in bridge._stream_translate_zh_to_ja("合成文本")]


class Stream:
    def __init__(self, *, read=None, close=None):
        self.read = read
        self.on_close = close
        self.reads = 0
        self.closes = 0
        self.reading = False

    def __iter__(self):
        return self

    def __next__(self):
        self.reading = True
        try:
            self.reads += 1
            if self.read:
                self.read(self.reads)
            if self.reads > 2:
                raise StopIteration
            return SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=f"{self.reads}。"))])
        finally:
            self.reading = False

    def close(self):
        assert not self.reading, "close must not race a synchronous read"
        self.closes += 1
        if self.on_close:
            self.on_close()


@pytest.mark.parametrize("outcome", ["complete", "read_error", "consumer_close"])
async def test_owned_stream_is_closed_on_exit(monkeypatch, outcome):
    loop_thread = threading.get_ident()

    def read(count):
        if outcome == "read_error" and count == 2:
            raise RuntimeError("synthetic read failure")

    def close():
        assert threading.get_ident() != loop_thread

    stream = Stream(read=read, close=close)
    install_request(monkeypatch, lambda **_: stream)
    if outcome == "read_error":
        with pytest.raises(RuntimeError, match="synthetic read failure"):
            await consume()
    elif outcome == "consumer_close":
        translation = bridge._stream_translate_zh_to_ja("合成文本")
        assert await anext(translation) == "1。"
        await translation.aclose()
    else:
        assert await consume() == ["1。", "2。"]
    assert stream.closes == 1


@pytest.mark.parametrize("phase", ["create", "first_read", "later_read", "read_error"])
async def test_cancelled_worker_closes_stream_without_late_delivery(monkeypatch, phase):
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    blocked, closed = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    delivered = []

    def block():
        loop.call_soon_threadsafe(blocked.set)
        assert release.wait(WATCHDOG)

    def read(count):
        if (phase == "first_read" and count == 1) or (phase in {"later_read", "read_error"} and count == 2):
            block()
            if phase == "read_error":
                raise RuntimeError("synthetic late read failure")

    def close():
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(closed.set)

    stream = Stream(read=read, close=close)

    def create(**_):
        if phase == "create":
            block()
        return stream

    install_request(monkeypatch, create)

    async def deliver():
        async for piece in bridge._stream_translate_zh_to_ja("合成文本"):
            delivered.append(piece)

    task = asyncio.create_task(deliver())
    try:
        await asyncio.wait_for(blocked.wait(), WATCHDOG)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, WATCHDOG)
        assert not release.is_set()
        assert stream.closes == 0
        release.set()
        await asyncio.wait_for(closed.wait(), WATCHDOG)
        assert stream.closes == 1
        assert delivered == (["1。"] if phase in {"later_read", "read_error"} else [])
        if phase == "create":
            assert stream.reads == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["cancel", "error"])
async def test_bridge_closes_translation_while_dispatching(monkeypatch, outcome):
    blocked = asyncio.Event()
    stream = Stream()
    install_request(monkeypatch, lambda **_: stream)

    async def feed(*_):
        blocked.set()
        if outcome == "error":
            raise RuntimeError("synthetic queue failure")
        await asyncio.Future()

    monkeypatch.setattr(bridge._StreamingSentenceDispatcher, "feed", feed)
    task = asyncio.create_task(bridge._run_vn_tts_job(
        display_text="合成文本", voice_text="", pending_sentence_items=asyncio.Queue(), metadata={}
    ))
    try:
        await asyncio.wait_for(blocked.wait(), WATCHDOG)
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, WATCHDOG)
        else:
            assert (await task)["status"] == "error"
        assert stream.closes == 1
        assert stream.reads == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_unread_sdk_streams_release_shared_connection_pool(monkeypatch):
    """A one-slot real HTTP pool remains usable by subtitles after repeated cancellation."""
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def handle(self):
            try:
                super().handle()
            except ConnectionResetError:
                # Closing an unread response can reset its keep-alive socket.
                pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if payload["stream"]:
                body = b'data: {"choices":[{"delta":{"content":"voice"}}]}\n\ndata: [DONE]\n\n'
                content_type = "text/event-stream"
            else:
                body = b'{"choices":[{"message":{"content":"subtitle"}}]}'
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    server_thread.start()
    loop = asyncio.get_running_loop()
    created = asyncio.Event()
    release = threading.Event()
    task = None
    http_client = httpx.Client(limits=httpx.Limits(max_connections=1), trust_env=False)
    client = OpenAI(api_key="test-key", base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    http_client=http_client, max_retries=0)

    def create(**kwargs):
        # Only the pool wait is shortened; coordination uses explicit events.
        kwargs["timeout"] = httpx.Timeout(WATCHDOG, pool=1)
        result = client.chat.completions.create(**kwargs)
        if kwargs["stream"]:
            # The real response owns the only slot but has never been iterated.
            loop.call_soon_threadsafe(created.set)
            assert release.wait(WATCHDOG)
        return result

    install_request(monkeypatch, create)
    monkeypatch.setenv("VN_SUBTITLE_TRANSLATE_PROVIDER", "deepseek")
    try:
        for _ in range(3):
            created.clear()
            release.clear()
            task = asyncio.create_task(consume())
            await asyncio.wait_for(created.wait(), WATCHDOG)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, WATCHDOG)
            release.set()
            assert await asyncio.wait_for(bridge._translate_ja_to_zh("合成文本"), WATCHDOG) == "subtitle"
    finally:
        release.set()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(client.close)
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        server_thread.join(WATCHDOG)
