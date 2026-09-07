"""Adapter for Amadeus's embedded, v3-only GPT-SoVITS inference rewrite."""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

from tts.backend import (
    BaseTTSBackend,
    TTSAudioChunk,
    TTSSynthesisRequest,
    TTSBackendError,
)


logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SIDECAR_SCRIPT = _PROJECT_ROOT / "tts" / "gpt_sovits_sidecar.py"


class GPTSoVITSBackend(BaseTTSBackend):
    """Run GPT-SoVITS v3 checkpoints through the Amadeus low-latency pipeline."""

    backend_id = "gpt_sovits"
    deployment = "embedded"
    supports_streaming = True

    def __init__(self) -> None:
        self.deployment = "embedded"
        self._inferencer = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._io_lock = threading.Lock()
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail: deque[str] = deque(maxlen=50)
        self._ready_info: dict[str, Any] = {}

    @staticmethod
    def _sidecar_enabled() -> bool:
        # Direct backend users may not have imported config.settings yet.  Load
        # the project dotenv before looking for the two sidecar switches.
        from config.environment import load_project_environment

        load_project_environment(_PROJECT_ROOT)
        mode = os.environ.get("TTS_MODE", "").strip().lower()
        return mode in {"sidecar", "subprocess", "process"} or bool(
            os.environ.get("TTS_PYTHON", "").strip()
        )

    def load(self) -> None:
        if self._inferencer is not None or self._is_running():
            return
        if self._sidecar_enabled():
            self._load_sidecar()
            return
        self.deployment = "embedded"
        from config import settings
        from local_tts_infer import TTSInferencer

        self._inferencer = TTSInferencer(
            device=settings.TTS_DEVICE,
            gpt_path=settings.TTS_GPT_MODEL_PATH or None,
            sovits_path=settings.TTS_SOVITS_MODEL_PATH or None,
        )

    def _is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _drain_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        stream = proc.stderr
        if stream is None:
            return
        for raw_line in iter(stream.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            self._stderr_tail.append(line)
            logger.info("[TTS:GPT-SoVITS sidecar] %s", line)

    def _read_message(self, proc: subprocess.Popen[bytes]) -> dict[str, Any]:
        stream = proc.stdout
        if stream is None:
            raise TTSBackendError("GPT-SoVITS sidecar stdout is unavailable")
        raw_line = stream.readline()
        if not raw_line:
            detail = "\n".join(self._stderr_tail)
            suffix = f"\n{detail}" if detail else ""
            raise TTSBackendError(
                f"GPT-SoVITS sidecar exited unexpectedly (code={proc.poll()}){suffix}"
            )
        try:
            return json.loads(raw_line.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TTSBackendError(
                "GPT-SoVITS sidecar emitted invalid JSON on stdout"
            ) from exc

    def _load_sidecar(self) -> None:
        python = os.environ.get("TTS_PYTHON", "").strip() or sys.executable
        if not Path(python).is_file():
            raise FileNotFoundError(f"GPT-SoVITS sidecar Python not found: {python}")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        proc = subprocess.Popen(
            [python, str(_SIDECAR_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            cwd=str(_PROJECT_ROOT),
            creationflags=creationflags,
        )
        self._proc = proc
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(proc,),
            name="gpt-sovits-sidecar-stderr",
            daemon=True,
        )
        self._stderr_thread.start()
        try:
            message = self._read_message(proc)
            if message.get("type") != "ready":
                detail = message.get("msg") or message
                raise TTSBackendError(f"GPT-SoVITS sidecar failed to load: {detail}")
            self._ready_info = dict(message)
            self.deployment = "subprocess"
            logger.info(
                "[TTS:GPT-SoVITS] sidecar ready "
                "(device=%s, torch=%s, hip=%s, cuda_available=%s)",
                message.get("device", "?"),
                message.get("torch", "?"),
                message.get("hip"),
                message.get("cuda_available", False),
            )
        except Exception:
            self._stop_sidecar(proc)
            raise

    def _ready(self):
        self.load()
        if self._inferencer is None:
            raise RuntimeError("GPT-SoVITS inferencer is unavailable")
        return self._inferencer

    @staticmethod
    def _serialize_request(request: TTSSynthesisRequest) -> dict[str, Any]:
        return {
            "text": request.text,
            "language": request.language,
            "voice": request.voice,
            "speed": request.speed,
            "reference_audio": request.reference_audio,
            "reference_text": request.reference_text,
            "reference_language": request.reference_language,
            "chunk_size_seconds": request.chunk_size_seconds,
            "options": dict(request.options),
        }

    @staticmethod
    def _decode_chunk(message: dict[str, Any]) -> TTSAudioChunk:
        try:
            raw = base64.b64decode(message["audio_b64"], validate=True)
            if len(raw) % np.dtype("<f4").itemsize:
                raise ValueError("float32 payload has an invalid byte length")
            audio = np.frombuffer(raw, dtype="<f4").astype(np.float32, copy=True)
            return TTSAudioChunk(
                int(message["sample_rate"]),
                audio,
                str(message.get("text") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TTSBackendError("invalid audio chunk from GPT-SoVITS sidecar") from exc

    def _send_request_locked(
        self,
        operation: str,
        request: TTSSynthesisRequest,
    ) -> tuple[subprocess.Popen[bytes], str]:
        self.load()
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise TTSBackendError("GPT-SoVITS sidecar is not running")
        request_id = uuid.uuid4().hex
        try:
            payload = json.dumps(
                {
                    "type": operation,
                    "request_id": request_id,
                    "request": self._serialize_request(request),
                },
                ensure_ascii=True,
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise TTSBackendError("GPT-SoVITS request options are not JSON serializable") from exc
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise TTSBackendError("failed to write to GPT-SoVITS sidecar") from exc
        return proc, request_id

    def _messages_for_request_locked(
        self,
        proc: subprocess.Popen[bytes],
        request_id: str,
    ):
        while True:
            message = self._read_message(proc)
            if str(message.get("request_id") or "") != request_id:
                raise TTSBackendError("GPT-SoVITS sidecar returned a mismatched request_id")
            kind = message.get("type")
            if kind == "chunk":
                yield self._decode_chunk(message)
                continue
            if kind == "done":
                return
            if kind == "error":
                raise TTSBackendError(
                    f"GPT-SoVITS sidecar inference failed: {message.get('msg', 'unknown error')}"
                )
            raise TTSBackendError(f"unexpected GPT-SoVITS sidecar message: {kind!r}")

    def _synthesize_sidecar_stream(self, request: TTSSynthesisRequest, *, streaming: bool):
        with self._io_lock:
            proc, request_id = self._send_request_locked(
                "infer_stream" if streaming else "infer", request
            )
            messages = self._messages_for_request_locked(proc, request_id)
            completed = False
            try:
                for chunk in messages:
                    yield chunk
                completed = True
            finally:
                # The subprocess is strictly serial.  If playback interruption
                # closes this generator, consume the remainder of this request
                # before allowing another request onto the same JSONL channel.
                if not completed and self._is_running():
                    try:
                        for _discarded in messages:
                            pass
                    except Exception as exc:
                        logger.warning(
                            "[TTS:GPT-SoVITS] failed to drain interrupted request: %s", exc
                        )

    @staticmethod
    def _kwargs(request: TTSSynthesisRequest, *, streaming: bool) -> dict:
        options = dict(request.options)
        options.pop("text_language", None)
        options.pop("prompt_language", None)
        options.pop("speed", None)
        if not streaming:
            options.pop("collect_t2s_stats", None)
        return {
            "ref_audio_path": request.reference_audio,
            "prompt_text": request.reference_text,
            "text_language": request.options.get("text_language", request.language),
            "prompt_language": request.options.get(
                "prompt_language",
                request.reference_language or request.language,
            ),
            "speed": request.speed,
            **options,
        }

    def synthesize(self, request: TTSSynthesisRequest) -> TTSAudioChunk:
        if self._sidecar_enabled():
            chunks = list(self._synthesize_sidecar_stream(request, streaming=False))
            if len(chunks) != 1:
                raise TTSBackendError(
                    f"GPT-SoVITS sidecar returned {len(chunks)} chunks for non-streaming inference"
                )
            return chunks[0]
        sample_rate, audio = self._ready().infer(
            text=request.text,
            **self._kwargs(request, streaming=False),
        )
        return TTSAudioChunk(int(sample_rate), audio, request.text)

    def synthesize_stream(self, request: TTSSynthesisRequest):
        if self._sidecar_enabled():
            yield from self._synthesize_sidecar_stream(request, streaming=True)
            return
        kwargs = self._kwargs(request, streaming=True)
        kwargs["chunk_size_seconds"] = request.chunk_size_seconds
        for item in self._ready().infer_stream(text=request.text, **kwargs):
            if len(item) == 2:
                sample_rate, audio = item
                text = ""
            else:
                sample_rate, audio, text = item
            yield TTSAudioChunk(int(sample_rate), audio, str(text or ""))

    def close(self) -> None:
        inferencer = self._inferencer
        self._inferencer = None
        close = getattr(inferencer, "close", None)
        if callable(close):
            close()
        proc = self._proc
        self._proc = None
        self._ready_info = {}
        if proc is not None:
            self._stop_sidecar(proc)

    @staticmethod
    def _stop_sidecar(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
