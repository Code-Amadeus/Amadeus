"""Standalone Fish Audio transport. Only the public extension SDK joins the Host."""
from __future__ import annotations

import importlib
import ipaddress
import math
import threading
import urllib.request
from collections.abc import Iterable, Iterator
from urllib.parse import urlsplit

import msgpack
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from amadeus_extension import AudioChunk, Extension, RequestContext, TTSRequest

ENDPOINT = "wss://api.fish.audio/v1/tts/live"
SAMPLE_RATE = 44100
MAX_AUDIO_BYTES = 64 * 1024 * 1024
MAX_CHUNK_BYTES = 1024 * 1024
_SOCKS_SCHEMES = frozenset({"socks", "socks4", "socks4a", "socks5", "socks5h"})


class FishAudioError(RuntimeError):
    """Safe provider-independent diagnostics; never raw headers or payloads."""


def _has_python_socks() -> bool:
    try:
        importlib.import_module("python_socks.sync")
        return True
    except ImportError:
        return False


def resolve_proxy(ws_url: str) -> str | None:
    """Preserve the built-in HTTP proxy choice before making a single connection."""
    endpoint = urlsplit(ws_url)
    host = endpoint.hostname or ""
    if not host or host == "localhost":
        return None
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    port = endpoint.port or (443 if endpoint.scheme == "wss" else 80)
    if urllib.request.proxy_bypass(f"{host}:{port}"):
        return None
    proxies = urllib.request.getproxies()
    keys = ("wss", "socks", "https", "all", "http") if endpoint.scheme == "wss" else ("ws", "socks", "https", "http", "all")
    has_socks = _has_python_socks()
    missing_socks = False
    for key in keys:
        proxy = str(proxies.get(key) or "").strip()
        if not proxy:
            continue
        if key == "socks" and proxy.startswith("http://"):
            proxy = "socks5h://" + proxy[7:]
        elif key == "socks" and "://" not in proxy:
            proxy = "socks5h://" + proxy
        scheme = urlsplit(proxy).scheme.lower()
        if scheme in _SOCKS_SCHEMES:
            if has_socks:
                return proxy
            missing_socks = True
        elif scheme in {"http", "https"}:
            return proxy
    if missing_socks:
        raise FishAudioError("Configured SOCKS proxy requires python-socks; no usable HTTP proxy is configured")
    return None


