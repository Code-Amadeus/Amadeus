"""Realtime WebRTC AEC bridge for TTS reference and microphone capture.

The module is default-off. When enabled, playback code feeds TTS PCM into the
reverse stream, and ASR/wake code can pass microphone chunks through the
capture stream before VAD/recognition.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from math import gcd

import numpy as np

from config.settings import (
    AEC_DELAY_MS_BLUETOOTH,
    AEC_DELAY_MS_INTERNAL,
    AEC_DELAY_MS_USB,
    AEC_REALTIME_BARGE_IN,
    AEC_REALTIME_DEBUG,
    AEC_REALTIME_DELAY_MS,
    AEC_REALTIME_ENABLED,
    AEC_REALTIME_ENABLE_AGC,
    AEC_REALTIME_ENABLE_NS,
)

logger = logging.getLogger(__name__)

_TARGET_SR = 16000
_FRAME = _TARGET_SR // 100


def _normalize_device_class(device_class: str | None) -> str:
    value = str(device_class or "unknown").strip().lower()
    return value if value in {"bluetooth", "internal", "usb"} else "unknown"


def select_aec_delay_ms(device_class: str | None) -> tuple[float, str]:
    if "AEC_REALTIME_DELAY_MS" in os.environ:
        try:
            return float(os.environ.get("AEC_REALTIME_DELAY_MS", "")), "explicit AEC_REALTIME_DELAY_MS"
        except Exception:
            return float(AEC_REALTIME_DELAY_MS), "explicit AEC_REALTIME_DELAY_MS"
    normalized = _normalize_device_class(device_class)
    if normalized == "bluetooth":
        return float(AEC_DELAY_MS_BLUETOOTH), "bluetooth device_class"
    if normalized == "internal":
        return float(AEC_DELAY_MS_INTERNAL), "internal device_class"
    if normalized == "usb":
        return float(AEC_DELAY_MS_USB), "usb device_class"
    return float(AEC_REALTIME_DELAY_MS), "unknown device_class"


def _peek_mic_device_class() -> str:
    try:
        import asr.mic_input_service as mic_mod

        svc = getattr(mic_mod, "_INSTANCE", None)
        device = getattr(svc, "device", None) if svc is not None else None
        return _normalize_device_class(getattr(device, "device_class", "unknown"))
    except Exception:
        return "unknown"


def _to_float32(audio) -> np.ndarray:
    if audio is None:
        return np.zeros(0, dtype=np.float32)
    arr = np.asarray(audio)
    if arr.size == 0:
        return np.zeros(0, dtype=np.float32)
    if arr.dtype != np.float32:
        arr = arr.astype(np.float32)
    return np.ascontiguousarray(arr.reshape(-1))


def _resample(audio: np.ndarray, source_rate: int) -> np.ndarray:
    source_rate = int(source_rate or _TARGET_SR)
    if source_rate == _TARGET_SR or audio.size == 0:
        return audio.astype(np.float32, copy=False)

    from scipy.signal import resample_poly

    factor = gcd(source_rate, _TARGET_SR)
    up = _TARGET_SR // factor
    down = source_rate // factor
    return resample_poly(audio, up, down).astype(np.float32, copy=False)


def _float_to_i16_bytes(frame: np.ndarray) -> bytes:
    pcm = np.clip(np.asarray(frame, dtype=np.float32), -1.0, 1.0)
    return (pcm * 32767.0).astype("<i2").tobytes()


def _bytes_to_float_i16(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


class RealtimeAECProcessor:
    def __init__(self) -> None:
        self.enabled = bool(AEC_REALTIME_ENABLED)
        self.barge_in_enabled = bool(AEC_REALTIME_BARGE_IN)
        self._debug = bool(AEC_REALTIME_DEBUG)
        self._device_class = _peek_mic_device_class()
        self._delay_ms, self._delay_reason = select_aec_delay_ms(self._device_class)
        self._lock = threading.RLock()
        self._ap = None
        self._init_error: str | None = None
        self._reverse_tail = np.zeros(0, dtype=np.float32)
        self._reference_history: deque[tuple[float, float, np.ndarray]] = deque()
        self._reference_history_seconds = 30.0
        self._reference_frames = 0
        self._capture_frames = 0
        self._last_log_at = 0.0

    def _ensure(self) -> bool:
        if not self.enabled:
            return False
        if self._ap is not None:
            return True
        if self._init_error:
            return False

        try:
            from aec_audio_processing import AudioProcessor

            ap = AudioProcessor(
                enable_aec=True,
                enable_ns=bool(AEC_REALTIME_ENABLE_NS),
                enable_agc=bool(AEC_REALTIME_ENABLE_AGC),
                enable_vad=False,
            )
            ap.set_stream_format(_TARGET_SR, 1, _TARGET_SR, 1)
            ap.set_reverse_stream_format(_TARGET_SR, 1)
            ap.set_stream_delay(int(round(max(0.0, self._delay_ms))))
            self._ap = ap
            logger.info(
                "[AEC:Realtime] enabled delay_ms=%.1f ns=%s agc=%s barge_in=%s",
                self._delay_ms,
                bool(AEC_REALTIME_ENABLE_NS),
                bool(AEC_REALTIME_ENABLE_AGC),
                self.barge_in_enabled,
            )
            return True
        except Exception as exc:
            self._init_error = str(exc)
            logger.warning("[AEC:Realtime] unavailable: %s", exc)
            return False

    def set_delay_ms(self, delay_ms: float, *, reason: str = "") -> None:
        """Update the WebRTC AEC stream delay for the currently opened input path."""

        with self._lock:
            self._delay_ms = float(max(0.0, delay_ms))
            self._delay_reason = reason
            if self._ap is not None:
                try:
                    self._ap.set_stream_delay(int(round(self._delay_ms)))
                except Exception:
                    logger.exception("[AEC:Realtime] failed to update delay_ms=%.1f", self._delay_ms)
                    return
            logger.info(
                "[AEC:Realtime] delay_ms=%.1f%s",
                self._delay_ms,
                f" ({reason})" if reason else "",
            )

    def set_delay_for_device_class(self, device_class: str | None, *, reason: str = "") -> None:
        normalized = _normalize_device_class(device_class)
        delay_ms, delay_reason = select_aec_delay_ms(normalized)
        self._device_class = normalized
        detail = f"{delay_reason}; {reason}" if reason else delay_reason
        self.set_delay_ms(delay_ms, reason=detail)

    def push_reference(self, audio, sample_rate: int | None = None) -> None:
        if not self.enabled:
            return
        ref = _resample(_to_float32(audio), int(sample_rate or _TARGET_SR))
        if ref.size == 0:
            return

        with self._lock:
            self._remember_reference(ref)
            if not self._ensure():
                return
            data = np.concatenate([self._reverse_tail, ref])
            usable = (data.size // _FRAME) * _FRAME
            if usable <= 0:
                self._reverse_tail = data
                return
            for start in range(0, usable, _FRAME):
                self._ap.process_reverse_stream(_float_to_i16_bytes(data[start : start + _FRAME]))
            self._reverse_tail = data[usable:].astype(np.float32, copy=False)
            self._reference_frames += usable // _FRAME

    def get_reference_window(self, start_time: float, end_time: float, *, pad_s: float = 0.9) -> np.ndarray:
        """Return recent reference PCM around a mic capture time window.

        Timestamps are monotonic seconds from the same process. The returned
        audio is only for lightweight echo classification; WebRTC AEC still
        owns the actual reverse stream.
        """

        lo = float(start_time) - max(0.0, float(pad_s))
        hi = float(end_time) + max(0.0, float(pad_s))
        if hi <= lo:
            return np.zeros(0, dtype=np.float32)
        pieces: list[np.ndarray] = []
        with self._lock:
            for block_start, block_end, block in self._reference_history:
                if block_end < lo or block_start > hi:
                    continue
                offset_start = max(0, int((max(lo, block_start) - block_start) * _TARGET_SR))
                offset_end = min(block.size, int((min(hi, block_end) - block_start) * _TARGET_SR))
                if offset_end > offset_start:
                    pieces.append(block[offset_start:offset_end])
        if not pieces:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(pieces).astype(np.float32, copy=False)

    def process_mic(self, audio, sample_rate: int = _TARGET_SR) -> np.ndarray:
        mic = _resample(_to_float32(audio), sample_rate)
        if not self.enabled or mic.size == 0:
            return mic

        with self._lock:
            if not self._ensure():
                return mic

            original_len = mic.size
            pad = (-original_len) % _FRAME
            mic_padded = np.pad(mic, (0, pad), mode="constant") if pad else mic

            out: list[np.ndarray] = []
            for start in range(0, mic_padded.size, _FRAME):
                processed = self._ap.process_stream(
                    _float_to_i16_bytes(mic_padded[start : start + _FRAME])
                )
                out.append(_bytes_to_float_i16(processed))
            residual = np.concatenate(out).astype(np.float32, copy=False)[:original_len]
            self._capture_frames += max(1, mic_padded.size // _FRAME)
            self._debug_log(mic, residual)
            return residual

    def _remember_reference(self, ref: np.ndarray) -> None:
        now = time.monotonic()
        block = np.asarray(ref, dtype=np.float32).reshape(-1).copy()
        duration = block.size / float(_TARGET_SR)
        self._reference_history.append((now, now + duration, block))
        cutoff = now - self._reference_history_seconds
        while self._reference_history and self._reference_history[0][1] < cutoff:
            self._reference_history.popleft()

    def _debug_log(self, mic: np.ndarray, residual: np.ndarray) -> None:
        if not self._debug:
            return
        now = time.time()
        if now - self._last_log_at < 1.0:
            return
        self._last_log_at = now
        mic_rms = float(np.sqrt(np.mean(np.square(mic))) if mic.size else 0.0)
        out_rms = float(np.sqrt(np.mean(np.square(residual))) if residual.size else 0.0)
        logger.info(
            "[AEC:Realtime] mic_rms=%.5f residual_rms=%.5f ref_frames=%s cap_frames=%s",
            mic_rms,
            out_rms,
            self._reference_frames,
            self._capture_frames,
        )


_INSTANCE: RealtimeAECProcessor | None = None
_INSTANCE_LOCK = threading.Lock()


def get_realtime_aec_processor() -> RealtimeAECProcessor:
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = RealtimeAECProcessor()
    return _INSTANCE
