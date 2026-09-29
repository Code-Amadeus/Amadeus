"""Adaptive grouping must preserve continuity before spending playback cover."""
from __future__ import annotations

import asyncio

import pytest

from tts.contract import TTSRequest
from tts.utterance_scheduler import TTSUtteranceScheduler


@pytest.fixture(autouse=True)
def grouping_settings(monkeypatch):
    monkeypatch.setenv("ENABLE_TTS_UTTERANCE_SCHEDULER", "1")
    monkeypatch.setenv("TTS_UTTERANCE_MIN_START_SEQ", "2")
    monkeypatch.delenv("TTS_UTTERANCE_MAX_SENTENCES", raising=False)
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
        cover_seconds_getter=lambda: cover, synthesis_seconds_getter=lambda text: len(text) * rtf / 10,
        deadline_enabled=True, cover_safety_margin_sec=0.5,
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


def test_zero_flush_preserves_no_merge_contract(monkeypatch):
    monkeypatch.setenv("TTS_UTTERANCE_FLUSH_TIMEOUT_MS", "0")
    async def run():
        queue = ReadyOnlyQueue()
        for seq in range(2, 6):
            queue.put_nowait(request(seq))
        assert (await scheduler().next_job(queue)).consumed_count == 1
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


@pytest.mark.parametrize('ending', ['。', '.', '!', '?', '！', '？', '。』', '."', '。）」', '\n'])
def test_sentence_end_stops_even_with_ready_next_sentence(ending):
    async def run():
        queue = ReadyOnlyQueue()
        queue.put_nowait(request(2, '前半、'))
        queue.put_nowait(request(3, '結論' + ending))
        queue.put_nowait(request(4, '次の文。'))
        sched = scheduler()
        assert (await sched.next_job(queue)).consumed_count == 2
        assert (await sched.next_job(queue)).utterance_id == 'sentence_4_test'
    asyncio.run(run())


@pytest.mark.parametrize('cap', ['1', '2', '3', 'invalid'])
def test_explicit_fragment_cap_is_always_honored(monkeypatch, cap):
    monkeypatch.setenv('TTS_UTTERANCE_MAX_SENTENCES', cap)
    async def run():
        queue = ReadyOnlyQueue()
        for seq in range(2, 10):
            queue.put_nowait(request(seq))
        expected = 3 if cap == 'invalid' else int(cap)
        assert (await scheduler(cover=100).next_job(queue)).consumed_count == expected
    asyncio.run(run())


@pytest.mark.parametrize('enabled,cover,predict', [(False, 100, lambda _: .1), (True, None, lambda _: .1),
                                                 (True, 100, lambda _: None)])
def test_unavailable_timing_uses_three_fragment_fallback(enabled, cover, predict):
    async def run():
        queue = ReadyOnlyQueue()
        for seq in range(2, 10):
            queue.put_nowait(request(seq))
        sched = TTSUtteranceScheduler(cover_seconds_getter=lambda: cover,
                                     synthesis_seconds_getter=predict, deadline_enabled=enabled)
        assert (await sched.next_job(queue)).consumed_count == 3
    asyncio.run(run())


def test_cover_is_sampled_once_per_group():
    observations = []
    async def run():
        def cover():
            observations.append(1)
            return 20
        sched = TTSUtteranceScheduler(cover_seconds_getter=cover,
                                     synthesis_seconds_getter=lambda _: .1, deadline_enabled=True)
        queue = ReadyOnlyQueue()
        for seq in range(2, 8):
            queue.put_nowait(request(seq, '説明、' if seq < 7 else '終わり。'))
        assert (await sched.next_job(queue)).consumed_count == 6
    asyncio.run(run())
    assert len(observations) == 1


def test_cancellation_restores_every_consumed_fragment():
    async def run():
        entered = asyncio.Event()
        class Queue(asyncio.Queue):
            async def get(self):
                if self.empty():
                    entered.set()
                return await super().get()
        queue = Queue()
        queue.put_nowait(request(2))
        queue.put_nowait(request(3))
        sched = scheduler()
        task = asyncio.create_task(sched.next_job(queue))
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        queue.put_nowait(request(4, '完成です。'))
        job = await sched.next_job(queue)
        assert [s.seq for s in job.segments] == [2, 3, 4]
        for _ in job.segments:
            queue.task_done()
        await asyncio.wait_for(queue.join(), 1)
    asyncio.run(run())
