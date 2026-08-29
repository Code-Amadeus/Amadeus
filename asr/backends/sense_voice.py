"""SenseVoice-Small ASR backend."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Optional

import numpy as np

from asr.backend import BaseASRBackend
from config.log_privacy import protected_text
from config.settings import SENSEVOICE_LANGUAGE, SENSEVOICE_MODEL_PATH

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<\|[^|]*\|>")


class SenseVoiceBackend(BaseASRBackend):
    """FunASR SenseVoiceSmall backend used by the lightweight wake path."""

    MODEL_ID = "iic/SenseVoiceSmall"

    def __init__(self) -> None:
        self._model = None
        self._language = SENSEVOICE_LANGUAGE or "auto"

    def _resolve_model_path(self) -> str:
        env_path = SENSEVOICE_MODEL_PATH.strip()
        candidates: list[Path] = []
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(Path.home() / ".cache" / "modelscope" / "hub" / "models" / "iic" / "SenseVoiceSmall")
        candidates.append(
            Path.home() / ".cache" / "modelscope" / "hub" / "models" / "._____temp" / "iic" / "SenseVoiceSmall"
        )
        for path in candidates:
            if (path / "config.yaml").is_file() and (path / "model.pt").is_file():
                return str(path)
        raise FileNotFoundError(
            "SenseVoiceSmall model cache not found. Set SENSEVOICE_MODEL_PATH or pre-download iic/SenseVoiceSmall."
        )

    def load(self, device: str) -> None:
        os.environ.setdefault("MODELSCOPE_ENVIRONMENT", "local")
        if os.environ.get("MODELSCOPE_LOG_LEVEL", "").upper() == "DEBUG":
            os.environ.pop("MODELSCOPE_LOG_LEVEL", None)

        load_device = "cpu"
        if threading.current_thread() is threading.main_thread():
            load_device = device

        from funasr import AutoModel

        model_path = self._resolve_model_path()
        self._model = AutoModel(
            model=model_path,
            trust_remote_code=False,
            device=load_device,
            disable_update=True,
            disable_pbar=True,
            ncpu=2,
        )
        logger.info("[ASR:SenseVoice] loaded (device=%s, model=%s)", load_device, model_path)

    def set_language(self, language: str) -> None:
        self._language = str(language or "auto").strip() or "auto"

    def transcribe_with_language(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        context: str = "",
        language: str = "auto",
    ) -> Optional[str]:
        return self._transcribe_impl(audio, sample_rate=sample_rate, language=language)

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        context: str = "",
    ) -> Optional[str]:
        return self._transcribe_impl(audio, sample_rate=sample_rate, language=self._language)

    def _transcribe_impl(
        self,
        audio: np.ndarray,
        *,
        sample_rate: int = 16000,
        language: str = "auto",
    ) -> Optional[str]:
        if self._model is None:
            raise RuntimeError("SenseVoiceBackend.load() has not been called")

        duration_ms = len(audio) / sample_rate * 1000
        t0 = time.perf_counter()
        try:
            res = self._model.generate(
                input=audio,
                cache={},
                language=language,
                use_itn=True,
                batch_size_s=60,
            )
            if not res:
                return None
            raw = res[0].get("text", "")
            text = _TAG_RE.sub("", raw).strip()
            dt = (time.perf_counter() - t0) * 1000
            logger.info(
                "[ASR:SenseVoice] %.0fms audio -> %.0fms infer lang=%s: %s",
                duration_ms,
                dt,
                language,
                protected_text(text),
            )
            return text or None
        except Exception as exc:
            logger.error("[ASR:SenseVoice] inference failed: %s", exc)
            return None
