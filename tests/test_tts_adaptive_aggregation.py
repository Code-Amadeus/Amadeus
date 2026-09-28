"""Adaptive grouping must preserve continuity before spending playback cover."""
from __future__ import annotations

import asyncio
import math

import pytest

from tts.contract import TTSRequest
from tts.utterance_scheduler import TTSUtteranceScheduler


@pytest.fixture(autouse=True)
def grouping_settings(monkeypatch):
    monkeypatch.setenv("ENABLE_TTS_UTTERANCE_SCHEDULER", "1")
    monkeypatch.setenv("TTS_UTTERANCE_MIN_START_SEQ", "2")
    monkeypatch.setenv("TTS_UTTERANCE_MAX_SENTENCES", "2")
    monkeypatch.setenv("TTS_UTTERANCE_MAX_CHARS", "80")
    monkeypatch.setenv("TTS_UTTERANCE_FLUSH_TIMEOUT_MS", "350")


def request(seq: int, text: str = "説明を続けると、") -> TTSRequest:
    return TTSRequest(
        sentence_id=f"sentence_{seq}_test", text=text,
        is_first=seq == 1, stream_tts=seq == 1,
        source="chat", turn_id="turn-test",
    )


def scheduler(cover=10.0, rtf=0.5):
    return TTSUtteranceScheduler(
        cover_seconds_getter=lambda: cover, rtf_getter=lambda: rtf,
        deadline_enabled=True, cover_safety_margin_sec=0.5, chars_per_sec=10.0,
    )


class ReadyOnlyQueue(asyncio.Queue):
    async def get(self):
        assert not self.empty(), "a ready utterance must not wait for more text here"
        return self.get_nowait()


def test_short_opening_does_not_delay_second_unit_for_lookahead():
    async def run():
        queue = ReadyOnlyQueue()
        queue.put_nowait(request(2))
        job = await scheduler(cover=0.6, rtf=0.8).next_job(queue)
        assert job.consumed_count == 1
    asyncio.run(run())


def test_first_unit_still_bypasses_grouping():
    async def run():
        queue = ReadyOnlyQueue()
        queue.put_nowait(request(1))
        assert (await scheduler().next_job(queue)).is_first
    asyncio.run(run())


def test_available_cover_allows_more_than_two_comma_fragments():
    async def run():
        queue = ReadyOnlyQueue()
        for seq in range(2, 7):
            queue.put_nowait(request(seq, "説明を続けると、" if seq < 6 else "結論になります。"))
        job = await scheduler().next_job(queue)
        assert job.consumed_count == 5
        assert job.text.endswith("結論になります。")
        assert [s.seq for s in job.segments] == list(range(2, 7))
    asyncio.run(run())


def test_same_cover_produces_smaller_jobs_for_slower_synthesis():
    async def run(rtf):
        queue = asyncio.Queue()
        for seq in range(2, 10):
            queue.put_nowait(request(seq))
        return (await scheduler(cover=3, rtf=rtf).next_job(queue)).consumed_count
    fast = asyncio.run(run(0.2))
    slow = asyncio.run(run(1.0))
    assert fast > slow >= 1


def test_complete_sentence_does_not_wait_for_another_sentence():
    async def run():
        queue = ReadyOnlyQueue()
        queue.put_nowait(request(2, "これで説明は終わりです。"))
        assert (await scheduler().next_job(queue)).consumed_count == 1
    asyncio.run(run())


def test_ready_text_is_consumed_even_when_lookahead_wait_is_disabled(monkeypatch):
    monkeypatch.setenv("TTS_UTTERANCE_FLUSH_TIMEOUT_MS", "0")
    async def run():
        queue = ReadyOnlyQueue()
        for seq in range(2, 6):
            queue.put_nowait(request(seq))
        assert (await scheduler().next_job(queue)).consumed_count == 4
    asyncio.run(run())


