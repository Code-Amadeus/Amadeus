"""Debug capture for future AEC experiments.

When AEC_DEBUG_CAPTURE=1, playback records two aligned raw float32 streams:
- reference.f32: TTS PCM resampled to 16 kHz mono
- mic.f32: microphone PCM captured at 16 kHz mono

The module is intentionally passive and off by default. It does not run any AEC;
it only gives us real reference/mic material for testing WebRTC AEC quality and
latency before we wire barge-in into production.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_TARGET_SR = 16000
_CHUNK_SAMPLES = 160


def _enabled() -> bool:
    return str(os.environ.get("AEC_DEBUG_CAPTURE", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _safe_label(label: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(label or "tts"))
    return text.strip("_")[:80] or "tts"


def _mic_index() -> int | None:
    raw = os.environ.get("AEC_DEBUG_MIC_INDEX") or os.environ.get("MICROPHONE_DEVICE_INDEX", "")
    try:
        value = int(str(raw).strip())
    except Exception:
        return None
    return value if value >= 0 else None


def _capture_root() -> Path:
    root = os.environ.get("AEC_DEBUG_CAPTURE_DIR", "").strip()
    if root:
        return Path(root)
    return Path.cwd() / "logs" / "aec_capture"


def _resample_to_target(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return audio
    sample_rate = int(sample_rate or _TARGET_SR)
    if sample_rate == _TARGET_SR:
        return audio
    try:
        from scipy.signal import resample_poly

        divisor = math.gcd(sample_rate, _TARGET_SR)
        up = _TARGET_SR // divisor
        down = sample_rate // divisor
        return resample_poly(audio, up, down).astype(np.float32, copy=False)
    except Exception:
        # Nearest-neighbor fallback is ugly but good enough for timestamped
        # debug material if scipy is unavailable.
        duration = audio.size / float(sample_rate)
        out_n = max(1, int(duration * _TARGET_SR))
        idx = np.linspace(0, audio.size - 1, out_n).astype(np.int64)
        return audio[idx].astype(np.float32, copy=False)


class AecDebugCapture:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active = False
        self._dir: Path | None = None
        self._ref_file = None
        self._ref_meta = None
        self._mic_file = None
        self._mic_meta = None
        self._mic_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pa = None
        self._stream = None

    def start(self, label: str) -> None:
        if not _enabled():
            return
        with self._lock:
            self.stop()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self._dir = _capture_root() / f"{stamp}_{_safe_label(label)}"
            self._dir.mkdir(parents=True, exist_ok=True)
            self._ref_file = (self._dir / "reference.f32").open("ab")
            self._ref_meta = (self._dir / "reference.jsonl").open("a", encoding="utf-8")
            self._mic_file = (self._dir / "mic.f32").open("ab")
            self._mic_meta = (self._dir / "mic.jsonl").open("a", encoding="utf-8")
            self._open_mic_stream()
            self._active = True
            self._stop_event.clear()
            self._mic_thread = threading.Thread(
                target=self._mic_loop,
                name="aec-debug-mic-capture",
                daemon=True,
            )
            self._mic_thread.start()
            logger.info("[AEC] debug capture started: %s", self._dir)

    def push_reference(self, audio: np.ndarray, sample_rate: int, sentence_id: str = "") -> None:
        if not _enabled() or not self._active:
            return
        ref = _resample_to_target(audio, sample_rate)
        if ref.size == 0:
            return
        now = time.perf_counter()
        with self._lock:
            if not self._active or self._ref_file is None:
                return
            self._ref_file.write(ref.astype(np.float32, copy=False).tobytes())
            if self._ref_meta is not None:
                self._ref_meta.write(
                    json.dumps(
                        {
                            "t": now,
                            "sentence_id": sentence_id,
                            "sample_rate": _TARGET_SR,
                            "samples": int(ref.size),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    def stop(self) -> None:
        with self._lock:
            if not self._active:
                return
            self._active = False
            self._stop_event.set()
        if self._mic_thread is not None and self._mic_thread.is_alive():
            self._mic_thread.join(timeout=1.0)
        with self._lock:
            stream = self._stream
            pa = self._pa
            self._stream = None
            self._pa = None
            for handle_name in ("_ref_file", "_ref_meta", "_mic_file", "_mic_meta"):
                handle = getattr(self, handle_name)
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass
                    setattr(self, handle_name, None)
            self._mic_thread = None
            logger.info("[AEC] debug capture stopped")
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except Exception:
            pass
        try:
            if pa is not None:
                pa.terminate()
        except Exception:
            pass

    def _open_mic_stream(self) -> None:
        try:
            import pyaudio

            self._pa = pyaudio.PyAudio()
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=_TARGET_SR,
                input=True,
                input_device_index=_mic_index(),
                frames_per_buffer=_CHUNK_SAMPLES,
            )
        except Exception as exc:
            logger.warning("[AEC] mic debug capture unavailable: %s", exc)
            self._stream = None
            if self._pa is not None:
                try:
                    self._pa.terminate()
                except Exception:
                    pass
                self._pa = None

    def _mic_loop(self) -> None:
        stream = self._stream
        if stream is None:
            return

        while not self._stop_event.is_set():
            try:
                raw = stream.read(_CHUNK_SAMPLES, exception_on_overflow=False)
            except Exception:
                break
            mic = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            now = time.perf_counter()
            with self._lock:
                if not self._active or self._mic_file is None:
                    break
                self._mic_file.write(mic.tobytes())
                if self._mic_meta is not None:
                    self._mic_meta.write(
                        json.dumps(
                            {"t": now, "sample_rate": _TARGET_SR, "samples": int(mic.size)},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )


_CAPTURE = AecDebugCapture()


def get_aec_debug_capture() -> AecDebugCapture:
    return _CAPTURE
