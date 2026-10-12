"""Standalone Fish package against local binary MessagePack WebSocket peers."""
from __future__ import annotations

import ast
import json
import struct
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "examples/extensions/fish-audio/src"
from server.extensions.sdk import sdk_spec
sys.path.insert(0, str(ROOT / "vendor/extension-sdk" / sdk_spec()["filename"]))
sys.path.insert(0, str(PACKAGE))

import msgpack
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

import amadeus_example_fish_audio as fish
from amadeus_extension import CancelledError, RequestContext, TTSRequest
from server.extensions.package import validate_config, validate_manifest


def send(ws, event):
    ws.send(msgpack.packb(event, use_bin_type=True))


def receive(ws):
    return msgpack.unpackb(ws.recv(timeout=2), raw=False)


def drain_input(ws):
    while receive(ws)["event"] != "stop":
        pass


@contextmanager
def peer(handler, *, timeout=1):
    connections, errors = [], []
    def wrapped(ws):
        connections.append(ws)
        try:
            handler(ws)
        except ConnectionClosed:
            pass
        except Exception as error:
            errors.append(error)
    with serve(wrapped, "127.0.0.1", 0, close_timeout=.2) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = fish.FishAudioClient(ws_url=f"ws://127.0.0.1:{server.socket.getsockname()[1]}/v1/tts/live",
                                          api_key="test-secret", reference_id="test-voice", timeout_seconds=timeout)
            yield client, connections
        finally:
            for connection in connections:
                connection.close()
            server.shutdown()
            worker.join(timeout=2)
            assert not worker.is_alive()
    assert not errors


def context():
    return RequestContext(config={"reference_id": "test-voice"}, secrets={"api_key": "test-secret"})


def request(text="こんにちは。", speed=1):
    return TTSRequest("synthetic-utterance", text, "ja", speed)


def test_manifest_and_imports_are_independent_of_host():
    path = PACKAGE / "amadeus_example_fish_audio"
    definition = json.loads((path / "amadeus-extension.json").read_text(encoding="utf-8"))
    validate_manifest(definition)
    values, missing = validate_config(definition, {}, allow_incomplete=True)
    assert values == {"model": "s2.1-pro-free", "latency": "balanced"}
    assert missing == ["api_key", "reference_id"]
    assert definition["contributes"]["voice_backend"][0]["languages"] == ["ja", "en"]
    assert "api_key" not in values and "endpoint" not in definition["config"]
    tree = ast.parse((path / "__init__.py").read_text(encoding="utf-8"))
    imported = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    imported += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    assert all(name.split(".")[0] not in {"server", "core", "config", "tts", "llm", "asr"} for name in imported)


def test_initialize_validates_without_opening_network(monkeypatch):
    def unexpected_connect(*args, **kwargs):
        raise AssertionError("initialize must not connect")
    monkeypatch.setattr(fish, "connect", unexpected_connect)
    monkeypatch.setattr(fish, "resolve_proxy", lambda url: None)
    fish.initialize(context())
    with pytest.raises(fish.FishAudioError, match="required"):
        fish.initialize(RequestContext())


def test_sdk_sentence_sends_start_text_flush_stop_once(monkeypatch):
    seen, headers = [], {}
    pcm = struct.pack("<3h", -32768, 0, 32767)
    def handler(ws):
        headers.update(ws.request.headers)
        while True:
            event = receive(ws)
            seen.append(event)
            if event["event"] == "stop":
                break
        send(ws, {"event": "audio", "audio": pcm})
        send(ws, {"event": "finish", "reason": "stop"})
    with peer(handler) as (client, connections):
        monkeypatch.setattr(fish, "client_from_context", lambda value: client)
        chunks = list(fish.synthesize(request(speed=1.2), context()))
        assert len(connections) == 1
    assert headers["authorization"] == "Bearer test-secret" and headers["model"] == "s2.1-pro-free"
    assert seen == [
        {"event": "start", "request": {"text": "", "reference_id": "test-voice", "format": "pcm", "sample_rate": 44100,
                                        "latency": "balanced", "prosody": {"speed": 1.2}}},
        {"event": "text", "text": "こんにちは。"}, {"event": "flush"}, {"event": "stop"},
    ]
    assert b"".join(chunk.pcm_s16le for chunk in chunks) == pcm
    assert all(chunk.sample_rate == 44100 for chunk in chunks)


def test_duplex_audio_precedes_future_input_and_preserves_odd_samples():
    first_audio = threading.Event()
    seen = []
    pcm = struct.pack("<3h", -32768, 0, 32767)
    def handler(ws):
        seen.extend([receive(ws), receive(ws)])
        send(ws, {"event": "audio", "audio": pcm[:3]})
        seen.extend([receive(ws), receive(ws)])
        send(ws, {"event": "audio", "audio": pcm[3:]})
        send(ws, {"event": "finish", "reason": "stop"})
    def source():
        yield "first"
        assert first_audio.wait(2)
        yield "second"
    with peer(handler) as (client, connections):
        stream = client.synthesize_text_stream(request("not repeated"), context(), source())
        first = next(stream)
        assert first.pcm_s16le == pcm[:2]
        first_audio.set()
        chunks = [first, *list(stream)]
        assert len(connections) == 1
    assert b"".join(chunk.pcm_s16le for chunk in chunks) == pcm
    assert [event["event"] for event in seen] == ["start", "text", "text", "stop"]
    assert [event["text"] for event in seen if event["event"] == "text"] == ["first", "second"]