def test_char_cap_still_bounds_fast_synthesis_and_preserves_overflow(monkeypatch):
    monkeypatch.setenv("TTS_UTTERANCE_MAX_CHARS", "20")
    async def run():
        queue = asyncio.Queue()
        for seq in range(2, 7):
            queue.put_nowait(request(seq))
        sched = scheduler(rtf=0.01)
        jobs = []
        while sum(job.consumed_count for job in jobs) < 5:
            jobs.append(await sched.next_job(queue))
        assert all(len(job.text) <= 20 for job in jobs)
        assert [s.seq for job in jobs for s in job.segments] == list(range(2, 7))
    asyncio.run(run())


def test_batch_size_is_maximal_and_monotonic_across_synthesis_rates():
    async def count(cover, rtf):
        queue = ReadyOnlyQueue()
        for seq in range(2, 42):
            queue.put_nowait(request(seq))
        return (await scheduler(cover, rtf).next_job(queue)).consumed_count

    size = len(request(2).text)
    previous_by_rate = {}
    for cover in (0.2, 1.0, 3.0, 8.0, 30.0):
        previous = math.inf
        for rtf in (0.1, 0.2, 0.5, 0.9, 1.3):
            actual = asyncio.run(count(cover, rtf))
            expected = max(1, min(80 // size, math.floor((cover - 0.5) * 10 / rtf / size + 1e-9)))
            assert actual == expected
            assert actual <= previous  # a slower engine must not batch more
            assert actual >= previous_by_rate.get(rtf, 1)
            previous = actual
            previous_by_rate[rtf] = actual


def test_lookahead_wait_cannot_spend_the_synthesis_reserve(monkeypatch):
    import tts.utterance_scheduler as module
    waits = []

    async def wait_for(awaitable, timeout):
        awaitable.close()
        waits.append(timeout)
        raise asyncio.TimeoutError

    monkeypatch.setattr(module.asyncio, "wait_for", wait_for)
    async def run():
        queue = asyncio.Queue()
        queue.put_nowait(request(2))
        # 0.5 s margin + 0.8 s synthesis leaves only 0.05 s for text.
        await scheduler(cover=1.35, rtf=1.0).next_job(queue)
    asyncio.run(run())
    assert len(waits) == 1
    assert 0 < waits[0] <= 0.05000001


def test_text_arriving_while_synthesis_slot_is_busy_can_join():
    async def run():
        queue = ReadyOnlyQueue()
        queue.put_nowait(request(2))
        entered, available = asyncio.Event(), asyncio.Event()

        async def before_grouping(_segment):
            entered.set()
            await available.wait()
            return True

        task = asyncio.create_task(scheduler().next_job(queue, before_grouping=before_grouping))
        await entered.wait()
        for seq in range(3, 7):
            queue.put_nowait(request(seq, "続けて説明すると、" if seq < 6 else "これで完成です。"))
        available.set()
        assert (await task).consumed_count == 5
    asyncio.run(run())


def test_cancelled_slot_wait_preserves_the_first_request():
    async def run():
        queue = ReadyOnlyQueue()
        queue.put_nowait(request(2, "完成です。"))
        entered = asyncio.Event()
        sched = scheduler()

        async def before_grouping(_segment):
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(sched.next_job(queue, before_grouping=before_grouping))
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert (await sched.next_job(queue)).utterance_id == "sentence_2_test"
    asyncio.run(run())


def test_cost_estimate_reacts_to_slowdowns_and_recovers_gradually(monkeypatch):
    from tts import pipeline
    monkeypatch.setattr(pipeline, "TTS_CHARS_PER_SEC", 10)
    monkeypatch.setattr(pipeline, "_rtf_ema", 0.5)
    monkeypatch.setattr(pipeline, "_last_synthesis_chars", None)
    pipeline._update_rtf_ema(2, 10)  # slowdown: 2 seconds per 10 chars
    assert pipeline.get_rtf_estimate() == 2
    pipeline._update_rtf_ema(0.2, 10)
    assert 0.2 < pipeline.get_rtf_estimate() < 2
    assert pipeline._last_synthesis_chars == 10


def test_growth_is_bounded_by_measured_request_size_even_with_excess_cover():
    async def run():
        previous_chars = 8
        sched = TTSUtteranceScheduler(
            cover_seconds_getter=lambda: 100, rtf_getter=lambda: 0.1,
            last_synthesis_chars_getter=lambda: previous_chars,
        )
        queue = ReadyOnlyQueue()
        for seq in range(2, 40):
            queue.put_nowait(request(seq))
        sizes = []
        for _ in range(4):
            job = await sched.next_job(queue)
            assert len(job.text) <= 2 * previous_chars
            previous_chars = len(job.text)
            sizes.append(previous_chars)
        assert sizes == [16, 32, 64, 80]
    asyncio.run(run())


def test_disabled_adaptation_keeps_the_explicit_fragment_limit():
    async def run():
        sched = TTSUtteranceScheduler(
            cover_seconds_getter=lambda: 100, rtf_getter=lambda: 0.1,
            last_synthesis_chars_getter=lambda: 4, deadline_enabled=False,
        )
        queue = ReadyOnlyQueue()
        for seq in range(2, 5):
            queue.put_nowait(request(seq))
        assert (await sched.next_job(queue)).consumed_count == 2
    asyncio.run(run())


def test_late_llm_fragment_can_complete_a_sentence_within_the_wait_budget():
    async def run():
        queue = asyncio.Queue()
        queue.put_nowait(request(2))
        task = asyncio.create_task(scheduler().next_job(queue))
        await asyncio.sleep(0)
        queue.put_nowait(request(3, "これで完成です。"))
        job = await task
        assert job.consumed_count == 2
        assert job.text.endswith("これで完成です。")
    asyncio.run(run())


def test_completed_audio_keeps_its_cover_while_waiting_for_playback():
    import numpy as np
    from tts.playback import PlaybackManager

    async def run():
        manager = PlaybackManager(player_instance=object())
        manager.player_is_ready.clear()
        await manager.add_to_playlist(np.zeros(72000, dtype=np.float32), 24000,
                                      'sentence_1_test', 'synthetic')
        task = asyncio.create_task(manager.run())
        try:
            for _ in range(10):
                if manager._normal_waiting_seq is not None:
                    break
                await asyncio.sleep(0)
            assert manager._normal_waiting_seq == 1
            assert manager.estimate_cover_seconds() == 3.0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("invalidate", [False, True])
def test_worker_groups_after_permit_and_keeps_pre_wait_epoch(monkeypatch, invalidate):
    from tts import pipeline
    async def run():
        queue = asyncio.Queue()
        permit = asyncio.BoundedSemaphore(1)
        await permit.acquire()
        entered = asyncio.Event()
        calls = []

        async def gate(_turn):
            entered.set()
            return "go"

        async def synth(text, _sid, _first, **kwargs):
            calls.append(text)
            kwargs['task_semaphore'].release()

        monkeypatch.setattr(pipeline, "_tts_interrupt_epoch", 7)
        monkeypatch.setattr(pipeline, "_exp_tts_semaphore", permit)
        monkeypatch.setattr(pipeline, "_pending_sentence_items", queue)
        monkeypatch.setattr(pipeline, "_utterance_scheduler", scheduler())
        monkeypatch.setattr(pipeline, "_gate_job_turn", gate)
        monkeypatch.setattr(pipeline, "select_synthesis", lambda *_, **__: ("test", synth))
        queue.put_nowait(request(2, "続けて説明すると、"))
        worker = asyncio.create_task(pipeline.play_sentence_worker())
        try:
            await entered.wait()
            if invalidate:
                pipeline._tts_interrupt_epoch = 8
            else:
                for seq in range(3, 7):
                    queue.put_nowait(request(seq, "続けて説明すると、" if seq < 6 else "これで完成です。"))
            permit.release()
            await asyncio.wait_for(queue.join(), 1)
            # Acquiring this proves that the child either released ownership
            # after synthesis, or the stale-job path returned it to the pool.
            await asyncio.wait_for(permit.acquire(), 1)
            permit.release()
            assert len(calls) == (0 if invalidate else 1)
            if calls:
                assert calls[0].count("続けて説明すると、") == 4
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())
