"""Scheduling uses engine readiness and accounts for queued playback."""
import asyncio
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
        cover_seconds_getter=lambda: cover, synthesis_seconds_getter=lambda text: len(text) * rtf / 10,
        deadline_enabled=True, cover_safety_margin_sec=0.5,
    )


class ReadyOnlyQueue(asyncio.Queue):
    async def get(self):
        assert not self.empty(), "a ready utterance must not wait for more text here"
        return self.get_nowait()


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
        for seq in range(3, 4):
            queue.put_nowait(request(seq, "続けて説明すると、" if seq < 3 else "これで完成です。"))
        available.set()
        assert (await task).consumed_count == 2
    asyncio.run(run())


def test_cancelled_slot_wait_preserves_the_first_request():
    async def run():
        queue = asyncio.Queue()
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
        assert manager.pending_audio == {}
        assert manager._normal_waiting_seq is None
        assert manager.estimate_cover_seconds() == 0.0
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
                for seq in range(3, 4):
                    queue.put_nowait(request(seq, "続けて説明すると、" if seq < 3 else "これで完成です。"))
            permit.release()
            await asyncio.wait_for(queue.join(), 1)
            # Acquiring this proves that the child either released ownership
            # after synthesis, or the stale-job path returned it to the pool.
            await asyncio.wait_for(permit.acquire(), 1)
            permit.release()
            assert len(calls) == (0 if invalidate else 1)
            if calls:
                assert calls[0].count("続けて説明すると、") == 1
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())
