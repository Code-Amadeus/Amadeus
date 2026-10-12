"""MiMo via the public Runner API, with no imports from the Amadeus Host.

The API shape follows the existing built-in backend's tested PCM/SSE behavior.
No claim of Japanese support is made. Speed is a hint and is not transmitted:
Amadeus's current value is an internal GPT-SoVITS pacing heuristic.
"""
from __future__ import annotations

import base64
import binascii
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from amadeus_extension import AudioChunk, Extension, RequestContext, TTSRequest


extension = Extension()
_ENDPOINT = "https://api.xiaomimimo.com/v1/chat/completions"
_MAX_AUDIO_BYTES = 64 * 1024 * 1024
_MAX_EVENT_BYTES = 2 * 1024 * 1024
_CHUNK_BYTES = 24000 * 2 * 80 // 1000


def _events(response):
    lines = []
    size = 0
    while True:
        raw = response.readline(_MAX_EVENT_BYTES + 1)
        if not raw:
            if lines:
                yield "\n".join(lines)
            return
        size += len(raw)
        if size > _MAX_EVENT_BYTES:
            raise ValueError("MiMo event exceeded its size limit")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if lines:
                yield "\n".join(lines)
            lines, size = [], 0
        elif line.startswith("data:"):
            lines.append(line[5:].lstrip())


@extension.initialize
def initialize(context: RequestContext):
    if not context.secrets.get("api_key") or not context.config.get("voice"):
        raise ValueError("MiMo requires an API key and voice")


@extension.tts("mimo")
def synthesize(request: TTSRequest, context: RequestContext):
    if request.language != "en":
        raise ValueError("This experimental MiMo package supports English output only")
    context.check_cancelled()
    payload = {
        "model": "mimo-v2.5-tts",
        "messages": [{"role": "assistant", "content": request.text}],
        "audio": {"format": "pcm16", "voice": context.config["voice"]},
        "stream": True,
    }
    http_request = Request(_ENDPOINT, data=json.dumps(payload).encode("utf-8"), method="POST",
                           headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                                    "Authorization": "Bearer " + context.secrets["api_key"]})
    timeout = min(60.0, max(0.1, (request.deadline_ms or 60000) / 1000))
    pending = bytearray()
    received = 0
    emitted = False
    try:
        # One request only: partial failure must not duplicate a paid POST.
        with urlopen(http_request, timeout=timeout) as response:
            context.on_cancel(response.close)
            for raw in _events(response):
                context.check_cancelled()
                if raw == "[DONE]":
                    break
                event = json.loads(raw)
                if not isinstance(event, dict) or event.get("error"):
                    raise ValueError("MiMo returned a service error")
                choices = event.get("choices") or []
                if not choices or not isinstance(choices[0], dict):
                    continue
                delta = choices[0].get("delta") or {}
                audio = delta.get("audio") if isinstance(delta, dict) else None
                if not isinstance(audio, dict):
                    continue
                encoded = audio.get("data")
                if not isinstance(encoded, str) or not encoded:
                    raise ValueError("MiMo returned invalid audio")
                decoded = base64.b64decode(encoded, validate=True)
                received += len(decoded)
                if received > _MAX_AUDIO_BYTES:
                    raise ValueError("MiMo audio exceeded its size limit")
                pending.extend(decoded)
                while len(pending) >= _CHUNK_BYTES:
                    context.check_cancelled()
                    chunk = bytes(pending[:_CHUNK_BYTES])
                    del pending[:_CHUNK_BYTES]
                    emitted = True
                    yield AudioChunk(24000, chunk)
    except (HTTPError, URLError, OSError, UnicodeError, json.JSONDecodeError, binascii.Error):
        context.check_cancelled()
        raise ValueError("MiMo speech request failed; check service availability and credentials") from None
    if len(pending) % 2:
        raise ValueError("MiMo returned an incomplete PCM sample")
    if pending:
        context.check_cancelled()
        yield AudioChunk(24000, bytes(pending))
        emitted = True
    if not emitted:
        raise ValueError("MiMo returned no audio")
