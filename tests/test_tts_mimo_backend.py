"""MiMo TTS backend: chat-completions protocol mapping tests."""

from __future__ import annotations

import base64
import io
import json
import wave

import httpx
import numpy as np
import pytest

from tts.backend import TTSSynthesisRequest, TTSBackendError
from tts.backends.mimo import MiMoTTSBackend, _chat_endpoint
from tts.registry import tts_backend_ids, tts_backend_statuses


def _wav_bytes(*, sample_rate: int = 24000) -> bytes:
    output = io.BytesIO()
    samples = (np.asarray([0.0, 0.25, -0.25], dtype=np.float32) * 32767).astype("<i2")
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())
    return output.getvalue()


def _backend(**overrides) -> MiMoTTSBackend:
    kwargs = {
        "base_url": "https://api.xiaomimimo.com/v1",
        "api_key": "secret",
        "model": "mimo-v2.5-tts",
        "voice": "mimo_default",
    }
    kwargs.update(overrides)
    return MiMoTTSBackend(**kwargs)


def _sse_lines(chunks: list[dict]) -> list[str]:
    lines: list[str] = []
    for chunk in chunks:
        lines.append(f"data: {json.dumps(chunk)}")
        lines.append("")
    lines.append("data: [DONE]")
    lines.append("")
    return lines


def _fake_stream(lines, observed):
    class FakeStreamingResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            observed["closed"] = True

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def iter_lines():
            yield from lines

    def fake_stream(method, url, **kwargs):
        observed.update(method=method, url=url, **kwargs)
        return FakeStreamingResponse()

    return fake_stream


def test_mimo_registered_as_builtin_remote_backend() -> None:
    assert "mimo" in tts_backend_ids()
    statuses = {item["id"]: item for item in tts_backend_statuses()}
    assert statuses["mimo"]["deployment"] == "remote"
    assert statuses["mimo"]["supports_streaming"] is True


def test_chat_endpoint_normalization() -> None:
    assert _chat_endpoint("https://api.xiaomimimo.com/v1") == (
        "https://api.xiaomimimo.com/v1/chat/completions"
    )
    assert _chat_endpoint("https://x.example/v1/chat/completions/") == (
        "https://x.example/v1/chat/completions"
    )


def test_load_requires_api_key() -> None:
    with pytest.raises(TTSBackendError, match="MIMO_TTS_API_KEY"):
        _backend(api_key="").load()


def test_load_rejects_unsupported_mimo_variants() -> None:
    with pytest.raises(TTSBackendError, match="unsupported MIMO_TTS_MODEL"):
        _backend(model="mimo-v2.5-tts-voiceclone").load()


def test_payload_uses_assistant_message_and_pcm16_stream() -> None:
    backend = _backend()
    request = TTSSynthesisRequest("你好，世界", voice="茉莉")
    payload = backend._payload(request, stream=True)
    assert payload["model"] == "mimo-v2.5-tts"
    assert payload["messages"] == [{"role": "assistant", "content": "你好，世界"}]
    assert payload["audio"] == {"format": "pcm16", "voice": "茉莉"}
    assert payload["stream"] is True
    assert backend._payload(request, stream=False)["audio"]["format"] == "wav"


def test_buffered_synthesize_decodes_message_audio(monkeypatch) -> None:
    observed: dict = {}
    body = {
        "choices": [
            {"message": {"role": "assistant", "audio": {"data": base64.b64encode(_wav_bytes()).decode()}}}
        ]
    }

    def fake_post(url, **kwargs):
        observed.update(url=url, **kwargs)
        return httpx.Response(
            200, json=body, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    chunk = _backend().synthesize(TTSSynthesisRequest("Hello"))

    assert observed["url"] == "https://api.xiaomimimo.com/v1/chat/completions"
    assert observed["headers"]["Authorization"] == "Bearer secret"
    assert chunk.sample_rate == 24000
    assert chunk.audio.dtype == np.float32
    assert chunk.text == "Hello"


def test_buffered_synthesize_missing_audio_raises(monkeypatch) -> None:
    def fake_post(url, **kwargs):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": ""}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(TTSBackendError, match="audio.data"):
        _backend().synthesize(TTSSynthesisRequest("Hello"))


def test_stream_yields_pcm_chunks_and_skips_empty_choices(monkeypatch) -> None:
    samples = np.arange(-2000, 2000, dtype="<i2")
    pcm = samples.tobytes()
    lines = _sse_lines([
        {"choices": []},
        {"choices": [{"delta": {"role": "assistant"}}]},
        {"choices": [{"delta": {"audio": {"data": base64.b64encode(pcm[:2000]).decode()}}}]},
        {"choices": [{"delta": {"audio": {"data": base64.b64encode(pcm[2000:]).decode()}}}]},
    ])
    observed: dict = {}
    monkeypatch.setattr(httpx, "stream", _fake_stream(lines, observed))

    chunks = list(_backend().synthesize_stream(TTSSynthesisRequest("Hello")))

    assert observed["headers"]["Accept"] == "text/event-stream"
    assert observed["json"]["stream"] is True
    assert observed["json"]["audio"]["format"] == "pcm16"
    assert observed["closed"] is True
    assert all(chunk.sample_rate == 24000 for chunk in chunks)
    assert chunks[0].text == "Hello"
    assert all(chunk.text == "" for chunk in chunks[1:])
    restored = np.concatenate([chunk.audio for chunk in chunks])
    np.testing.assert_allclose(restored, samples.astype(np.float32) / 32768.0)


def test_stream_malformed_json_raises(monkeypatch) -> None:
    observed: dict = {}
    monkeypatch.setattr(httpx, "stream", _fake_stream(["data: {oops", ""], observed))
    with pytest.raises(TTSBackendError, match="malformed SSE JSON"):
        list(_backend().synthesize_stream(TTSSynthesisRequest("Hello")))


def test_stream_without_audio_raises(monkeypatch) -> None:
    observed: dict = {}
    lines = _sse_lines([{"choices": [{"delta": {"role": "assistant"}}]}])
    monkeypatch.setattr(httpx, "stream", _fake_stream(lines, observed))
    with pytest.raises(TTSBackendError, match="without audio"):
        list(_backend().synthesize_stream(TTSSynthesisRequest("Hello")))


def test_stream_surfaces_provider_errors(monkeypatch) -> None:
    observed: dict = {}
    lines = _sse_lines([{"error": {"message": "quota exceeded"}}])
    monkeypatch.setattr(httpx, "stream", _fake_stream(lines, observed))
    with pytest.raises(TTSBackendError, match="quota exceeded"):
        list(_backend().synthesize_stream(TTSSynthesisRequest("Hello")))


def test_stream_rejects_an_incomplete_pcm_sample(monkeypatch) -> None:
    observed: dict = {}
    odd_pcm = base64.b64encode(b"\x01").decode()
    lines = _sse_lines([{"choices": [{"delta": {"audio": {"data": odd_pcm}}}]}])
    monkeypatch.setattr(httpx, "stream", _fake_stream(lines, observed))
    with pytest.raises(TTSBackendError, match="incomplete PCM16"):
        list(_backend().synthesize_stream(TTSSynthesisRequest("Hello")))
