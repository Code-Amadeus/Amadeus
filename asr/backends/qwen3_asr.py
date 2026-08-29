"""Qwen3-ASR backend.

When qwen_asr is available in the current environment, the backend loads it in
process. The old subprocess sidecar remains as an explicit fallback for
isolated environments.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import time
import gc
from pathlib import Path
from typing import Optional

import numpy as np

from asr.backend import ASRBackendFatalError, BaseASRBackend
from asr.qwen_model import resolve_qwen_model_source
from config.settings import QWEN3_ASR_REQUIRE_CUDA

logger = logging.getLogger(__name__)

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SIDECAR_SCRIPT = _PROJECT_ROOT / "asr" / "qwen3_asr_sidecar.py"
_VENV_ASR_PYTHON = _PROJECT_ROOT / ".venv_asr" / "Scripts" / "python.exe"
_VENV_CU124_PYTHON = _PROJECT_ROOT / ".venv_cu124" / "Scripts" / "python.exe"
_TOKENS_PER_SEC = 10
_MAX_TOKENS_CAP = 256
_MAX_TOKENS_FLOOR = 32
_LANGUAGE_ALIASES = {
    "zh": "Chinese",
    "zh-cn": "Chinese",
    "zh-tw": "Chinese",
    "yue": "Cantonese",
    "en": "English",
    "ar": "Arabic",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "pt": "Portuguese",
    "id": "Indonesian",
    "it": "Italian",
    "ko": "Korean",
    "ru": "Russian",
    "th": "Thai",
    "vi": "Vietnamese",
    "ja": "Japanese",
    "jp": "Japanese",
    "tr": "Turkish",
    "hi": "Hindi",
    "ms": "Malay",
    "nl": "Dutch",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "pl": "Polish",
    "cs": "Czech",
    "fil": "Filipino",
    "tl": "Filipino",
    "fa": "Persian",
    "el": "Greek",
    "ro": "Romanian",
    "hu": "Hungarian",
    "mk": "Macedonian",
}


def _qwen_language(value: object) -> str | None:
    raw = str(value or "").strip()
    clean = raw.lower().replace("_", "-")
    if clean in {"", "auto", "automatic", "detect"}:
        return None
    return _LANGUAGE_ALIASES.get(clean, raw[:1].upper() + raw[1:].lower())
_FATAL_CUDA_MARKERS = (
    "unspecified launch failure",
    "illegal memory access",
    "device-side assert",
    "cuda error",
    "cublas_status",
    "cudnn_status",
)


def _calc_max_tokens(duration_s: float) -> int:
    estimated = int(duration_s * _TOKENS_PER_SEC)
    return max(_MAX_TOKENS_FLOOR, min(estimated, _MAX_TOKENS_CAP))


class Qwen3ASRBackend(BaseASRBackend):
    """Qwen3-ASR-0.6B, in-process by default with sidecar fallback."""

    _process_lock = threading.Lock()
    _model_lock = threading.Lock()
    _io_lock = threading.Lock()
    _shared_proc: Optional[subprocess.Popen] = None
    _shared_model = None
    _shared_device = ""
    _shared_attn_impl = ""
    _inprocess_disabled_until = 0.0
    _inprocess_disabled_reason = ""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._model = None
        self._device = ""
        self._attn_impl = ""
        self._lock = Qwen3ASRBackend._io_lock
        self._owns_proc = False
        self._owns_model = False
        self._mode = "unloaded"
        self._language: str | None = None

    def set_language(self, language: str) -> None:
        self._language = _qwen_language(language)

    @staticmethod
    def _is_running(proc: Optional[subprocess.Popen]) -> bool:
        return proc is not None and proc.poll() is None

    @staticmethod
    def _terminate_process_tree(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                proc.terminate()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    @staticmethod
    def _select_python() -> str:
        env_python = os.environ.get("QWEN3_ASR_PYTHON", "").strip()
        if env_python and os.path.exists(env_python):
            return env_python

        if importlib.util.find_spec("qwen_asr") is not None:
            return sys.executable

        if _VENV_CU124_PYTHON.exists():
            return str(_VENV_CU124_PYTHON)

        return str(_VENV_ASR_PYTHON)

    @classmethod
    def _can_use_inprocess(cls) -> bool:
        mode = os.environ.get("QWEN3_ASR_MODE", "auto").strip().lower()
        if mode in {"sidecar", "subprocess", "process"}:
            return False
        if time.time() < cls._inprocess_disabled_until:
            return False
        return importlib.util.find_spec("qwen_asr") is not None

    @staticmethod
    def _is_fatal_cuda_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(marker in text for marker in _FATAL_CUDA_MARKERS)

    @classmethod
    def _disable_inprocess_temporarily(cls, reason: str) -> None:
        try:
            cooldown = float(os.environ.get("QWEN3_ASR_CUDA_FAILURE_SIDECAR_SECONDS", "900"))
        except ValueError:
            cooldown = 900.0
        cooldown = max(30.0, cooldown)
        cls._inprocess_disabled_until = max(cls._inprocess_disabled_until, time.time() + cooldown)
        cls._inprocess_disabled_reason = reason
        logger.warning(
            "[ASR:Qwen3ASR] disabling in-process ASR for %.0fs after fatal CUDA error; "
            "future loads will use sidecar",
            cooldown,
        )

    def _drop_inprocess_model(self, reason: str) -> None:
        model = self._model
        if Qwen3ASRBackend._shared_model is model:
            Qwen3ASRBackend._shared_model = None
            Qwen3ASRBackend._shared_device = ""
            Qwen3ASRBackend._shared_attn_impl = ""
        self._model = None
        self._owns_model = False
        self._mode = "unloaded"
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            logger.debug("[ASR:Qwen3ASR] cuda cache cleanup skipped after %s", reason, exc_info=True)

    def _load_inprocess(self, device: str) -> None:
        with self._model_lock:
            if Qwen3ASRBackend._shared_model is not None:
                self._model = Qwen3ASRBackend._shared_model
                self._device = Qwen3ASRBackend._shared_device
                self._attn_impl = Qwen3ASRBackend._shared_attn_impl
                self._owns_model = False
                self._mode = "inprocess"
                logger.info(
                    "[ASR:Qwen3ASR] in-process model already loaded; reuse "
                    "(device=%s, attn=%s)",
                    self._device,
                    self._attn_impl,
                )
                return

            import torch
            from qwen_asr import Qwen3ASRModel

            cuda_available = bool(torch.cuda.is_available())
            device_map = "cuda:0" if str(device).startswith("cuda") and cuda_available else "cpu"
            dtype = torch.bfloat16 if "cuda" in device_map else torch.float32
            extra_kwargs = {}
            attn_impl = "eager"
            if "cuda" in device_map:
                try:
                    import flash_attn  # noqa: F401
                    extra_kwargs["attn_implementation"] = "flash_attention_2"
                    attn_impl = "flash_attention_2"
                except ImportError:
                    extra_kwargs["attn_implementation"] = "sdpa"
                    attn_impl = "sdpa"

            logger.info(
                "[ASR:Qwen3ASR] torch runtime exe=%s torch=%s cuda_available=%s "
                "cuda_version=%s visible=%s requested=%s resolved=%s attn=%s",
                sys.executable,
                getattr(torch, "__version__", "?"),
                cuda_available,
                getattr(torch.version, "cuda", None),
                os.environ.get("CUDA_VISIBLE_DEVICES"),
                device,
                device_map,
                attn_impl,
            )
            require_cuda = bool(QWEN3_ASR_REQUIRE_CUDA)
            if str(device).startswith("cuda") and device_map == "cpu":
                message = (
                    "[ASR:Qwen3ASR] requested CUDA but torch reports CUDA unavailable "
                    f"(exe={sys.executable}, torch={getattr(torch, '__version__', '?')}, "
                    f"torch_cuda={getattr(torch.version, 'cuda', None)}, "
                    f"visible={os.environ.get('CUDA_VISIBLE_DEVICES')})"
                )
                if require_cuda:
                    raise RuntimeError(message)
                logger.warning("%s; falling back to CPU/eager", message)

            logger.info("[ASR:Qwen3ASR] loading in-process model (device=%s)", device_map)
            t0 = time.perf_counter()
            model = Qwen3ASRModel.from_pretrained(
                resolve_qwen_model_source(),
                dtype=dtype,
                device_map=device_map,
                max_inference_batch_size=1,
                max_new_tokens=_MAX_TOKENS_CAP,
                **extra_kwargs,
            )
            dt = time.perf_counter() - t0
            Qwen3ASRBackend._shared_model = model
            Qwen3ASRBackend._shared_device = device_map
            Qwen3ASRBackend._shared_attn_impl = attn_impl
            self._model = model
            self._device = device_map
            self._attn_impl = attn_impl
            self._owns_model = True
            self._mode = "inprocess"
            logger.info(
                "[ASR:Qwen3ASR] in-process ready (device=%s, attn=%s, %.2fs)",
                device_map,
                attn_impl,
                dt,
            )

    def load(self, device: str) -> None:
        if self._can_use_inprocess():
            self._load_inprocess(device)
            return

        mode = os.environ.get("QWEN3_ASR_MODE", "auto").strip().lower()
        if mode == "inprocess":
            raise RuntimeError("QWEN3_ASR_MODE=inprocess but qwen_asr is not importable")

        with self._process_lock:
            if self._is_running(self._proc):
                self._mode = "sidecar"
                logger.info("[ASR:Qwen3ASR] sidecar already running; reuse current process")
                return
            if self._is_running(Qwen3ASRBackend._shared_proc):
                self._proc = Qwen3ASRBackend._shared_proc
                self._owns_proc = False
                self._mode = "sidecar"
                logger.info("[ASR:Qwen3ASR] sidecar already running; reuse shared process")
                return

            python = self._select_python()
            if not os.path.exists(python):
                raise FileNotFoundError(
                    f"[ASR:Qwen3ASR] Python not found: {python}\n"
                    "Please install the ASR environment first."
                )

            logger.info("[ASR:Qwen3ASR] starting sidecar process: %s", python)
            proc = subprocess.Popen(
                [python, str(_SIDECAR_SCRIPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                cwd=str(_PROJECT_ROOT),
            )
            self._proc = proc
            self._owns_proc = True

            deadline = time.time() + 120
            while time.time() < deadline:
                if proc.poll() is not None:
                    err = b""
                    if proc.stderr is not None:
                        err = proc.stderr.read()
                    self._proc = None
                    self._owns_proc = False
                    raise RuntimeError(
                        "[ASR:Qwen3ASR] sidecar exited unexpectedly\n"
                        + err.decode("utf-8", errors="replace")
                    )
                if proc.stdout is None:
                    time.sleep(0.1)
                    continue
                line = proc.stdout.readline()
                if not line:
                    time.sleep(0.1)
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace").strip())
                except json.JSONDecodeError:
                    continue

                if msg.get("type") == "ready":
                    logger.info(
                        "[ASR:Qwen3ASR] ready "
                        f"(device={msg.get('device', '?')}, "
                        f"attn={msg.get('attn_impl', msg.get('flash_attn', '?'))})"
                    )
                    Qwen3ASRBackend._shared_proc = proc
                    self._mode = "sidecar"
                    return
                if msg.get("type") == "error":
                    self._terminate_process_tree(proc)
                    self._proc = None
                    self._owns_proc = False
                    raise RuntimeError(f"[ASR:Qwen3ASR] sidecar load failed: {msg.get('msg')}")

            self._terminate_process_tree(proc)
            self._proc = None
            self._owns_proc = False
            raise TimeoutError("[ASR:Qwen3ASR] sidecar startup timed out")

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        context: str = "",
    ) -> Optional[str]:
        if self._mode == "inprocess" and self._model is not None:
            return self._transcribe_inprocess(audio, sample_rate=sample_rate, context=context)

        if not self._is_running(self._proc):
            logger.warning("[ASR:Qwen3ASR] sidecar is not running; restarting")
            self.load("cuda")

        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            return None

        audio_b64 = base64.b64encode(audio.astype(np.float32).tobytes()).decode("ascii")
        req = json.dumps(
            {
                "audio_b64": audio_b64,
                "sample_rate": sample_rate,
                "context": context,
                "language": getattr(self, "_language", "Chinese"),
            }
        ) + "\n"

        duration_ms = len(audio) / sample_rate * 1000
        t0 = time.perf_counter()
        try:
            with self._lock:
                self._proc.stdin.write(req.encode("utf-8"))
                self._proc.stdin.flush()
                deadline = time.time() + 30
                while time.time() < deadline:
                    line = self._proc.stdout.readline()
                    if not line:
                        time.sleep(0.02)
                        continue
                    msg = json.loads(line.decode("utf-8", errors="replace").strip())
                    if msg.get("type") == "result":
                        text = msg.get("text", "").strip()
                        dt = (time.perf_counter() - t0) * 1000
                        logger.info(
                            f"[ASR:Qwen3ASR] {duration_ms:.0f}ms audio -> {dt:.0f}ms: {text!r}"
                        )
                        return text or None
                    if msg.get("type") == "error":
                        logger.error(f"[ASR:Qwen3ASR] sidecar inference error: {msg.get('msg')}")
                        return None
        except Exception as exc:
            logger.error(f"[ASR:Qwen3ASR] IPC failed: {exc}")
            if self._proc is Qwen3ASRBackend._shared_proc:
                Qwen3ASRBackend._shared_proc = None
            self._proc = None
            self._owns_proc = False
        return None

    def _transcribe_inprocess(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        context: str = "",
    ) -> Optional[str]:
        model = self._model
        if model is None:
            self.load("cuda")
            model = self._model
        if model is None:
            return None

        audio = audio.astype(np.float32, copy=False)
        duration_ms = len(audio) / sample_rate * 1000
        max_tokens = _calc_max_tokens(len(audio) / sample_rate)
        language = getattr(self, "_language", "Chinese")
        t0 = time.perf_counter()
        try:
            with self._lock:
                prev_tokens = getattr(model, "max_new_tokens", _MAX_TOKENS_CAP)
                model.max_new_tokens = max_tokens
                try:
                    results = model.transcribe(
                        audio=(audio, sample_rate),
                        language=language,
                        context=context,
                    )
                finally:
                    model.max_new_tokens = prev_tokens
            text = (results[0].text if results else "").strip()
            dt = (time.perf_counter() - t0) * 1000
            logger.info(
                "[ASR:Qwen3ASR] in-process %sms audio -> %sms: %r",
                f"{duration_ms:.0f}",
                f"{dt:.0f}",
                text,
            )
            return text or None
        except Exception as exc:
            logger.error("[ASR:Qwen3ASR] in-process inference failed: %s", exc)
            if self._is_fatal_cuda_error(exc):
                reason = str(exc).splitlines()[0]
                self._disable_inprocess_temporarily(reason)
                self._drop_inprocess_model(reason)
                raise ASRBackendFatalError(reason) from exc
            return None

    def close(self) -> None:
        if self._model is not None:
            if self._owns_model and Qwen3ASRBackend._shared_model is self._model:
                Qwen3ASRBackend._shared_model = None
                Qwen3ASRBackend._shared_device = ""
                Qwen3ASRBackend._shared_attn_impl = ""
            self._model = None
            self._owns_model = False
            self._mode = "unloaded"
            gc.collect()
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            return

        proc = self._proc
        if proc is None:
            return
        if self._owns_proc:
            self._terminate_process_tree(proc)
            if Qwen3ASRBackend._shared_proc is proc:
                Qwen3ASRBackend._shared_proc = None
        self._proc = None
        self._owns_proc = False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
