"""Fish Audio duplex contract against a real local MessagePack WebSocket peer."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import numpy as np
import pytest

msgpack = pytest.importorskip("msgpack")
from websockets.asyncio.server import serve

from tts.backend import TTSSynthesisRequest, TTSBackendError
from tts.backends.fish_audio import FishAudioTTSBackend


async def send(ws, event):
    await ws.send(msgpack.packb(event, use_bin_type=True))


async def receive(ws):
    return msgpack.unpackb(await ws.recv(), raw=False)


async def text_source(*parts):
    for part in parts:
        yield part


@asynccontextmanager
async def peer(handler, **kwargs):
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield FishAudioTTSBackend(
            ws_url=f"ws://127.0.0.1:{port}/v1/tts/live",
            api_key="test-secret",
            model="s2.1-pro-free",
            reference_id="test-voice",
            latency="balanced",
            timeout_seconds=1,
            **kwargs,
        )


async def drain_input(ws):
    while (await receive(ws))["event"] != "stop":
        pass


async def test_audio_arrives_before_future_text_is_available():
    first_audio = asyncio.Event()
    seen = []
    headers = {}
    pcm = np.array([-32768, 0, 32767], dtype="<i2").tobytes()

    async def handler(ws):
        headers.update(ws.request.headers)
        seen.append(await receive(ws))
        seen.append(await receive(ws))
        # Server needs more text only after the client has consumed this audio.
        await send(ws, {"event": "audio", "audio": pcm[:3]})
        seen.append(await receive(ws))
        seen.append(await receive(ws))
        await send(ws, {"event": "audio", "audio": pcm[3:]})
        await send(ws, {"event": "finish", "reason": "stop"})

    async def source():
        yield "こんにちは、"
        await asyncio.wait_for(first_audio.wait(), 2)
        yield " 紅莉栖です。"

    async with peer(handler) as backend:
        request = TTSSynthesisRequest("subtitle", voice="override-voice", speed=1.2)
        stream = backend.synthesize_text_stream(request, source())
        async with asyncio.timeout(3):
            first = await anext(stream)
            first_audio.set()
            chunks = [first, *[chunk async for chunk in stream]]

    assert headers["authorization"] == "Bearer test-secret"
    assert headers["model"] == "s2.1-pro-free"
    assert seen == [
        {
            "event": "start",
            "request": {
                "text": "",
                "reference_id": "override-voice",
                "format": "pcm",
                "sample_rate": 44100,
                "latency": "balanced",
                "prosody": {"speed": 1.2},
            },
        },
        {"event": "text", "text": "こんにちは、"},
        {"event": "text", "text": " 紅莉栖です。"},
        {"event": "stop"},
    ]
    assert [chunk.text for chunk in chunks] == ["subtitle", ""]
    assert all(chunk.sample_rate == 44100 and chunk.audio.dtype == np.float32 for chunk in chunks)
    np.testing.assert_allclose(
        np.concatenate([chunk.audio for chunk in chunks]),
        np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768,
    )


async def test_flush_is_explicit_and_empty_chunks_do_not_flush():
    seen = []

    async def handler(ws):
        while True:
            event = await receive(ws)
            seen.append(event)
            if event["event"] == "stop":
                break
        await send(ws, {"event": "future-metadata"})
        await send(ws, {"event": "audio", "audio": b"\0\0"})
        await send(ws, {"event": "finish", "reason": "stop"})

    async with peer(handler) as backend:
        chunks = [
            chunk
            async for chunk in backend.synthesize_text_stream(
                TTSSynthesisRequest(""), text_source("one", "", " two"), flush_each_chunk=True
            )
        ]
    assert len(chunks) == 1
    assert [event["event"] for event in seen] == ["start", "text", "flush", "text", "flush", "stop"]


@pytest.mark.parametrize("mode", ["stream", "buffered"])
async def test_existing_sentence_worker_consumes_same_transport(mode):
    connections = 0
    events = []

    async def handler(ws):
        nonlocal connections
        connections += 1
        while True:
            event = await receive(ws)
            events.append(event)
            if event["event"] == "stop":
                break
        await send(ws, {"event": "audio", "audio": b"\0\0\0\x40"})
        await send(ws, {"event": "finish", "reason": "stop"})

    async with peer(handler) as backend:

        def consume():
            request = TTSSynthesisRequest("Hello")
            if mode == "stream":
                return list(backend.synthesize_stream(request))
            return [backend.synthesize(request)]

        chunks = await asyncio.to_thread(consume)
    assert connections == 1
    assert [event["event"] for event in events] == ["start", "text", "flush", "stop"]
    assert events[1]["text"] == "Hello"
    assert chunks[0].text == "Hello"
    np.testing.assert_array_equal(chunks[0].audio, [0.0, 0.5])


@pytest.mark.parametrize(
    ("events", "message"),
    [
        ([{"event": "finish", "reason": "error"}], "finished with an error"),
        ([{"event": "finish", "reason": "stop"}], "without audio"),
        ([{"event": "audio", "audio": "not binary"}], "binary audio"),
        (
            [{"event": "audio", "audio": b"\x01"}, {"event": "finish", "reason": "stop"}],
            "incomplete PCM16",
        ),
        ([[]], "event object"),
    ],
)
async def test_invalid_responses_fail_without_retry(events, message):
    connections = 0

    async def handler(ws):
        nonlocal connections
        connections += 1
        await drain_input(ws)
        for event in events:
            await send(ws, event)
        await ws.wait_closed()

    async with peer(handler) as backend:
        with pytest.raises(TTSBackendError, match=message):
            _ = [
                chunk
                async for chunk in backend.synthesize_text_stream(
                    TTSSynthesisRequest(""), text_source("Hello")
                )
            ]
    assert connections == 1


@pytest.mark.parametrize("raw", ["json is not msgpack", b"\xc1"])
async def test_malformed_wire_data(raw):
    async def handler(ws):
        await drain_input(ws)
        await ws.send(raw)
        await ws.wait_closed()

    async with peer(handler) as backend:
        with pytest.raises(TTSBackendError, match="MessagePack"):
            _ = [
                chunk
                async for chunk in backend.synthesize_text_stream(
                    TTSSynthesisRequest(""), text_source("Hello")
                )
            ]


async def test_truncated_connection_after_audio_is_not_success():
    async def handler(ws):
        await drain_input(ws)
        await send(ws, {"event": "audio", "audio": b"\0\0"})
        await ws.close()

    async with peer(handler) as backend:
        stream = backend.synthesize_text_stream(TTSSynthesisRequest(""), text_source("Hi"))
        assert (await anext(stream)).audio.size == 1
        with pytest.raises(TTSBackendError, match="no retry"):
            await anext(stream)


async def test_timeout_closes_socket():
    closed = asyncio.Event()

    async def handler(ws):
        await drain_input(ws)
        await ws.wait_closed()
        closed.set()

    async with peer(handler) as backend:
        backend._timeout = 0.05
        with pytest.raises(TTSBackendError, match="timed out"):
            _ = [
                chunk
                async for chunk in backend.synthesize_text_stream(
                    TTSSynthesisRequest(""), text_source("Hi")
                )
            ]
        await asyncio.wait_for(closed.wait(), 1)


@pytest.mark.parametrize("cancel", [False, True])
async def test_abandoned_stream_cancels_text_producer_and_closes_socket(cancel):
    source_closed = asyncio.Event()
    socket_closed = asyncio.Event()

    async def handler(ws):
        await receive(ws)
        await receive(ws)
        await send(ws, {"event": "audio", "audio": b"\0\0"})
        await ws.wait_closed()
        socket_closed.set()

    async def source():
        try:
            yield "First"
            await asyncio.Event().wait()
        finally:
            source_closed.set()

    async with peer(handler) as backend:
        stream = backend.synthesize_text_stream(TTSSynthesisRequest(""), source())
        await anext(stream)
        if cancel:
            task = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await stream.aclose()
        await asyncio.wait_for(source_closed.wait(), 1)
        await asyncio.wait_for(socket_closed.wait(), 1)


async def test_input_error_wakes_receive_and_closes_socket():
    async def handler(ws):
        await ws.wait_closed()

    async def source():
        yield 123

    async with peer(handler) as backend:
        with pytest.raises(TTSBackendError, match="must be strings"):
            async with asyncio.timeout(0.5):
                _ = [
                    chunk
                    async for chunk in backend.synthesize_text_stream(
                        TTSSynthesisRequest(""), source()
                    )
                ]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"api_key": ""}, "FISH_TTS_API_KEY"),
        ({"ws_url": "https://api.fish.audio"}, "FISH_TTS_WS_URL"),
        ({"reference_id": ""}, "FISH_TTS_REFERENCE_ID"),
        ({"model": ""}, "FISH_TTS_MODEL"),
        ({"latency": "invalid"}, "FISH_TTS_LATENCY"),
        ({"timeout_seconds": 0}, "TIMEOUT"),
    ],
)
def test_invalid_configuration(overrides, message):
    config = dict(api_key="test-secret", reference_id="voice", model="s2.1-pro-free")
    config.update(overrides)
    with pytest.raises(TTSBackendError, match=message):
        FishAudioTTSBackend(**config).load()


async def test_early_finish_does_not_silently_drop_future_text():
    async def handler(ws):
        await receive(ws)
        await receive(ws)
        await send(ws, {"event": "finish", "reason": "stop"})

    async def source():
        yield "First phrase"
        await asyncio.Event().wait()

    async with peer(handler) as backend:
        with pytest.raises(TTSBackendError, match="before text input completed"):
            _ = [
                chunk
                async for chunk in backend.synthesize_text_stream(TTSSynthesisRequest(""), source())
            ]


async def test_audio_limit_and_partial_stream_error_are_observable(monkeypatch):
    from tts.backends import fish_audio

    monkeypatch.setattr(fish_audio, "_MAX_AUDIO_BYTES", 128)

    async def handler(ws):
        await drain_input(ws)
        for _ in range(3):
            await send(ws, {"event": "audio", "audio": b"\0" * 64})
        await send(ws, {"event": "finish", "reason": "stop"})

    async with peer(handler) as backend:
        stream = backend.synthesize_text_stream(TTSSynthesisRequest(""), text_source("Hi"))
        await anext(stream)
        await anext(stream)
        with pytest.raises(TTSBackendError, match="exceeded"):
            await anext(stream)


async def test_sync_generator_close_closes_websocket():
    closed = asyncio.Event()

    async def handler(ws):
        await drain_input(ws)
        await send(ws, {"event": "audio", "audio": b"\0\0"})
        await ws.wait_closed()
        closed.set()

    async with peer(handler) as backend:

        def consume_one():
            stream = backend.synthesize_stream(TTSSynthesisRequest("Hello"))
            next(stream)
            stream.close()

        await asyncio.to_thread(consume_one)
        await asyncio.wait_for(closed.wait(), 1)


def test_registry_validates_settings_and_creates_remote_runtime(monkeypatch):
    from config import settings
    from tts.registry import create_tts_runtime, tts_backend_statuses

    monkeypatch.setattr(settings, "FISH_TTS_API_KEY", "")
    status = next(item for item in tts_backend_statuses() if item["id"] == "fish_audio")
    assert status["available"] is False
    monkeypatch.setattr(settings, "FISH_TTS_API_KEY", "test-secret")
    status = next(item for item in tts_backend_statuses("fish_audio") if item["selected"])
    assert status["available"] and status["supports_streaming"]
    assert not status["supports_reference_conditioning"]
    runtime = create_tts_runtime("fish_audio")
    assert runtime.backend_id == "fish_audio"
    assert runtime.deployment == "remote"


def test_resolve_websocket_proxy_bypass_loopback_and_unconfigured(monkeypatch):
    import urllib.request
    from tts.backends.fish_audio import _resolve_websocket_proxy

    # Loopback targets must always bypass proxy
    assert _resolve_websocket_proxy("ws://127.0.0.1:17777/ws") is None
    assert _resolve_websocket_proxy("ws://localhost:17777/ws") is None

    # Users without proxy configured connect directly
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    assert _resolve_websocket_proxy("wss://api.fish.audio/v1/tts/live") is None

    # Explicit proxy bypass matches direct connection
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:7890"},
    )
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: True)
    assert _resolve_websocket_proxy("wss://api.fish.audio/v1/tts/live") is None


def test_resolve_websocket_proxy_detects_socks_protocol_and_falls_back_to_http(monkeypatch):
    import urllib.request
    from tts.backends.fish_audio import _resolve_websocket_proxy

    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    monkeypatch.setattr("tts.backends.fish_audio._has_python_socks", lambda: False)

    # SOCKS in wss_proxy falls back to HTTPS HTTP proxy
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {
            "wss": "socks5://127.0.0.1:1080",
            "https": "http://127.0.0.1:7890",
        },
    )
    assert _resolve_websocket_proxy("wss://api.fish.audio/v1/tts/live") == "http://127.0.0.1:7890"

    # SOCKS in https_proxy falls back to HTTP proxy
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {
            "https": "socks5h://127.0.0.1:1080",
            "http": "http://127.0.0.1:7890",
        },
    )
    assert _resolve_websocket_proxy("wss://api.fish.audio/v1/tts/live") == "http://127.0.0.1:7890"

    # macOS SOCKS entry normalized and falls back to HTTPS proxy
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {
            "socks": "http://127.0.0.1:7890",
            "https": "http://127.0.0.1:7890",
        },
    )
    assert _resolve_websocket_proxy("wss://api.fish.audio/v1/tts/live") == "http://127.0.0.1:7890"


def test_resolve_websocket_proxy_uses_socks_when_python_socks_available(monkeypatch):
    import urllib.request
    from tts.backends.fish_audio import _resolve_websocket_proxy

    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    monkeypatch.setattr("tts.backends.fish_audio._has_python_socks", lambda: True)

    # Uses SOCKS directly when python-socks is available
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {
            "https": "socks5://127.0.0.1:1080",
            "http": "http://127.0.0.1:7890",
        },
    )
    assert _resolve_websocket_proxy("wss://api.fish.audio/v1/tts/live") == "socks5://127.0.0.1:1080"

    # Normalized macOS SOCKS entry is preferred when python-socks is available
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {
            "socks": "http://127.0.0.1:7890",
            "https": "http://127.0.0.1:7890",
        },
    )
    assert (
        _resolve_websocket_proxy("wss://api.fish.audio/v1/tts/live") == "socks5h://127.0.0.1:7890"
    )


def test_resolve_websocket_proxy_raises_clear_error_when_proxy_unusable(monkeypatch):
    import urllib.request
    from tts.backend import TTSBackendError
    from tts.backends.fish_audio import _resolve_websocket_proxy

    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    monkeypatch.setattr("tts.backends.fish_audio._has_python_socks", lambda: False)

    # Only SOCKS proxy configured, no HTTP fallback
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"https": "socks5://127.0.0.1:1080"},
    )
    with pytest.raises(TTSBackendError, match="requires python-socks"):
        _resolve_websocket_proxy("wss://api.fish.audio/v1/tts/live")

    # SOCKS in socks entry without HTTP fallback
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"socks": "socks5h://127.0.0.1:1080"},
    )
    with pytest.raises(TTSBackendError, match="requires python-socks"):
        _resolve_websocket_proxy("wss://api.fish.audio/v1/tts/live")
