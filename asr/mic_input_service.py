"""Shared microphone input service for wake, barge-in, and ASR.

The service owns a single PyAudio input stream, applies realtime AEC once, and
keeps a short processed-audio ring buffer. Consumers create cursors over that
stream instead of opening their own microphone streams.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pyaudio
import torch

from asr.microphone import (
    configured_device_index,
    device_descriptor_for_index,
    MicDeviceDescriptor,
    open_input_stream_with_fallback,
)
from tts.aec_realtime import get_realtime_aec_processor

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000
_CHUNK_SAMPLES = 512
_DEFAULT_BUFFER_SECONDS = 12.0
_HANDOFF_MAX_CAPTURE_SEC = 5.0
_ENERGY_END_RMS = 0.008
_ENERGY_END_MS = 450


def _startup_timeout_seconds() -> float:
    raw = os.environ.get("MIC_INPUT_START_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return 15.0
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 15.0


def _startup_grace_seconds() -> float:
    raw = os.environ.get("MIC_INPUT_START_GRACE_SECONDS", "").strip()
    if not raw:
        return 3.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 3.0


@dataclass(frozen=True)
class MicFrame:
    seq: int
    timestamp: float
    audio: np.ndarray
    rms: float
    raw_audio: np.ndarray | None = None


class MicFrameCursor:
    def __init__(self, service: "MicInputService", start_seq: int) -> None:
        self._service = service
        self._next_seq = start_seq

    def read(self, timeout: float = 0.25) -> MicFrame | None:
        frame = self._service.read_frame(self._next_seq, timeout=timeout)
        if frame is None:
            return None
        self._next_seq = frame.seq + 1
        return frame


class MicInputService:
    def __init__(
        self,
        *,
        sample_rate: int = _SAMPLE_RATE,
        chunk_samples: int = _CHUNK_SAMPLES,
        buffer_seconds: float = _DEFAULT_BUFFER_SECONDS,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.chunk_samples = int(chunk_samples)
        max_frames = max(32, int(buffer_seconds * self.sample_rate / self.chunk_samples))
        self._frames: deque[MicFrame] = deque(maxlen=max_frames)
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pa: pyaudio.PyAudio | None = None
        self._stream = None
        self._ready_event = threading.Event()
        self._seq = 0
        self._mic_index: int | None = configured_device_index()
        self._device: MicDeviceDescriptor | None = None
        self._last_error = ""
        self._handoff_seq: int | None = None
        self._handoff_at = 0.0

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    @property
    def mic_index(self) -> int | None:
        return self._mic_index

    @property
    def device(self) -> MicDeviceDescriptor | None:
        return self._device

    def start(self, preferred_index: int | None = None, *, wait_timeout: float | None = None) -> None:
        timeout = _startup_timeout_seconds() if wait_timeout is None else max(0.0, wait_timeout)
        if self.running:
            if self._stream is None:
                if not self._wait_until_ready(timeout):
                    self.stop()
                    raise TimeoutError("[MicInput] microphone service startup timed out")
            if self._stream is None and self._last_error:
                raise RuntimeError(f"[MicInput] microphone service failed to start: {self._last_error}")
            if self._stream is None:
                raise RuntimeError("[MicInput] microphone service is running without an input stream")
            if preferred_index is not None and preferred_index != self._mic_index:
                logger.warning(
                    "[MicInput] requested microphone index=%s differs from active index=%s; "
                    "keeping the active stream",
                    preferred_index,
                    self._mic_index,
                )
            return
        if preferred_index is not None:
            self._mic_index = preferred_index
        self._last_error = ""
        self._ready_event.clear()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="mic-input-service", daemon=True)
        self._thread.start()
        if not self._wait_until_ready(timeout):
            self.stop()
            raise TimeoutError("[MicInput] microphone service startup timed out")
        if self._stream is None and self._last_error:
            self.stop()
            raise RuntimeError(f"[MicInput] microphone service failed to start: {self._last_error}")

    def _wait_until_ready(self, timeout: float) -> bool:
        started_at = time.monotonic()
        if self._ready_event.wait(timeout=timeout):
            return True
        grace = _startup_grace_seconds()
        if grace > 0 and self._ready_event.wait(timeout=grace):
            logger.warning(
                "[MicInput] microphone startup crossed timeout boundary; accepted after %.2fs",
                time.monotonic() - started_at,
            )
            return True
        if self._stream is not None:
            logger.warning(
                "[MicInput] microphone stream exists after timeout without ready event; accepting stream"
            )
            return True
        return False

    def stop(self) -> None:
        self._stop_event.set()
        with self._condition:
            self._condition.notify_all()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        if thread is None or not thread.is_alive() or thread is threading.current_thread():
            self._thread = None

    def close(self) -> None:
        self.stop()

    def cursor(self, *, include_recent_ms: float = 0.0) -> MicFrameCursor:
        self.start()
        with self._condition:
            if include_recent_ms > 0:
                frames_back = int(include_recent_ms / (self.chunk_samples / self.sample_rate * 1000.0))
                start_seq = max(0, self._seq - max(0, frames_back))
            else:
                start_seq = self._seq
            return MicFrameCursor(self, start_seq)

    def read_frame(self, seq: int, *, timeout: float = 0.25) -> MicFrame | None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while not self._stop_event.is_set():
                if self._frames and seq < self._frames[0].seq:
                    seq = self._frames[0].seq
                for frame in self._frames:
                    if frame.seq >= seq:
                        return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=remaining)
        return None

    def mark_handoff(self, seq: int, *, preroll_ms: float = 800.0) -> None:
        frames_back = int(preroll_ms / (self.chunk_samples / self.sample_rate * 1000.0))
        with self._condition:
            self._handoff_seq = max(0, int(seq) - max(0, frames_back))
            self._handoff_at = time.monotonic()
            logger.info("[MicInput] handoff marked seq=%s from=%s", seq, self._handoff_seq)

    def consume_handoff_frames(self, *, max_age_s: float = 3.0) -> list[MicFrame]:
        with self._condition:
            if self._handoff_seq is None:
                return []
            if time.monotonic() - self._handoff_at > max_age_s:
                self._handoff_seq = None
                return []
            start_seq = self._handoff_seq
            self._handoff_seq = None
            return [frame for frame in self._frames if frame.seq >= start_seq]

    def capture_utterance(
        self,
        *,
        vad_model,
        threshold: float,
        timeout_s: float,
        min_silence_ms: int,
        speech_pad_ms: int,
        min_speech_ms: int,
        max_speech_sec: float,
        preroll_ms: int,
        energy_end_rms: float = _ENERGY_END_RMS,
        energy_end_ms: int = _ENERGY_END_MS,
        handoff_max_capture_sec: float = _HANDOFF_MAX_CAPTURE_SEC,
        block_mic_fn: Callable[[], bool] | None = None,
        on_speech_start: Callable[[], None] | None = None,
        consume_handoff: bool = True,
        probable_end_silence_ms: int = 0,
        on_probable_end: Callable[[np.ndarray], None] | None = None,
        on_probable_end_cancelled: Callable[[], None] | None = None,
    ) -> np.ndarray | None:
        from silero_vad import VADIterator

        self.start()
        vad_iter = VADIterator(
            vad_model,
            threshold=float(threshold),
            sampling_rate=self.sample_rate,
            min_silence_duration_ms=int(min_silence_ms),
            speech_pad_ms=int(speech_pad_ms),
        )

        speech_chunks: list[np.ndarray] = []
        speech_raw_chunks: list[np.ndarray] = []
        speech_times: list[float] = []
        speech_started = False
        handoff_frames = self.consume_handoff_frames() if consume_handoff else []
        handoff_capture = bool(handoff_frames)
        if handoff_frames:
            speech_chunks = [frame.audio for frame in handoff_frames]
            speech_raw_chunks = [
                frame.raw_audio if frame.raw_audio is not None else frame.audio
                for frame in handoff_frames
            ]
            speech_times = [frame.timestamp for frame in handoff_frames]
            speech_started = True
            logger.info("[MicInput] ASR capture consumed handoff frames=%s", len(handoff_frames))

        preroll_chunks = max(1, int(preroll_ms / (self.chunk_samples / self.sample_rate * 1000.0)))
        chunk_ms = self.chunk_samples / self.sample_rate * 1000.0
        energy_end_chunks = max(1, int(max(1, int(energy_end_ms)) / chunk_ms))
        energy_end_rms = max(0.0, float(energy_end_rms))
        handoff_max_capture_sec = max(0.1, float(handoff_max_capture_sec))
        max_speech_sec = max(0.1, float(max_speech_sec))
        silence_chunks = 0
        # A handoff starts with buffered speech, so ``speech_started`` alone
        # cannot prove that this fresh Conversation VAD iterator owns the live
        # utterance.  Keep the short recovery watchdog until this iterator sees
        # its own start event.  Once it does, normal VAD/energy/max-speech
        # endpointing owns the utterance and the watchdog must not cut a user
        # off merely because they have spoken for five seconds.
        handoff_vad_owned = False
        # 两段式投机端点：短静音先行触发 on_probable_end(部分音频)，
        # 说话恢复则 on_probable_end_cancelled() 作废并重新武装。
        probable_end_chunks = (
            max(1, int(int(probable_end_silence_ms) / chunk_ms))
            if (on_probable_end is not None and probable_end_silence_ms > 0)
            else 0
        )
        probable_end_fired = False

        def _fire_probable_end() -> None:
            nonlocal probable_end_fired
            probable_end_fired = True
            if len(speech_chunks) * chunk_ms < min_speech_ms:
                return
            try:
                on_probable_end(np.concatenate(speech_chunks))
            except Exception:
                logger.debug("[MicInput] probable-end callback failed", exc_info=True)

        def _cancel_probable_end() -> None:
            nonlocal probable_end_fired
            probable_end_fired = False
            if on_probable_end_cancelled is None:
                return
            try:
                on_probable_end_cancelled()
            except Exception:
                logger.debug("[MicInput] probable-end cancel callback failed", exc_info=True)
        capture_started_at = time.monotonic() if speech_started else 0.0
        echo_dropped = False
        preroll_buf: deque[np.ndarray] = deque(maxlen=preroll_chunks)
        cursor = self.cursor()
        # ``timeout_s`` is the wait-for-speech-start deadline.  It must not
        # shorten an utterance that started late; active speech has its own
        # bounded ``max_speech_sec`` deadline below.
        speech_start_deadline = time.monotonic() + max(0.0, float(timeout_s))

        def finish_audio(reason: str) -> np.ndarray | None:
            nonlocal echo_dropped
            if not speech_chunks:
                return None
            total_ms = len(speech_chunks) * chunk_ms
            if total_ms < min_speech_ms:
                return None
            audio = np.concatenate(speech_chunks)
            if handoff_capture and speech_raw_chunks and speech_times:
                from asr.echo_guard import should_drop_handoff_candidate

                raw_audio = np.concatenate(speech_raw_chunks)
                decision = should_drop_handoff_candidate(
                    raw_mic=raw_audio,
                    residual=audio,
                    start_time=min(speech_times),
                    end_time=max(speech_times) + (self.chunk_samples / self.sample_rate),
                )
                if decision.drop:
                    echo_dropped = True
                    logger.info(
                        "[MicInput] ASR handoff dropped by echo guard reason=%s duration=%.0fms",
                        decision.reason,
                        total_ms,
                    )
                    return None
            logger.info(
                "[MicInput] ASR capture finish reason=%s duration=%.0fms chunks=%s handoff=%s",
                reason,
                total_ms,
                len(speech_chunks),
                handoff_capture,
            )
            return audio

        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if not speech_started and now >= speech_start_deadline:
                    break
                if (
                    speech_started
                    and capture_started_at > 0
                    and now - capture_started_at >= max_speech_sec
                ):
                    return finish_audio("max_speech")
                frame = cursor.read(timeout=0.25)
                if frame is None:
                    continue
                chunk_np = frame.audio
                block_mic = bool(block_mic_fn and block_mic_fn())
                if block_mic and not speech_started:
                    preroll_buf.clear()
                    vad_iter.reset_states()
                    continue
                if not speech_started:
                    preroll_buf.append(chunk_np)

                vad_out = vad_iter(torch.from_numpy(chunk_np), return_seconds=False)
                if vad_out is not None:
                    if "start" in vad_out:
                        if handoff_capture and not handoff_vad_owned:
                            handoff_vad_owned = True
                            logger.info("[MicInput] handoff Conversation VAD took ownership")
                        if not speech_started:
                            speech_started = True
                            speech_chunks = list(preroll_buf)
                            speech_raw_chunks = list(preroll_buf)
                            speech_times = []
                            capture_started_at = time.monotonic()
                            silence_chunks = 0
                            if on_speech_start is not None:
                                try:
                                    on_speech_start()
                                except Exception:
                                    logger.debug("[MicInput] speech-start callback failed", exc_info=True)
                    elif "end" in vad_out and speech_started:
                        audio = finish_audio("vad_end")
                        if echo_dropped:
                            return None
                        if audio is not None:
                            return audio
                        if probable_end_fired:
                            _cancel_probable_end()
                        speech_chunks = []
                        speech_raw_chunks = []
                        speech_times = []
                        speech_started = False
                        preroll_buf.clear()
                        capture_started_at = 0.0
                        silence_chunks = 0

                if speech_started:
                    speech_chunks.append(chunk_np)
                    speech_raw_chunks.append(frame.raw_audio if frame.raw_audio is not None else chunk_np)
                    speech_times.append(frame.timestamp)
                    if frame.rms <= energy_end_rms:
                        silence_chunks += 1
                        if (
                            probable_end_chunks
                            and not probable_end_fired
                            and silence_chunks >= probable_end_chunks
                        ):
                            _fire_probable_end()
                    else:
                        if probable_end_fired:
                            _cancel_probable_end()
                        silence_chunks = 0
                    if silence_chunks >= energy_end_chunks:
                        audio = finish_audio("energy_end")
                        if echo_dropped:
                            return None
                        if audio is not None:
                            return audio
                        if probable_end_fired:
                            _cancel_probable_end()
                        speech_chunks = []
                        speech_raw_chunks = []
                        speech_times = []
                        speech_started = False
                        capture_started_at = 0.0
                        silence_chunks = 0
                        preroll_buf.clear()
                        vad_iter.reset_states()
                        continue
                    if (
                        handoff_capture
                        and not handoff_vad_owned
                        and capture_started_at > 0
                        and time.monotonic() - capture_started_at >= handoff_max_capture_sec
                    ):
                        audio = finish_audio("handoff_max")
                        if echo_dropped:
                            return None
                        if audio is not None:
                            return audio
                    if len(speech_chunks) >= int(max_speech_sec * self.sample_rate / self.chunk_samples):
                        return finish_audio("max_speech")
        finally:
            vad_iter.reset_states()
        if speech_started and speech_chunks:
            return finish_audio("timeout")
        return None

    def _run(self) -> None:
        stream = None
        pa = None
        try:
            pa = pyaudio.PyAudio()
            logger.info("[MicInput] opening microphone stream index=%s", self._mic_index)
            stream, self._mic_index = open_input_stream_with_fallback(
                pa,
                preferred_index=self._mic_index,
                sample_rate=self.sample_rate,
                frames_per_buffer=self.chunk_samples,
                log_label="[MicInput]",
            )
            self._device = device_descriptor_for_index(self._mic_index, pa=pa)
            get_realtime_aec_processor().set_delay_for_device_class(
                self._device.device_class,
                reason=(
                    f"mic-input index={self._device.index} "
                    f"name={self._device.name!r} host={self._device.host_api!r}"
                ),
            )
            self._pa = pa
            self._stream = stream
            logger.info(
                "[MicInput] listening mic_index=%s device_class=%s",
                self._mic_index,
                self._device.device_class,
            )
            self._ready_event.set()
            with self._condition:
                self._condition.notify_all()
            while not self._stop_event.is_set():
                raw = stream.read(self.chunk_samples, exception_on_overflow=False)
                raw_chunk = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                chunk = raw_chunk
                aec = get_realtime_aec_processor()
                if aec.enabled:
                    chunk = aec.process_mic(chunk, self.sample_rate)
                rms = float(np.sqrt(np.mean(np.square(chunk))) if chunk.size else 0.0)
                with self._condition:
                    frame = MicFrame(self._seq, time.monotonic(), chunk, rms, raw_chunk)
                    self._frames.append(frame)
                    self._seq += 1
                    self._condition.notify_all()
        except Exception as exc:
            self._last_error = str(exc)
            logger.exception("[MicInput] service failed")
            self._ready_event.set()
            with self._condition:
                self._condition.notify_all()
        finally:
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
            self._stream = None
            self._pa = None
            self._ready_event.set()


_INSTANCE: MicInputService | None = None
_INSTANCE_LOCK = threading.Lock()


def get_mic_input_service() -> MicInputService:
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = MicInputService()
    return _INSTANCE


def close_mic_input_service() -> None:
    if _INSTANCE is not None:
        _INSTANCE.close()
