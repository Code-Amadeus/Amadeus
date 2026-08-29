from __future__ import annotations

import base64
import io
import builtins
import json
import wave
from pathlib import Path
from unittest.mock import patch

import httpx
import numpy as np
import pytest

from asr.backends.openai_compatible import OpenAICompatibleASRBackend
from asr.backends.qwen3_asr import Qwen3ASRBackend
from asr.backend import BaseASRBackend
from asr.qwen_model import REQUIRED_MODEL_FILES, qwen_model_status, resolve_qwen_model_source
from asr.registry import (
    ASRBackendDescriptor,
    asr_backend_ids,
    asr_backend_statuses,
    create_asr_backend,
    register_asr_backend,
    unregister_asr_backend,
)
from tts.backend import (
    BaseTTSBackend,
    TTSAudioChunk,
    TTSSynthesisRequest,
    TTSBackendError,
    TTSRuntimeAdapter,
)
from tts.backends.openai_compatible import OpenAICompatibleTTSBackend
from tts.backends.gpt_sovits import GPTSoVITSBackend
from tts.registry import (
    create_tts_runtime,
    tts_backend_ids,
    tts_backend_statuses,
)


def _wav_bytes(*, sample_rate: int = 24000) -> bytes:
    output = io.BytesIO()
    samples = (np.asarray([0.0, 0.25, -0.25], dtype=np.float32) * 32767).astype("<i2")
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())
    return output.getvalue()


def test_builtin_voice_registries_keep_embedded_defaults_and_remote_sidepaths() -> None:
    assert asr_backend_ids()[:2] == ("qwen3_asr", "sense_voice")
    assert "openai_compatible" in asr_backend_ids()
    assert tts_backend_ids() == ("gpt_sovits", "openai_compatible", "mimo", "disabled")

    remote_asr = create_asr_backend("openai_compatible")
    assert remote_asr.deployment == "remote"
    assert remote_asr.supports_speculative_transcription is False
    assert any(item["deployment"] == "remote" for item in asr_backend_statuses())
    tts_statuses = tts_backend_statuses()
    assert any(item["state"] == "disabled" for item in tts_statuses)
    embedded_tts = next(item for item in tts_statuses if item["id"] == "gpt_sovits")
    assert "v3" in embedded_tts["label"]
    assert "only GPT-SoVITS v3 checkpoints" in embedded_tts["summary"]


def test_qwen_conversation_language_maps_iso_codes_and_auto_detection() -> None:
    backend = Qwen3ASRBackend()

    assert backend._language is None
    backend.set_language("ja")
    assert backend._language == "Japanese"
    backend.set_language("zh-CN")
    assert backend._language == "Chinese"
    backend.set_language("auto")
    assert backend._language is None


def test_qwen_model_prefers_the_canonical_asset_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config import settings

    model = tmp_path / "qwen"
    model.mkdir()
    for name in REQUIRED_MODEL_FILES:
        (model / name).write_bytes(b"fixture")
    monkeypatch.setattr(settings, "QWEN3_ASR_MODEL_PATH", str(model))

    ready, detail, path = qwen_model_status()

    assert ready is True
    assert "asset directory" in detail
    assert path == model
    assert resolve_qwen_model_source() == str(model)


def test_qwen_model_reports_an_explicit_incomplete_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config import settings

    missing = tmp_path / "missing"
    monkeypatch.setattr(settings, "QWEN3_ASR_MODEL_PATH", str(missing))

    ready, detail, path = qwen_model_status()

    assert ready is False
    assert str(missing) in detail
    assert path is None
    with pytest.raises(FileNotFoundError, match="incomplete"):
        resolve_qwen_model_source()


def test_public_asr_registry_accepts_and_removes_third_party_backends() -> None:
    class PluginASRBackend(BaseASRBackend):
        backend_id = "test_plugin"

        def load(self, device: str) -> None:
            del device

        def transcribe(self, audio, sample_rate: int = 16000, context: str = ""):
            del audio, sample_rate, context
            return "plugin result"

    register_asr_backend(
        ASRBackendDescriptor(
            "test_plugin",
            "Test Plugin",
            "plugin",
            PluginASRBackend,
            lambda: ("installed", "test adapter"),
        )
    )
    try:
        assert create_asr_backend("test_plugin").transcribe(np.zeros(1)) == "plugin result"
    finally:
        unregister_asr_backend("test_plugin")
    assert "test_plugin" not in asr_backend_ids()


