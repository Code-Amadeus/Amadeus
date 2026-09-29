"""Opt-in local Windows V3 experiment; no provider-neutral emotion inference."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import sys

logger = logging.getLogger(__name__)
SWITCH = "ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING"
ROOT = Path(__file__).resolve().parents[1]
_emotion: ContextVar[str] = ContextVar("windows_v3_tts_emotion", default="")
REFERENCE_FILES = {
    "angry": "angry.wav",
    "sad": "sad.wav",
    "shy": "shy_b.ogg",
    "blush": "shy_b.ogg",
    "surprised": "surprised.ogg",
    "disappointed": "disappointed_a.ogg",
}
ACOUSTIC_KEYS = ("refer_spec", "prompt_fea_ref", "prompt_ge", "mel2_norm")


@dataclass(frozen=True)
class Reference:
    audio: str
    text: str


def requested() -> bool:
    return sys.platform == "win32" and os.environ.get(SWITCH, "0") == "1"


def active(runtime) -> bool:
    return bool(requested() and runtime is not None
                and getattr(runtime, "backend_id", "") == "gpt_sovits"
                and getattr(runtime, "deployment", "") == "embedded"
                and getattr(getattr(runtime, "backend", None), "experimental_emotion_enabled", False))


def voice_key(emotion: str) -> str:
    return REFERENCE_FILES.get(str(emotion).strip().lower(), "default")


def stream(runtime, emotion: str, **params):
    """Own the conditioning scope on the producer thread until generator close."""
    token = _emotion.set(emotion if active(runtime) else "")
    inner = None
    try:
        inner = runtime.infer_stream(**params)
        for item in inner:
            yield item
    finally:
        try:
            close = getattr(inner, "close", None)
            if callable(close):
                close()
        finally:
            _emotion.reset(token)


def inferencer_type(base):
    """Isolate the cache composition from the supported inference implementation."""
    class WindowsV3EmotionInferencer(base):
        def __init__(self, *args, **kwargs):
            # Base initialization warms its cache through this overridden method.
            self.experimental_emotion_enabled = False
            super().__init__(*args, **kwargs)
            self.experimental_emotion_enabled = bool(
                requested() and self.model_version == "v3"
                and not self.is_rocm and str(self.device).startswith("cuda")
            )
            self._emotion_references: dict[str, Reference] = {}
            if self.experimental_emotion_enabled:
                for filename in set(REFERENCE_FILES.values()):
                    audio = ROOT / "assets/audio/reference/emotions" / filename
                    transcript = audio.with_suffix(".txt")
                    if not audio.is_file() or not transcript.is_file():
                        raise RuntimeError(f"Emotion experiment reference pair missing: {filename}")
                    text = transcript.read_text(encoding="utf-8").strip()
                    if not text:
                        raise RuntimeError(f"Emotion experiment reference transcript empty: {filename}")
                    self._emotion_references[filename] = Reference(str(audio), text)
            logger.info("[EmotionLab] enabled=%s model=%s device=%s",
                        self.experimental_emotion_enabled, self.model_version, self.device)

        def _build_session_cache(self, ref_audio_path, prompt_text, prompt_language_code):
            default = super()._build_session_cache(ref_audio_path, prompt_text, prompt_language_code)
            filename = voice_key(_emotion.get())
            if not self.experimental_emotion_enabled or not requested() or filename == "default":
                return default
            ref = self._emotion_references[filename]
            text = ref.text if ref.text[-1] in self.splits else ref.text + "。"
            semantic = super()._build_session_cache(ref.audio, text, prompt_language_code)
            if any(default.get(key) is None for key in ACOUSTIC_KEYS) or any(
                semantic.get(key) is None for key in ("prompt", "phones1", "bert1")
            ):
                raise RuntimeError("Incomplete emotion conditioning cache; refusing mixed fallback")
            # A request-local view, never mutate either shared reference cache.
            result = dict(semantic)
            result.update({key: default[key] for key in ACOUSTIC_KEYS})
            logger.info("[EmotionLab] conditioning emo=%s semantic=%s acoustic=%s",
                        _emotion.get(), filename, Path(ref_audio_path).name)
            return result

    return WindowsV3EmotionInferencer
