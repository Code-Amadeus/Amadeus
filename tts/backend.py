"""Public speech-synthesis backend contract and legacy pipeline adapter."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator

import numpy as np


class TTSBackendError(RuntimeError):
    """Speech backend could not complete a synthesis request."""


@dataclass(frozen=True)
class TTSSynthesisRequest:
    text: str
    language: str = "ja"
    voice: str = ""
    speed: float = 1.0
    reference_audio: str = ""
    reference_text: str = ""
    reference_language: str = ""
    chunk_size_seconds: float | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TTSAudioChunk:
    sample_rate: int
    audio: np.ndarray
    text: str = ""


class BaseTTSBackend(ABC):
    backend_id = "unknown"
    deployment = "embedded"
    supports_streaming = False

    def load(self) -> None:
        """Initialize backend resources. Remote backends may only validate config."""

    @abstractmethod
    def synthesize(self, request: TTSSynthesisRequest) -> TTSAudioChunk:
        """Synthesize one complete audio response."""

    def synthesize_stream(self, request: TTSSynthesisRequest) -> Iterator[TTSAudioChunk]:
        yield self.synthesize(request)

    def close(self) -> None:
        """Release optional backend resources."""


def _language_code(value: str) -> str:
    clean = str(value or "").strip().lower()
    if clean in {"日文", "ja", "jp", "japanese"}:
        return "ja"
    if clean in {"英文", "en", "english"}:
        return "en"
    return clean or "auto"


class TTSRuntimeAdapter:
    """Translate the established pipeline call shape into the public contract.

    This keeps sentence scheduling, CUDA-Graph policy, playback, subtitles, and
    mouth signals unchanged while allowing the actual synthesizer to vary.
    """

    def __init__(self, backend: BaseTTSBackend) -> None:
        self.backend = backend

    @property
    def backend_id(self) -> str:
        return str(self.backend.backend_id)

    @property
    def supports_streaming(self) -> bool:
        return bool(self.backend.supports_streaming)

    @staticmethod
    def _request(
        *,
        text: str,
        ref_audio_path: str = "",
        prompt_text: str | None = None,
        text_language: str = "日文",
        prompt_language: str = "日文",
        speed: float = 1.0,
        chunk_size_seconds: float | None = None,
        **options: Any,
    ) -> TTSSynthesisRequest:
        return TTSSynthesisRequest(
            text=str(text or ""),
            language=_language_code(text_language),
            speed=float(speed),
            reference_audio=str(ref_audio_path or ""),
            reference_text=str(prompt_text or ""),
            reference_language=_language_code(prompt_language),
            chunk_size_seconds=chunk_size_seconds,
            options={
                "text_language": text_language,
                "prompt_language": prompt_language,
                "speed": speed,
                **options,
            },
        )

    def infer(
        self,
        text: str,
        ref_audio_path: str,
        prompt_text: str | None = None,
        text_language: str = "日文",
        prompt_language: str = "日文",
        how_to_cut: str = "不切",
        top_k: int = 20,
        top_p: float = 0.6,
        temperature: float = 0.6,
        speed: float = 1.0,
        sample_steps: int = 16,
        ref_free: bool = False,
        pause_second: float = 0.3,
        if_freeze: bool = False,
        inp_refs=None,
        if_sr: bool = False,
        enable_cuda_graph: bool = False,
        enable_static_kv: bool = True,
        max_sec_override: float | None = None,
    ) -> tuple[int, np.ndarray]:
        request = self._request(
            text=text,
            ref_audio_path=ref_audio_path,
            prompt_text=prompt_text,
            text_language=text_language,
            prompt_language=prompt_language,
            how_to_cut=how_to_cut,
            top_k=top_k,
            top_p=top_p,
            temperature=temperature,
            speed=speed,
            sample_steps=sample_steps,
            ref_free=ref_free,
            pause_second=pause_second,
            if_freeze=if_freeze,
            inp_refs=inp_refs,
            if_sr=if_sr,
            enable_cuda_graph=enable_cuda_graph,
            enable_static_kv=enable_static_kv,
            max_sec_override=max_sec_override,
        )
        chunk = self.backend.synthesize(request)
        return chunk.sample_rate, chunk.audio

    def infer_stream(
        self,
        text: str,
        ref_audio_path: str,
        prompt_text: str | None = None,
        text_language: str = "日文",
        prompt_language: str = "日文",
        how_to_cut: str = "按标点符号切",
        top_k: int = 20,
        top_p: float = 0.6,
        temperature: float = 0.6,
        speed: float = 1.0,
        sample_steps: int = 16,
        ref_free: bool = False,
        pause_second: float = 0.3,
        if_freeze: bool = False,
        inp_refs=None,
        if_sr: bool = False,
        enable_cuda_graph: bool = False,
        enable_static_kv: bool = True,
        chunk_size_seconds: float | None = None,
        max_sec_override: float | None = None,
        collect_t2s_stats: bool = False,
    ):
        request = self._request(
            text=text,
            ref_audio_path=ref_audio_path,
            prompt_text=prompt_text,
            text_language=text_language,
            prompt_language=prompt_language,
            how_to_cut=how_to_cut,
            top_k=top_k,
            top_p=top_p,
            temperature=temperature,
            speed=speed,
            sample_steps=sample_steps,
            ref_free=ref_free,
            pause_second=pause_second,
            if_freeze=if_freeze,
            inp_refs=inp_refs,
            if_sr=if_sr,
            enable_cuda_graph=enable_cuda_graph,
            enable_static_kv=enable_static_kv,
            chunk_size_seconds=chunk_size_seconds,
            max_sec_override=max_sec_override,
            collect_t2s_stats=collect_t2s_stats,
        )
        for chunk in self.backend.synthesize_stream(request):
            yield chunk.sample_rate, chunk.audio, chunk.text