def test_remote_asr_posts_wav_model_language_and_context(monkeypatch) -> None:
    observed: dict = {}

    def fake_post(url, **kwargs):
        observed.update(url=url, **kwargs)
        return httpx.Response(
            200,
            json={"text": "hello Amadeus"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    backend = OpenAICompatibleASRBackend(
        base_url="https://voice.example/v1",
        api_key="secret",
        model="transcribe-model",
        language="en",
    )
    backend.load("cpu")

    text = backend.transcribe(
        np.asarray([0.0, 0.2, -0.2], dtype=np.float32),
        16000,
        context="Amadeus, Kurisu",
    )

    assert text == "hello Amadeus"
    assert observed["url"] == "https://voice.example/v1/audio/transcriptions"
    assert observed["headers"]["Authorization"] == "Bearer secret"
    assert observed["data"] == {
        "model": "transcribe-model",
        "response_format": "json",
        "language": "en",
        "prompt": "Amadeus, Kurisu",
    }
    filename, wav_data, content_type = observed["files"]["file"]
    assert filename == "speech.wav"
    assert content_type == "audio/wav"
    assert wav_data[:4] == b"RIFF"


def test_remote_tts_normalizes_wav_into_pipeline_audio(monkeypatch) -> None:
    observed: dict = {}

    def fake_post(url, **kwargs):
        observed.update(url=url, **kwargs)
        return httpx.Response(
            200,
            content=_wav_bytes(),
            headers={"Content-Type": "audio/wav"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    backend = OpenAICompatibleTTSBackend(
        base_url="https://voice.example/v1",
        api_key="secret",
        model="speech-model",
        voice="kurisu-compatible",
    )

    chunk = backend.synthesize(TTSSynthesisRequest("Hello", language="en", speed=1.2))

    assert observed["url"] == "https://voice.example/v1/audio/speech"
    assert observed["json"] == {
        "model": "speech-model",
        "input": "Hello",
        "voice": "kurisu-compatible",
        "response_format": "wav",
        "speed": 1.2,
    }
    assert chunk.sample_rate == 24000
    assert chunk.audio.dtype == np.float32
    assert chunk.audio.shape == (3,)
    assert backend.supports_streaming is False


def test_remote_tts_openai_sse_yields_pcm_before_response_completion(monkeypatch) -> None:
    observed: dict = {}
    samples = np.arange(-2000, 2000, dtype="<i2")
    pcm = samples.tobytes()
    encoded_parts = [
        base64.b64encode(pcm[:1001]).decode("ascii"),
        base64.b64encode(pcm[1001:]).decode("ascii"),
    ]
    lines = []
    for encoded in encoded_parts:
        lines.extend(
            [
                "event: speech.audio.delta",
                f"data: {json.dumps({'type': 'speech.audio.delta', 'audio': encoded})}",
                "",
            ]
        )
    lines.extend(
        [
            "event: speech.audio.done",
            'data: {"type":"speech.audio.done","usage":{}}',
            "",
        ]
    )

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
            for line in lines:
                if "speech.audio.done" in line:
                    observed["saw_done"] = True
                yield line

    def fake_stream(method, url, **kwargs):
        observed.update(method=method, url=url, **kwargs)
        return FakeStreamingResponse()

    monkeypatch.setattr(httpx, "stream", fake_stream)
    backend = OpenAICompatibleTTSBackend(
        base_url="https://api.openai.com/v1",
        api_key="secret",
        model="gpt-4o-mini-tts",
        voice="alloy",
        stream_protocol="openai_sse",
    )

    stream = backend.synthesize_stream(TTSSynthesisRequest("Hello"))
    first_chunk = next(stream)

    assert backend.supports_streaming is True
    assert observed["method"] == "POST"
    assert observed["url"] == "https://api.openai.com/v1/audio/speech"
    assert observed["headers"]["Accept"] == "text/event-stream"
    assert observed["json"]["response_format"] == "pcm"
    assert observed["json"]["stream_format"] == "sse"
    assert observed.get("saw_done") is None
    chunks = [first_chunk, *stream]
    assert observed["closed"] is True
    assert observed["saw_done"] is True
    assert len(chunks) == 3
    assert all(chunk.sample_rate == 24000 for chunk in chunks)
    assert chunks[0].text == "Hello"
    assert chunks[1].text == ""
    restored = np.concatenate([chunk.audio for chunk in chunks])
    np.testing.assert_allclose(restored, samples.astype(np.float32) / 32768.0)


def test_remote_tts_status_only_claims_streaming_for_explicit_protocol() -> None:
    from config import settings

    with patch.object(settings, "TTS_API_STREAM_PROTOCOL", "buffered"):
        buffered = next(
            item for item in tts_backend_statuses() if item["id"] == "openai_compatible"
        )
    with patch.object(settings, "TTS_API_STREAM_PROTOCOL", "openai_sse"):
        streaming = next(
            item for item in tts_backend_statuses() if item["id"] == "openai_compatible"
        )

    assert buffered["supports_streaming"] is False
    assert "buffered WAV" in buffered["detail"]
    assert streaming["supports_streaming"] is True
    assert "SSE PCM streaming" in streaming["detail"]


def test_remote_tts_stream_error_does_not_retry_a_billable_buffered_request(
    monkeypatch,
) -> None:
    calls = {"stream": 0, "post": 0}

    class FailedStreamingResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def iter_lines():
            yield 'data: {"type":"error","message":"stream unavailable"}'
            yield ""

    def fake_stream(*_args, **_kwargs):
        calls["stream"] += 1
        return FailedStreamingResponse()

    def forbidden_post(*_args, **_kwargs):
        calls["post"] += 1
        raise AssertionError("stream failure must not trigger a second request")

    monkeypatch.setattr(httpx, "stream", fake_stream)
    monkeypatch.setattr(httpx, "post", forbidden_post)
    backend = OpenAICompatibleTTSBackend(
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini-tts",
        voice="alloy",
        stream_protocol="openai_sse",
    )

    try:
        list(backend.synthesize_stream(TTSSynthesisRequest("Hello")))
        assert False, "stream error should be observable"
    except TTSBackendError as exc:
        assert "stream unavailable" in str(exc)

    assert calls == {"stream": 1, "post": 0}


def test_remote_tts_runtime_does_not_import_embedded_model_stack(monkeypatch) -> None:
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "local_tts_infer":
            raise AssertionError("remote TTS must not import the embedded model stack")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    runtime = create_tts_runtime("openai_compatible")

    assert runtime is not None
    assert runtime.backend_id == "openai_compatible"


def test_runtime_adapter_preserves_existing_pipeline_tuple_contract() -> None:
    class FakeBackend(BaseTTSBackend):
        backend_id = "fake"
        deployment = "remote"
        supports_streaming = True

        def synthesize(self, request: TTSSynthesisRequest) -> TTSAudioChunk:
            return TTSAudioChunk(22050, np.ones(2, dtype=np.float32), request.text)

    adapter = TTSRuntimeAdapter(FakeBackend())

    sample_rate, audio = adapter.infer("hello", "unused.wav")
    streamed = list(adapter.infer_stream("hello", "unused.wav"))

    assert adapter.deployment == "remote"
    assert adapter.supports_streaming is True
    assert sample_rate == 22050
    assert audio.tolist() == [1.0, 1.0]
    assert streamed[0][0] == 22050
    assert streamed[0][2] == "hello"


def test_embedded_tts_adapter_preserves_local_inference_options() -> None:
    calls: list[tuple[str, dict]] = []

    class FakeInferencer:
        def infer(self, *, text, **kwargs):
            calls.append((text, kwargs))
            return 24000, np.ones(2, dtype=np.float32)

        def infer_stream(self, *, text, **kwargs):
            calls.append((text, kwargs))
            yield 24000, np.ones(2, dtype=np.float32), text

    backend = GPTSoVITSBackend()
    backend._inferencer = FakeInferencer()
    request = TTSSynthesisRequest(
        "こんにちは",
        language="ja",
        speed=1.1,
        reference_audio="reference.wav",
        reference_text="reference",
        reference_language="ja",
        chunk_size_seconds=0.35,
        options={
            "text_language": "日文",
            "prompt_language": "日文",
            "top_k": 5,
            "collect_t2s_stats": True,
        },
    )

    chunk = backend.synthesize(request)
    streamed = list(backend.synthesize_stream(request))

    assert chunk.sample_rate == 24000
    assert streamed[0].text == "こんにちは"
    assert calls[0][1]["top_k"] == 5
    assert calls[0][1]["text_language"] == "日文"
    assert "collect_t2s_stats" not in calls[0][1]
    assert calls[1][1]["chunk_size_seconds"] == 0.35
    assert calls[1][1]["collect_t2s_stats"] is True
