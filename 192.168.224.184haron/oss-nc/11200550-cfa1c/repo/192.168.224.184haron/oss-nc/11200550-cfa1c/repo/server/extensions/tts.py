"""Stable public TTS backend identity; Runner recovery remains inside the proxy."""
from __future__ import annotations

from typing import Iterator

import numpy as np

from server.extensions.runner import ExtensionUnavailable, RunnerClient
from tts.backend import BaseTTSBackend, TTSAudioChunk, TTSBackendError, TTSSynthesisRequest


class ExtensionTTSBackend(BaseTTSBackend):
    def __init__(self, package_id: str, contribution: dict, client: RunnerClient | None) -> None:
        self.backend_id = f"{package_id}/{contribution['id']}"
        self.contribution_id = contribution["id"]
        self.deployment = contribution["deployment"]
        self.supports_streaming = contribution["streaming"]
        self.languages = tuple(contribution["languages"])
        self.client = client

    def load(self) -> None:
        # Registration and selected backend construction never wait for plugin ready.
        if self.client:
            self.client.start()

    def synthesize_stream(self, request: TTSSynthesisRequest) -> Iterator[TTSAudioChunk]:
        if self.client is None:
            raise TTSBackendError("extension voice is inactive or needs configuration")
        language = request.language.lower().split('-')[0]
        if not any(value.lower().split('-')[0] == language for value in self.languages):
            raise TTSBackendError("extension voice does not declare this output language")
        if not 1 <= len(request.text) <= 2000:
            raise TTSBackendError("extension voice requires text of 1 to 2000 characters")
        # The native pipeline's first-utterance pacing belongs to GPT-SoVITS.
        # Extension authors may expose their own explicit voice-speed setting.
        stream = self.client.stream(self.contribution_id, text=request.text, language=request.language, speed=1.0)
        try:
            for index, (sample_rate, pcm) in enumerate(stream):
                audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                yield TTSAudioChunk(sample_rate, audio, request.text if index == 0 else "")
        except ExtensionUnavailable as exc:
            raise TTSBackendError(str(exc)) from None
        finally:
            stream.close()

    def synthesize(self, request: TTSSynthesisRequest) -> TTSAudioChunk:
        chunks = list(self.synthesize_stream(request))
        if not chunks:
            raise TTSBackendError("extension voice produced no audio")
        return TTSAudioChunk(chunks[0].sample_rate, np.concatenate([chunk.audio for chunk in chunks]), request.text)

    def close(self) -> None:
        # The package owns its process (possibly shared by multiple contributions).
        # ExtensionRuntime closes that process at backend shutdown.
        pass

    def cancel_pending(self) -> None:
        if self.client:
            self.client.cancel_inflight(contribution_id=self.contribution_id)
