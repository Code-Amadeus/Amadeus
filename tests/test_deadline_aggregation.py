"""Deadline-aware TTS aggregation tests.

运行：.venv\\Scripts\\python.exe -X utf8 tests\\test_deadline_aggregation.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tts.contract import TTSRequest
from tts.deadline import deadline_budget_exceeded, estimate_synthesis_seconds
from tts.utterance_scheduler import TTSUtteranceScheduler


def _enable_scheduler_env() -> None:
    os.environ["ENABLE_TTS_UTTERANCE_SCHEDULER"] = "1"
    os.environ["TTS_UTTERANCE_MIN_START_SEQ"] = "2"
    os.environ["TTS_UTTERANCE_FLUSH_TIMEOUT_MS"] = "50"
    os.environ["TTS_UTTERANCE_MAX_CHARS"] = "120"


def _clear_scheduler_env() -> None:
    for key in (
        "ENABLE_TTS_UTTERANCE_SCHEDULER",
        "TTS_UTTERANCE_MIN_START_SEQ",
        "TTS_UTTERANCE_FLUSH_TIMEOUT_MS",
        "TTS_UTTERANCE_MAX_CHARS",
    ):
        os.environ.pop(key, None)


async def _two_segment_job(sched: TTSUtteranceScheduler):
    q: asyncio.Queue = asyncio.Queue()
    await q.put(TTSRequest(sentence_id="sentence_2_a", text="つづき、", source="chat", turn_id="t1"))
    await q.put(TTSRequest(sentence_id="sentence_3_b", text="そのまま。", source="chat", turn_id="t1"))
    return await sched.next_job(q), q


def test_deadline_math_boundaries():
    assert estimate_synthesis_seconds(15, rtf=0.5, chars_per_sec=7.5) == 1.0
    assert deadline_budget_exceeded(
        15,
        cover_seconds_getter=lambda: 10.0,
        rtf_getter=lambda: 0.5,
        cover_safety_margin_sec=1.5,
        chars_per_sec=7.5,
    ) is False
    assert deadline_budget_exceeded(
        1,
        cover_seconds_getter=lambda: 0.0,
        rtf_getter=lambda: 0.5,
        cover_safety_margin_sec=1.5,
        chars_per_sec=7.5,
    ) is True
    assert deadline_budget_exceeded(
        300,
        cover_seconds_getter=lambda: 3.0,
        rtf_getter=lambda: 0.6,
        cover_safety_margin_sec=1.5,
        chars_per_sec=7.5,
    ) is True


def test_scheduler_merges_when_cover_is_sufficient():
    async def run():
        _enable_scheduler_env()
        try:
            sched = TTSUtteranceScheduler(
                cover_seconds_getter=lambda: 30.0,
                rtf_getter=lambda: 0.5,
                deadline_enabled=True,
                cover_safety_margin_sec=1.5,
                chars_per_sec=7.5,
            )
            job, _ = await _two_segment_job(sched)
            assert job.is_merged and job.consumed_count == 2
        finally:
            _clear_scheduler_env()

    asyncio.run(run())


def test_scheduler_rejects_merge_when_cover_is_tight():
    async def run():
        _enable_scheduler_env()
        try:
            sched = TTSUtteranceScheduler(
                cover_seconds_getter=lambda: 0.2,
                rtf_getter=lambda: 0.6,
                deadline_enabled=True,
                cover_safety_margin_sec=1.5,
                chars_per_sec=7.5,
            )
            job1, q = await _two_segment_job(sched)
            assert not job1.is_merged and job1.utterance_id == "sentence_2_a"
            job2 = await sched.next_job(q)
            assert job2.utterance_id == "sentence_3_b"
        finally:
            _clear_scheduler_env()

    asyncio.run(run())


def test_scheduler_degrades_when_estimator_unavailable():
    async def run():
        _enable_scheduler_env()
        try:
            sched_none = TTSUtteranceScheduler(
                cover_seconds_getter=lambda: None,
                rtf_getter=lambda: 0.6,
                deadline_enabled=True,
            )
            job_none, _ = await _two_segment_job(sched_none)
            assert job_none.is_merged

            def _boom():
                raise RuntimeError("no estimate")

            sched_error = TTSUtteranceScheduler(
                cover_seconds_getter=_boom,
                rtf_getter=lambda: 0.6,
                deadline_enabled=True,
            )
            job_error, _ = await _two_segment_job(sched_error)
            assert job_error.is_merged
        finally:
            _clear_scheduler_env()

    asyncio.run(run())


def test_scheduler_switch_off_preserves_existing_merge():
    async def run():
        _enable_scheduler_env()
        try:
            sched = TTSUtteranceScheduler(
                cover_seconds_getter=lambda: 0.0,
                rtf_getter=lambda: 10.0,
                deadline_enabled=False,
            )
            job, _ = await _two_segment_job(sched)
            assert job.is_merged and job.consumed_count == 2
        finally:
            _clear_scheduler_env()

    asyncio.run(run())


def test_playback_estimate_cover_seconds():
    from tts.playback import PlaybackManager

    pm = PlaybackManager(player_instance=object())
    pm._clock = lambda: 100.0
    assert pm.estimate_cover_seconds() == 0.0

    pm.pending_audio[2] = (0, np.zeros(24000, dtype=np.float32), 24000, "s2", "", None)
    pm.pending_audio[3] = (np.zeros(12000, dtype=np.float32), 24000, "s3", "", None)
    assert abs(pm.estimate_cover_seconds() - 1.5) < 0.001

    pm.pending_audio.clear()
    pm._current_audio_started_at = 90.0
    pm._current_audio_duration_sec = 12.0
    assert abs(pm.estimate_cover_seconds() - 2.0) < 0.001


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all deadline aggregation tests passed")


if __name__ == "__main__":
    _main()
