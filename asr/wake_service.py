"""Always-on wake word service.

The wake path is intentionally separate from ASRManager. It keeps only the
cheap microphone + VAD + lightweight ASR loop alive, then asks the regular ASR
handler to lazy-load the full recognizer after a wake phrase is detected.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Coroutine
from difflib import SequenceMatcher
from typing import Any

import numpy as np
import torch

from config.log_privacy import protected_text
from config.settings import (
    WAKE_ASR_BACKEND,
    WAKE_BRIDGE_AUTO_SEND,
    WAKE_BRIDGE_MAX_SECONDS,
    WAKE_DEBUG_AUDIO,
    WAKE_ENERGY_END_RMS,
    WAKE_ENERGY_FALLBACK,
    WAKE_ENERGY_START_RMS,
    WAKE_MATCH_THRESHOLD,
    WAKE_MIN_SEGMENT_RMS,
    WAKE_PHRASES,
    WAKE_SENSEVOICE_LANGUAGES,
    WAKE_TEMPLATE_CACHE_DIR,
    WAKE_TEMPLATE_CACHE_ENABLED,
    WAKE_TEMPLATE_CACHE_LEARN_THRESHOLD,
    WAKE_TEMPLATE_CACHE_MAX_MS,
    WAKE_TEMPLATE_CACHE_MAX_PER_DEVICE,
    WAKE_TEMPLATE_CACHE_MIN_MS,
    WAKE_TEMPLATE_CACHE_THRESHOLD,
    WAKE_VAD_THRESHOLD,
)
from asr.microphone import choose_microphone, configured_device_index
from asr.mic_input_service import get_mic_input_service
from asr.wake_template_cache import WakeTemplateCache, WakeTemplateMatch
from server.event_bus import bus
from server.protocol import Method
from tts.aec_realtime import get_realtime_aec_processor

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000
_CHUNK_SAMPLES = 512
_SILENCE_MS = 350
_MIN_WAKE_MS = 250
_MAX_WAKE_SEC = 3.0
_PREROLL_MS = 300
_PREROLL_CHUNKS = max(1, int(_PREROLL_MS / (_CHUNK_SAMPLES / _SAMPLE_RATE * 1000)))


def _normalise_text(value: str) -> str:
    text = str(value or "").strip().lower()
    for alias in ("i'm as", "im as", "i am as", "i'ms", "ims", "i'm mys", "im mys", "i am mys"):
        text = text.replace(alias, "amadeus")
    for alias in ("amadues", "amadius", "amadeous", "amadeos", "amadios", "armadeus"):
        text = text.replace(alias, "amadeus")
    text = text.replace("アマデウス", "amadeus")
    text = text.replace("阿马迪斯", "阿玛迪斯")
    text = text.replace("阿玛丢斯", "阿玛迪斯")
    text = text.replace("阿玛迪乌斯", "阿玛迪斯")
    text = text.replace("阿玛德斯", "阿玛迪斯")
    text = text.replace("嗨安", "hiamadeus")
    text = text.replace("嗨爱ad", "hiamadeus")
    text = text.replace("嗨艾ad", "hiamadeus")
    text = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
    return text


class WakeService:
    def __init__(
        self,
        *,
        on_wake: Callable[[dict[str, Any]], Coroutine[Any, Any, Any]] | None = None,
        on_awake_text: Callable[[dict[str, Any]], Coroutine[Any, Any, Any]] | None = None,
        tts_playing_fn: Callable[[], bool] | None = None,
        backend_name: str | None = None,
        phrases: str | list[str] | None = None,
        threshold: float | None = None,
    ) -> None:
        self._on_wake = on_wake
        self._on_awake_text = on_awake_text
        self._tts_playing_fn = tts_playing_fn
        self._backend_name = backend_name or WAKE_ASR_BACKEND
        self._sensevoice_languages = [
            item.strip()
            for item in str(WAKE_SENSEVOICE_LANGUAGES or "auto").split(",")
            if item.strip()
        ] or ["auto"]
        raw_phrases = phrases if phrases is not None else WAKE_PHRASES
        if isinstance(raw_phrases, str):
            self._phrases = [p.strip() for p in raw_phrases.split(",") if p.strip()]
        else:
            self._phrases = [str(p).strip() for p in raw_phrases if str(p).strip()]
        self._normalised_phrases = [_normalise_text(p) for p in self._phrases]
        self._threshold = float(WAKE_MATCH_THRESHOLD if threshold is None else threshold)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._backend = None
        self._vad_model = None
        self._mic_index = configured_device_index()
        self._status = "idle"
        self._last_error = ""
        self._last_text = ""
        self._last_wake_at = 0.0
        self._last_energy_emit_at = 0.0
        self._last_audio_log_at = 0.0
        self._bridge_until = 0.0
        self._bridge_wake_payload: dict[str, Any] = {}
        self._template_cache = (
            WakeTemplateCache(
                cache_dir=WAKE_TEMPLATE_CACHE_DIR,
                threshold=WAKE_TEMPLATE_CACHE_THRESHOLD,
                max_templates=WAKE_TEMPLATE_CACHE_MAX_PER_DEVICE,
                min_ms=WAKE_TEMPLATE_CACHE_MIN_MS,
                max_ms=WAKE_TEMPLATE_CACHE_MAX_MS,
            )
            if WAKE_TEMPLATE_CACHE_ENABLED
            else None
        )

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "running": self.running,
            "backend": self._backend_name,
            "phrases": list(self._phrases),
            "threshold": self._threshold,
            "mic_index": self._mic_index,
            "last_text": self._last_text,
            "last_wake_at": self._last_wake_at,
            "bridge_remaining": max(0.0, self._bridge_until - time.time()),
            "template_cache": bool(self._template_cache),
            "template_count": self._template_count(),
            "error": self._last_error,
        }

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> dict[str, Any]:
        if self._thread is not None and not self._thread.is_alive():
            self._thread = None
        if self.running:
            return self.status() | {"status": "already_running"}
        self._loop = loop or asyncio.get_event_loop()
        self._stop_event.clear()
        self._last_error = ""
        self._thread = threading.Thread(target=self._run, name="wake-service", daemon=True)
        self._thread.start()
        self._status = "starting"
        self._emit(Method.WAKE_STATUS, self.status())
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if thread is None or not thread.is_alive() or thread is threading.current_thread():
            self._thread = None
            self._status = "idle"
        else:
            self._status = "stopping"
        self._bridge_until = 0.0
        self._bridge_wake_payload = {}
        self._emit(Method.WAKE_STATUS, self.status())
        return self.status()

    def close(self) -> None:
        self.stop()
        backend = self._backend
        self._backend = None
        close = getattr(backend, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _emit(self, method: str, params: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(bus.emit(method, params), loop)

    def _run_coro(self, coro: Coroutine[Any, Any, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(coro, loop)

    def _template_device_key(self) -> str:
        return f"mic_{self._mic_index if self._mic_index is not None else 'auto'}"

    def _template_count(self) -> int:
        cache = self._template_cache
        if cache is None:
            return 0
        try:
            return cache.template_count(self._template_device_key())
        except Exception:
            return 0

    def _load_runtime(self) -> None:
        if self._vad_model is not None and self._backend is not None:
            logger.info("[Wake] runtime already loaded; reusing backend=%s", self._backend_name)
            self._pick_microphone_index()
            return

        logger.info("[Wake] loading runtime backend=%s phrases=%s", self._backend_name, self._phrases)
        if self._vad_model is None:
            from silero_vad import load_silero_vad

            self._vad_model = load_silero_vad()
            self._vad_model.eval()
        if self._backend is None:
            from asr.registry import create_asr_backend

            backend = create_asr_backend(self._backend_name)
            backend.load("cpu")
            self._backend = backend
        self._pick_microphone_index()

    def _pick_microphone_index(self) -> None:
        configured_index = configured_device_index()
        if configured_index is not None:
            self._mic_index = configured_index
            logger.info("[Wake] using configured microphone index=%s", self._mic_index)
            return

        chosen = choose_microphone()
        self._mic_index = chosen.index if chosen is not None else None
        logger.info(
            "[Wake] selected microphone index=%s name=%s rms=%s",
            self._mic_index,
            chosen.name if chosen is not None else "",
            chosen.rms if chosen is not None else -1,
        )

    def _run(self) -> None:
        try:
            self._status = "loading"
            self._emit(Method.WAKE_STATUS, self.status())
            self._load_runtime()

            from silero_vad import VADIterator

            vad_iter = VADIterator(
                self._vad_model,
                threshold=WAKE_VAD_THRESHOLD,
                sampling_rate=_SAMPLE_RATE,
                min_silence_duration_ms=_SILENCE_MS,
                speech_pad_ms=60,
            )
            mic_service = get_mic_input_service()
            logger.info("[Wake] starting shared microphone stream preferred_index=%s", self._mic_index)
            mic_service.start(preferred_index=self._mic_index)
            cursor = mic_service.cursor()
            self._mic_index = mic_service.mic_index
            self._status = "listening"
            self._emit(Method.WAKE_STATUS, self.status())
            logger.info("[Wake] listening shared_mic_index=%s vad_threshold=%.2f", self._mic_index, WAKE_VAD_THRESHOLD)
            last_logged_mic_index = self._mic_index

            speech_chunks: list[np.ndarray] = []
            preroll_buf: deque[np.ndarray] = deque(maxlen=_PREROLL_CHUNKS)
            speech_started = False
            silence_chunks = 0
            wait_silence_after_forced_segment = False
            energy_end_chunks = max(1, int(_SILENCE_MS / (_CHUNK_SAMPLES / _SAMPLE_RATE * 1000)))

            while not self._stop_event.is_set():
                if self._bridge_until and time.time() >= self._bridge_until:
                    logger.info("[Wake] SenseVoice bridge expired; returning to wake listening")
                    self._bridge_until = 0.0
                    self._bridge_wake_payload = {}
                    self._status = "listening"
                    self._emit(Method.WAKE_STATUS, self.status())

                frame = cursor.read(timeout=0.25)
                if frame is None:
                    continue
                self._mic_index = mic_service.mic_index
                if self._mic_index != last_logged_mic_index:
                    last_logged_mic_index = self._mic_index
                    logger.info("[Wake] shared microphone actual_index=%s", self._mic_index)
                chunk = frame.audio
                aec_processor = get_realtime_aec_processor()
                rms = frame.rms
                self._emit_energy(rms)
                self._log_audio_rms(rms)

                if (
                    self._tts_playing_fn is not None
                    and self._tts_playing_fn()
                    and not aec_processor.barge_in_enabled
                ):
                    preroll_buf.clear()
                    speech_chunks = []
                    speech_started = False
                    silence_chunks = 0
                    wait_silence_after_forced_segment = False
                    vad_iter.reset_states()
                    continue

                if wait_silence_after_forced_segment:
                    if rms <= WAKE_ENERGY_END_RMS:
                        silence_chunks += 1
                    else:
                        silence_chunks = 0
                    if silence_chunks >= energy_end_chunks:
                        wait_silence_after_forced_segment = False
                        silence_chunks = 0
                        preroll_buf.clear()
                        vad_iter.reset_states()
                        logger.info("[Wake] silence reset after forced segment")
                    continue

                chunk_t = torch.from_numpy(chunk)
                if not speech_started:
                    preroll_buf.append(chunk)
                vad_out = vad_iter(chunk_t, return_seconds=False)

                if vad_out is not None:
                    if "start" in vad_out:
                        speech_started = True
                        speech_chunks = list(preroll_buf)
                        silence_chunks = 0
                        logger.info("[Wake] VAD speech start rms=%.4f", rms)
                    elif "end" in vad_out:
                        if speech_started and speech_chunks:
                            self._handle_segment(np.concatenate(speech_chunks))
                        speech_started = False
                        speech_chunks = []
                        silence_chunks = 0
                        logger.info("[Wake] VAD speech end")

                if WAKE_ENERGY_FALLBACK and vad_out is None:
                    if not speech_started and rms >= WAKE_ENERGY_START_RMS:
                        speech_started = True
                        speech_chunks = list(preroll_buf)
                        silence_chunks = 0
                        logger.info("[Wake] energy speech start rms=%.4f", rms)
                    elif speech_started:
                        if rms <= WAKE_ENERGY_END_RMS:
                            silence_chunks += 1
                        else:
                            silence_chunks = 0
                        if silence_chunks >= energy_end_chunks:
                            if speech_chunks:
                                self._handle_segment(np.concatenate(speech_chunks))
                            speech_started = False
                            speech_chunks = []
                            silence_chunks = 0
                            vad_iter.reset_states()
                            logger.info("[Wake] energy speech end")

                if speech_started:
                    speech_chunks.append(chunk)
                    max_chunks = int(_MAX_WAKE_SEC * _SAMPLE_RATE / _CHUNK_SAMPLES)
                    if len(speech_chunks) >= max_chunks:
                        self._handle_segment(np.concatenate(speech_chunks))
                        speech_started = False
                        speech_chunks = []
                        silence_chunks = 0
                        wait_silence_after_forced_segment = True
                        vad_iter.reset_states()
        except Exception as exc:
            self._last_error = str(exc)
            self._status = "error"
            logger.exception("wake service failed")
            self._emit(Method.WAKE_STATUS, self.status())
        finally:
            if self._thread is threading.current_thread():
                self._thread = None
            if self._status != "error":
                self._status = "idle"
            self._emit(Method.WAKE_STATUS, self.status())

    def _log_audio_rms(self, rms: float) -> None:
        if not WAKE_DEBUG_AUDIO:
            return
        now = time.time()
        if now - self._last_audio_log_at < 1.0:
            return
        self._last_audio_log_at = now
        logger.info("[Wake] audio rms=%.4f", rms)

    def _emit_energy(self, rms: float) -> None:
        now = time.time()
        if now - self._last_energy_emit_at < 0.25:
            return
        self._last_energy_emit_at = now
        self._emit(
            Method.VAD_ENERGY,
            {
                "source": "wake",
                "rms": rms,
                "level": min(1.0, rms / 0.08),
                "timestamp": now,
            },
        )

    def _handle_segment(self, audio: np.ndarray) -> None:
        duration_ms = len(audio) / _SAMPLE_RATE * 1000
        if duration_ms < _MIN_WAKE_MS:
            return
        rms = float(np.sqrt(np.mean(np.square(audio))) if audio.size else 0.0)
        if rms < WAKE_MIN_SEGMENT_RMS:
            logger.info("[Wake] ignored low-energy segment duration=%.0fms rms=%.4f", duration_ms, rms)
            return
        template_result = self._match_template(audio, duration_ms)
        template_fast_accept = bool(
            template_result
            and template_result.matched
            and duration_ms <= min(float(WAKE_TEMPLATE_CACHE_MAX_MS), 1800.0)
        )
        source = "sense_voice"
        if template_fast_accept and template_result is not None:
            text, matched, phrase, score, language = (
                "hi amadeus",
                True,
                "wake_template",
                template_result.score,
                "template",
            )
            source = "wake_template"
        else:
            backend = self._backend
            if backend is None:
                return
            text, matched, phrase, score, language = self._recognize_and_match(audio, backend)
            if not matched and template_result is not None and template_result.matched:
                text, matched, phrase, score, language = (
                    "hi amadeus",
                    True,
                    "wake_template",
                    template_result.score,
                    "template",
                )
                source = "wake_template"
        self._last_text = text
        logger.info(
            "[Wake] segment duration=%.0fms rms=%.4f lang=%s source=%s text=%r matched=%s phrase=%r score=%.3f",
            duration_ms,
            rms,
            language,
            source,
            text,
            matched,
            phrase,
            score,
        )
        if not matched:
            if self._bridge_until and time.time() < self._bridge_until and text.strip():
                if not WAKE_BRIDGE_AUTO_SEND:
                    logger.info(
                        "[Wake] SenseVoice bridge heard but auto-send disabled: text=%r score=%.3f",
                        text.strip(),
                        score,
                    )
                    self._emit(
                        Method.WAKE_STATUS,
                        self.status()
                        | {
                            "heard": text,
                            "matched": False,
                            "score": score,
                            "bridge": True,
                            "bridge_auto_send": False,
                        },
                    )
                    return
                payload = {
                    "text": text.strip(),
                    "is_final": True,
                    "source": "wake",
                    "bridge": True,
                    "wake": self._bridge_wake_payload,
                    "timestamp": time.time(),
                }
                logger.info(
                    "[Wake] SenseVoice bridge command: %s",
                    protected_text(payload["text"]),
                )
                if self._on_awake_text is not None:
                    self._run_coro(self._on_awake_text(payload))
                return
            self._emit(
                Method.WAKE_STATUS,
                self.status() | {"heard": text, "matched": False, "score": score},
            )
            return

        payload = {
            "text": text,
            "phrase": phrase,
            "score": score,
            "source": source,
            "timestamp": time.time(),
        }
        if source != "wake_template":
            self._remember_template(audio, score=score, text=text, phrase=phrase)
        command_text = self._extract_inline_command(text, phrase)
        if command_text:
            payload["command_text"] = command_text
        self._last_wake_at = payload["timestamp"]
        self._status = "awake_bridge"
        self._bridge_until = time.time() + max(1.0, float(WAKE_BRIDGE_MAX_SECONDS))
        self._bridge_wake_payload = dict(payload)
        logger.info("[Wake] entering SenseVoice bridge until awake ASR is ready")
        self._emit(Method.WAKE_DETECTED, payload)
        self._emit(Method.WAKE_STATUS, self.status() | {"heard": text, "matched": True})
        if self._on_wake is not None:
            self._run_coro(self._on_wake(payload))

    def _match_template(self, audio: np.ndarray, duration_ms: float) -> WakeTemplateMatch | None:
        cache = self._template_cache
        if cache is None:
            return None
        try:
            result = cache.match(audio, _SAMPLE_RATE, device_key=self._template_device_key())
        except Exception:
            logger.exception("[WakeTemplate] match failed")
            return None
        if result.template_count <= 0:
            return result
        logger.info(
            "[WakeTemplate] duration=%.0fms templates=%s matched=%s score=%.3f distance=%.4f",
            duration_ms,
            result.template_count,
            result.matched,
            result.score,
            result.distance,
        )
        self._emit(
            Method.WAKE_STATUS,
            self.status()
            | {
                "template_score": result.score,
                "template_matched": result.matched,
                "template_count": result.template_count,
            },
        )
        return result

    def _remember_template(self, audio: np.ndarray, *, score: float, text: str, phrase: str) -> None:
        cache = self._template_cache
        if cache is None or score < WAKE_TEMPLATE_CACHE_LEARN_THRESHOLD:
            return
        try:
            count = cache.add_positive(audio, _SAMPLE_RATE, device_key=self._template_device_key())
        except Exception:
            logger.exception(
                "[WakeTemplate] learn failed text=%s phrase=%s",
                protected_text(text),
                protected_text(phrase),
            )
            return
        logger.info(
            "[WakeTemplate] learned positive template device=%s count=%s score=%.3f text=%r phrase=%r",
            self._template_device_key(),
            count,
            score,
            text,
            phrase,
        )

    def _recognize_and_match(self, audio: np.ndarray, backend) -> tuple[str, bool, str, float, str]:
        transcribe_lang = getattr(backend, "transcribe_with_language", None)
        best_text = ""
        best_phrase = ""
        best_score = 0.0
        best_language = ""
        command_text = ""
        command_language = ""
        command_quality = -1.0
        languages = self._sensevoice_languages if callable(transcribe_lang) else ["auto"]
        for language in languages:
            if callable(transcribe_lang):
                text = transcribe_lang(audio, _SAMPLE_RATE, context="", language=language) or ""
            else:
                text = backend.transcribe(audio, _SAMPLE_RATE, context="") or ""
            matched, phrase, score = self._match(text)
            logger.info(
                "[Wake] candidate lang=%s text=%s matched=%s score=%.3f",
                language,
                protected_text(text),
                matched,
                score,
            )
            if matched:
                if self._extract_inline_command(text, phrase):
                    return text, True, phrase, score, language
                if not best_text:
                    best_text, best_phrase, best_score, best_language = text, phrase, score, language
                return text, True, phrase, score, language
            if score > best_score:
                best_text, best_phrase, best_score, best_language = text, phrase, score, language
            quality = self._command_candidate_quality(text, language)
            if quality > command_quality:
                command_text, command_language, command_quality = text, language, quality
        if command_text:
            return command_text, False, best_phrase, best_score, command_language
        return best_text, False, best_phrase, best_score, best_language

    def _command_candidate_quality(self, text: str, language: str) -> float:
        raw = str(text or "").strip()
        norm = _normalise_text(raw)
        if not norm:
            return -1.0
        cjk_count = sum(1 for ch in raw if "\u4e00" <= ch <= "\u9fff")
        ascii_count = sum(1 for ch in raw if ch.isascii() and ch.isalpha())
        quality = min(len(norm), 24) / 24.0
        if cjk_count:
            quality += 1.0 + min(cjk_count, 16) / 16.0
        if language == "zh":
            quality += 0.35
        elif language == "auto":
            quality += 0.15
        elif language == "en":
            quality -= 0.1
        if ascii_count and len(norm) <= 4:
            quality -= 0.8
        return quality

    def _match(self, text: str) -> tuple[bool, str, float]:
        heard = _normalise_text(text)
        best_phrase = ""
        best_score = 0.0
        if not heard:
            return False, "", 0.0
        if heard in {"hi", "hey", "hello", "high", "嗨", "嘿", "你好"}:
            return False, "", 0.0
        for phrase, norm_phrase in zip(self._phrases, self._normalised_phrases):
            if not norm_phrase:
                continue
            if norm_phrase in heard:
                return True, phrase, 1.0
            if heard in norm_phrase and len(heard) >= max(6, int(len(norm_phrase) * 0.65)):
                return True, phrase, 1.0
            score = SequenceMatcher(None, heard, norm_phrase).ratio()
            if score > best_score:
                best_score = score
                best_phrase = phrase
        return best_score >= self._threshold, best_phrase, best_score

    def _extract_inline_command(self, text: str, phrase: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        patterns = [
            r"^\s*(hi|hey|hello|high)\s*(amadeus|amadues|amadius|amadeous|amadeos|amadios|armadeus)\s*[,，。.!！?？\s]*(.+)$",
            r"^\s*(hi|hey|hello|high)\s*(i'?m\s*(as|mys)|i\s*am\s*(as|mys)|ims|i'?ms)\s*[,，。.!！?？\s]*(.+)$",
            r"^\s*(嗨|嘿|你好)\s*(阿玛迪斯|阿马迪斯|阿玛丢斯|阿玛迪乌斯|阿玛德斯)\s*[,，。.!！?？\s]*(.+)$",
        ]
        for pattern in patterns:
            match = re.match(pattern, raw, flags=re.IGNORECASE)
            if not match:
                continue
            tail = str(match.group(match.lastindex) or "").strip(" ,，。.!！?？\t\r\n")
            if len(_normalise_text(tail)) >= 2:
                return tail
        return ""
