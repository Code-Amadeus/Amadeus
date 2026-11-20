"""OpenAI-compatible remote conversation transcription backend."""

from __future__ import annotations

import io
import wave
from typing import Optional

import httpx
import numpy as np

from asr.backend import ASRBackendError, BaseASRBackend


_MAX_TRANSCRIPTION_RESPONSE_BYTES = 2 * 1024 * 1024


def _transcription_endpoint(base_url: str) -> str:
    value = str(base_url or "").strip().rstrip("/")
    if value.endswith("/audio/transcriptions"):
        return value
    return f"{value}/audio/transcriptions"


def _wav_payload(audio: np.ndarray, sample_rate: int) -> bytes:
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(max(1, int(sample_rate)))
        wav.writeframes(pcm.tobytes())
    return output.getvalue()


class OpenAICompatibleASRBackend(BaseASRBackend):
    backend_id = "openai_compatible"
    deployment = "remote"
    # Partial endpoint speculation can duplicate metered network requests.
    supports_speculative_transcription = False

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        language: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        from config import settings

        self._base_url = str(base_url or settings.ASR_API_BASE_URL or "").strip()
        self._api_key = str(api_key if api_key is not None else settings.ASR_API_KEY).strip()
        self._model = str(model or settings.ASR_API_MODEL or "").strip()
        self._language = str(language or settings.ASR_LANGUAGE or "auto").strip().lower()
        self._timeout = max(
            1.0,
            float(timeout_seconds or settings.ASR_API_TIMEOUT_SECONDS or 45.0),
        )

    def load(self, device: str) -> None:
        del device
        if not self._base_url:
            raise ASRBackendError("ASR_API_BASE_URL is required")
        if not self._model:
            raise ASRBackendError("ASR_API_MODEL is required")

    def set_language(self, language: str) -> None:
        self._language = str(language or "auto").strip().lower()

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        context: str = "",
    ) -> Optional[str]:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        data: dict[str, str] = {
            "model": self._model,
            "response_format": "json",
        }
        if self._language and self._language != "auto":
            data["language"] = self._language
        if str(context or "").strip():
            data["prompt"] = str(context).strip()
        try:
            response = httpx.post(
                _transcription_endpoint(self._base_url),
                headers=headers,
                data=data,
                files={"file": ("speech.wav", _wav_payload(audio, sample_rate), "audio/wav")},
                timeout=self._timeout,
            )
            response.raise_for_status()
            if len(response.content) > _MAX_TRANSCRIPTION_RESPONSE_BYTES:
                raise ASRBackendError("remote ASR response exceeded 2 MiB")
            payload = response.json()
        except ASRBackendError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ASRBackendError(f"remote ASR request failed: {exc}") from exc
        text = str(payload.get("text") or "").strip() if isinstance(payload, dict) else ""
        return text or None
