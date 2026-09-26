"""A cache keyed by local weights must never substitute another provider's audio."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(("backend_id", "cached"), [
    ("gpt_sovits", True), ("gpt_sovits", False), ("fish_audio", True),
    ("openai_compatible", True), ("mimo", True),
])
async def test_local_audio_cache_boundary(monkeypatch, streaming, backend_id, cached):
    from tts import pipeline

    calls = []
    actual = []
    done = asyncio.Event()

    class Runtime:
        def infer_stream(self, **kwargs):
            calls.append("synthesize")
            yield 24000, np.array([0.25], dtype=np.float32), kwargs["text"]

    runtime = Runtime()
    runtime.backend_id = backend_id

    class Cache:
        def lookup(self, *_):
            calls.append("lookup")
            return (24000, np.array([0.75], dtype=np.float32)) if cached else None

        def store(self, *_, **kwargs):
            calls.append("store")

    class Playback:
        async def add_to_playlist(self, audio, *_args, **_kwargs):
            actual.extend(audio)
            done.set()

        async def play_s1_stream(self, queue, *_args, **_kwargs):
            while (item := await queue.get()) is not None:
                actual.extend(item[1])
            done.set()

    monkeypatch.setattr(pipeline, "_tts_runtime", runtime)
    monkeypatch.setattr(pipeline, "_playback_manager", Playback())
    monkeypatch.setattr(pipeline, "get_first_sentence_audio_cache", lambda: Cache())
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(pipeline, "_tts_executor", executor)
        await pipeline.speak_stream_enhanced_asyncio_queue(
            "Hello", "sentence_1_cache_boundary", is_first_sentence=True,
            stream_to_player=streaming,
        )
        await asyncio.wait_for(done.wait(), 2)
    if backend_id == "gpt_sovits":
        assert calls == (["lookup"] if cached else ["lookup", "synthesize", "store"])
        assert actual == ([0.75] if cached else [0.25])
    else:
        assert calls == ["synthesize"]
        assert actual == [0.25]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("cached", [False, True])
async def test_graph_lock_guards_inference_not_cached_playback(monkeypatch, streaming, cached):
    from tts import pipeline

    checked = asyncio.Event()
    played = asyncio.Event()
    calls, audio = [], []
    lock = asyncio.Lock()
    await lock.acquire()  # An earlier sentence still owns the inference engine.

    def lookup(*_args):
        checked.set()
        return (24000, np.array([0.75], dtype=np.float32)) if cached else None

    def infer_stream(**kwargs):
        calls.append("synthesize")
        assert lock.locked()
        yield 24000, np.array([0.25], dtype=np.float32), kwargs["text"]

    class Playback:
        async def add_to_playlist(self, data, *_args, **_kwargs):
            audio.extend(data)
            played.set()

        async def play_s1_stream(self, queue, *_args, **_kwargs):
            while (item := await queue.get()) is not None:
                audio.extend(item[1])
            played.set()

    monkeypatch.setattr(pipeline, "graph_tts_lock", lock)
    monkeypatch.setattr(pipeline, "_tts_runtime", SimpleNamespace(
        backend_id="gpt_sovits", infer_stream=infer_stream))
    monkeypatch.setattr(pipeline, "_playback_manager", Playback())
    monkeypatch.setattr(pipeline, "get_first_sentence_audio_cache", lambda: SimpleNamespace(
        lookup=lookup, store=lambda *_args, **_kwargs: None))
    permit = asyncio.BoundedSemaphore(1)
    await permit.acquire()
    with ThreadPoolExecutor(max_workers=2) as executor:
        monkeypatch.setattr(pipeline, "_tts_executor", executor)
        task = asyncio.create_task(pipeline.speak_stream_graph_serial(
            "Hello", "sentence_1_lock_boundary", True,
            stream_tts=streaming, task_semaphore=permit))
        try:
            await asyncio.wait_for(checked.wait(), 1)
            if cached:
                await asyncio.wait_for(played.wait(), 1)
                await task
                assert lock.locked(), "A cache hit must not release another producer's lock"
                assert calls == []
                assert audio == [0.75]
            else:
                assert not task.done() and not played.is_set()
                assert calls == []
                lock.release()
                await asyncio.wait_for(task, 2)
                await asyncio.wait_for(played.wait(), 1)
                assert calls == ["synthesize"] and audio == [0.25]
                assert not lock.locked()
            await asyncio.wait_for(permit.acquire(), 1)
            permit.release()
        finally:
            if lock.locked():
                lock.release()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["stale_cache", "stale_waiter", "cancel_waiter"])
async def test_cache_and_graph_waiters_preserve_epoch_and_lock_ownership(monkeypatch, outcome):
    from tts import pipeline

    checked = asyncio.Event()
    lock = asyncio.Lock()
    await lock.acquire()
    playback = SimpleNamespace(add_to_playlist=AsyncMock(), play_s1_stream=AsyncMock())
    monkeypatch.setattr(pipeline, "graph_tts_lock", lock)
    monkeypatch.setattr(pipeline, "_tts_interrupt_epoch", 10)
    monkeypatch.setattr(pipeline, "_playback_manager", playback)

    def infer_stream(**_kwargs):
        raise AssertionError("An invalidated turn must not start inference")

    def lookup(*_args):
        checked.set()
        if outcome == "stale_cache":
            pipeline._tts_interrupt_epoch = 11
            return 24000, np.ones(4, dtype=np.float32)
        return None

    monkeypatch.setattr(pipeline, "_tts_runtime", SimpleNamespace(
        backend_id="gpt_sovits", infer_stream=infer_stream))
    monkeypatch.setattr(pipeline, "get_first_sentence_audio_cache", lambda: SimpleNamespace(lookup=lookup))
    permit = asyncio.BoundedSemaphore(1)
    await permit.acquire()
    task = asyncio.create_task(pipeline.speak_stream_graph_serial(
        "Hello", "sentence_1_invalidated_cache", True,
        stream_tts=True, interrupt_epoch=10, task_semaphore=permit))
    try:
        await asyncio.wait_for(checked.wait(), 1)
        if outcome == "cancel_waiter":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert lock.locked()
        elif outcome == "stale_waiter":
            pipeline._tts_interrupt_epoch = 11
            lock.release()
            await asyncio.wait_for(task, 1)
            assert not lock.locked()
        else:
            await asyncio.wait_for(task, 1)
            assert lock.locked()
        playback.add_to_playlist.assert_not_called()
        playback.play_s1_stream.assert_not_called()
        await asyncio.wait_for(permit.acquire(), 1)
        permit.release()
    finally:
        if lock.locked():
            lock.release()
        await asyncio.gather(task, return_exceptions=True)