@pytest.mark.parametrize("failure", ["error_finish", "disconnect", "text_frame", "malformed", "not_object", "audio_string", "incomplete", "empty"])
def test_protocol_failures_do_not_retry_or_return_success(failure):
    def handler(ws):
        drain_input(ws)
        if failure == "empty":
            send(ws, {"event": "finish", "reason": "stop"})
            return
        if failure == "text_frame":
            ws.send("not a binary frame")
            return
        if failure == "malformed":
            ws.send(b"\xc1")
            return
        if failure == "not_object":
            send(ws, [])
            return
        if failure == "audio_string":
            send(ws, {"event": "audio", "audio": "not PCM bytes"})
            return
        send(ws, {"event": "audio", "audio": b"\x00" if failure == "incomplete" else b"\x00\x00"})
        if failure == "disconnect":
            ws.close()
        else:
            send(ws, {"event": "finish", "reason": "error" if failure == "error_finish" else "stop"})
    with peer(handler) as (client, connections):
        with pytest.raises(fish.FishAudioError) as error:
            list(client.synthesize_text_stream(request(), context(), ["text"]))
        assert "test-secret" not in str(error.value) and len(connections) == 1


def test_provider_finish_before_text_eof_is_failure():
    release_input = threading.Event()
    def handler(ws):
        receive(ws)
        receive(ws)
        send(ws, {"event": "audio", "audio": b"\x00\x00"})
        send(ws, {"event": "finish", "reason": "stop"})
        release_input.set()
    def source():
        yield "first"
        # Keep input open until main thread sees the protocol failure.
        release_input.wait(2)
        time.sleep(.1)
        yield "second"
    with peer(handler) as (client, connections):
        with pytest.raises(fish.FishAudioError, match="before text"):
            list(client.synthesize_text_stream(request(), context(), source()))
        assert len(connections) == 1


def test_audio_limit_and_wire_chunk_splitting(monkeypatch):
    def large_handler(ws):
        drain_input(ws)
        send(ws, {"event": "audio", "audio": b"\x00\x00" * (fish.MAX_CHUNK_BYTES // 2 + 1)})
        send(ws, {"event": "finish", "reason": "stop"})
    with peer(large_handler) as (client, _):
        chunks = list(client.synthesize_text_stream(request(), context(), ["text"]))
    assert [len(chunk.pcm_s16le) for chunk in chunks] == [1024 * 1024, 2]
    monkeypatch.setattr(fish, "MAX_AUDIO_BYTES", 100)
    def bound_handler(ws):
        drain_input(ws)
        send(ws, {"event": "audio", "audio": b"\x00\x00" * 30})
        send(ws, {"event": "audio", "audio": b"\x00\x00" * 21})
    with peer(bound_handler) as (client, connections):
        with pytest.raises(fish.FishAudioError, match="64 MiB"):
            list(client.synthesize_text_stream(request(), context(), ["text"]))
        assert len(connections) == 1


def test_cancellation_unblocks_receive_and_closes_socket_promptly():
    receiving, closed = threading.Event(), threading.Event()
    def handler(ws):
        drain_input(ws)
        receiving.set()
        try:
            ws.recv(timeout=2)
        finally:
            closed.set()
    ctx, errors = context(), []
    with peer(handler, timeout=5) as (client, connections):
        def synthesize():
            try:
                list(client.synthesize_text_stream(request(), ctx, ["text"]))
            except CancelledError:
                errors.append("cancelled")
        worker = threading.Thread(target=synthesize)
        worker.start()
        assert receiving.wait(2)
        started = time.monotonic()
        ctx._cancel()
        worker.join(timeout=.5)
        assert not worker.is_alive() and errors == ["cancelled"]
        assert closed.wait(.5) and time.monotonic() - started < 1 and len(connections) == 1


def test_timeout_and_sender_failure_close_without_retry():
    def handler(ws):
        try:
            drain_input(ws)
            ws.recv(timeout=2)
        except ConnectionClosed:
            pass
    with peer(handler, timeout=.1) as (client, connections):
        with pytest.raises(fish.FishAudioError, match="timed out"):
            list(client.synthesize_text_stream(request(), context(), ["text"]))
        assert len(connections) == 1
    def broken_source():
        yield "first"
        raise RuntimeError("test-secret")
    with peer(handler) as (client, connections):
        with pytest.raises(fish.FishAudioError) as error:
            list(client.synthesize_text_stream(request(), context(), broken_source()))
        assert "test-secret" not in str(error.value) and len(connections) == 1


def test_proxy_http_baseline_and_explicit_missing_socks(monkeypatch):
    monkeypatch.setattr(fish.urllib.request, "proxy_bypass", lambda value: False)
    monkeypatch.setattr(fish, "_has_python_socks", lambda: False)
    monkeypatch.setattr(fish.urllib.request, "getproxies", lambda: {"socks": "127.0.0.1:1080", "https": "http://proxy.example:7890"})
    assert fish.resolve_proxy(fish.ENDPOINT) == "http://proxy.example:7890"
    assert fish.resolve_proxy("ws://127.0.0.1:1234/test") is None
    monkeypatch.setattr(fish.urllib.request, "getproxies", lambda: {"socks": "127.0.0.1:1080"})
    with pytest.raises(fish.FishAudioError, match="python-socks"):
        fish.resolve_proxy(fish.ENDPOINT)
    monkeypatch.setattr(fish, "_has_python_socks", lambda: True)
    assert fish.resolve_proxy(fish.ENDPOINT) == "socks5h://127.0.0.1:1080"
