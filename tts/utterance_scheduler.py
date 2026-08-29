"""Utterance-level TTS scheduling.

This module intentionally stays independent from GUI, VTS, and playback
details. It turns sentence queue items into synthesis jobs. A job may contain
one sentence or a small group of consecutive sentences that should be
synthesized as a single utterance.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tts.contract import TTSRequest
from tts.deadline import deadline_budget_exceeded


_SENTENCE_ID_RE = re.compile(r"sentence_(\d+)_")
_PUNCT_RE = re.compile(r"^[\s,.;:!?，。！？、…「」『』（）()［］\[\]【】\-ー~～]+$")


@dataclass(slots=True)
class UtteranceSegment:
    sentence_id: str
    text: str
    is_first: bool = False
    stream_tts: bool | None = None
    source: str = "legacy"
    turn_id: str = ""
    tts_epoch: int | None = None

    @property
    def seq(self) -> int:
        match = _SENTENCE_ID_RE.search(self.sentence_id)
        return int(match.group(1)) if match else 0


@dataclass(slots=True)
class UtteranceJob:
    utterance_id: str
    text: str
    segments: list[UtteranceSegment]
    is_first: bool
    stream_tts: bool | None
    source: str = "legacy"
    turn_id: str = ""
    tts_epoch: int | None = None

    @property
    def consumed_count(self) -> int:
        return len(self.segments)

    @property
    def is_merged(self) -> bool:
        return len(self.segments) > 1

    def playback_segments(self) -> list[dict[str, Any]]:
        return [
            {
                "sentence_id": segment.sentence_id,
                "text": segment.text,
                "seq": segment.seq,
            }
            for segment in self.segments
        ]


class TTSUtteranceScheduler:
    """Build synthesis jobs from sentence queue items.

    Environment switches:
      ENABLE_TTS_UTTERANCE_SCHEDULER=1 enables multi-sentence jobs.
      TTS_UTTERANCE_MIN_START_SEQ gates merging until playback has buffer.
      TTS_UTTERANCE_MAX_SENTENCES caps sentences per job.
      TTS_UTTERANCE_MAX_CHARS caps merged text length.
      TTS_UTTERANCE_FLUSH_TIMEOUT_MS controls how long to wait for lookahead.
      ENABLE_TTS_KV_WINDOW is reserved for future AR KV-window experiments.
    """

    def __init__(
        self,
        logger=None,
        *,
        cover_seconds_getter: Callable[[], float | None] | None = None,
        rtf_getter: Callable[[], float | None] | None = None,
        deadline_enabled: bool | None = None,
        cover_safety_margin_sec: float | None = None,
        chars_per_sec: float | None = None,
    ):
        self.logger = logger
        self._buffer: list[Any] = []  # TTSRequest 或旧元组（过渡期）
        self._cover_seconds_getter = cover_seconds_getter
        self._rtf_getter = rtf_getter
        self._deadline_enabled = deadline_enabled
        self._cover_safety_margin_sec = cover_safety_margin_sec
        self._chars_per_sec = chars_per_sec

    def configure_deadline(
        self,
        *,
        cover_seconds_getter: Callable[[], float | None] | None = None,
        rtf_getter: Callable[[], float | None] | None = None,
    ) -> None:
        self._cover_seconds_getter = cover_seconds_getter
        self._rtf_getter = rtf_getter

    def clear(self) -> int:
        count = len(self._buffer)
        self._buffer.clear()
        return count

    def discard(self, predicate: Callable[[TTSRequest], bool]) -> int:
        """Remove buffered requests matching a caller-owned supersession rule."""

        kept: list[Any] = []
        discarded = 0
        for item in self._buffer:
            try:
                request = TTSRequest.from_queue_item(item)
            except TypeError:
                kept.append(item)
                continue
            if predicate(request):
                discarded += 1
            else:
                kept.append(item)
        self._buffer = kept
        return discarded

    @property
    def enabled(self) -> bool:
        return os.environ.get("ENABLE_TTS_UTTERANCE_SCHEDULER", "0") == "1"

    @property
    def kv_window_enabled(self) -> bool:
        return os.environ.get("ENABLE_TTS_KV_WINDOW", "0") == "1"

    @property
    def max_sentences(self) -> int:
        return max(1, _get_int_env("TTS_UTTERANCE_MAX_SENTENCES", 3))

    @property
    def min_start_seq(self) -> int:
        return max(2, _get_int_env("TTS_UTTERANCE_MIN_START_SEQ", 4))

    @property
    def max_chars(self) -> int:
        return max(1, _get_int_env("TTS_UTTERANCE_MAX_CHARS", 120))

    @property
    def flush_timeout(self) -> float:
        return max(0.0, _get_int_env("TTS_UTTERANCE_FLUSH_TIMEOUT_MS", 120) / 1000.0)

    async def next_job(self, queue: asyncio.Queue) -> UtteranceJob:
        first_item = await self._get_next_item(queue)
        first_segment = self._to_segment(first_item)

        if not self._can_start_merge(first_segment):
            return self._make_job([first_segment])

        deadline = asyncio.get_running_loop().time() + self.flush_timeout
        segments = [first_segment]

        while len(segments) < self.max_sentences:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                item = await asyncio.wait_for(self._get_next_item(queue), timeout=remaining)
            except asyncio.TimeoutError:
                break

            segment = self._to_segment(item)
            if not self._can_append(segments, segment):
                self._buffer.insert(0, item)
                break
            segments.append(segment)

        job = self._make_job(segments)
        if job.is_merged and self.logger is not None:
            self.logger.info(
                "[UtteranceScheduler] merged %d sentence(s): %s chars=%d",
                len(job.segments),
                job.utterance_id,
                len(job.text),
            )
        return job

    async def _get_next_item(self, queue: asyncio.Queue) -> Any:
        if self._buffer:
            return self._buffer.pop(0)
        return await queue.get()

    def _to_segment(self, item: Any) -> UtteranceSegment:
        request = TTSRequest.from_queue_item(item)
        return UtteranceSegment(
            sentence_id=request.sentence_id,
            text=request.text,
            is_first=request.is_first,
            stream_tts=request.stream_tts,
            source=request.source,
            turn_id=request.turn_id,
            tts_epoch=request.tts_epoch,
        )

    def _make_job(self, segments: list[UtteranceSegment]) -> UtteranceJob:
        text = "".join(segment.text.strip() for segment in segments)
        first = segments[0]
        return UtteranceJob(
            utterance_id=first.sentence_id,
            text=text,
            segments=segments,
            is_first=first.is_first,
            stream_tts=first.stream_tts,
            source=first.source,
            turn_id=first.turn_id,
            tts_epoch=first.tts_epoch,
        )

    def _can_start_merge(self, segment: UtteranceSegment) -> bool:
        if not self.enabled:
            return False
        if segment.is_first or segment.stream_tts:
            return False
        if 0 < segment.seq < self.min_start_seq:
            return False
        if self._is_punctuation_only(segment.text):
            return False
        return True

    def _can_append(self, current: list[UtteranceSegment], segment: UtteranceSegment) -> bool:
        if segment.is_first or segment.stream_tts:
            return False
        if self._is_punctuation_only(segment.text):
            return False
        # 契约增益：不同轮次/来源的句子绝不合并为同一 utterance
        if (
            segment.turn_id != current[-1].turn_id
            or segment.source != current[-1].source
            or segment.tts_epoch != current[-1].tts_epoch
        ):
            return False
        if not self._is_consecutive(current[-1], segment):
            return False
        merged_len = sum(len(item.text.strip()) for item in current) + len(segment.text.strip())
        if merged_len > self.max_chars:
            return False
        over_budget = deadline_budget_exceeded(
            merged_len,
            cover_seconds_getter=self._cover_seconds_getter,
            rtf_getter=self._rtf_getter,
            enabled=self._deadline_enabled,
            cover_safety_margin_sec=self._cover_safety_margin_sec,
            chars_per_sec=self._chars_per_sec,
            logger=self.logger,
        )
        if over_budget is True:
            if self.logger is not None:
                self.logger.info(
                    "[UtteranceScheduler] deadline budget tight; refusing merge chars=%d",
                    merged_len,
                )
            return False
        if self._is_hard_boundary(current[-1].text):
            return False
        return True

    def _is_consecutive(self, prev: UtteranceSegment, nxt: UtteranceSegment) -> bool:
        if prev.seq <= 0 or nxt.seq <= 0:
            return True
        return nxt.seq == prev.seq + 1

    def _is_punctuation_only(self, text: str) -> bool:
        return not text.strip() or bool(_PUNCT_RE.match(text.strip()))

    def _is_hard_boundary(self, text: str) -> bool:
        stripped = text.rstrip()
        return stripped.endswith(("?", "!", "？", "！"))


def _get_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default
