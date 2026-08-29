"""MiMo remote speech synthesis backend (Xiaomi MiMo-TTS series).

MiMo exposes synthesis through the OpenAI-shaped chat-completions endpoint
instead of ``/audio/speech``: the text travels in an ``assistant`` message and
audio comes back base64-encoded. Streaming (``mimo-v2.5-tts`` only) delivers
24kHz PCM16LE mono in ``choices[].delta.audio.data`` SSE frames.
"""

from __future__ import annotations

import base64
import binascii
import json

import httpx

from tts.backend import BaseTTSBackend, TTSAudioChunk, TTSSynthesisRequest, TTSBackendError
from tts.backends.openai_compatible import (
    _decode_pcm16le,
    _decode_wav,
    _iter_sse_data,
)

_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_SAMPLE_RATE = 24000
_STREAM_CHUNK_MILLISECONDS = 80
MIMO_TTS_MODEL_ID = "mimo-v2.5-tts"


def _chat_endpoint(base_url: str) -> str:
    value = str(base_url or "").strip().rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    return f"{value}/chat/completions"


class MiMoTTSBackend(BaseTTSBackend):
    backend_id = "mimo"
    deployment = "remote"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        voice: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        from config import settings

        self._base_url = str(base_url or settings.MIMO_TTS_BASE_URL or "").strip()
        self._api_key = str(api_key if api_key is not None else settings.MIMO_TTS_API_KEY).strip()
        self._model = str(model or settings.MIMO_TTS_MODEL or "").strip()
        self._voice = str(voice or settings.MIMO_TTS_VOICE or "").strip()
        self._timeout = max(
            1.0,
            float(timeout_seconds or settings.TTS_API_TIMEOUT_SECONDS or 60.0),
        )

    @property
    def supports_streaming(self) -> bool:
        return True

    def load(self) -> None:
        if not self._base_url:
            raise TTSBackendError("MIMO_TTS_BASE_URL is required")
        if not self._api_key:
            raise TTSBackendError("MIMO_TTS_API_KEY is required")
        if not self._model:
            raise TTSBackendError("MIMO_TTS_MODEL is required")
        if self._model != MIMO_TTS_MODEL_ID:
            raise TTSBackendError(
                f"unsupported MIMO_TTS_MODEL {self._model!r}; expected {MIMO_TTS_MODEL_ID!r}"
            )
        if not self._voice:
            raise TTSBackendError("MIMO_TTS_VOICE is required")

    def _headers(self, *, streaming: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if streaming:
            headers["Accept"] = "text/event-stream"
        return headers

    def _payload(self, request: TTSSynthesisRequest, *, stream: bool) -> dict:
        return {
            "model": self._model,
            "messages": [{"role": "assistant", "content": request.text}],
            "audio": {
                "format": "pcm16" if stream else "wav",
                "voice": request.voice or self._voice,
            },
            "stream": stream,
        }

    @staticmethod
    def _decode_b64(encoded: object, *, what: str) -> bytes:
        if not isinstance(encoded, str) or not encoded:
            raise TTSBackendError(f"MiMo TTS {what} did not contain base64 audio")
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise TTSBackendError(f"MiMo TTS returned invalid base64 audio ({what})") from exc

    def synthesize(self, request: TTSSynthesisRequest) -> TTSAudioChunk:
        self.load()
        try:
            response = httpx.post(
                _chat_endpoint(self._base_url),
                headers=self._headers(),
                json=self._payload(request, stream=False),
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TTSBackendError(f"MiMo TTS request failed: {exc}") from exc
        try:
            body = response.json()
            encoded = body["choices"][0]["message"]["audio"]["data"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise TTSBackendError("MiMo TTS response missing choices[0].message.audio.data") from exc
        data = self._decode_b64(encoded, what="response")
        if len(data) > _MAX_RESPONSE_BYTES:
            raise TTSBackendError("MiMo TTS response exceeded 64 MiB")
        sample_rate, audio = _decode_wav(data)
        return TTSAudioChunk(sample_rate, audio, request.text)

    def synthesize_stream(self, request: TTSSynthesisRequest):
        self.load()
        target_bytes = max(
            2,
            (_SAMPLE_RATE * 2 * _STREAM_CHUNK_MILLISECONDS // 1000) & ~1,
        )
        pending = bytearray()
        received_bytes = 0
        yielded = False
        try:
            with httpx.stream(
                "POST",
                _chat_endpoint(self._base_url),
                headers=self._headers(streaming=True),
                json=self._payload(request, stream=True),
                timeout=self._timeout,
            ) as response:
                response.raise_for_status()
                for raw_event in _iter_sse_data(response.iter_lines()):
                    if raw_event == "[DONE]":
                        break
                    try:
                        event = json.loads(raw_event)
                    except json.JSONDecodeError as exc:
                        raise TTSBackendError("MiMo TTS returned malformed SSE JSON") from exc
                    if event.get("error"):
                        raise TTSBackendError(f"MiMo TTS streaming failed: {event['error']}")
                    choices = event.get("choices")
                    if not choices:
                        continue
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        continue
                    audio = delta.get("audio")
                    if not isinstance(audio, dict):
                        continue
                    decoded = self._decode_b64(audio.get("data"), what="stream delta")
                    received_bytes += len(decoded)
                    if received_bytes > _MAX_RESPONSE_BYTES:
                        raise TTSBackendError("MiMo TTS stream exceeded 64 MiB")
                    pending.extend(decoded)
                    while len(pending) >= target_bytes:
                        chunk_bytes = bytes(pending[:target_bytes])
                        del pending[:target_bytes]
                        yield TTSAudioChunk(
                            _SAMPLE_RATE,
                            _decode_pcm16le(chunk_bytes),
                            request.text if not yielded else "",
                        )
                        yielded = True
        except httpx.HTTPError as exc:
            raise TTSBackendError(f"MiMo TTS streaming request failed: {exc}") from exc

        if pending:
            if len(pending) % 2:
                raise TTSBackendError("MiMo TTS returned an incomplete PCM16 sample")
            yield TTSAudioChunk(
                _SAMPLE_RATE,
                _decode_pcm16le(bytes(pending)),
                request.text if not yielded else "",
            )
            yielded = True
        if not yielded:
            raise TTSBackendError("MiMo TTS stream completed without audio")
