from __future__ import annotations

import sys
from types import ModuleType

import numpy as np

from tts.backend import TTSSynthesisRequest
from tts.backends import gpt_sovits as backend_module
from tts.backends.gpt_sovits import GPTSoVITSBackend


def test_embedded_backend_remains_the_default(monkeypatch):
    observed = {}

    class FakeInferencer:
        def __init__(self, **kwargs):
            observed["init"] = kwargs

        def infer(self, **kwargs):
            observed["infer"] = kwargs
            return 22050, np.array([0.5], dtype=np.float32)

    fake_module = ModuleType("local_tts_infer")
    fake_module.TTSInferencer = FakeInferencer
    monkeypatch.setitem(sys.modules, "local_tts_infer", fake_module)
    monkeypatch.setattr(GPTSoVITSBackend, "_sidecar_enabled", staticmethod(lambda: False))

    backend = GPTSoVITSBackend()
    chunk = backend.synthesize(
        TTSSynthesisRequest(
            text="hello",
            language="ja",
            reference_audio="reference.wav",
            reference_text="reference",
            options={"top_k": 7},
        )
    )

    assert chunk.sample_rate == 22050
    np.testing.assert_array_equal(chunk.audio, np.array([0.5], dtype=np.float32))
    assert observed["infer"]["text"] == "hello"
    assert observed["infer"]["ref_audio_path"] == "reference.wav"
    assert observed["infer"]["top_k"] == 7


def _write_fake_sidecar(path):
    path.write_text(
        r'''
import base64
import json
import struct
import sys


def emit(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


def chunk(request_id, values, text):
    emit({
        "type": "chunk",
        "request_id": request_id,
        "sample_rate": 24000,
        "audio_b64": base64.b64encode(struct.pack("<" + "f" * len(values), *values)).decode("ascii"),
        "text": text,
    })


emit({
    "type": "ready",
    "device": "cuda:0",
    "torch": "test+rocm",
    "hip": "7.test",
    "cuda_available": True,
})
for line in sys.stdin:
    message = json.loads(line)
    request_id = message["request_id"]
    request = message["request"]
    sys.stderr.write("handled " + request_id + "\n")
    sys.stderr.flush()
    if message["type"] == "infer_stream":
        chunk(request_id, [0.1, 0.2], "first")
        chunk(request_id, [0.3], "second")
    else:
        observed = "|".join([
            request["reference_audio"],
            request["reference_text"],
            request["reference_language"],
            request["voice"],
            str(request["speed"]),
            request["options"]["marker"],
        ])
        chunk(request_id, [0.25, -0.5], observed)
    emit({"type": "done", "request_id": request_id})
'''.lstrip(),
        encoding="utf-8",
    )


def test_sidecar_round_trip_preserves_request_and_float32_audio(tmp_path, monkeypatch):
    script = tmp_path / "fake_tts_sidecar.py"
    _write_fake_sidecar(script)
    monkeypatch.setattr(backend_module, "_SIDECAR_SCRIPT", script)
    monkeypatch.setenv("TTS_MODE", "sidecar")
    monkeypatch.setenv("TTS_PYTHON", sys.executable)

    backend = GPTSoVITSBackend()
    try:
        chunk = backend.synthesize(
            TTSSynthesisRequest(
                text="hello",
                language="ja",
                voice="kurisu",
                speed=1.15,
                reference_audio="voice.wav",
                reference_text="prompt",
                reference_language="ja",
                options={"marker": "kept", "top_k": 11},
            )
        )
        assert backend._ready_info == {
            "type": "ready",
            "device": "cuda:0",
            "torch": "test+rocm",
            "hip": "7.test",
            "cuda_available": True,
        }
        assert backend.deployment == "subprocess"
        assert chunk.sample_rate == 24000
        assert chunk.audio.dtype == np.float32
        np.testing.assert_allclose(chunk.audio, [0.25, -0.5])
        assert chunk.text == "voice.wav|prompt|ja|kurisu|1.15|kept"
    finally:
        backend.close()
    assert backend._proc is None


def test_closing_stream_drains_to_done_before_the_next_request(tmp_path, monkeypatch):
    script = tmp_path / "fake_tts_sidecar.py"
    _write_fake_sidecar(script)
    monkeypatch.setattr(backend_module, "_SIDECAR_SCRIPT", script)
    monkeypatch.setenv("TTS_MODE", "sidecar")
    monkeypatch.setenv("TTS_PYTHON", sys.executable)

    backend = GPTSoVITSBackend()
    stream_request = TTSSynthesisRequest(text="stream", options={"marker": "stream"})
    stream = backend.synthesize_stream(stream_request)
    first = next(stream)
    assert first.text == "first"
    np.testing.assert_allclose(first.audio, [0.1, 0.2])
    stream.close()

    try:
        following = backend.synthesize(
            TTSSynthesisRequest(text="next", options={"marker": "next"})
        )
        np.testing.assert_allclose(following.audio, [0.25, -0.5])
        assert following.text.endswith("|next")
    finally:
        backend.close()


def test_sidecar_request_kwargs_match_embedded_rules():
    from tts import gpt_sovits_sidecar as sidecar

    payload = {
        "language": "ja",
        "speed": 0.9,
        "reference_audio": "ref.wav",
        "reference_text": "prompt",
        "reference_language": "en",
        "chunk_size_seconds": 0.25,
        "options": {
            "text_language": "日文",
            "prompt_language": "英文",
            "speed": 9.0,
            "top_k": 13,
            "collect_t2s_stats": True,
        },
    }
    regular = sidecar._request_kwargs(payload, streaming=False)
    streaming = sidecar._request_kwargs(payload, streaming=True)
    assert regular == {
        "ref_audio_path": "ref.wav",
        "prompt_text": "prompt",
        "text_language": "日文",
        "prompt_language": "英文",
        "speed": 0.9,
        "top_k": 13,
    }
    assert streaming["collect_t2s_stats"] is True
    assert streaming["chunk_size_seconds"] == 0.25


def test_sidecar_skips_stream_metadata_and_rejects_non_finite_audio(monkeypatch):
    from tts import gpt_sovits_sidecar as sidecar

    emitted = []
    monkeypatch.setattr(sidecar, "_emit", emitted.append)

    sidecar._emit_chunk("request", 24000, None)
    assert emitted == []

    try:
        sidecar._emit_chunk("request", 24000, np.asarray([np.nan], dtype=np.float32))
        assert False, "non-finite audio must not cross the sidecar protocol"
    except ValueError as exc:
        assert "non-finite audio" in str(exc)
