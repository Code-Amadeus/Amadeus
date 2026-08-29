"""
ASR 后端抽象基类

所有 ASR 后端实现此接口：
  load(device)          — 加载模型权重到指定设备
  transcribe(audio)     — float32 16kHz PCM → 纯文本（失败返回 None）

VAD 和麦克风管理由 ASRManager 负责，后端只关心"给我 PCM，我返回文字"。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import numpy as np


class ASRBackendError(RuntimeError):
    """Base class for recognizer backend runtime failures."""


class ASRBackendFatalError(ASRBackendError):
    """Raised when the backend instance should be rebuilt before reuse."""


class BaseASRBackend(ABC):
    """Conversation/wake transcription implementation contract.

    Managers serialize normal recognition for one instance. Backends that set
    ``supports_speculative_transcription`` to true must additionally serialize
    their own speculative and final calls, as the built-in local backends do.
    """

    backend_id = "unknown"
    deployment = "embedded"
    supports_speculative_transcription = True

    @abstractmethod
    def load(self, device: str) -> None:
        """加载模型。device 形如 'cuda' / 'cuda:0' / 'cpu'。"""

    @abstractmethod
    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000,
                   context: str = "") -> Optional[str]:
        """
        将 float32 PCM 转为文字。

        Parameters
        ----------
        audio:        float32 ndarray，值域 [-1, 1]，采样率由 sample_rate 给定
        sample_rate:  通常 16000
        context:      热词/领域提示词（逗号分隔），支持的后端会用其偏置解码

        Returns
        -------
        识别到的纯文本，失败或空语音返回 None。
        """

    def close(self) -> None:
        """Release optional backend resources."""
