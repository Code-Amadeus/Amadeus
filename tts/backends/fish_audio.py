"""Fish Audio MessagePack WebSocket synthesis with concurrent text/audio streams."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Iterator
from urllib.parse import urlsplit

import msgpack
import numpy as np
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from tts.backend import BaseTTSBackend, TTSAudioChunk, TTSSynthesisRequest, TTSBackendError
from tts.backends.openai_compatible import _decode_pcm16le

_SAMPLE_RATE = 44100
_MAX_AUDIO_BYTES = 64 * 1024 * 1024


def _resolve_websocket_proxy(ws_url: str) -> str | None:
    import ipaddress
    import urllib.request

    endpoint = urlsplit(ws_url)
    host = endpoint.hostname or ""
    if not host or host == "localhost":
        return None
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    port = endpoint.port or (443 if endpoint.scheme in {"wss", "https"} else 80)
    if urllib.request.proxy_bypass(f"{host}:{port}"):
        return None

    proxies = urllib.request.getproxies()
    is_secure = endpoint.scheme == "wss"
    schemes = ["wss", "socks", "https"] if is_secure else ["ws", "socks", "https", "http"]

    has_python_socks = None
    for scheme in schemes:
        proxy = proxies.get(scheme)
        if not proxy:
            continue
        if scheme == "socks":
            if has_python_socks is None:
                try:
                    import python_socks.async_  # noqa: F401

                    has_python_socks = True
                except ImportError:
                    has_python_socks = False
            if not has_python_socks:
                continue
            if proxy.startswith("http://"):
                proxy = "socks5h://" + proxy[7:]
            return proxy
        return proxy
    return None


class FishAudioTTSBackend(BaseTTSBackend):
    backend_id = "fish_audio"
    deployment = "remote"
    supports_streaming = True

    def __init__(
        self,
        *,
        ws_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        reference_id: str | None = None,
        latency: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        from config import settings

        self._ws_url = str(settings.FISH_TTS_WS_URL if ws_url is None else ws_url).strip()
        self._api_key = str(settings.FISH_TTS_API_KEY if api_key is None else api_key).strip()
        self._model = str(settings.FISH_TTS_MODEL if model is None else model).strip()
        self._reference_id = str(
            settings.FISH_TTS_REFERENCE_ID if reference_id is None else reference_id
        ).strip()
        self._latency = str(settings.FISH_TTS_LATENCY if latency is None else latency).strip()
        self._timeout = (
            settings.TTS_API_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        )

    def load(self) -> None:
        endpoint = urlsplit(self._ws_url)
        if endpoint.scheme not in {"ws", "wss"} or not endpoint.hostname:
            raise TTSBackendError("FISH_TTS_WS_URL must be a ws:// or wss:// endpoint")
        if not self._api_key:
            raise TTSBackendError("FISH_TTS_API_KEY is required")
        if not self._model:
            raise TTSBackendError("FISH_TTS_MODEL is required")
        if not self._reference_id:
            raise TTSBackendError("FISH_TTS_REFERENCE_ID is required")
        if self._latency not in {"normal", "balanced", "low"}:
            raise TTSBackendError("FISH_TTS_LATENCY must be normal, balanced, or low")
        if not np.isfinite(self._timeout) or self._timeout <= 0:
            raise TTSBackendError("TTS_API_TIMEOUT_SECONDS must be positive and finite")

    def synthesize(self, request: TTSSynthesisRequest) -> TTSAudioChunk:
        chunks = list(self.synthesize_stream(request))
        return TTSAudioChunk(
            _SAMPLE_RATE, np.concatenate([chunk.audio for chunk in chunks]), request.text
        )

    def synthesize_stream(self, request: TTSSynthesisRequest) -> Iterator[TTSAudioChunk]:
        """Bridge the sentence worker's synchronous iterator to the duplex transport."""

        async def text_chunks():
            yield request.text

        # The existing pipeline invokes this iterator on its synthesis worker thread.
        with asyncio.Runner() as runner:
            # The local scheduler already chose a stable sentence/utterance boundary.
            stream = self.synthesize_text_stream(request, text_chunks(), flush_each_chunk=True)
            try:
                while True:
                    try:
                        chunk = runner.run(anext(stream))
                    except StopAsyncIteration:
                        break
                    yield chunk
            finally:
                runner.run(stream.aclose())

    async def synthesize_text_stream(
        self,
        request: TTSSynthesisRequest,
        text_chunks: AsyncIterable[str],
        *,
        flush_each_chunk: bool = False,
    ) -> AsyncIterator[TTSAudioChunk]:
        """Synthesize arriving text without collecting it or waiting for input EOF.

        Only ``text_chunks`` is sent as text; ``request.text`` labels the first
        audio chunk for existing sentence consumers. Flush is opt-in because
        forcing a synthesis boundary after every token can degrade prosody.
        Callers that stop consuming early must close this async generator.
        """
        self.load()
        pending = bytearray()
        received_bytes = 0
        yielded = False
        input_finished = False
        try:
            proxy = _resolve_websocket_proxy(self._ws_url)
            async with connect(
                self._ws_url,
                additional_headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "model": self._model,
                },
                proxy=proxy,
                open_timeout=self._timeout,
                close_timeout=min(5.0, self._timeout),
                max_size=_MAX_AUDIO_BYTES,
            ) as websocket:

                async def send(event):
                    await asyncio.wait_for(
                        websocket.send(msgpack.packb(event, use_bin_type=True)),
                        timeout=self._timeout,
                    )

                await send(
                    {
                        "event": "start",
                        "request": {
                            "text": "",
                            "reference_id": request.voice or self._reference_id,
                            "format": "pcm",
                            "sample_rate": _SAMPLE_RATE,
                            "latency": self._latency,
                            "prosody": {"speed": request.speed},
                        },
                    }
                )

                async def send_text():
                    nonlocal input_finished
                    async for text in text_chunks:
                        if not isinstance(text, str):
                            raise TTSBackendError("Fish Audio text chunks must be strings")
                        if text:
                            await send({"event": "text", "text": text})
                            if flush_each_chunk:
                                await send({"event": "flush"})
                    input_finished = True
                    await send({"event": "stop"})

                sender = asyncio.create_task(send_text())
                receiver = None
                try:
                    while True:
                        receiver = asyncio.create_task(websocket.recv())
                        # A failed text source/send must wake a blocked receive immediately.
                        done, _ = await asyncio.wait(
                            {sender, receiver},
                            timeout=self._timeout,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if not done:
                            raise TimeoutError
                        if sender in done:
                            sender.result()
                        raw = await asyncio.wait_for(receiver, timeout=self._timeout)
                        if not isinstance(raw, bytes):
                            raise TTSBackendError("Fish Audio expected a binary MessagePack event")
                        try:
                            event = msgpack.unpackb(raw, raw=False)
                        except (ValueError, TypeError, msgpack.UnpackException) as exc:
                            raise TTSBackendError(
                                "Fish Audio returned malformed MessagePack"
                            ) from exc
                        if not isinstance(event, dict):
                            raise TTSBackendError("Fish Audio expected an event object")
                        kind = event.get("event")
                        if kind == "finish":
                            if event.get("reason") != "stop":
                                raise TTSBackendError("Fish Audio synthesis finished with an error")
                            if not input_finished:
                                raise TTSBackendError(
                                    "Fish Audio finished before text input completed"
                                )
                            await sender
                            break
                        if kind != "audio":
                            continue  # Official protocol allows future event types.
                        audio = event.get("audio")
                        if not isinstance(audio, bytes):
                            raise TTSBackendError("Fish Audio event missing binary audio")
                        received_bytes += len(audio)
                        if received_bytes > _MAX_AUDIO_BYTES:
                            raise TTSBackendError("Fish Audio stream exceeded 64 MiB")
                        pending.extend(audio)
                        complete_bytes = len(pending) & ~1
                        if complete_bytes:
                            pcm = bytes(pending[:complete_bytes])
                            del pending[:complete_bytes]
                            yield TTSAudioChunk(
                                _SAMPLE_RATE,
                                _decode_pcm16le(pcm),
                                request.text if not yielded else "",
                            )
                            yielded = True
                finally:
                    tasks = [task for task in (sender, receiver) if task is not None]
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError as exc:
            raise TTSBackendError("Fish Audio WebSocket timed out") from exc
        except (OSError, WebSocketException) as exc:
            # Do not include headers, credentials, or provider payloads in diagnostics.
            raise TTSBackendError(
                f"Fish Audio WebSocket failed ({type(exc).__name__}); no retry was made"
            ) from exc
        if pending:
            raise TTSBackendError("Fish Audio returned an incomplete PCM16 sample")
        if not yielded:
            raise TTSBackendError("Fish Audio stream completed without audio")
