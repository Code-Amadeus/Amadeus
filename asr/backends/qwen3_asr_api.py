"""Alibaba Cloud Model Studio Qwen3-ASR-Flash conversation backend."""

from __future__ import annotations

import base64
import io
import wave
from typing import Any, Optional

import httpx
import numpy as np

from asr.backend import ASRBackendError, BaseASRBackend


_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_GENERATION_SUFFIX = (
    "/services/aigc/multimodal-generation/generation"
)


def _generation_endpoint(base_url: str) -> str:
    value = str(base_url or "").strip().rstrip("/")

    if value.endswith(_GENERATION_SUFFIX):
        return value

    return f"{value}{_GENERATION_SUFFIX}"


def _wav_payload(
    audio: np.ndarray,
    sample_rate: int,
) -> bytes:
    samples = np.asarray(
        audio,
        dtype=np.float32,
    ).reshape(-1)

    pcm = (
        np.clip(samples, -1.0, 1.0)
        * 32767.0
    ).astype("<i2")

    output = io.BytesIO()

    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(
            max(1, int(sample_rate))
        )
        wav.writeframes(pcm.tobytes())

    return output.getvalue()


def _extract_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""

    output = payload.get("output")

    if not isinstance(output, dict):
        return ""

    choices = output.get("choices")

    if not isinstance(choices, list) or not choices:
        return ""

    first = choices[0]

    if not isinstance(first, dict):
        return ""

    message = first.get("message")

    if not isinstance(message, dict):
        return ""

    content = message.get("content")

    # Future/compatibility handling.
    if isinstance(content, str):
        return content.strip()

    if not isinstance(content, list):
        return ""

    parts = [
        str(item.get("text") or "")
        for item in content
        if (
            isinstance(item, dict)
            and item.get("text")
        )
    ]

    return "".join(parts).strip()


class Qwen3ASRAPIBackend(BaseASRBackend):
    backend_id = "qwen3_asr_api"
    deployment = "remote"

    # Very important:
    # do not send speculative metered API calls.
    supports_speculative_transcription = False

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        language: str | None = None,
        enable_itn: bool | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        from config import settings

        self._base_url = str(
            base_url
            or settings.QWEN3_ASR_API_BASE_URL
            or ""
        ).strip()

        self._api_key = str(
            api_key
            if api_key is not None
            else settings.QWEN3_ASR_API_KEY
        ).strip()

        self._model = str(
            model
            or settings.QWEN3_ASR_API_MODEL
            or "qwen3-asr-flash"
        ).strip()

        self._language = str(
            language
            or settings.ASR_LANGUAGE
            or "auto"
        ).strip().lower()

        self._enable_itn = (
            bool(settings.QWEN3_ASR_API_ENABLE_ITN)
            if enable_itn is None
            else bool(enable_itn)
        )

        self._timeout = max(
            1.0,
            float(
                timeout_seconds
                or settings.ASR_API_TIMEOUT_SECONDS
                or 45.0
            ),
        )

    def load(self, device: str) -> None:
        del device

        if not self._base_url:
            raise ASRBackendError(
                "QWEN3_ASR_API_BASE_URL is required"
            )

        if not self._api_key:
            raise ASRBackendError(
                "QWEN3_ASR_API_KEY is required"
            )

        if not self._model:
            raise ASRBackendError(
                "QWEN3_ASR_API_MODEL is required"
            )

    def set_language(
        self,
        language: str,
    ) -> None:
        self._language = str(
            language or "auto"
        ).strip().lower()

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        context: str = "",
    ) -> Optional[str]:

        # Amadeus PCM -> standard mono PCM16 WAV
        wav_data = _wav_payload(
            audio,
            sample_rate,
        )

        audio_data_url = (
            "data:audio/wav;base64,"
            + base64.b64encode(
                wav_data
            ).decode("ascii")
        )

        messages: list[dict[str, Any]] = []

        context_text = str(
            context or ""
        ).strip()

        if context_text:
            messages.append(
                {
                    "role": "system",
                    "content": [
                        {
                            "text":
                                context_text
                        }
                    ],
                }
            )

        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "audio":
                            audio_data_url
                    }
                ],
            }
        )

        asr_options: dict[str, Any] = {
            "enable_itn":
                self._enable_itn,
        }

        # auto means:
        # do not send language to Qwen.
        if (
            self._language
            and self._language != "auto"
        ):
            asr_options["language"] = (
                self._language
            )

        request_payload = {
            "model": self._model,
            "input": {
                "messages": messages,
            },
            "parameters": {
                "asr_options":
                    asr_options,
            },
        }

        try:
            response = httpx.post(
                _generation_endpoint(
                    self._base_url
                ),
                headers={
                    "Authorization":
                        f"Bearer {self._api_key}",
                    "Content-Type":
                        "application/json",
                },
                json=request_payload,
                timeout=self._timeout,
            )

            response.raise_for_status()

            if (
                len(response.content)
                > _MAX_RESPONSE_BYTES
            ):
                raise ASRBackendError(
                    "Qwen3 ASR response exceeded 2 MiB"
                )

            payload = response.json()

        except ASRBackendError:
            raise

        except (
            httpx.HTTPError,
            ValueError,
        ) as exc:
            raise ASRBackendError(
                "Qwen3 ASR API request failed: "
                f"{exc}"
            ) from exc

        text = _extract_text(payload)

        if text:
            return text

        # DashScope may return application-level
        # error information as JSON.
        if isinstance(payload, dict):
            code = str(
                payload.get("code") or ""
            ).strip()

            message = str(
                payload.get("message") or ""
            ).strip()

            if code or message:
                detail = ": ".join(
                    part
                    for part in (
                        code,
                        message,
                    )
                    if part
                )

                raise ASRBackendError(
                    "Qwen3 ASR API returned "
                    f"an error: {detail}"
                )

        return None