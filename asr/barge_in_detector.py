"""Lightweight AEC + VAD barge-in detector.

This deliberately avoids running the full ASR recognizer while TTS is playing.
It only decides whether the user has started speaking over playback; once that
happens, the server interrupts playback and lets the regular ASR flow take over.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any

import numpy as np

# NOTE: asr.mic_input_service is imported lazily inside _run() — it pulls
# voice-tier deps (pyaudio) and must not break module import in audio-less
# installs (this module is imported from backend bootstrap).
from config.settings import (
    BARGE_IN_ECHO_CONFIRM_MS,
    BARGE_IN_MIN_RMS,
    BARGE_IN_START_DELAY_MS,
    BARGE_IN_VAD_THRESHOLD,
)

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000
_TTS_IDLE_GRACE_SECONDS = 0.9
_RAW_FALLBACK_MIN_MULTIPLIER = 1.8
_RAW_FALLBACK_RESIDUAL_RATIO = 0.32
_ECHO_SUPPRESSION_RATIO = 0.24
_PRE_INTERRUPT_CONFIRM_MS = max(64.0, float(BARGE_IN_ECHO_CONFIRM_MS))


def _rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(audio))) if audio.size else 0.0)


class BargeInDetector:
    def __init__(
        self,
        *,
        tts_playing_fn: Callable[[], bool],
        on_barge_in: Callable[[], Coroutine[Any, Any, Any]],
        on_debug: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self._tts_playing_fn = tts_playing_fn
        self._on_barge_in = on_barge_in
        self._on_debug = on_debug
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._vad_model = None
        self._vad_capability: tuple[str, str] | None = None
        self._triggered = False
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        with self._lock:
            if self.running:
                return
            self._loop = loop or asyncio.get_event_loop()
            self._triggered = False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="barge-in-detector",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        if thread is None or not thread.is_alive() or thread is threading.current_thread():
            self._thread = None

    def _is_tts_playing(self) -> bool:
        try:
            return bool(self._tts_playing_fn())
        except Exception:
            return False

    def _ensure_vad(self):
        if self._vad_model is None:
            from silero_vad import load_silero_vad

            self._vad_model = load_silero_vad()
            self._vad_model.eval()
        return self._vad_model

    def vad_status(self) -> tuple[str, str]:
        """Three-state VAD capability fact: ("ready" | "fallback" | "degraded", reason).

        The barge-in thread depends on silero-vad/torch (the L3 voice tier),
        so the startup boundary must consult this before spawning the thread:
        on an L2 install a config-only gate would start a doomed thread that
        reports barge_in_error on every sentence. Absent dependency ->
        documented fallback; installed but broken -> degraded with the reason
        kept observable (never disguised as absence).

        Probing is lazy (never in __init__: L1/L2 bootstrap must stay
        importable) and happens exactly once per detector instance — later
        playback sentences re-read the cache instead of re-failing. This
        detector owns its own VAD model (see _ensure_vad) and is
        intentionally independent of the ASRManager lazy lifecycle, whose
        manager is only created when ASR first listens.
        """
        # Called from the event-loop thread only (startup boundary); no lock.
        if self._vad_capability is None:
            self._vad_capability = self._probe_vad_capability()
        return self._vad_capability

    def _probe_vad_capability(self) -> tuple[str, str]:
        try:
            from silero_vad import load_silero_vad
        except ModuleNotFoundError as exc:
            if exc.name == "silero_vad":
                # Absent dependency: documented L2 fallback.
                return "fallback", ""
            # silero-vad present but a dependency (e.g. torch) missing:
            # a broken install must stay observable, not fake absence.
            return "degraded", f"missing dependency {exc.name}"
        except Exception as exc:  # e.g. torch DLL/ABI failure raised at import
            return "degraded", f"{type(exc).__name__}: {exc}"
        try:
            model = load_silero_vad()
            model.eval()
        except Exception as exc:
            return "degraded", f"{type(exc).__name__}: {exc}"
        # Cache the loaded model so _ensure_vad() in the detector thread
        # reuses it instead of loading a second copy.
        self._vad_model = model
        return "ready", ""

    def _emit_debug(self, **payload: Any) -> None:
        callback = self._on_debug
        loop = self._loop
        if callback is None or loop is None or loop.is_closed():
            return

        def _run_callback() -> None:
            try:
                result = callback(payload)
                if hasattr(result, "__await__"):
                    asyncio.create_task(result)
            except Exception:
                logger.exception("[BargeIn] debug callback failed")

        loop.call_soon_threadsafe(_run_callback)

    def _collect_confirm_frames(self, cursor, first_frame) -> list:
        frame_ms = first_frame.audio.size / float(_SAMPLE_RATE) * 1000.0
        target = max(1, int(round(_PRE_INTERRUPT_CONFIRM_MS / max(frame_ms, 1.0))))
        frames = [first_frame]
        deadline = time.monotonic() + (_PRE_INTERRUPT_CONFIRM_MS / 1000.0) + 0.12
        while len(frames) < target and time.monotonic() < deadline and not self._stop_event.is_set():
            frame = cursor.read(timeout=0.06)
            if frame is not None:
                frames.append(frame)
        return frames

    def _is_self_echo_candidate(self, frames: list) -> bool:
        if not frames:
            return False
        from asr.echo_guard import should_suppress_barge_in_candidate

        residual = np.concatenate([frame.audio for frame in frames])
        raw = np.concatenate([
            frame.raw_audio if frame.raw_audio is not None else frame.audio
            for frame in frames
        ])
        decision = should_suppress_barge_in_candidate(
            raw_mic=raw,
            residual=residual,
            start_time=frames[0].timestamp,
            end_time=frames[-1].timestamp + (frames[-1].audio.size / float(_SAMPLE_RATE)),
        )
        return bool(decision.drop)

    def _schedule_barge_in(self) -> None:
        if self._triggered:
            return
        self._triggered = True
        self._stop_event.set()
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._on_barge_in(), loop)

    def _run(self) -> None:
        try:
            delay = max(0.0, float(BARGE_IN_START_DELAY_MS) / 1000.0)
            if delay:
                time.sleep(delay)
            if self._stop_event.is_set() or not self._is_tts_playing():
                return

            from silero_vad import VADIterator
            # torch is a voice-tier (T2) dependency; the barge-in VAD loop only
            # runs after start() in a voice-capable environment.
            import torch

            from asr.mic_input_service import get_mic_input_service

            vad_iter = VADIterator(
                self._ensure_vad(),
                threshold=float(BARGE_IN_VAD_THRESHOLD),
                sampling_rate=_SAMPLE_RATE,
                min_silence_duration_ms=180,
                speech_pad_ms=20,
            )
            mic_service = get_mic_input_service()
            mic_service.start()
            cursor = mic_service.cursor()
            logger.info(
                "[BargeIn] listening shared_mic_index=%s threshold=%.2f min_rms=%.4f",
                mic_service.mic_index,
                float(BARGE_IN_VAD_THRESHOLD),
                float(BARGE_IN_MIN_RMS),
            )
            self._emit_debug(
                status="barge_in_listening",
                mic_index=mic_service.mic_index,
                threshold=float(BARGE_IN_VAD_THRESHOLD),
                min_rms=float(BARGE_IN_MIN_RMS),
            )
            last_tts_seen = time.monotonic()
            while not self._stop_event.is_set():
                if self._is_tts_playing():
                    last_tts_seen = time.monotonic()
                elif time.monotonic() - last_tts_seen > _TTS_IDLE_GRACE_SECONDS:
                    self._emit_debug(status="barge_in_tts_idle")
                    return
                frame = cursor.read(timeout=0.25)
                if frame is None:
                    continue
                residual = frame.audio
                raw = frame.raw_audio if frame.raw_audio is not None else residual
                raw_rms = _rms(raw)
                residual_rms = frame.rms
                min_rms = float(BARGE_IN_MIN_RMS)
                if max(raw_rms, residual_rms) < min_rms:
                    continue

                residual_ratio = residual_rms / max(raw_rms, 1e-6)
                if raw_rms >= min_rms * _RAW_FALLBACK_MIN_MULTIPLIER and residual_ratio < _ECHO_SUPPRESSION_RATIO:
                    continue

                # Prefer AEC residual for the actual VAD decision. Raw mic is
                # only a fallback when the residual still carries enough near-end
                # energy; otherwise loudspeaker echo would stop playback.
                if residual_rms >= min_rms:
                    chunk = residual
                    source = "residual"
                elif (
                    raw_rms >= min_rms * _RAW_FALLBACK_MIN_MULTIPLIER
                    and residual_ratio >= _RAW_FALLBACK_RESIDUAL_RATIO
                ):
                    chunk = raw
                    source = "raw_fallback"
                else:
                    continue
                vad_out = vad_iter(torch.from_numpy(chunk), return_seconds=False)
                if vad_out is not None and "start" in vad_out:
                    confirm_frames = self._collect_confirm_frames(cursor, frame)
                    if self._is_self_echo_candidate(confirm_frames):
                        logger.info(
                            "[BargeIn] suppress self-echo candidate source=%s raw_rms=%.4f "
                            "residual_rms=%.4f residual_ratio=%.3f",
                            source,
                            raw_rms,
                            residual_rms,
                            residual_ratio,
                        )
                        self._emit_debug(
                            status="barge_in_suppressed",
                            source=source,
                            raw_rms=raw_rms,
                            residual_rms=residual_rms,
                            residual_ratio=residual_ratio,
                        )
                        vad_iter.reset_states()
                        continue
                    logger.info(
                        "[BargeIn] speech start source=%s raw_rms=%.4f residual_rms=%.4f residual_ratio=%.3f",
                        source,
                        raw_rms,
                        residual_rms,
                        residual_ratio,
                    )
                    self._emit_debug(
                        status="barge_in_detected",
                        source=source,
                        raw_rms=raw_rms,
                        residual_rms=residual_rms,
                        residual_ratio=residual_ratio,
                    )
                    mic_service.mark_handoff(frame.seq, preroll_ms=900.0)
                    self._schedule_barge_in()
                    return
        except Exception:
            logger.exception("[BargeIn] detector failed")
            self._emit_debug(status="barge_in_error")
