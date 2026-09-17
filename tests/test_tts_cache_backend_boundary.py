"""A cache keyed by local weights must never substitute another provider's audio."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

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
