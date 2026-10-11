from __future__ import annotations

import base64
import importlib
import io
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk/extension-python/src"))
sys.path.insert(0, str(ROOT / "examples/extensions/mimo-tts/src"))
sys.path.insert(0, str(ROOT / "examples/extensions/notes-mcp/src"))

from amadeus_extension import CancelledError, RequestContext, TTSRequest
import amadeus_example_mimo as mimo


def context():
    return RequestContext(config={"voice": "Mia"}, secrets={"api_key": "synthetic-key"})


def event(pcm):
    return ("data: " + json.dumps({"choices": [{"delta": {"audio": {
        "data": base64.b64encode(pcm).decode("ascii")}}}]}) + "\n\n").encode()


def test_mimo_preserves_single_request_stream_audio_and_treats_speed_as_hint(monkeypatch):
    pcm = b"\x00\x01" * 2400
    response = io.BytesIO(event(pcm[:3001]) + event(pcm[3001:]) + b"data: [DONE]\n\n")
    calls = []

    def open_response(request, timeout):
        calls.append((request, timeout))
        return response

    monkeypatch.setattr(mimo, "urlopen", open_response)
    chunks = list(mimo.synthesize(TTSRequest("u1", "Hello.", "en", speed=1.067), context()))
    assert len(calls) == 1
    assert b"".join(chunk.pcm_s16le for chunk in chunks) == pcm
    assert {chunk.sample_rate for chunk in chunks} == {24000}
    payload = json.loads(calls[0][0].data)
    assert payload["messages"] == [{"role": "assistant", "content": "Hello."}]
    assert payload["stream"] is True and "speed" not in payload and "language" not in payload
    assert response.closed


def test_mimo_rejects_unverified_language_before_any_remote_request(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("unsupported language must not call the service")
    monkeypatch.setattr(mimo, "urlopen", unexpected)
    with pytest.raises(ValueError, match="English"):
        list(mimo.synthesize(TTSRequest("u1", "こんにちは。", "ja"), context()))


@pytest.mark.parametrize("body", [b"data: not-json\n\n", event(b"x"),
                                  b'data: {"error":"synthetic-key"}\n\n',
                                  b"data: [DONE]\n\n"])
def test_mimo_stream_errors_never_retry_or_echo_service_error_secrets(monkeypatch, body):
    calls = []
    def opened(*args, **kwargs):
        calls.append(True)
        return io.BytesIO(body)
    monkeypatch.setattr(mimo, "urlopen", opened)
    with pytest.raises(ValueError) as failure:
        list(mimo.synthesize(TTSRequest("u1", "Hello.", "en"), context()))
    assert calls == [True]
    assert "synthetic-key" not in str(failure.value)


def test_mimo_cancel_closes_current_response_and_never_replays_audio(monkeypatch):
    response = io.BytesIO(event(b"\0\0" * 4000) + b"data: [DONE]\n\n")
    monkeypatch.setattr(mimo, "urlopen", lambda *a, **k: response)
    ctx = context()
    stream = mimo.synthesize(TTSRequest("u1", "Hello.", "en"), ctx)
    assert next(stream).pcm_s16le
    ctx._cancel()
    assert response.closed
    with pytest.raises(CancelledError):
        next(stream)


def test_notes_protocol_uses_package_data_and_rejects_path_escape(tmp_path):
    notes = importlib.import_module("amadeus_example_notes.__main__")
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "save_note", "arguments": {"name": "review", "text": "Synthetic note"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "read_note", "arguments": {"name": "review"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "save_note", "arguments": {"name": "../outside", "text": "must not write"}}},
    ]
    output = io.StringIO()
    notes.serve(io.StringIO("\n".join(json.dumps(row) for row in requests)), output, tmp_path / "notes")
    replies = {item["id"]: item for item in map(json.loads, output.getvalue().splitlines())}
    assert replies[1]["result"]["capabilities"]["tools"]
    assert {tool["name"] for tool in replies[2]["result"]["tools"]} == {"list_notes", "read_note", "save_note"}
    assert replies[3]["result"]["isError"] is False
    assert replies[4]["result"]["content"][0]["text"] == "Synthetic note"
    assert replies[5]["result"]["isError"] is True
    assert not (tmp_path / "outside.txt").exists()
