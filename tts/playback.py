"""
音频播放引擎
- StreamPlayer：底层音频流 + 口型同步（pyaudio）
- PlaybackManager：基于句子序号的顺序播放调度器
- StreamPlayerWithBuffer：带缓冲区 + 字幕集成的播放器（包含子类扩展）

注意：StreamPlayerWithBuffer 中的字幕回调通过 SubtitleHooks dataclass 注入，
避免直接引用 main.py 全局变量。
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Coroutine, Iterable

import numpy as np
import pyaudio
import torch

from config.log_privacy import protected_text
from tools.text_utils import _parse_sentence_seq
from config.settings import USE_FIRST_SENTENCE_SPRINT
from tts.aec_debug_capture import get_aec_debug_capture
from tts.aec_realtime import get_realtime_aec_processor
from tts.latency_clock import log_latency_marker
from tts.mouth_signal import MouthSignalSink

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 字幕回调容器（依赖注入，替代直接引用 main.py 全局）
# ---------------------------------------------------------------------------
@dataclass
class SubtitleHooks:
    """
    StreamPlayerWithBuffer 所需的字幕 / 翻译回调。
    main.py 在构造 StreamPlayerWithBuffer 时传入。
    """
    # async fn(sentence_id, japanese_text) -> None
    check_and_display_pre_translation: Callable[..., Coroutine] | None = None
    # async fn(sentence_id, japanese_text, chinese_text) -> None
    display_chinese_subtitle_with_text: Callable[..., Coroutine] | None = None
    # async fn(japanese_text) -> dict | None  (pre_translation_cache.get_translation)
    get_translation: Callable[..., Coroutine] | None = None
    # async context manager lock for pre_translation_cache.cache access
    cache_lock: asyncio.Lock | None = None
    # ref to cache dict itself (for fuzzy match)
    cache_ref: dict | None = None
    # sync fn(japanese_text, chinese_text) -> None
    update_subtitle_display: Callable[[str, str], None] | None = None
    # bool: whether subtitle window is available
    subtitle_available: bool = False


# ---------------------------------------------------------------------------
# StreamPlayer
# ---------------------------------------------------------------------------
class StreamPlayer:
    def __init__(self, mouth_sink: MouthSignalSink):
        self.mouth_sink = mouth_sink
        self.chunk_size = 512
        self.volume_multiplier = 3.75
        self.send_interval = 0.05
        self.is_playing = False
        self.pyaudio_instance = None
        self.stream = None
        self.last_send_time = 0
        self._stream_lock = threading.RLock()
        self._audio_write_queue: queue.Queue = queue.Queue()
        self._audio_writer_stop = threading.Event()
        self._audio_writer_thread: threading.Thread | None = None

    def _ensure_audio_writer(self) -> None:
        if self._audio_writer_thread is not None and self._audio_writer_thread.is_alive():
            return
        self._audio_writer_stop.clear()
        self._audio_writer_thread = threading.Thread(
            target=self._audio_writer_loop,
            name="tts-audio-writer",
            daemon=True,
        )
        self._audio_writer_thread.start()

    def _audio_writer_loop(self) -> None:
        while True:
            job = self._audio_write_queue.get()
            if job is None:
                break

            mouth_segments = None
            if len(job) == 7:
                data, loop, future, before_write, after_write, is_current, mouth_segments = job
            elif len(job) == 6:
                data, loop, future, before_write, after_write, is_current = job
            else:
                data, loop, future, before_write, after_write = job
                is_current = None
            error = None
            try:
                if is_current is None or is_current():
                    if before_write is not None:
                        before_write()
                if mouth_segments is None:
                    mouth_segments = ((data, None),)
                for segment_data, mouth_value in mouth_segments:
                    if is_current is not None and not is_current():
                        break
                    if mouth_value is not None:
                        self.mouth_sink.publish_mouth_value(float(mouth_value))
                        self.last_send_time = time.time()
                    with self._stream_lock:
                        if is_current is not None and not is_current():
                            break
                        if self.stream is None or not self.is_playing:
                            raise RuntimeError("audio stream is not initialized")
                        self.stream.write(segment_data)
                if is_current is None or is_current():
                    if after_write is not None:
                        after_write()
            except Exception as exc:
                error = exc

            if future is not None and loop is not None and not loop.is_closed():
                if error is None:
                    loop.call_soon_threadsafe(
                        lambda fut=future: None if fut.cancelled() else fut.set_result(None)
                    )
                else:
                    loop.call_soon_threadsafe(
                        lambda fut=future, err=error: None if fut.cancelled() else fut.set_exception(err)
                    )

    async def write_audio_async(
        self,
        audio_chunk,
        loop: asyncio.AbstractEventLoop | None = None,
        before_write: Callable[[], None] | None = None,
        after_write: Callable[[], None] | None = None,
        is_current: Callable[[], bool] | None = None,
        mouth_envelope: bool = False,
        sample_rate: int | None = None,
        first_mouth_minimum: float | None = None,
    ) -> None:
        if loop is None:
            loop = asyncio.get_running_loop()
        if torch.is_tensor(audio_chunk):
            audio_chunk = audio_chunk.cpu().detach().numpy()
        if audio_chunk.dtype != np.float32:
            audio_chunk = audio_chunk.astype(np.float32)
        mouth_segments = None
        if mouth_envelope:
            rate = int(sample_rate or getattr(self, "_current_rate", None) or 24000)
            window_samples = max(self.chunk_size, int(round(rate * self.send_interval)))
            mouth_segments = []
            for offset in range(0, len(audio_chunk), window_samples):
                segment = audio_chunk[offset : offset + window_samples]
                if len(segment) == 0:
                    continue
                minimum = first_mouth_minimum if offset == 0 else None
                mouth_segments.append(
                    (segment.tobytes(), self._mouth_value_for_audio(segment, minimum=minimum))
                )
        self._ensure_audio_writer()
        future = loop.create_future()
        self._audio_write_queue.put(
            (
                audio_chunk.tobytes(),
                loop,
                future,
                before_write,
                after_write,
                is_current,
                mouth_segments,
            )
        )
        await future

    def _stop_audio_writer(self) -> None:
        self._audio_writer_stop.set()
        if self._audio_writer_thread is not None and self._audio_writer_thread.is_alive():
            self._audio_write_queue.put(None)
            self._audio_writer_thread.join(timeout=1.0)
        self._audio_writer_thread = None

    def _mouth_value_for_audio(self, audio_chunk, minimum: float | None = None) -> float:
        if audio_chunk is None:
            return 0.0
        if torch.is_tensor(audio_chunk):
            audio_chunk = audio_chunk.cpu().detach().numpy()
        audio_chunk = np.asarray(audio_chunk)
        if audio_chunk.size == 0:
            return 0.0
        if audio_chunk.dtype != np.float32:
            audio_chunk = audio_chunk.astype(np.float32)

        rms = float(np.sqrt(np.mean(audio_chunk ** 2)))
        mouth_value = min(1.0, rms * self.volume_multiplier)
        if minimum is not None and mouth_value > 0.0:
            mouth_value = max(float(minimum), mouth_value)
        return mouth_value

    def _emit_mouth_value_for_audio(self, audio_chunk, minimum: float | None = None) -> float:
        mouth_value = self._mouth_value_for_audio(audio_chunk, minimum=minimum)
        self.mouth_sink.publish_mouth_value(mouth_value)
        self.last_send_time = time.time()
        return mouth_value

    def initialize(self, sample_rate: int) -> None:
        if self.pyaudio_instance is None:
            self.pyaudio_instance = pyaudio.PyAudio()

        current_rate = getattr(self, "_current_rate", None)
        if self.stream is not None and self.is_playing and current_rate == sample_rate:
            self.last_send_time = time.time()
            return

        if self.stream is not None:
            self.stop()

        with self._stream_lock:
            self.stream = self.pyaudio_instance.open(
                format=pyaudio.paFloat32,
                channels=1,
                rate=sample_rate,
                output=True,
            )
        self._current_rate = sample_rate
        self.is_playing = True
        self.last_send_time = time.time()

    def prewarm(self, sample_rate: int, silence_ms: int = 80) -> bool:
        """Open the output stream early and keep the device path warm."""
        try:
            self.initialize(sample_rate)
            self._ensure_audio_writer()
            if self.stream is None:
                return False

            silence_ms = max(0, int(silence_ms))
            if silence_ms:
                n_samples = max(1, int(sample_rate * silence_ms / 1000))
                silence = np.zeros(n_samples, dtype=np.float32)
                with self._stream_lock:
                    self.stream.write(silence.tobytes())
            self.mouth_sink.publish_mouth_value(0.0)
            self.last_send_time = time.time()
            return True
        except Exception as exc:
            logger.warning(f"Audio prewarm failed: {exc}")
            return False

    def play_chunk(self, audio_chunk) -> None:
        if not self.is_playing or self.stream is None:
            return

        if torch.is_tensor(audio_chunk):
            audio_chunk = audio_chunk.cpu().detach().numpy()
        if audio_chunk.dtype != np.float32:
            audio_chunk = audio_chunk.astype(np.float32)

        first_chunk = True
        for i in range(0, len(audio_chunk), self.chunk_size):
            if not self.is_playing:
                break
            chunk = audio_chunk[i : i + self.chunk_size]
            if len(chunk) == 0:
                break
            if first_chunk:
                first_chunk = False
                self._emit_mouth_value_for_audio(chunk, minimum=0.12)
            get_realtime_aec_processor().push_reference(
                chunk,
                getattr(self, "_current_rate", None) or getattr(self, "sample_rate", None) or 24000,
            )
            with self._stream_lock:
                self.stream.write(chunk.tobytes())

            current_time = time.time()
            if current_time - self.last_send_time >= self.send_interval:
                rms = np.sqrt(np.mean(chunk ** 2))
                mouth_value = min(1.0, rms * self.volume_multiplier)
                self.mouth_sink.publish_mouth_value(mouth_value)
                self.last_send_time = current_time

    def stop(self) -> None:
        self.is_playing = False
        self._drain_audio_writer_queue()
        with self._stream_lock:
            stream = self.stream
            # Detach first so concurrent writer work cannot reuse a stream
            # whose shutdown has begun. PyAudio Stream has no abort_stream(),
            # and both stop_stream()/close() may raise when another owner has
            # already closed the device; stopping is deliberately idempotent.
            self.stream = None
            if stream is not None:
                try:
                    is_active = getattr(stream, "is_active", None)
                    if not callable(is_active) or bool(is_active()):
                        stream.stop_stream()
                except Exception:
                    logger.debug("audio stream was already stopped", exc_info=True)
                try:
                    stream.close()
                except Exception:
                    logger.debug("audio stream was already closed", exc_info=True)
        self.mouth_sink.publish_mouth_value(0.0)

    def _drain_audio_writer_queue(self) -> None:
        while True:
            try:
                self._audio_write_queue.get_nowait()
            except queue.Empty:
                break

    def cleanup(self) -> None:
        self._stop_audio_writer()
        self.stop()
        if self.pyaudio_instance is not None:
            self.pyaudio_instance.terminate()
            self.pyaudio_instance = None


# ---------------------------------------------------------------------------
# PlaybackManager
# ---------------------------------------------------------------------------
class PlaybackManager:
    """
    中心化顺序播放调度器，确保 TTS 句子按文本顺序输出。
    """

    def __init__(self, player_instance: StreamPlayer):
        self.player = player_instance
        self.player_is_ready = asyncio.Event()
        self.player_is_ready.set()
        self.current_playing_id = None
        self.logger = logging.getLogger("PlaybackManager")

        self.pending_audio: dict[int, tuple] = {}
        self.next_seq_to_play = 1
        self._playback_epoch = 0
        self._clock: Callable[[], float] = time.monotonic
        self._current_audio_started_at: float | None = None
        self._current_audio_duration_sec: float = 0.0
        self.play_condition = asyncio.Condition()
        # 句子开始播放回调：fn(sentence_id: str) -> None
        self.on_sentence_start: Callable[[str], None] | None = None
        self.on_sentence_complete: Callable[[str, str], None] | None = None
        # 轮次最后一句播完回调：fn() -> None
        self.on_turn_playback_complete: Callable[[], None] | None = None
        self._turn_last_sentence_id: str | None = None
        self._turn_id_by_last_sentence: dict[str, str | None] = {}
        self._turn_sentence_texts: dict[str, str] = {}
        self._turn_completed_sentence_ids: list[str] = []
        self._current_playing_segment_ids: set[str] = set()
        # play_s1_stream 互斥锁：确保同一时刻只有一个句子在流式播放，
        # 避免 asyncio.Event.wait()+clear() 非原子性导致多句同时唤醒（串音）。
        self._stream_play_lock: asyncio.Lock = asyncio.Lock()
        # Normal playlist playback and direct streaming share one global
        # sentence sequence.  A dequeued normal item may be waiting for the
        # physical player even though ``next_seq_to_play`` already advanced;
        # without these claims a later AUIP/work stream can jump over it and
        # strand the earlier sentence forever.
        self._normal_waiting_seq: int | None = None
        self._stream_claimed_seq: int | None = None

    @property
    def playback_epoch(self) -> int:
        return self._playback_epoch

    def is_epoch_current(self, epoch: int) -> bool:
        return int(epoch) == self._playback_epoch

    def _epoch_checker(self, epoch: int) -> Callable[[], bool]:
        return lambda: self.is_epoch_current(epoch)

    async def _claim_stream_sequence(self, sentence_seq: int, epoch: int) -> bool:
        """Claim one streaming sequence without bypassing normal playback."""

        async with self.play_condition:
            while self.is_epoch_current(epoch):
                if self.next_seq_to_play > sentence_seq:
                    self.logger.info(
                        "[Streaming] sequence already passed; dropping seq=%s next=%s",
                        sentence_seq,
                        self.next_seq_to_play,
                    )
                    return False
                if (
                    self.next_seq_to_play == sentence_seq
                    and self._normal_waiting_seq is None
                    and self._stream_claimed_seq is None
                ):
                    self._stream_claimed_seq = sentence_seq
                    self.next_seq_to_play = sentence_seq + 1
                    self.play_condition.notify_all()
                    return True
                await self.play_condition.wait()
        return False

    async def _release_stream_sequence(self, sentence_seq: int) -> None:
        async with self.play_condition:
            if self._stream_claimed_seq == sentence_seq:
                self._stream_claimed_seq = None
            self.play_condition.notify_all()

    @staticmethod
    def _audio_duration_seconds(audio_data, sample_rate: int | float | None) -> float:
        try:
            rate = float(sample_rate or 0)
            if rate <= 0:
                return 0.0
            return max(0.0, float(len(audio_data)) / rate)
        except Exception:
            return 0.0

    def _note_current_audio_started(self, audio_data=None, sample_rate: int | float | None = None) -> None:
        self._current_audio_started_at = self._clock()
        self._current_audio_duration_sec = self._audio_duration_seconds(audio_data, sample_rate)

    def _extend_current_audio_cover(self, audio_data, sample_rate: int | float | None) -> None:
        if self._current_audio_started_at is None:
            self._note_current_audio_started(audio_data, sample_rate)
            return
        self._current_audio_duration_sec += self._audio_duration_seconds(audio_data, sample_rate)

    def estimate_cover_seconds(self) -> float:
        """Estimate current and queued audio seconds available to cover TTS work."""
        try:
            now = self._clock()
            current_remaining = 0.0
            if self._current_audio_started_at is not None:
                elapsed = max(0.0, now - float(self._current_audio_started_at))
                current_remaining = max(0.0, self._current_audio_duration_sec - elapsed)

            pending_total = 0.0
            for audio_item in list((self.pending_audio or {}).values()):
                try:
                    if len(audio_item) == 6:
                        _, audio_data, sample_rate, _, _, _ = audio_item
                    elif len(audio_item) == 5:
                        audio_data, sample_rate, _, _, _ = audio_item
                    else:
                        audio_data, sample_rate, _, _ = audio_item
                    pending_total += self._audio_duration_seconds(audio_data, sample_rate)
                except Exception:
                    continue
            return max(0.0, current_remaining + pending_total)
        except Exception:
            return 0.0

    # ── TurnCoordinator 上报（观察期：只记录，失败绝不影响播放）──────────────

    def _note_stale_drop(self, kind: str) -> None:
        try:
            from core.turn_coordinator import get_turn_coordinator

            get_turn_coordinator().on_stale_dropped(kind=kind)
        except Exception:
            pass

    def _fire_sentence_start(self, sentence_id: str, *, label: str = "") -> None:
        try:
            from core.turn_coordinator import get_turn_coordinator

            get_turn_coordinator().on_sentence_playback_started(sentence_id=sentence_id)
        except Exception:
            pass
        if self.on_sentence_start is not None:
            try:
                self.on_sentence_start(sentence_id)
            except Exception as _cb_err:
                self.logger.warning(f"on_sentence_start{label} callback failed: {_cb_err}")

    def _fire_turn_playback_complete(
        self,
        turn_id: str | None = None,
        *,
        label: str = "",
    ) -> None:
        try:
            from core.turn_coordinator import get_turn_coordinator

            get_turn_coordinator().on_turn_playback_complete(turn_id)
        except Exception:
            pass
        if self.on_turn_playback_complete is not None:
            try:
                self.on_turn_playback_complete()
            except Exception as _cb_err:
                self.logger.warning(f"on_turn_playback_complete{label} callback failed: {_cb_err}")

    async def add_to_playlist(
        self,
        full_audio_data,
        sample_rate: int,
        sentence_id: str,
        japanese_text: str,
        segments: list[dict] | None = None,
        playback_epoch: int | None = None,
    ) -> None:
        sentence_seq = _parse_sentence_seq(sentence_id)
        epoch = self._playback_epoch if playback_epoch is None else int(playback_epoch)
        async with self.play_condition:
            if not self.is_epoch_current(epoch):
                self.logger.info(
                    "[TTS-INTERRUPT] drop stale playlist item: %s item_epoch=%s current_epoch=%s",
                    sentence_id,
                    epoch,
                    self._playback_epoch,
                )
                self._note_stale_drop("playlist")
                return
            self.pending_audio[sentence_seq] = (
                epoch,
                full_audio_data,
                sample_rate,
                sentence_id,
                japanese_text,
                segments,
            )
            self._register_turn_sentence(sentence_id, japanese_text)
            for segment in segments or []:
                self._register_turn_sentence(
                    str(segment.get("sentence_id", "")),
                    str(segment.get("text", "")),
                )
            self.logger.info(
                f"[Monitor] sentence '{sentence_id}' (seq={sentence_seq}) audio is ready and added to the playlist"
            )
            self.logger.info(
                f"[Debug] current pending_audio: {list(self.pending_audio.keys())}, "
                f"next_seq_to_play: {self.next_seq_to_play}"
            )
            self.play_condition.notify()

    async def add_streaming_chunk(
        self,
        audio_chunk,
        sample_rate: int,
        sentence_id: str,
        japanese_text: str,
        is_first_chunk: bool = False,
        is_last_chunk: bool = False,
        playback_epoch: int | None = None,
    ) -> None:
        """流式播放模式：立即播放第一句音频块。"""
        sentence_seq = _parse_sentence_seq(sentence_id)

        epoch = self._playback_epoch if playback_epoch is None else int(playback_epoch)
        if not self.is_epoch_current(epoch):
            self.logger.info(
                "[TTS-INTERRUPT] drop stale streaming chunk: %s item_epoch=%s current_epoch=%s",
                sentence_id,
                epoch,
                self._playback_epoch,
            )
            self._note_stale_drop("stream_chunk")
            return

        if sentence_seq == 1:
            self.logger.info(f"[StreamingPlayback] playing the first sentence chunk immediately: {sentence_id}")
            await self.player_is_ready.wait()
            if not self.is_epoch_current(epoch):
                self.logger.info("[TTS-INTERRUPT] stale streaming chunk skipped after wait: %s", sentence_id)
                return
            self.player_is_ready.clear()
            self.current_playing_id = sentence_id
            self._current_playing_segment_ids = {sentence_id}
            if is_first_chunk:
                self._note_current_audio_started(audio_chunk, sample_rate)
            else:
                self._extend_current_audio_cover(audio_chunk, sample_rate)
            self._register_turn_sentence(sentence_id, japanese_text)
            self.logger.info("[Debug] first-sentence streaming playback started; player_is_ready cleared")
            self.logger.info(
                f"[PLAYBACK] received first audio chunk and started streaming playback: {sentence_id} "
                f"(size: {len(audio_chunk)} samples)"
            )

            if is_first_chunk:
                self._fire_sentence_start(sentence_id)

            asyncio.create_task(
                self.player.play_audio_chunk_and_signal_completion(
                    audio_chunk,
                    sample_rate,
                    sentence_id,
                    japanese_text,
                    self.player_is_ready,
                    is_last_chunk=is_last_chunk,
                    is_current=self._epoch_checker(epoch),
                )
            )

            if is_first_chunk:
                self.next_seq_to_play = 2
                self.logger.info("[OrderOptimization] first sentence started; next_seq_to_play set to 2")
                if USE_FIRST_SENTENCE_SPRINT:
                    self.player_is_ready.set()
                    self.logger.info("[BatchTTSTiming] first-sentence sprint mode: batch TTS may start immediately")
                else:
                    self.logger.info("[BatchMode] batch production mode: PlaybackManager controls batch TTS timing")
                async with self.play_condition:
                    self.play_condition.notify()

            if is_last_chunk:
                self.next_seq_to_play = 2
                self.logger.info("[OrderConfirm] first-sentence streaming playback finished; ensuring next_seq_to_play is 2")
                async with self.play_condition:
                    self.play_condition.notify()
        else:
            self.logger.warning(
                f"[StreamingPlayback] non-first sentence attempted streaming playback; falling back to normal playback: {sentence_id}"
            )
            await self.add_to_playlist(audio_chunk, sample_rate, sentence_id, japanese_text)

    def mark_turn_last_sentence(
        self,
        sentence_id: str,
        turn_id: str | None = None,
    ) -> None:
        """标记本轮最后一句 ID；该句播完后触发 on_turn_playback_complete。"""
        sentence_id = str(sentence_id or "")
        self._turn_last_sentence_id = sentence_id or None
        if sentence_id:
            self._turn_id_by_last_sentence[sentence_id] = (
                str(turn_id) if turn_id else None
            )
            # Streaming can finish a short sentence before ChatRuntime has
            # closed the model turn and identified its final sentence.  Turn
            # completion is a fact about playback, so a late binding must
            # close immediately instead of waiting for another audio item.
            if sentence_id in self._turn_completed_sentence_ids:
                completed_turn, completed_turn_id = self._take_completed_turn(
                    sentence_id
                )
                if completed_turn:
                    self._fire_turn_playback_complete(completed_turn_id)

    def _take_completed_turn(self, sentence_id: str | None) -> tuple[bool, str | None]:
        sentence_id = str(sentence_id or "")
        if sentence_id not in self._turn_id_by_last_sentence:
            return False, None
        turn_id = self._turn_id_by_last_sentence.pop(sentence_id)
        if self._turn_last_sentence_id == sentence_id:
            self._turn_last_sentence_id = None
        return True, turn_id

    def _last_sentence_in_audio(
        self,
        primary_id: str,
        segments: list[dict] | None,
    ) -> str | None:
        candidates = [str(primary_id or "")]
        candidates.extend(
            str(segment.get("sentence_id", ""))
            for segment in (segments or [])
        )
        return next(
            (
                sentence_id
                for sentence_id in candidates
                if sentence_id in self._turn_id_by_last_sentence
            ),
            None,
        )

    def _register_turn_sentence(self, sentence_id: str, text: str) -> None:
        sentence_id = str(sentence_id or "")
        if not sentence_id:
            return
        if text:
            self._turn_sentence_texts[sentence_id] = str(text)

    def _mark_sentence_complete(self, sentence_id: str | None) -> None:
        sentence_id = str(sentence_id or "")
        if not sentence_id or sentence_id in self._turn_completed_sentence_ids:
            return
        text = self._turn_sentence_texts.get(sentence_id, "")
        self._turn_completed_sentence_ids.append(sentence_id)
        try:
            from core.turn_coordinator import get_turn_coordinator

            get_turn_coordinator().on_sentence_playback_complete(
                sentence_id=sentence_id
            )
        except Exception:
            pass
        if self.on_sentence_complete is not None:
            try:
                self.on_sentence_complete(sentence_id, text)
            except Exception as exc:
                self.logger.warning("on_sentence_complete callback error: %s", exc)

    def _mark_current_audio_complete(self, primary_id: str | None) -> None:
        """Close every logical sentence carried by one physical audio item."""

        sentence_ids = set(self._current_playing_segment_ids)
        if primary_id:
            sentence_ids.add(str(primary_id))
        for sentence_id in sorted(sentence_ids, key=_parse_sentence_seq):
            self._mark_sentence_complete(sentence_id)

    def _finish_audio_item(
        self,
        primary_id: str,
        logical_sentence_ids: Iterable[str],
    ) -> None:
        """Record physical completion independently of future queue traffic."""

        sentence_ids = {str(value or "") for value in logical_sentence_ids}
        sentence_ids.add(str(primary_id or ""))
        ordered = [
            sentence_id
            for sentence_id in sorted(sentence_ids, key=_parse_sentence_seq)
            if sentence_id
        ]
        for sentence_id in ordered:
            self._mark_sentence_complete(sentence_id)
        for sentence_id in ordered:
            completed_turn, turn_id = self._take_completed_turn(sentence_id)
            if completed_turn:
                self._fire_turn_playback_complete(turn_id)

    def get_completed_turn_text(self) -> str:
        parts = []
        for sentence_id in self._turn_completed_sentence_ids:
            text = self._turn_sentence_texts.get(sentence_id, "").strip()
            if text:
                parts.append(text)
        return "".join(parts).strip()

    def clear_turn_tracking(self) -> None:
        self._turn_sentence_texts.clear()
        self._turn_completed_sentence_ids.clear()

    def is_current_playback_sentence(self, sentence_id: str) -> bool:
        sentence_id = str(sentence_id or "")
        if not sentence_id:
            return False
        return sentence_id == self.current_playing_id or sentence_id in self._current_playing_segment_ids

    def _next_playback_epoch(self) -> int:
        """向 TurnCoordinator 账本申领下一 playback epoch（所有权迁移·切片 B）。

        发放权在账本；self._playback_epoch 仅作热路径只读缓存
        （is_epoch_current/_epoch_checker 在播放循环内高频检查）。
        账本不可用时回退本地自增（旧行为）。
        """
        try:
            from core.turn_coordinator import get_turn_coordinator

            return get_turn_coordinator().advance_playback_epoch(
                local_next=self._playback_epoch + 1, source="playback"
            )
        except Exception:
            return self._playback_epoch + 1

    async def interrupt(self, *, reset_sequence: bool = True) -> None:
        """Stop current playback and discard queued audio for the active turn."""
        self._playback_epoch = self._next_playback_epoch()
        epoch = self._playback_epoch
        self.player.stop()
        async with self.play_condition:
            self.pending_audio.clear()
            if reset_sequence:
                self.next_seq_to_play = 1
            self.current_playing_id = None
            self._current_playing_segment_ids.clear()
            self._current_audio_started_at = None
            self._current_audio_duration_sec = 0.0
            self._turn_last_sentence_id = None
            self._turn_id_by_last_sentence.clear()
            self._normal_waiting_seq = None
            self._stream_claimed_seq = None
            self.player_is_ready.set()
            self.play_condition.notify_all()
        self.logger.info("[TTS-INTERRUPT] playback queue cleared epoch=%s", epoch)
        try:
            from core.turn_coordinator import get_turn_coordinator

            get_turn_coordinator().on_playback_interrupted(playback_epoch=epoch)
        except Exception:
            pass

    async def play_s1_stream(
        self,
        chunk_queue: asyncio.Queue,
        sentence_id: str,
        japanese_text: str,
        playback_epoch: int | None = None,
    ) -> None:
        """流式播放：从 chunk_queue 串行消费 chunk 并立即播放。
        支持任意句号（S1/S2/…），next_seq_to_play 动态设为 sentence_seq+1。

        chunk_queue 协议：
          - 元素为 np.ndarray（float32 音频数据）
          - put(None) 表示 EOF，收到后停止播放并置位 player_is_ready

        设计要点：
          - 每个 chunk 交给常驻音频 writer 线程同步写 pyaudio，await 保证串行，
            避免每块重复提交 run_in_executor
          - player_is_ready 仅在 finally 中置位一次，与 PlaybackManager.run()
            的句2+ 路径契约完全相同
          - next_seq_to_play 在此处更新为 seq+1 并通知 play_condition，确保
            PlaybackManager.run() 能在 pending_audio 里等到正确序号
        """
        sentence_seq = _parse_sentence_seq(sentence_id)
        next_expected_seq = sentence_seq + 1
        epoch = self._playback_epoch if playback_epoch is None else int(playback_epoch)
        if not self.is_epoch_current(epoch):
            self.logger.info(
                "[TTS-INTERRUPT] drop stale stream before start: %s item_epoch=%s current_epoch=%s",
                sentence_id,
                epoch,
                self._playback_epoch,
            )
            self._note_stale_drop("stream_start")
            return
        self._register_turn_sentence(sentence_id, japanese_text)

        # ── 互斥进入流式播放（替换 Event.wait()+clear()）──────────────────
        # asyncio.Event.set() 会唤醒所有 wait()；随后某协程 clear() 时，
        # 其它协程可能已通过 wait()，导致多句 play_s1_stream 同时写声卡（串音）。
        # Lock 保证同一时刻只有一个 play_s1_stream 占用播放器；须在 finally 中 release。
        sequence_claimed = False
        stream_lock_acquired = False
        try:
            if not self.is_epoch_current(epoch):
                self.logger.info("[TTS-INTERRUPT] stale stream skipped before claim: %s", sentence_id)
                return
            sequence_claimed = await self._claim_stream_sequence(sentence_seq, epoch)
            if not sequence_claimed:
                return
            # Claim sequence order before serializing physical playback. A
            # later streaming task must not hold the playback lock while it
            # waits for an earlier stream that still needs that same lock.
            await self._stream_play_lock.acquire()
            stream_lock_acquired = True
            await self.player_is_ready.wait()
            if not self.is_epoch_current(epoch):
                self.logger.info(
                    "[TTS-INTERRUPT] stale stream skipped after player wait: %s",
                    sentence_id,
                )
                return
            self.player_is_ready.clear()
            self.current_playing_id = sentence_id
            self._current_playing_segment_ids = {sentence_id}
            self._note_current_audio_started()
            self.logger.info(f"[Streaming] starting stream playback: {sentence_id} (seq={sentence_seq})")

            # ── 触发 on_sentence_start 回调（表情切换等） ────────────────
            self._fire_sentence_start(sentence_id)

            # The sequence was claimed before waiting for the physical player,
            # so normal playlist playback cannot consume a later sentence.
            self.logger.info(f"[Streaming] next_seq_to_play updated to {next_expected_seq}")

            # ── 字幕 / 预翻译 ─────────────────────────────────────────────
            hooks = self.player._hooks
            if hooks.subtitle_available and hooks.update_subtitle_display:
                hooks.update_subtitle_display(japanese_text, "")
            if hooks.check_and_display_pre_translation:
                asyncio.create_task(
                    hooks.check_and_display_pre_translation(sentence_id, japanese_text)
                )

            # ── 初始化 pyaudio stream ─────────────────────────────────────
            player = self.player
            player.mouth_sink.publish_mouth_value(0.0)
            player.last_send_time = time.time()
            loop = asyncio.get_running_loop()
            first_sound_logged = [False]
            current_sample_rate = None
            aec_capture = get_aec_debug_capture()
            aec_capture.start(sentence_id)

            # ── 串行播放所有 chunk ───────────────────────────────────────
            # 流式块通常很短，asyncio 调度开销可能
            # 导致硬件缓冲在两块之间短暂耗尽（underrun → 咔哒声）。
            # 贪婪合并：等到第一块后立即把队列里已堆积的块一并写入，
            # 减少 writer 唤醒频率，给硬件缓冲更大余量。
            try:
                eof_in_drain = False
                while True:
                    item = await chunk_queue.get()
                    if not self.is_epoch_current(epoch):
                        self.logger.info("[TTS-INTERRUPT] stop stale stream playback: %s", sentence_id)
                        break
                    if item is None:
                        break

                    if isinstance(item, tuple):
                        sample_rate, audio_chunk = item
                    else:
                        sample_rate, audio_chunk = 24000, item

                    if current_sample_rate != sample_rate:
                        self.player.initialize(sample_rate)
                        current_sample_rate = sample_rate

                    if audio_chunk.dtype != np.float32:
                        audio_chunk = audio_chunk.astype(np.float32)

                    # 贪婪地把队列中已就绪的块合并，减少硬件 underrun
                    chunks_to_play = [audio_chunk]
                    eof_in_drain = False
                    while True:
                        try:
                            nxt = chunk_queue.get_nowait()
                            if nxt is None:
                                eof_in_drain = True
                                break
                            _, nc = (nxt[0], nxt[1]) if isinstance(nxt, tuple) else (24000, nxt)
                            if nc.dtype != np.float32:
                                nc = nc.astype(np.float32)
                            chunks_to_play.append(nc)
                        except asyncio.QueueEmpty:
                            break

                    merged = (
                        np.concatenate(chunks_to_play)
                        if len(chunks_to_play) > 1
                        else chunks_to_play[0]
                    )

                    # ── 句间 fade-in / fade-out，消除硬切换爆破音 ──────────
                    _FADE_MS = 10  # ms，10ms ≈ 240 samples @24kHz
                    _fade_n = int(_FADE_MS * 0.001 * (player.sample_rate or 24000))
                    _is_first_chunk = not first_sound_logged[0]
                    if _is_first_chunk or eof_in_drain:
                        merged = merged.copy()
                        if _is_first_chunk:
                            _fn = min(_fade_n, len(merged))
                            merged[:_fn] *= np.linspace(0.0, 1.0, _fn, dtype=np.float32)
                        if eof_in_drain:
                            _fn = min(_fade_n, len(merged))
                            merged[-_fn:] *= np.linspace(1.0, 0.0, _fn, dtype=np.float32)

                    _chunk = merged
                    _first_mouth_minimum = 0.12 if not first_sound_logged[0] else None

                    def _before_write(chunk=_chunk):
                        if not first_sound_logged[0]:
                            first_sound_logged[0] = True
                            _stream_mode = (
                                "s1_stream" if sentence_seq == 1 else f"s{sentence_seq}_stream"
                            )
                            _lat_ms = log_latency_marker(
                                self.logger,
                                "first_play",
                                clear=(sentence_seq == 1),
                                id=sentence_id,
                                samples=len(chunk),
                                mode=_stream_mode,
                            )
                            _lat_part = (
                                f" | api_to_first_sound_ms={_lat_ms:.1f}"
                                if _lat_ms is not None
                                else ""
                            )
                            self.logger.info(
                                f"[PLAYBACK-STREAM] first sound started seq={sentence_seq}: {sentence_id} "
                                f"(first_frame={len(chunk)} samples){_lat_part}"
                            )

                    if not player.is_playing:
                        break
                    if not self.is_epoch_current(epoch):
                        self.logger.info("[TTS-INTERRUPT] stop stale stream before write: %s", sentence_id)
                        break
                    _sample_rate = current_sample_rate or player.sample_rate or 24000
                    self._extend_current_audio_cover(_chunk, _sample_rate)
                    get_realtime_aec_processor().push_reference(_chunk, _sample_rate)
                    aec_capture.push_reference(
                        _chunk,
                        _sample_rate,
                        sentence_id,
                    )
                    await player.write_audio_async(
                        _chunk,
                        loop=loop,
                        before_write=_before_write,
                        is_current=self._epoch_checker(epoch),
                        mouth_envelope=True,
                        sample_rate=_sample_rate,
                        first_mouth_minimum=_first_mouth_minimum,
                    )
                    self.logger.debug(
                        f"[Streaming] chunk playback completed seq={sentence_seq}: {len(merged)} samples "
                        f"(merged {len(chunks_to_play)} vox-chunks)"
                    )

                    if eof_in_drain:
                        # 末句写完后补 60ms 静音，填补 Python 调度间隙防止句间 underrun
                        _pad_n = int(0.060 * (player.sample_rate or 24000))
                        _silence = np.zeros(_pad_n, dtype=np.float32)
                        if player.is_playing:
                            await player.write_audio_async(
                                _silence,
                                loop=loop,
                                is_current=self._epoch_checker(epoch),
                                mouth_envelope=True,
                                sample_rate=player.sample_rate or 24000,
                            )
                        break

                player.mouth_sink.publish_mouth_value(0.0)
                self.logger.info(f"[Streaming] all audio playback finished seq={sentence_seq}: {sentence_id}")

            except Exception as e:
                self.logger.error(f"[Streaming] playback failed seq={sentence_seq}: {e}", exc_info=True)

            finally:
                aec_capture.stop()
                player.mouth_sink.publish_mouth_value(0.0)
                # pyaudio ring buffer 连续，stream.write 已将数据排入硬件队列，
                # 无需显式等待。仅让 event loop 完成一次调度即可。
                await asyncio.sleep(0.0)
                if player.is_playing and self.is_epoch_current(epoch):
                    self._mark_sentence_complete(sentence_id)

                # ── 本轮最后一句回调 ──────────────────────────────────────
                if self.is_epoch_current(epoch):
                    completed_turn, turn_id = self._take_completed_turn(sentence_id)
                    if completed_turn:
                        self._fire_turn_playback_complete(turn_id)

                if self.is_epoch_current(epoch):
                    self.player_is_ready.set()
                self.logger.info(f"[Streaming] player_is_ready set seq={sentence_seq}: {sentence_id}")
        finally:
            if sequence_claimed:
                await self._release_stream_sequence(sentence_seq)
            if stream_lock_acquired:
                self._stream_play_lock.release()

    def _audio_contains_sentence(self, primary_id: str, segments: list[dict] | None, target_id: str | None) -> bool:
        if not target_id:
            return False
        if primary_id == target_id:
            return True
        return any(segment.get("sentence_id") == target_id for segment in (segments or []))

    def _segment_speech_weight(self, text: str) -> float:
        cleaned = str(text or "").replace("*", "").replace("_", "").replace("`", "")
        if not cleaned.strip():
            return 1.0

        weight = 0.0
        i = 0
        while i < len(cleaned):
            ch = cleaned[i]
            code = ord(ch)

            if ch.isspace():
                i += 1
                continue

            if ch.isascii() and ch.isalnum():
                j = i + 1
                while j < len(cleaned) and cleaned[j].isascii() and (
                    cleaned[j].isalnum() or cleaned[j] in "_+-/#."
                ):
                    j += 1
                token_len = j - i
                weight += max(1.0, token_len * 0.75)
                i = j
                continue

            if ch in "。.!?！？":
                weight += 2.2
            elif ch in "、,，;；:：":
                weight += 1.2
            elif ch in "…":
                weight += 1.8
            elif ch in "「」『』（）()[]【】":
                weight += 0.1
            elif ch in "ゃゅょぁぃぅぇぉャュョァィゥェォ":
                weight += 0.35
            elif ch in "っッ":
                weight += 0.65
            elif ch == "ー":
                weight += 0.55
            elif 0x3040 <= code <= 0x30FF:
                weight += 1.0
            elif 0x4E00 <= code <= 0x9FFF:
                weight += 1.15
            else:
                weight += 0.8
            i += 1

        return max(1.0, weight)

    def _segment_offsets(self, segments: list[dict], total_duration: float) -> list[tuple[str, str, float]]:
        if not segments or total_duration <= 0:
            return []
        weights = [self._segment_speech_weight(str(segment.get("text", ""))) for segment in segments]
        total_weight = max(1.0, sum(weights))
        offsets: list[tuple[str, str, float]] = []
        elapsed = 0.0
        for segment, weight in zip(segments, weights):
            offsets.append(
                (
                    str(segment.get("sentence_id", "")),
                    str(segment.get("text", "")),
                    min(elapsed, max(0.0, total_duration - 0.05)),
                )
            )
            elapsed += total_duration * (weight / total_weight)
        return offsets

    def _display_segment_subtitle(self, sentence_id: str, text: str) -> None:
        sentence_id = str(sentence_id or "")
        text = str(text or "")
        if not sentence_id or not text:
            return
        hooks = self.player._hooks
        if hooks.subtitle_available and hooks.update_subtitle_display:
            hooks.update_subtitle_display(text, "")
        if hooks.check_and_display_pre_translation:
            asyncio.create_task(
                hooks.check_and_display_pre_translation(sentence_id, text)
            )

    async def _fire_segment_starts(
        self,
        primary_id: str,
        segments: list[dict] | None,
        total_samples: int,
        sample_rate: int,
        playback_epoch: int | None = None,
    ) -> None:
        if not segments or len(segments) <= 1 or self.on_sentence_start is None:
            return
        epoch = self._playback_epoch if playback_epoch is None else int(playback_epoch)
        total_duration = total_samples / max(1, sample_rate)
        start = time.time()
        offsets = self._segment_offsets(segments, total_duration)
        previous_id = offsets[0][0] if offsets else str(primary_id or "")
        for segment_id, segment_text, offset in offsets[1:]:
            delay = max(0.0, offset - (time.time() - start))
            if delay:
                await asyncio.sleep(delay)
            if not self.is_epoch_current(epoch):
                return
            if self.current_playing_id != primary_id and primary_id not in self._current_playing_segment_ids:
                return
            self._mark_sentence_complete(previous_id)
            self._display_segment_subtitle(segment_id, segment_text)
            self._fire_sentence_start(segment_id, label="(segment)")
            previous_id = segment_id

    async def run(self) -> None:
        """播放主循环，按句子序号顺序消费 pending_audio。"""
        self.logger.info("[Monitor] PlaybackManager run loop started; waiting for playback jobs")
        while True:
            try:
                async with self.play_condition:
                    while (
                        self._stream_claimed_seq is not None
                        or self.next_seq_to_play not in self.pending_audio
                    ):
                        self.logger.debug(
                            f"[Wait] waiting for sentence seq {self.next_seq_to_play} to become ready"
                        )
                        self.logger.info(
                            f"[Debug] current pending_audio: {list(self.pending_audio.keys())}, "
                            f"next_seq_to_play: {self.next_seq_to_play}"
                        )
                        await self.play_condition.wait()

                    audio_item = self.pending_audio[self.next_seq_to_play]
                    if len(audio_item) == 6:
                        item_epoch, full_audio_data, sample_rate, sentence_id, japanese_text, segments = audio_item
                    elif len(audio_item) == 5:
                        item_epoch = self._playback_epoch
                        full_audio_data, sample_rate, sentence_id, japanese_text, segments = audio_item
                    else:
                        item_epoch = self._playback_epoch
                        full_audio_data, sample_rate, sentence_id, japanese_text = audio_item
                        segments = None
                    del self.pending_audio[self.next_seq_to_play]
                    if not self.is_epoch_current(item_epoch):
                        self.logger.info(
                            "[TTS-INTERRUPT] drop stale pending playback: %s item_epoch=%s current_epoch=%s",
                            sentence_id,
                            item_epoch,
                            self._playback_epoch,
                        )
                        self._note_stale_drop("pending_playback")
                        continue
                    seq_advance = max(1, len(segments or []))
                    sentence_seq = _parse_sentence_seq(sentence_id)
                    self._normal_waiting_seq = sentence_seq
                    self.next_seq_to_play += seq_advance
                    self.play_condition.notify_all()
                    self.logger.info(
                        f"[Debug] played seq {self.next_seq_to_play - seq_advance}; "
                        f"next_seq_to_play updated to: {self.next_seq_to_play}"
                    )

                await self.player_is_ready.wait()
                if not self.is_epoch_current(item_epoch):
                    async with self.play_condition:
                        if self._normal_waiting_seq == sentence_seq:
                            self._normal_waiting_seq = None
                        self.play_condition.notify_all()
                    self.logger.info(
                        "[TTS-INTERRUPT] stale playback skipped after ready wait: %s item_epoch=%s current_epoch=%s",
                        sentence_id,
                        item_epoch,
                        self._playback_epoch,
                    )
                    continue
                self.player_is_ready.clear()
                async with self.play_condition:
                    if self._normal_waiting_seq == sentence_seq:
                        self._normal_waiting_seq = None
                    self.play_condition.notify_all()
                self.current_playing_id = sentence_id
                self._current_playing_segment_ids = {
                    str(segment.get("sentence_id", ""))
                    for segment in (segments or [])
                    if str(segment.get("sentence_id", ""))
                } or {sentence_id}
                self.logger.info(f"[Monitor] starting sentence playback: {sentence_id}")
                self.logger.info(
                    f"[PLAYBACK] received first audio chunk and started playback: {sentence_id} "
                    f"(size: {len(full_audio_data)} samples)"
                )

                self._fire_sentence_start(sentence_id)
                self._note_current_audio_started(full_audio_data, sample_rate)

                if segments and len(segments) > 1:
                    asyncio.create_task(
                        self._fire_segment_starts(
                            sentence_id,
                            segments,
                            len(full_audio_data),
                            sample_rate,
                            playback_epoch=item_epoch,
                        )
                    )

                asyncio.create_task(
                    self.player.play_full_audio_and_signal_completion(
                        full_audio_data,
                        sample_rate,
                        sentence_id,
                        japanese_text,
                        self.player_is_ready,
                        subtitle_segments=segments,
                        is_current=self._epoch_checker(item_epoch),
                    )
                )

                # Physical completion owns sentence completion.  Do not wait
                # for a later queue item to notice the previous one ended:
                # the current audio may be the last item, and ChatRuntime may
                # bind that last-sentence identity shortly after playback
                # already started.
                _logical_sentence_ids = tuple(self._current_playing_segment_ids)

                async def _finish_on_audio_done(
                    primary_id=sentence_id,
                    logical_sentence_ids=_logical_sentence_ids,
                    watched_epoch=item_epoch,
                ):
                    await self.player_is_ready.wait()
                    if not self.is_epoch_current(watched_epoch):
                        return
                    self._finish_audio_item(primary_id, logical_sentence_ids)

                asyncio.create_task(_finish_on_audio_done())

            except Exception as e:
                self.logger.error(f"PlaybackManager run loop failed: {e}", exc_info=True)
                async with self.play_condition:
                    self._normal_waiting_seq = None
                    self.play_condition.notify_all()
                if not self.player_is_ready.is_set():
                    self.player_is_ready.set()
                await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# StreamPlayerWithBuffer
# ---------------------------------------------------------------------------
class StreamPlayerWithBuffer(StreamPlayer):
    """
    带前置缓冲区 + 字幕集成的播放器。

    字幕/翻译操作通过 SubtitleHooks 依赖注入，
    main.py 在构造时传入：
        player = StreamPlayerWithBuffer(mouth_sink, hooks=SubtitleHooks(...))
    """

    def __init__(
        self,
        mouth_sink: MouthSignalSink,
        buffer_size: float = 0.3,
        hooks: SubtitleHooks | None = None,
    ):
        super().__init__(mouth_sink)
        self.buffer_size = buffer_size
        self.buffer: list = []
        self.buffer_samples = 0
        self.sample_rate = 0
        self.current_sentence = ""
        self.current_translation = ""
        self.subtitle_display_index = 0
        self.subtitle_start_time = 0
        self.logger = logging.getLogger("StreamPlayer")
        self._hooks = hooks or SubtitleHooks()

    def initialize(self, sample_rate: int) -> None:
        super().initialize(sample_rate)
        self.sample_rate = sample_rate
        self.buffer_samples = int(sample_rate * self.buffer_size)

    async def play_full_audio_and_signal_completion(
        self,
        full_audio_data,
        sample_rate: int,
        sentence_id: str,
        japanese_text: str,
        completion_event: asyncio.Event,
        subtitle_segments: list[dict] | None = None,
        is_current: Callable[[], bool] | None = None,
    ) -> None:
        aec_capture = None
        try:
            if is_current is not None and not is_current():
                self.logger.info("[TTS-INTERRUPT] skip stale full audio before initialize: %s", sentence_id)
                return
            self.initialize(sample_rate)
            aec_capture = get_aec_debug_capture()
            aec_capture.start(sentence_id)

            hooks = self._hooks
            subtitle_sentence_id = sentence_id
            subtitle_text = japanese_text
            if subtitle_segments:
                first_segment = subtitle_segments[0]
                if isinstance(first_segment, dict):
                    subtitle_sentence_id = str(first_segment.get("sentence_id") or sentence_id)
                    subtitle_text = str(first_segment.get("text") or japanese_text)
            if hooks.subtitle_available and hooks.update_subtitle_display:
                hooks.update_subtitle_display(subtitle_text, "")
            if hooks.check_and_display_pre_translation:
                asyncio.create_task(
                    hooks.check_and_display_pre_translation(subtitle_sentence_id, subtitle_text)
                )

            def sync_play_and_lipsync(player_instance, _loop):
                if is_current is not None and not is_current():
                    return
                first_write = True
                player_instance.logger.info(
                    f"[Monitor] physical playback and lip sync started: {sentence_id}"
                )
                player_instance.logger.info(
                    f"[PLAYBACK-PHYSICAL] physical playback and lip sync started: {sentence_id}"
                )
                player_instance.mouth_sink.publish_mouth_value(0.0)
                player_instance.last_send_time = time.time()

                for i in range(0, len(full_audio_data), player_instance.chunk_size):
                    if is_current is not None and not is_current():
                        break
                    chunk = full_audio_data[i : i + player_instance.chunk_size]
                    if len(chunk) == 0:
                        break
                    if not player_instance.is_playing or player_instance.stream is None:
                        break
                    first_mouth_prime = first_write
                    if first_write and _parse_sentence_seq(sentence_id) == 1:
                        first_write = False
                        _lat_ms = log_latency_marker(
                            player_instance.logger,
                            "first_play",
                            clear=True,
                            id=sentence_id,
                            samples=len(chunk),
                            mode="full_audio",
                        )
                        if _lat_ms is not None:
                            player_instance.logger.info(
                                f"[PLAYBACK] first_play_e2e_ms={_lat_ms:.1f} id={sentence_id}"
                            )
                    if first_mouth_prime:
                        first_write = False
                        player_instance._emit_mouth_value_for_audio(chunk, minimum=0.12)
                    if is_current is not None and not is_current():
                        break
                    get_realtime_aec_processor().push_reference(chunk, sample_rate)
                    aec_capture.push_reference(chunk, sample_rate, sentence_id)
                    with player_instance._stream_lock:
                        if is_current is not None and not is_current():
                            break
                        player_instance.stream.write(chunk.tobytes())
                    current_time = time.time()
                    if current_time - player_instance.last_send_time >= player_instance.send_interval:
                        rms = np.sqrt(np.mean(chunk ** 2))
                        mouth_value = min(1.0, rms * player_instance.volume_multiplier)
                        player_instance.mouth_sink.publish_mouth_value(mouth_value)
                        player_instance.last_send_time = current_time

                player_instance.mouth_sink.publish_mouth_value(0.0)
                player_instance.logger.info(
                    f"[Monitor] physical playback finished: {sentence_id}"
                )

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, sync_play_and_lipsync, self, loop)

        except Exception as e:
            self.logger.error(f"player play_full_audio failed: {e}", exc_info=True)
        finally:
            try:
                if aec_capture is not None:
                    aec_capture.stop()
            except Exception:
                pass
            completion_event.set()
            self.logger.info(
                f"[Monitor] player is ready; completion signal set for '{sentence_id}'"
            )

    async def play_audio_chunk_and_signal_completion(
        self,
        audio_chunk,
        sample_rate: int,
        sentence_id: str,
        japanese_text: str,
        completion_event: asyncio.Event,
        is_last_chunk: bool = False,
        is_current: Callable[[], bool] | None = None,
    ) -> None:
        aec_capture = None
        try:
            if is_current is not None and not is_current():
                self.logger.info("[TTS-INTERRUPT] skip stale audio chunk before initialize: %s", sentence_id)
                return
            self.initialize(sample_rate)
            aec_capture = get_aec_debug_capture()
            aec_capture.start(sentence_id)

            hooks = self._hooks
            if hooks.subtitle_available and hooks.update_subtitle_display:
                hooks.update_subtitle_display(japanese_text, "")
            if hooks.check_and_display_pre_translation:
                asyncio.create_task(
                    hooks.check_and_display_pre_translation(sentence_id, japanese_text)
                )

            def sync_play_chunk(player_instance, _loop):
                if is_current is not None and not is_current():
                    return
                player_instance.logger.info(
                    f"[StreamingPlayback] playing audio chunk: {sentence_id}"
                )
                player_instance.logger.info(
                    f"[PLAYBACK-PHYSICAL] physical playback and lip sync started: {sentence_id}"
                )
                player_instance.mouth_sink.publish_mouth_value(0.0)
                player_instance.last_send_time = time.time()

                first_mouth_prime = True
                for i in range(0, len(audio_chunk), player_instance.chunk_size):
                    if is_current is not None and not is_current():
                        break
                    chunk = audio_chunk[i : i + player_instance.chunk_size]
                    if len(chunk) == 0:
                        break
                    if not player_instance.is_playing or player_instance.stream is None:
                        break
                    if first_mouth_prime:
                        first_mouth_prime = False
                        player_instance._emit_mouth_value_for_audio(chunk, minimum=0.12)
                    if is_current is not None and not is_current():
                        break
                    get_realtime_aec_processor().push_reference(chunk, sample_rate)
                    aec_capture.push_reference(chunk, sample_rate, sentence_id)
                    with player_instance._stream_lock:
                        if is_current is not None and not is_current():
                            break
                        player_instance.stream.write(chunk.tobytes())
                    current_time = time.time()
                    if current_time - player_instance.last_send_time >= player_instance.send_interval:
                        rms = np.sqrt(np.mean(chunk ** 2))
                        mouth_value = min(1.0, rms * player_instance.volume_multiplier)
                        player_instance.mouth_sink.publish_mouth_value(mouth_value)
                        player_instance.last_send_time = current_time

                player_instance.mouth_sink.publish_mouth_value(0.0)
                player_instance.logger.info(
                    f"[StreamingPlayback] audio chunk playback completed: {sentence_id}"
                )

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, sync_play_chunk, self, loop)

        except Exception as e:
            self.logger.error(f"streaming audio chunk playback failed: {e}", exc_info=True)
        finally:
            try:
                if aec_capture is not None:
                    aec_capture.stop()
            except Exception:
                pass
            if is_last_chunk:
                # stream.write() 是阻塞调用，run_in_executor 返回时音频数据已全部
                # 送入 OS 声卡驱动缓冲区，只需预留极短的硬件缓冲余量即可。
                # 原来的 asyncio.sleep(audio_duration + 0.05) 会在物理播放结束后
                # 再额外等待整个句子的时长，造成句间静音间隙，已删除。
                await asyncio.sleep(0.08)
                completion_event.set()
                audio_duration = len(audio_chunk) / sample_rate
                self.logger.info(
                    f"[StreamingPlayback] first sentence playback completed; completion signal set for '{sentence_id}' "
                    f"(duration={audio_duration:.3f}s)"
                )

    def set_subtitle_content(self, japanese_text: str, chinese_text: str) -> None:
        self.current_sentence = japanese_text
        self.current_translation = chinese_text
        self.subtitle_display_index = 0
        self.subtitle_start_time = time.time()
        logger.info(
            f"[Subtitle] set subtitle text - ja='{japanese_text}', zh='{chinese_text}'"
        )

    async def _check_and_update_translation(
        self, sentence_id: str, japanese_text: str
    ) -> None:
        hooks = self._hooks
        try:
            normalized = japanese_text.strip()
            translation_data = None
            if hooks.get_translation:
                translation_data = await hooks.get_translation(normalized)

            if translation_data:
                if translation_data["status"] == "completed":
                    chinese_text = translation_data["chinese"]
                    if hooks.display_chinese_subtitle_with_text:
                        await hooks.display_chinese_subtitle_with_text(
                            sentence_id, normalized, chinese_text
                        )
                    logger.info(
                        "[Subtitle] pre-translation completed and updated: %s",
                        protected_text(chinese_text, limit=30),
                    )
                elif translation_data["status"] == "translating":
                    logger.info(f"[Subtitle] translation in progress; waiting asynchronously: {sentence_id}")
                    asyncio.create_task(
                        self._wait_for_translation(sentence_id, normalized)
                    )
                else:
                    logger.warning(f"[Subtitle] translation failed: {sentence_id}")
            else:
                logger.warning(f"[Subtitle] translation cache missing: {sentence_id}")
                if hooks.cache_lock and hooks.cache_ref is not None:
                    async with hooks.cache_lock:
                        for cached_text, cached_data in hooks.cache_ref.items():
                            if cached_text.strip() == normalized or self._is_similar_text(
                                cached_text.strip(), normalized
                            ):
                                logger.info(
                                    f"🔍 找到匹配的缓存: '{cached_text[:30]}...'"
                                )
                                if cached_data["status"] == "completed":
                                    chinese_text = cached_data["chinese"]
                                    if hooks.display_chinese_subtitle_with_text:
                                        await hooks.display_chinese_subtitle_with_text(
                                            sentence_id, normalized, chinese_text
                                        )
                                    logger.info(
                                        f"[Subtitle] fuzzy matched translation completed and updated: '{chinese_text[:30]}...'"
                                    )
                                    return
                                break
        except Exception as e:
            logger.error(f"translation check failed: {e}")

    def _is_similar_text(self, text1: str, text2: str) -> bool:
        if not text1 or not text2:
            return False
        kana = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"
        clean1 = "".join(c for c in text1 if c.isalnum() or c in kana)
        clean2 = "".join(c for c in text2 if c.isalnum() or c in kana)
        if not clean1 or not clean2:
            return False
        if abs(len(clean1) - len(clean2)) / max(len(clean1), len(clean2)) > 0.2:
            return False
        matches = sum(1 for a, b in zip(clean1, clean2) if a == b)
        return matches / max(len(clean1), len(clean2)) > 0.8

    async def _wait_for_translation(
        self, sentence_id: str, japanese_text: str
    ) -> None:
        hooks = self._hooks
        normalized = japanese_text.strip()
        max_wait = 12.0
        interval = 0.1
        waited = 0.0

        while waited < max_wait:
            if hooks.get_translation:
                data = await hooks.get_translation(normalized)
                if data and data["status"] == "completed":
                    chinese_text = data["chinese"]
                    if hooks.display_chinese_subtitle_with_text:
                        await hooks.display_chinese_subtitle_with_text(
                            sentence_id, normalized, chinese_text
                        )
                    logger.info(
                        f"[Subtitle] translation completed after wait and updated: '{chinese_text[:30]}...'"
                    )
                    return
                elif data and data["status"] == "failed":
                    logger.warning(f"[Subtitle] translation failed: '{normalized[:30]}...'")
                    return
            await asyncio.sleep(interval)
            waited += interval

        logger.warning(f"[Subtitle] translation timed out: '{normalized[:30]}...'")

    def add_to_buffer(
        self,
        audio_chunk,
        subtitle_text: str = None,
        sentence_id: str = None,
    ) -> None:
        if torch.is_tensor(audio_chunk):
            audio_chunk = audio_chunk.cpu().detach().numpy()
        if audio_chunk.dtype != np.float32:
            audio_chunk = audio_chunk.astype(np.float32)

        if subtitle_text:
            self._pending_subtitle_text = subtitle_text
        if sentence_id:
            self._pending_sentence_id = sentence_id

        self.buffer.append(audio_chunk)

        total_samples = sum(len(c) for c in self.buffer)
        if total_samples >= self.buffer_samples:
            self.play_buffer()

    def play_buffer(self) -> None:
        if not self.buffer:
            return
        combined = np.concatenate(self.buffer)
        self.buffer = []

        if len(combined) > 0:
            threshold = 0.008
            non_zero = np.where(np.abs(combined) > threshold)[0]
            if len(non_zero) > 0:
                start_idx = max(0, non_zero[0] - int(0.01 * self.sample_rate))
                combined = combined[start_idx:]

            hooks = self._hooks
            if hasattr(self, "_pending_subtitle_text"):
                pending_sid = getattr(self, "_pending_sentence_id", None)

                if not hasattr(self, "_current_playing_sentence_id"):
                    self._current_playing_sentence_id = None
                current_id = self._current_playing_sentence_id

                allow_update = False
                if current_id is None and pending_sid is not None:
                    self._current_playing_sentence_id = pending_sid
                    allow_update = True
                elif pending_sid is not None and current_id == pending_sid:
                    allow_update = True
                elif pending_sid is not None:
                    logger.info(
                        f"[Subtitle] switched active playback id: {current_id} -> {pending_sid}"
                    )
                    self._current_playing_sentence_id = pending_sid
                    self._last_switch_time = time.time()
                    self._current_playing_sentence = self._pending_subtitle_text
                    allow_update = True

                if allow_update and hooks.subtitle_available and hooks.update_subtitle_display:
                    hooks.update_subtitle_display(self._pending_subtitle_text, "")
                    logger.info(
                        f"[Subtitle] displayed: '{self._pending_subtitle_text[:30]}...'"
                    )
                    try:
                        asyncio.create_task(
                            self._check_and_update_translation(
                                pending_sid, self._pending_subtitle_text
                            )
                        )
                    except Exception:
                        pass

                delattr(self, "_pending_subtitle_text")
                if hasattr(self, "_pending_sentence_id"):
                    delattr(self, "_pending_sentence_id")

        super().play_chunk(combined)

    def play_chunk(self, audio_chunk) -> None:
        if not self.is_playing or self.stream is None:
            return
        if torch.is_tensor(audio_chunk):
            audio_chunk = audio_chunk.cpu().detach().numpy()
        if audio_chunk.dtype != np.float32:
            audio_chunk = audio_chunk.astype(np.float32)

        first_chunk = True
        for i in range(0, len(audio_chunk), self.chunk_size):
            if not self.is_playing:
                break
            chunk = audio_chunk[i : i + self.chunk_size]
            if len(chunk) == 0:
                break
            if first_chunk:
                first_chunk = False
                self._emit_mouth_value_for_audio(chunk, minimum=0.12)
            get_realtime_aec_processor().push_reference(
                chunk,
                getattr(self, "_current_rate", None) or getattr(self, "sample_rate", None) or 24000,
            )
            with self._stream_lock:
                self.stream.write(chunk.tobytes())
            current_time = time.time()
            if current_time - self.last_send_time >= self.send_interval:
                rms = np.sqrt(np.mean(chunk ** 2))
                mouth_value = min(1.0, rms * self.volume_multiplier)
                self.mouth_sink.publish_mouth_value(mouth_value)
                self.last_send_time = current_time

    def _update_subtitle_display(self, current_time: float) -> None:
        hooks = self._hooks
        if not self.current_sentence or not hooks.subtitle_available:
            return
        elapsed = current_time - self.subtitle_start_time
        length = len(self.current_sentence)
        if length <= 20:
            char_dur = 0.06
        elif length <= 50:
            char_dur = 0.08
        else:
            char_dur = 0.10
        expected = min(int(elapsed / char_dur), length)
        if expected > self.subtitle_display_index:
            display_text = self.current_sentence[:expected]
            display_trans = self.current_translation[:expected] if self.current_translation else ""
            self.subtitle_display_index = expected
            try:
                if hooks.update_subtitle_display:
                    hooks.update_subtitle_display(display_text, display_trans)
            except Exception as e:
                logger.warning(f"subtitle update failed: {e}")
