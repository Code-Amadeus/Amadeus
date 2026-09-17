"""Blocking inference must leave the voice event loop responsive in every mode."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest

from tts import pipeline
from tts.synthesis_backend import select_synthesis


class Playback:
    def __init__(self):
        self.audio = []

    async def add_streaming_chunk(self, audio, _sr, _sid, _text, **kwargs):
        self.audio.append((audio.copy(), kwargs))

    async def add_to_playlist(self, audio, _sr, _sid, _text, **kwargs):
        self.audio.append((audio.copy(), kwargs))


@pytest.fixture
def voice_pipeline(monkeypatch):
    playback = Playback()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-tts") as executor:
        monkeypatch.setattr(pipeline, "_tts_executor", executor)
        monkeypatch.setattr(pipeline, "_playback_manager", playback)
        monkeypatch.setattr(pipeline, "_tts_interrupt_epoch", 7)
        monkeypatch.setattr(pipeline, "correct_pronunciation_for_tts", lambda text: text)
        monkeypatch.setattr(pipeline, "get_first_sentence_audio_cache", lambda: SimpleNamespace(
            lookup=lambda *_: None, store=lambda *_, **__: None,
        ))
        yield playback


def synthesis(mode, *, epoch=7, semaphore=None):
    _, synth = select_synthesis(
        None, cuda_graph_enabled=mode == "graph", experimental_enabled=mode == "experimental",
        backends=pipeline._SYNTHESIS_BACKENDS,
    )
    return synth(
        "hello", "sentence_1_test", True, stream_tts=False,
        segments=None, interrupt_epoch=epoch, task_semaphore=semaphore,
    )


@pytest.mark.parametrize("mode", ["enhanced", "experimental", "graph"])
@pytest.mark.parametrize("interrupt", [False, True])
def test_blocking_synthesis_allows_asr_work_and_drops_interrupted_audio(monkeypatch, voice_pipeline, mode, interrupt):
    async def run():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        closed = threading.Event()
        worker_names = []

        def infer_stream(**_kwargs):
            worker_names.append(threading.current_thread().name)

            def chunks():
                try:
                    loop.call_soon_threadsafe(entered.set)
                    assert release.wait(3), "event loop did not release the inference worker"
                    yield 24000, np.array([0.1, 0.2], dtype=np.float32), "hello"
                    yield 24000, np.array([0.3], dtype=np.float32), ""
                    yield 24000, np.array([0.4], dtype=np.float32), ""
                finally:
                    worker_names.append(threading.current_thread().name)
                    closed.set()
            return chunks()

        monkeypatch.setattr(pipeline, "_tts_runtime", SimpleNamespace(infer_stream=infer_stream, deployment="embedded"))
        monkeypatch.setattr(pipeline, "graph_tts_lock", asyncio.Lock())
        semaphore = asyncio.BoundedSemaphore(1)
        await semaphore.acquire()
        task = asyncio.create_task(synthesis(mode, semaphore=semaphore))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert not task.done()
            # ASR uses the default executor; it must remain usable during TTS.
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "asr-ready"), 1) == "asr-ready"
            if interrupt:
                pipeline._tts_interrupt_epoch += 1
            release.set()
            await asyncio.wait_for(task, 2)
            assert await asyncio.to_thread(closed.wait, 1)
            await asyncio.wait_for(semaphore.acquire(), 1)
            semaphore.release()
            if interrupt:
                assert voice_pipeline.audio == []
                # The same worker and graph lock must accept the next turn.
                await asyncio.wait_for(synthesis(mode, epoch=8), 2)
            audio = np.concatenate([item[0] for item in voice_pipeline.audio])
            np.testing.assert_allclose(audio, [0.1, 0.2, 0.3, 0.4])
            if mode == "enhanced":
                assert [len(item[0]) for item in voice_pipeline.audio] == [3, 1]
                assert [item[1]["is_first_chunk"] for item in voice_pipeline.audio] == [True, False]
                assert [item[1]["is_last_chunk"] for item in voice_pipeline.audio] == [False, True]
            else:
                assert len(voice_pipeline.audio) == 1
            assert all(name.startswith("test-tts") for name in worker_names)
            assert not pipeline.graph_tts_lock.locked()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["error", "cancel"])
def test_enhanced_failure_or_cancellation_closes_stream_and_releases_permit(monkeypatch, voice_pipeline, outcome):
    async def run():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        closed = threading.Event()

        def infer_stream(**_kwargs):
            try:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(3)
                if outcome == "error":
                    raise RuntimeError("test inference failure")
                yield 24000, np.ones(10, dtype=np.float32), "old"
            finally:
                closed.set()

        monkeypatch.setattr(pipeline, "_tts_runtime", SimpleNamespace(infer_stream=infer_stream))
        semaphore = asyncio.BoundedSemaphore(1)
        await semaphore.acquire()
        task = asyncio.create_task(synthesis("enhanced", semaphore=semaphore))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            release.set()
            if outcome == "error":
                await asyncio.wait_for(task, 2)
            assert await asyncio.to_thread(closed.wait, 1)
            await asyncio.wait_for(semaphore.acquire(), 1)
            semaphore.release()
            assert voice_pipeline.audio == []
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
