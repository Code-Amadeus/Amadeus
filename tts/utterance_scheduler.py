"""Utterance-level TTS scheduling.

This module intentionally stays independent from GUI, VTS, and playback
details. It turns sentence queue items into synthesis jobs. A job may contain
one sentence or a small group of consecutive sentences that should be
synthesized as a single utterance.
"""

from __future__ import annotations

import asyncio
import os
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from tts.contract import TTSRequest
from tts.deadline import playback_budget_seconds
from tools.text_utils import STRONG_SENTENCE_ENDINGS


_SENTENCE_ID_RE = re.compile(r"sentence_(\d+)_")
_SENTENCE_END_RE = re.compile(
    "[" + re.escape("".join(sorted(STRONG_SENTENCE_ENDINGS))) + r'''][\s"'”’」』）)\]】]*$'''
)
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
    emotion: str = ""

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
    emotion: str = ""

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
      TTS_UTTERANCE_MAX_SENTENCES, when set, strictly caps fragments per job.
      TTS_UTTERANCE_MAX_CHARS caps merged text length.
      TTS_UTTERANCE_FLUSH_TIMEOUT_MS controls how long to wait for lookahead.
      ENABLE_TTS_KV_WINDOW is reserved for future AR KV-window experiments.
    """

    def __init__(
        self,
        logger=None,
        *,
        cover_seconds_getter: Callable[[], float | None] | None = None,
        synthesis_seconds_getter: Callable[[str], float | None] | None = None,
        deadline_enabled: bool | None = None,
        cover_safety_margin_sec: float | None = None,
    ):
        self.logger = logger
        self._buffer: list[Any] = []  # TTSRequest 或旧元组（过渡期）
        self._cover_seconds_getter = cover_seconds_getter
        self._synthesis_seconds_getter = synthesis_seconds_getter
        self._deadline_enabled = deadline_enabled
        self._cover_safety_margin_sec = cover_safety_margin_sec

    def configure_deadline(
        self,
        *,
        cover_seconds_getter: Callable[[], float | None] | None = None,
        synthesis_seconds_getter: Callable[[str], float | None] | None = None,
    ) -> None:
        self._cover_seconds_getter = cover_seconds_getter
        self._synthesis_seconds_getter = synthesis_seconds_getter

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
    def max_sentences(self) -> int | None:
        if not os.environ.get("TTS_UTTERANCE_MAX_SENTENCES", "").strip():
            return None
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

    async def next_job(
        self, queue: asyncio.Queue, *,
        before_grouping: Callable[[UtteranceSegment], Awaitable[bool]] | None = None,
    ) -> UtteranceJob:
        first_item = await self._get_next_item(queue)
        try:
            first_segment = self._to_segment(first_item)
        except (TypeError, ValueError):
            queue.task_done()
            raise

        # The host owns turn permission and the synthesis slot. Text can keep
        # accumulating while it waits; only then choose the final utterance.
        if before_grouping is not None:
            try:
                ready = await before_grouping(first_segment)
            except asyncio.CancelledError:
                self._buffer.insert(0, first_item)
                raise
            except Exception:
                queue.task_done()
                raise
            if not ready:
                return self._make_job([first_segment])

        if not self._can_start_merge(first_segment):
            return self._make_job([first_segment])

        loop = asyncio.get_running_loop()
        started = loop.time()
        flush_deadline = started + self.flush_timeout
        budget = playback_budget_seconds(
            self._cover_seconds_getter, enabled=self._deadline_enabled,
            cover_safety_margin_sec=self._cover_safety_margin_sec, logger=self.logger,
        )
        text = first_segment.text.strip()
        try:
            predicted = await self._predict(text) if budget is not None else None
        except asyncio.CancelledError:
            self._buffer.insert(0, first_item)
            raise
        finish_deadline = started + budget if predicted is not None else None
        count_limit = self.max_sentences
        if finish_deadline is None and count_limit is None:
            count_limit = 3
        char_limit = self.max_chars
        segments = [first_segment]
        owned_items = [first_item]
        reason = "sentence_end"
        try:
            while not self._is_hard_boundary(segments[-1].text):
                if count_limit is not None and len(segments) >= count_limit:
                    reason = "fragment_limit"
                    break
                if len(text) >= char_limit:
                    reason = "character_limit"
                    break
                slack = (finish_deadline - loop.time() - predicted
                         if finish_deadline is not None else None)
                if slack is not None and slack <= 0:
                    reason = "synthesis_budget"
                    break
                try:
                    item = self._buffer.pop(0) if self._buffer else queue.get_nowait()
                except asyncio.QueueEmpty:
                    wait = flush_deadline - loop.time()
                    if slack is not None:
                        wait = min(wait, slack)
                    if wait <= 0:
                        reason = "lookahead_limit"
                        break
                    try:
                        item = await asyncio.wait_for(self._get_next_item(queue), timeout=wait)
                    except asyncio.TimeoutError:
                        reason = "lookahead_limit"
                        break
                try:
                    segment = self._to_segment(item)
                except (TypeError, ValueError):
                    # A malformed item is rejected once; valid earlier items
                    # remain recoverable without requeuing the poison item.
                    queue.task_done()
                    self._buffer[0:0] = owned_items
                    raise
                owned_items.append(item)
                candidate = text + segment.text.strip()
                if not self._can_append(segments, segment):
                    reason = "identity_boundary"
                elif len(candidate) > char_limit:
                    reason = "character_limit"
                else:
                    candidate_cost = await self._predict(candidate) if finish_deadline is not None else None
                    if finish_deadline is not None and (
                        candidate_cost is None or loop.time() + candidate_cost > finish_deadline
                    ):
                        reason = "synthesis_budget"
                    else:
                        segments.append(segment)
                        text, predicted = candidate, candidate_cost
                        continue
                self._buffer.insert(0, owned_items.pop())
                break
        except asyncio.CancelledError:
            self._buffer[0:0] = owned_items
            raise

        job = self._make_job(segments)
        if self.logger is not None:
            self.logger.info(
                "[UtteranceScheduler] reason=%s fragments=%d chars=%d predicted=%s budget=%s",
                reason, len(segments), len(text), predicted, budget,
            )
        return job

    async def _predict(self, text: str) -> float | None:
        if self._synthesis_seconds_getter is None:
            return None
        try:
            value = await asyncio.to_thread(self._synthesis_seconds_getter, text)
            if value is not None and math.isfinite(value) and value >= 0:
                return float(value)
        except Exception:
            if self.logger is not None:
                self.logger.debug("synthesis cost unavailable", exc_info=True)
        return None

    async def _get_next_item(self, queue: asyncio.Queue) -> Any:
        if self._buffer:
            return self._buffer.pop(0)
        return await queue.get()

    def _to_segment(self, item: Any) -> UtteranceSegment:
        request = TTSRequest.from_queue_item(item)
        if not isinstance(request.sentence_id, str) or not isinstance(request.text, str):
            raise TypeError("TTS queue item requires string identity and text")
        return UtteranceSegment(
            sentence_id=request.sentence_id,
            text=request.text,
            is_first=request.is_first,
            stream_tts=request.stream_tts,
            source=request.source,
            turn_id=request.turn_id,
            tts_epoch=request.tts_epoch,
            emotion=request.emotion,
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
            emotion=first.emotion,
        )

    def _can_start_merge(self, segment: UtteranceSegment) -> bool:
        if not self.enabled or self.flush_timeout <= 0:
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
            or segment.emotion != current[-1].emotion
        ):
            return False
        if not self._is_consecutive(current[-1], segment):
            return False
        return True

    def _is_consecutive(self, prev: UtteranceSegment, nxt: UtteranceSegment) -> bool:
        if prev.seq <= 0 or nxt.seq <= 0:
            return True
        return nxt.seq == prev.seq + 1

    def _is_punctuation_only(self, text: str) -> bool:
        return not text.strip() or bool(_PUNCT_RE.match(text.strip()))

    def _is_hard_boundary(self, text: str) -> bool:
        return bool(_SENTENCE_END_RE.search(text))


def _get_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default
