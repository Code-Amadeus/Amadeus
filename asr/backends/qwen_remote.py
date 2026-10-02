"""Native DashScope Qwen remote ASR backend."""

from __future__ import annotations

import io
import mimetypes
import os
import tempfile
import wave
from datetime import datetime
from time import mktime
from typing import Any, Optional
from wsgiref.handlers import format_date_time

import numpy as np
import requests

from asr.backend import ASRBackendError, BaseASRBackend


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


def _extract_text(payload: dict[str, Any]) -> Optional[str]:
    output = payload.get("output")
    if not isinstance(output, dict):
        return None
    choices = output.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, dict):
        return None
    message = choice.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        value = content.strip()
        return value or None
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            value = str(item.get("text") or "")
            if value:
                parts.append(value)
    value = "".join(parts).strip()
    return value or None


class QwenRemoteASRBackend(BaseASRBackend):
    """DashScope native multimodal API transcription for Qwen3-ASR."""

    backend_id = "qwen_remote"
    deployment = "remote"
    supports_speculative_transcription = False

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        workspace: str | None = None,
        timeout_seconds: float | None = None,
        trust_env: bool | None = None,
    ) -> None:
        from config import settings

        self._base_url = str(
            base_url or settings.QWEN_REMOTE_ASR_BASE_URL or "",
        ).strip().rstrip("/")
        configured_key = (
            settings.QWEN_REMOTE_ASR_API_KEY
            or settings.DASHSCOPE_API_KEY
        )
        self._api_key = str(
            api_key if api_key is not None else configured_key,
        ).strip()
        self._model = str(
            model or settings.QWEN_REMOTE_ASR_MODEL or "",
        ).strip()
        self._workspace = str(
            workspace if workspace is not None else settings.QWEN_REMOTE_ASR_WORKSPACE,
        ).strip()
        self._trust_env = bool(
            settings.QWEN_REMOTE_ASR_TRUST_ENV
            if trust_env is None
            else trust_env
        )
        self._timeout = max(
            1.0,
            float(
                timeout_seconds
                if timeout_seconds is not None
                else settings.QWEN_REMOTE_ASR_TIMEOUT_SECONDS
            ),
        )

    def load(self, device: str) -> None:
        del device
        if not self._base_url:
            raise ASRBackendError("QWEN_REMOTE_ASR_BASE_URL is required")
        if not self._api_key:
            raise ASRBackendError(
                "QWEN_REMOTE_ASR_API_KEY or DASHSCOPE_API_KEY is required",
            )
        if not self._model:
            raise ASRBackendError("QWEN_REMOTE_ASR_MODEL is required")

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        context: str = "",
    ) -> Optional[str]:
        if audio is None or np.asarray(audio).size == 0:
            return None
        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp:
                temp_path = temp.name
                temp.write(_wav_payload(audio, sample_rate))
            with requests.Session() as session:
                session.trust_env = self._trust_env
                file_url = self._upload_audio(session, temp_path)
                payload = self._call_generation(session, file_url, context)
            return _extract_text(payload)
        except ASRBackendError:
            raise
        except requests.RequestException as exc:
            raise ASRBackendError(f"Qwen remote ASR request failed: {exc}") from exc
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _headers(self, *, content_type: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if self._workspace:
            headers["X-DashScope-WorkSpace"] = self._workspace
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def _upload_audio(self, session: requests.Session, file_path: str) -> str:
        certificate = session.get(
            f"{self._base_url}/uploads",
            params={"action": "getPolicy", "model": self._model},
            headers=self._headers(),
            timeout=self._timeout,
        )
        if not certificate.ok:
            raise ASRBackendError(
                f"Qwen ASR upload policy failed: HTTP {certificate.status_code}",
            )
        try:
            root = certificate.json()
        except ValueError as exc:
            raise ASRBackendError("Qwen ASR upload policy was not JSON") from exc
        info = root.get("output") if isinstance(root, dict) else None
        if not isinstance(info, dict):
            info = root.get("data") if isinstance(root, dict) else None
        if not isinstance(info, dict):
            raise ASRBackendError("Qwen ASR upload policy response is incomplete")

        file_name = os.path.basename(file_path)
        form = {
            "OSSAccessKeyId": info["oss_access_key_id"],
            "Signature": info["signature"],
            "policy": info["policy"],
            "key": f"{info['upload_dir']}/{file_name}",
            "x-oss-object-acl": info["x_oss_object_acl"],
            "x-oss-forbid-overwrite": info["x_oss_forbid_overwrite"],
            "success_action_status": "200",
            "x-oss-content-type": (
                mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            ),
        }
        upload_headers = {
            "user-agent": "Amadeus-Qwen-Remote-ASR/1",
            "Accept": "application/json",
            "Date": format_date_time(mktime(datetime.now().timetuple())),
        }
        with open(file_path, "rb") as source:
            uploaded = session.post(
                info["upload_host"],
                files={"file": (file_name, source, "audio/wav")},
                data=form,
                headers=upload_headers,
                timeout=self._timeout,
            )
        if not uploaded.ok:
            raise ASRBackendError(
                f"Qwen ASR audio upload failed: HTTP {uploaded.status_code}",
            )
        return f"oss://{form['key']}"

    def _call_generation(
        self,
        session: requests.Session,
        file_url: str,
        context: str,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if str(context or "").strip():
            messages.append(
                {
                    "role": "system",
                    "content": [{"text": str(context).strip()}],
                },
            )
        messages.append(
            {
                "role": "user",
                "content": [{"audio": file_url}],
            },
        )
        response = session.post(
            f"{self._base_url}/services/aigc/multimodal-generation/generation",
            headers={
                **self._headers(content_type=True),
                "X-DashScope-OssResourceResolve": "enable",
            },
            json={
                "model": self._model,
                "input": {"messages": messages},
                "parameters": {"result_format": "message"},
            },
            timeout=self._timeout,
        )
        if not response.ok:
            raise ASRBackendError(
                f"Qwen remote ASR failed: HTTP {response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ASRBackendError("Qwen remote ASR returned non-JSON data") from exc
        if not isinstance(payload, dict):
            raise ASRBackendError("Qwen remote ASR returned an invalid response")
        return payload
