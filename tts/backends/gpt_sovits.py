"""Adapter for Amadeus's embedded GPT-SoVITS inference pipeline."""

from __future__ import annotations

import importlib.util

import base64
import json
import logging
import os
import subprocess
import sys
import threading
import time
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
    """Run supported GPT-SoVITS checkpoints through the low-latency pipeline."""

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
        self._emotion_pack = None
        self.emotion_reference_status = {"state": "disabled", "ready": False, "detail": "Emotion references are disabled."}

    def emotion_reference_key(self, emotion: str) -> str:
        return self._emotion_pack.key_for(emotion) if self._emotion_pack else ""

    def _configure_emotion_references(self) -> None:
        from config import settings
        if not settings.ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING:
            return
        infer = self._inferencer
        if (sys.platform != "win32" or infer is None or infer.model_version != "v3"
                or infer.is_rocm or not str(infer.device).startswith("cuda")
                or settings.TTS_OUTPUT_LANGUAGE != "日文"):
            self.emotion_reference_status = {"state": "unsupported", "ready": False,
                "detail": "Emotion references require Windows CUDA, embedded V3 and Japanese output."}
            return
        from tts.reference_pack import PACK_TREE, ReferencePackError, load_reference_pack
        root = _PROJECT_ROOT / "assets" / PACK_TREE
        if not (root / "references.json").is_file():
            self.emotion_reference_status = {"state": "not_installed", "ready": False,
                "detail": "Install the optional emotion reference pack, then restart."}
            logger.warning("[TTS-EMOTION] %s", self.emotion_reference_status["detail"])
            return
        start = time.perf_counter()
        try:
            pack = load_reference_pack(root)
            refs = pack.distinct_references()
            default = infer.warm_reference_cache(settings.TTS_REF_AUDIO_JA, settings.TTS_REF_TEXT_JA, settings.TTS_OUTPUT_LANGUAGE)
            if any(default.get(key) is None for key in ("refer_spec", "prompt_fea_ref", "prompt_ge", "mel2_norm")):
                raise ReferencePackError("Could not warm the configured default acoustic reference")
            for ref in refs:
                cache = infer.warm_reference_cache(str(ref.audio), ref.text, "日文")
                if any(cache.get(key) is None for key in ("prompt", "phones1", "bert1")):
                    raise ReferencePackError(f"Could not warm semantic reference: {ref.audio.name}")
            infer._sync_sovits_timing()
        except (ReferencePackError, RuntimeError, OSError, ValueError) as exc:
            self.emotion_reference_status = {"state": "invalid", "ready": False, "detail": str(exc)}
            logger.warning("[TTS-EMOTION] unavailable; default TTS remains active: %s", exc)
            return
        self._emotion_pack = pack
        elapsed = time.perf_counter() - start
        self.emotion_reference_status = {"state": "ready", "ready": True,
            "warmed_references": len(refs), "warmup_seconds": elapsed,
            "detail": "Emotion reference pack ready; all references prewarmed."}
        logger.info("[TTS-EMOTION] prewarmed %d distinct references in %.3fs", len(refs), elapsed)

    @property
    def is_rocm(self) -> bool:
        if self.deployment == "subprocess":
            return bool(
                self._ready_info.get("hip")
                and self._ready_info.get("cuda_available")
                and str(self._ready_info.get("device", "")).lower().startswith("cuda")
            )
        return bool(getattr(self._inferencer, "is_rocm", False))

    @property
    def cuda_graph_enabled(self) -> bool:
        from config.tts_acceleration import cuda_graph_enabled
        device = (self._ready_info.get("device", "") if self.deployment == "subprocess"
                  else getattr(self._inferencer, "device", ""))
        return cuda_graph_enabled(device, is_rocm=self.is_rocm)

    @staticmethod
    def _sidecar_enabled() -> bool:
        # Direct backend users may not have imported config.settings yet.  Load
        # the project dotenv before looking for the two sidecar switches.
        from config.environment import load_project_environment

        load_project_environment(_PROJECT_ROOT)
        mode = os.environ.get("TTS_MODE", "").strip().lower()
        if mode in {"sidecar", "subprocess", "process"} or bool(
            os.environ.get("TTS_PYTHON", "").strip()
        ):
            return True
        from config import settings

        # ASR/VAD dependencies change process-wide PyTorch CPU thread settings.
        # Keep CPU synthesis in the existing worker process, including when
        # device=auto resolves to CPU (Intel macOS).
        return str(settings.TTS_DEVICE).strip().lower().split(":", 1)[0] == "cpu"

    def load(self) -> None:
        if self._inferencer is not None or self._is_running():
            return
        if self._sidecar_enabled():
            self._load_sidecar()
            self._configure_emotion_references()
            return
        self.deployment = "embedded"
        from config import settings
        from local_tts_infer import TTSInferencer

        self._inferencer = TTSInferencer(
            device=settings.TTS_DEVICE,
            gpt_path=settings.TTS_GPT_MODEL_PATH or None,
            sovits_path=settings.TTS_SOVITS_MODEL_PATH or None,
        )
        self._configure_emotion_references()

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
        options = dict(request.options)
        # Semantic-reference routing is currently embedded-only.
        options.pop("emotion", None)
        return {
            "text": request.text,
            "language": request.language,
            "voice": request.voice,
            "speed": request.speed,
            "reference_audio": request.reference_audio,
            "reference_text": request.reference_text,
            "reference_language": request.reference_language,
            "chunk_size_seconds": request.chunk_size_seconds,
            "options": options,
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
            **self._request_kwargs(request, streaming=False),
        )
        return TTSAudioChunk(int(sample_rate), audio, request.text)

    def synthesize_stream(self, request: TTSSynthesisRequest):
        if self._sidecar_enabled():
            yield from self._synthesize_sidecar_stream(request, streaming=True)
            return
        self._ready()
        kwargs = self._request_kwargs(request, streaming=True)
        kwargs["chunk_size_seconds"] = request.chunk_size_seconds
        for item in self._ready().infer_stream(text=request.text, **kwargs):
            if len(item) == 2:
                sample_rate, audio = item
                text = ""
            else:
                sample_rate, audio, text = item
            yield TTSAudioChunk(int(sample_rate), audio, str(text or ""))

    def _request_kwargs(self, request: TTSSynthesisRequest, *, streaming: bool) -> dict:
        kwargs = self._kwargs(request, streaming=streaming)
        emotion = str(kwargs.pop("emotion", ""))
        if self._emotion_pack and request.language == "ja":
            ref = self._emotion_pack.references.get(emotion)
            if ref:
                kwargs["semantic_reference"] = (str(ref.audio), ref.text, "日文")
                logger.info("[TTS-EMOTION] semantic=%s acoustic=%s", ref.audio.name, Path(request.reference_audio).name)
        return kwargs

    def close(self) -> None:
        inferencer = self._inferencer
        self._inferencer = None
        self._emotion_pack = None
        self.emotion_reference_status = {"state": "disabled", "ready": False, "detail": "Voice runtime is closed."}
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


def probe() -> tuple[str, str]:
    from config import settings

    if importlib.util.find_spec("soundfile") is None:
        return "not_installed", "Local GPT-SoVITS dependencies are not installed"
    model_root = _PROJECT_ROOT / "assets" / "models" / "gpt-sovits"

    def configured_path(raw: str, fallback: Path) -> Path:
        path = Path(str(raw or "")) if str(raw or "").strip() else fallback
        return path if path.is_absolute() else _PROJECT_ROOT / path

    gpt = configured_path(
        settings.TTS_GPT_MODEL_PATH,
        model_root / "weights" / "gpt" / "v3" / "xxx-e15.ckpt",
    )
    sovits = configured_path(
        settings.TTS_SOVITS_MODEL_PATH,
        model_root / "weights" / "sovits" / "v3" / "xxx_e2_s174_l32.pth",
    )
    if gpt.is_file() and sovits.is_file():
        return "installed", f"Embedded GPT-SoVITS checkpoint pair found ({settings.TTS_VOICE_PROFILE})"
    return "not_installed", f"Embedded GPT-SoVITS checkpoint pair is not installed ({settings.TTS_VOICE_PROFILE})"
