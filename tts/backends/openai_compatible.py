"""OpenAI-compatible remote speech synthesis backend."""

from __future__ import annotations

import base64
import binascii
import io
import json
import wave
from collections.abc import Iterable, Iterator

import httpx
import numpy as np

from tts.backend import BaseTTSBackend, TTSAudioChunk, TTSSynthesisRequest, TTSBackendError


_MAX_SPEECH_RESPONSE_BYTES = 64 * 1024 * 1024
_OPENAI_PCM_SAMPLE_RATE = 24000
_STREAM_CHUNK_MILLISECONDS = 80
_STREAM_PROTOCOLS = frozenset({"buffered", "openai_sse"})


def _speech_endpoint(base_url: str) -> str:
    value = str(base_url or "").strip().rstrip("/")
    if value.endswith("/audio/speech"):
        return value
    return f"{value}/audio/speech"


def _decode_wav(data: bytes) -> tuple[int, np.ndarray]:
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels = wav.getnchannels()
            sample_rate = wav.getframerate()
            width = wav.getsampwidth()
            frames = wav.readframes(wav.getnframes())
    except (EOFError, wave.Error) as exc:
        raise TTSBackendError(f"remote TTS returned invalid WAV audio: {exc}") from exc
    if width == 1:
        audio = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        audio = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise TTSBackendError(f"unsupported WAV sample width: {width}")
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return int(sample_rate), np.ascontiguousarray(audio, dtype=np.float32)


def _decode_pcm16le(data: bytes) -> np.ndarray:
    if len(data) % 2:
        raise TTSBackendError("remote TTS returned an incomplete PCM16 sample")
    return np.ascontiguousarray(
        np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0,
        dtype=np.float32,
    )


def _iter_sse_data(lines: Iterable[str]) -> Iterator[str]:
    data_lines: list[str] = []
    for raw_line in lines:
        line = str(raw_line or "").rstrip("\r")
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield "\n".join(data_lines)


class OpenAICompatibleTTSBackend(BaseTTSBackend):
    backend_id = "openai_compatible"
    deployment = "remote"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        voice: str | None = None,
        stream_protocol: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        from config import settings

        self._base_url = str(base_url or settings.TTS_API_BASE_URL or "").strip()
        self._api_key = str(api_key if api_key is not None else settings.TTS_API_KEY).strip()
        self._model = str(model or settings.TTS_API_MODEL or "").strip()
        self._voice = str(voice or settings.TTS_API_VOICE or "").strip()
        self._stream_protocol = str(
            stream_protocol or settings.TTS_API_STREAM_PROTOCOL or "buffered"
        ).strip().lower()
        self._timeout = max(
            1.0,
            float(timeout_seconds or settings.TTS_API_TIMEOUT_SECONDS or 60.0),
        )

    @property
    def supports_streaming(self) -> bool:
        return self._stream_protocol == "openai_sse"

    def load(self) -> None:
        if not self._base_url:
            raise TTSBackendError("TTS_API_BASE_URL is required")
        if not self._model:
            raise TTSBackendError("TTS_API_MODEL is required")
        if not self._voice:
            raise TTSBackendError("TTS_API_VOICE is required")
        if self._stream_protocol not in _STREAM_PROTOCOLS:
            choices = ", ".join(sorted(_STREAM_PROTOCOLS))
            raise TTSBackendError(
                f"unsupported TTS_API_STREAM_PROTOCOL {self._stream_protocol!r}; "
                f"expected one of: {choices}"
            )

    def _headers(self, *, streaming: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        if streaming:
            headers["Accept"] = "text/event-stream"
        return headers

    def _payload(
        self,
        request: TTSSynthesisRequest,
        *,
        response_format: str,
    ) -> dict[str, str | float]:
        return {
            "model": self._model,
            "input": request.text,
            "voice": request.voice or self._voice,
            "response_format": response_format,
            "speed": max(0.25, min(4.0, float(request.speed))),
        }

    def synthesize(self, request: TTSSynthesisRequest) -> TTSAudioChunk:
        self.load()
        try:
            response = httpx.post(
                _speech_endpoint(self._base_url),
                headers=self._headers(),
                json=self._payload(request, response_format="wav"),
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TTSBackendError(f"remote TTS request failed: {exc}") from exc
        if not response.content:
            raise TTSBackendError("remote TTS returned empty audio")
        if len(response.content) > _MAX_SPEECH_RESPONSE_BYTES:
            raise TTSBackendError("remote TTS response exceeded 64 MiB")
        sample_rate, audio = _decode_wav(response.content)
        return TTSAudioChunk(sample_rate, audio, request.text)

    def synthesize_stream(
        self,
        request: TTSSynthesisRequest,
    ) -> Iterator[TTSAudioChunk]:
        self.load()
        if not self.supports_streaming:
            yield self.synthesize(request)
            return

        payload = self._payload(request, response_format="pcm")
        payload["stream_format"] = "sse"
        target_bytes = max(
            2,
            (_OPENAI_PCM_SAMPLE_RATE * 2 * _STREAM_CHUNK_MILLISECONDS // 1000) & ~1,
        )
        pending = bytearray()
        received_bytes = 0
        yielded = False
        try:
            with httpx.stream(
                "POST",
                _speech_endpoint(self._base_url),
                headers=self._headers(streaming=True),
                json=payload,
                timeout=self._timeout,
            ) as response:
                response.raise_for_status()
                for raw_event in _iter_sse_data(response.iter_lines()):
                    if raw_event == "[DONE]":
                        break
                    try:
                        event = json.loads(raw_event)
                    except json.JSONDecodeError as exc:
                        raise TTSBackendError(
                            "remote TTS returned malformed SSE JSON"
                        ) from exc
                    event_type = str(event.get("type") or "")
                    if event_type == "speech.audio.done":
                        break
                    if event_type == "error" or event_type.endswith(".error"):
                        detail = event.get("error") or event.get("message") or event_type
                        raise TTSBackendError(f"remote TTS streaming failed: {detail}")
                    if event_type != "speech.audio.delta":
                        continue
                    encoded = event.get("audio")
                    if not isinstance(encoded, str) or not encoded:
                        raise TTSBackendError(
                            "remote TTS audio delta did not contain base64 audio"
                        )
                    try:
                        decoded = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError) as exc:
                        raise TTSBackendError(
                            "remote TTS returned invalid base64 PCM audio"
                        ) from exc
                    received_bytes += len(decoded)
                    if received_bytes > _MAX_SPEECH_RESPONSE_BYTES:
                        raise TTSBackendError(
                            "remote TTS stream exceeded 64 MiB"
                        )
                    pending.extend(decoded)
                    while len(pending) >= target_bytes:
                        chunk_bytes = bytes(pending[:target_bytes])
                        del pending[:target_bytes]
                        yield TTSAudioChunk(
                            _OPENAI_PCM_SAMPLE_RATE,
                            _decode_pcm16le(chunk_bytes),
                            request.text if not yielded else "",
                        )
                        yielded = True
        except httpx.HTTPError as exc:
            raise TTSBackendError(f"remote TTS streaming request failed: {exc}") from exc

        if pending:
            yield TTSAudioChunk(
                _OPENAI_PCM_SAMPLE_RATE,
                _decode_pcm16le(bytes(pending)),
                request.text if not yielded else "",
            )
            yielded = True
        if not yielded:
            raise TTSBackendError("remote TTS stream completed without audio")