class FishAudioClient:
    def __init__(self, *, api_key: str, reference_id: str, model: str = "s2.1-pro-free",
                 latency: str = "balanced", timeout_seconds: float = 30,
                 ws_url: str = ENDPOINT) -> None:
        self.api_key = api_key.strip()
        self.reference_id = reference_id.strip()
        self.model = model.strip()
        self.latency = latency.strip()
        self.timeout = timeout_seconds
        self.ws_url = ws_url

    def validate(self) -> None:
        endpoint = urlsplit(self.ws_url)
        if endpoint.scheme not in {"ws", "wss"} or not endpoint.hostname:
            raise FishAudioError("Fish Audio requires a WebSocket endpoint")
        if not self.api_key or not self.reference_id or not self.model:
            raise FishAudioError("Fish Audio API key, voice reference and model are required")
        if self.latency not in {"normal", "balanced", "low"}:
            raise FishAudioError("Fish Audio latency must be normal, balanced, or low")
        if type(self.timeout) not in {int, float} or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise FishAudioError("Fish Audio timeout must be positive and finite")

    def synthesize_text_stream(self, request: TTSRequest, context: RequestContext, text_chunks: Iterable[str],
                               *, flush_each_chunk: bool = False) -> Iterator[AudioChunk]:
        """Duplex text/audio transport; input sources must bound their blocking waits."""
        self.validate()
        context.check_cancelled()
        if type(request.speed) not in {int, float} or not math.isfinite(request.speed) or not .5 <= request.speed <= 2:
            raise FishAudioError("Fish Audio pacing hint must satisfy the public TTS contract")
        timeout = min(self.timeout, request.deadline_ms / 1000) if request.deadline_ms else self.timeout
        closing = threading.Event()
        input_finished = threading.Event()
        sender_errors: list[Exception] = []
        sender = None
        received_bytes = 0
        odd = b""
        yielded = False
        try:
            with connect(self.ws_url, additional_headers={"Authorization": f"Bearer {self.api_key}", "model": self.model},
                         proxy=resolve_proxy(self.ws_url), open_timeout=timeout, close_timeout=min(2, timeout),
                         max_size=MAX_AUDIO_BYTES, max_queue=2) as websocket:
                context.on_cancel(closing.set)
                context.on_cancel(websocket.close)

                def send(event):
                    context.check_cancelled()
                    if closing.is_set():
                        return
                    websocket.send(msgpack.packb(event, use_bin_type=True))

                send({"event": "start", "request": {"text": "", "reference_id": self.reference_id,
                     "format": "pcm", "sample_rate": SAMPLE_RATE, "latency": self.latency,
                     "prosody": {"speed": request.speed}}})

                def send_text():
                    try:
                        for text in text_chunks:
                            context.check_cancelled()
                            if closing.is_set():
                                return
                            if not isinstance(text, str):
                                raise FishAudioError("Fish Audio text chunks must be strings")
                            if text:
                                send({"event": "text", "text": text})
                                if flush_each_chunk:
                                    send({"event": "flush"})
                        input_finished.set()
                        send({"event": "stop"})
                    except Exception as error:
                        sender_errors.append(error)
                        websocket.close()

                sender = threading.Thread(target=send_text, daemon=True, name="fish-audio-text")
                sender.start()
                try:
                    while True:
                        context.check_cancelled()
                        if sender_errors:
                            raise FishAudioError("Fish Audio text input or sending failed")
                        raw = websocket.recv(timeout=timeout)
                        context.check_cancelled()
                        if not isinstance(raw, bytes):
                            raise FishAudioError("Fish Audio expected binary MessagePack")
                        try:
                            event = msgpack.unpackb(raw, raw=False)
                        except (ValueError, TypeError, msgpack.UnpackException):
                            raise FishAudioError("Fish Audio returned malformed MessagePack") from None
                        if not isinstance(event, dict):
                            raise FishAudioError("Fish Audio expected an event object")
                        kind = event.get("event")
                        if kind == "finish":
                            if event.get("reason") != "stop":
                                raise FishAudioError("Fish Audio synthesis finished with an error")
                            if not input_finished.is_set():
                                raise FishAudioError("Fish Audio finished before text input completed")
                            sender.join(timeout=min(2, timeout))
                            if sender_errors or sender.is_alive():
                                raise FishAudioError("Fish Audio text input did not finish cleanly")
                            break
                        if kind != "audio":
                            continue
                        audio = event.get("audio")
                        if not isinstance(audio, bytes):
                            raise FishAudioError("Fish Audio event is missing binary PCM audio")
                        received_bytes += len(audio)
                        if received_bytes > MAX_AUDIO_BYTES:
                            raise FishAudioError("Fish Audio stream exceeded 64 MiB")
                        offset = 0
                        if odd and audio:
                            yield AudioChunk(SAMPLE_RATE, odd + audio[:1])
                            yielded = True
                            odd, offset = b"", 1
                        complete = offset + ((len(audio) - offset) & ~1)
                        while offset < complete:
                            context.check_cancelled()
                            end = min(complete, offset + MAX_CHUNK_BYTES)
                            yield AudioChunk(SAMPLE_RATE, audio[offset:end])
                            yielded = True
                            offset = end
                        if complete < len(audio):
                            odd = audio[complete:]
                finally:
                    closing.set()
                    websocket.close()
                    if sender:
                        sender.join(timeout=min(2, timeout))
        except TimeoutError:
            raise FishAudioError("Fish Audio WebSocket timed out; no retry was made") from None
        except (OSError, WebSocketException) as error:
            context.check_cancelled()
            if sender_errors:
                raise FishAudioError("Fish Audio text input or sending failed; no retry was made") from None
            raise FishAudioError(f"Fish Audio WebSocket failed ({type(error).__name__}); no retry was made") from None
        if odd:
            raise FishAudioError("Fish Audio returned an incomplete PCM16 sample")
        if not yielded:
            raise FishAudioError("Fish Audio stream completed without audio")


def client_from_context(context: RequestContext) -> FishAudioClient:
    return FishAudioClient(api_key=context.secrets.get("api_key", ""), reference_id=context.config.get("reference_id", ""),
                           model=context.config.get("model", "s2.1-pro-free"), latency=context.config.get("latency", "balanced"))


extension = Extension()


@extension.initialize
def initialize(context: RequestContext) -> None:
    client = client_from_context(context)
    client.validate()
    resolve_proxy(client.ws_url)


@extension.tts("fish")
def synthesize(request: TTSRequest, context: RequestContext) -> Iterator[AudioChunk]:
    # The Host already committed an utterance; flush that boundary once.
    yield from client_from_context(context).synthesize_text_stream(request, context, [request.text], flush_each_chunk=True)
