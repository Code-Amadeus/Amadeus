"""TTSRequest 契约 + UtteranceScheduler 消费行为测试。

运行方式（不依赖 pytest）：
    .venv\\Scripts\\python.exe -X utf8 tests\\test_tts_contract.py
也兼容 pytest。
"""

from __future__ import annotations

import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tts.contract import TTSRequest
from tts.utterance_scheduler import TTSUtteranceScheduler


def test_from_queue_item_normalization():
    # TTSRequest 原样通过
    req = TTSRequest(sentence_id="sentence_1_a", text="こんにちは", is_first=True,
                     stream_tts=True, source="chat", turn_id="t1")
    assert TTSRequest.from_queue_item(req) is req

    # 旧 4 元组（chat / vn 形状）
    r4 = TTSRequest.from_queue_item(("sentence_2_b", "テスト", False, True))
    assert (r4.sentence_id, r4.text, r4.is_first, r4.stream_tts) == ("sentence_2_b", "テスト", False, True)
    assert r4.source == "legacy" and r4.turn_id == ""

    # 旧 3 元组（CLI / live 形状）：stream_tts 缺省为 None（消费侧按 is_first 推断）
    r3 = TTSRequest.from_queue_item(("sentence_3_c", "はい", True))
    assert r3.stream_tts is None and r3.is_first is True

    # 非法条目
    try:
        TTSRequest.from_queue_item({"bad": 1})
        assert False, "should raise"
    except TypeError:
        pass


def test_scheduler_consumes_contract_and_legacy_mixed():
    async def run():
        sched = TTSUtteranceScheduler()
        q: asyncio.Queue = asyncio.Queue()
        await q.put(TTSRequest(sentence_id="sentence_1_a", text="第一句。",
                               is_first=True, stream_tts=True, source="chat", turn_id="turn_x"))
        await q.put(("sentence_2_b", "旧元组句。", False))  # 过渡期旧生产者

        job1 = await sched.next_job(q)
        assert job1.utterance_id == "sentence_1_a"
        assert job1.is_first and job1.stream_tts is True
        assert job1.source == "chat" and job1.turn_id == "turn_x"
        assert job1.tts_epoch is None

        job2 = await sched.next_job(q)
        assert job2.source == "legacy" and job2.turn_id == ""
        assert job2.stream_tts is None

    asyncio.run(run())


def test_scheduler_preserves_epoch_and_never_merges_across_it():
    async def run():
        with patch.dict(
            os.environ,
            {
                "ENABLE_TTS_UTTERANCE_SCHEDULER": "1",
                "TTS_UTTERANCE_MIN_START_SEQ": "2",
                "TTS_UTTERANCE_FLUSH_TIMEOUT_MS": "50",
            },
        ):
            sched = TTSUtteranceScheduler()
            q: asyncio.Queue = asyncio.Queue()
            await q.put(
                TTSRequest(
                    sentence_id="sentence_2_old",
                    text="old epoch,",
                    source="work_observer",
                    tts_epoch=7,
                )
            )
            await q.put(
                TTSRequest(
                    sentence_id="sentence_3_new",
                    text="new epoch.",
                    source="work_observer",
                    tts_epoch=8,
                )
            )
            old = await sched.next_job(q)
            new = await sched.next_job(q)
            assert old.is_merged is False and old.tts_epoch == 7
            assert new.is_merged is False and new.tts_epoch == 8

    asyncio.run(run())


def test_late_background_request_is_dropped_after_epoch_advance():
    async def run():
        import tts.pipeline as pipeline

        queue: asyncio.Queue = asyncio.Queue()
        scheduler = TTSUtteranceScheduler()
        saved_epoch = pipeline._tts_interrupt_epoch
        worker = None
        try:
            pipeline._tts_interrupt_epoch = 12
            await queue.put(
                TTSRequest(
                    sentence_id="sentence_1_late",
                    text="stale observer speech",
                    is_first=True,
                    source="work_observer",
                    tts_epoch=11,
                )
            )
            with (
                patch.object(pipeline, "_pending_sentence_items", queue),
                patch.object(pipeline, "_utterance_scheduler", scheduler),
            ):
                worker = asyncio.create_task(pipeline.play_sentence_worker())
                await asyncio.wait_for(queue.join(), timeout=1)
                assert worker.done() is False
        finally:
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            pipeline._tts_interrupt_epoch = saved_epoch

    asyncio.run(run())


def test_scheduler_never_merges_across_turn_or_source():
    async def run():
        os.environ["ENABLE_TTS_UTTERANCE_SCHEDULER"] = "1"
        os.environ["TTS_UTTERANCE_MIN_START_SEQ"] = "2"
        os.environ["TTS_UTTERANCE_FLUSH_TIMEOUT_MS"] = "50"
        try:
            sched = TTSUtteranceScheduler()
            q: asyncio.Queue = asyncio.Queue()
            # 同轮次同来源、可合并的两句（非首句、序号连续、无强句尾边界）
            await q.put(TTSRequest(sentence_id="sentence_4_a", text="つづき、",
                                   source="chat", turn_id="t1"))
            await q.put(TTSRequest(sentence_id="sentence_5_b", text="そのまま。",
                                   source="chat", turn_id="t1"))
            job = await sched.next_job(q)
            assert job.is_merged and job.consumed_count == 2, job

            # 跨轮次：绝不合并
            await q.put(TTSRequest(sentence_id="sentence_4_c", text="旧轮の句、",
                                   source="chat", turn_id="t1"))
            await q.put(TTSRequest(sentence_id="sentence_5_d", text="新轮の句。",
                                   source="chat", turn_id="t2"))
            job1 = await sched.next_job(q)
            assert not job1.is_merged and job1.turn_id == "t1"
            job2 = await sched.next_job(q)
            assert job2.turn_id == "t2"

            # 跨来源：绝不合并
            await q.put(TTSRequest(sentence_id="sentence_6_e", text="チャット句、",
                                   source="chat", turn_id="t2"))
            await q.put(TTSRequest(sentence_id="sentence_7_f", text="VN句。",
                                   source="vn"))
            j1 = await sched.next_job(q)
            assert not j1.is_merged and j1.source == "chat"
            j2 = await sched.next_job(q)
            assert j2.source == "vn"
        finally:
            os.environ.pop("ENABLE_TTS_UTTERANCE_SCHEDULER", None)
            os.environ.pop("TTS_UTTERANCE_MIN_START_SEQ", None)
            os.environ.pop("TTS_UTTERANCE_FLUSH_TIMEOUT_MS", None)

    asyncio.run(run())


def test_interrupt_acknowledges_scheduler_lookahead_items():
    async def run():
        import tts.pipeline as pipeline

        queue: asyncio.Queue = asyncio.Queue()
        scheduler = TTSUtteranceScheduler()
        saved_epoch = pipeline._tts_interrupt_epoch
        with patch.dict(
            os.environ,
            {
                "ENABLE_TTS_UTTERANCE_SCHEDULER": "1",
                "TTS_UTTERANCE_MIN_START_SEQ": "2",
                "TTS_UTTERANCE_FLUSH_TIMEOUT_MS": "50",
            },
        ):
            await queue.put(
                TTSRequest(
                    sentence_id="sentence_2_old",
                    text="old turn",
                    source="chat",
                    turn_id="old",
                )
            )
            await queue.put(
                TTSRequest(
                    sentence_id="sentence_3_new",
                    text="new turn",
                    source="chat",
                    turn_id="new",
                )
            )
            first = await scheduler.next_job(queue)
            assert first.consumed_count == 1
            queue.task_done()

            try:
                with (
                    patch.object(pipeline, "_pending_sentence_items", queue),
                    patch.object(pipeline, "_utterance_scheduler", scheduler),
                ):
                    pipeline.interrupt_pending_tts()
                    await asyncio.wait_for(queue.join(), timeout=0.2)
            finally:
                pipeline._tts_interrupt_epoch = saved_epoch

    asyncio.run(run())


def test_merged_job_playback_segments_shape():
    async def run():
        os.environ["ENABLE_TTS_UTTERANCE_SCHEDULER"] = "1"
        os.environ["TTS_UTTERANCE_MIN_START_SEQ"] = "2"
        os.environ["TTS_UTTERANCE_FLUSH_TIMEOUT_MS"] = "50"
        try:
            sched = TTSUtteranceScheduler()
            q: asyncio.Queue = asyncio.Queue()
            await q.put(TTSRequest(sentence_id="sentence_8_a", text="まえ、", source="chat", turn_id="t3"))
            await q.put(TTSRequest(sentence_id="sentence_9_b", text="うしろ。", source="chat", turn_id="t3"))
            job = await sched.next_job(q)
            segs = job.playback_segments()
            assert [s["seq"] for s in segs] == [8, 9]
            assert all({"sentence_id", "text", "seq"} <= set(s) for s in segs)
        finally:
            os.environ.pop("ENABLE_TTS_UTTERANCE_SCHEDULER", None)
            os.environ.pop("TTS_UTTERANCE_MIN_START_SEQ", None)
            os.environ.pop("TTS_UTTERANCE_FLUSH_TIMEOUT_MS", None)

    asyncio.run(run())


def test_merged_playback_closes_each_segment_before_starting_the_next() -> None:
    from types import SimpleNamespace

    from tts.playback import PlaybackManager

    async def run():
        player = SimpleNamespace(
            _hooks=SimpleNamespace(
                subtitle_available=False,
                update_subtitle_display=None,
                check_and_display_pre_translation=None,
            )
        )
        manager = PlaybackManager(player)
        segments = [
            {"sentence_id": "sentence_6_a", "text": "first"},
            {"sentence_id": "sentence_7_b", "text": "second"},
            {"sentence_id": "sentence_8_c", "text": "third"},
        ]
        for segment in segments:
            manager._register_turn_sentence(segment["sentence_id"], segment["text"])
        manager.current_playing_id = "sentence_6_a"
        manager._current_playing_segment_ids = {
            segment["sentence_id"] for segment in segments
        }
        manager._segment_offsets = lambda *_args: [
            ("sentence_6_a", "first", 0.0),
            ("sentence_7_b", "second", 0.0),
            ("sentence_8_c", "third", 0.0),
        ]
        events: list[tuple[str, str]] = []
        manager.on_sentence_start = lambda sentence_id: events.append(
            ("start", sentence_id)
        )
        manager.on_sentence_complete = lambda sentence_id, _text: events.append(
            ("end", sentence_id)
        )
        await manager._fire_segment_starts(
            "sentence_6_a",
            segments,
            total_samples=3,
            sample_rate=1,
        )
        manager._mark_current_audio_complete("sentence_6_a")
        assert events == [
            ("end", "sentence_6_a"),
            ("start", "sentence_7_b"),
            ("end", "sentence_7_b"),
            ("start", "sentence_8_c"),
            ("end", "sentence_8_c"),
        ]

    asyncio.run(run())


def test_physical_completion_and_late_last_sentence_binding_close_the_turn() -> None:
    from types import SimpleNamespace

    from tts.playback import PlaybackManager

    manager = PlaybackManager(SimpleNamespace())
    sentence_id = "sentence_2_short"
    manager._register_turn_sentence(sentence_id, "short")
    events: list[tuple[str, str]] = []
    manager.on_sentence_complete = lambda value, _text: events.append(
        ("sentence", value)
    )
    manager.on_turn_playback_complete = lambda: events.append(("turn", "done"))

    # The audio can finish before ChatRuntime receives the model's closing
    # boundary and identifies which sentence ended the turn.
    manager._finish_audio_item(sentence_id, (sentence_id,))
    assert events == [("sentence", sentence_id)]

    manager.mark_turn_last_sentence(sentence_id, "turn-short")
    assert events == [("sentence", sentence_id), ("turn", "done")]


def test_stream_sequence_waits_for_a_dequeued_normal_sentence() -> None:
    from types import SimpleNamespace

    from tts.playback import PlaybackManager

    async def run() -> None:
        manager = PlaybackManager(SimpleNamespace())
        manager.next_seq_to_play = 8
        # Sequence 6 has already been removed from pending_audio and is waiting
        # to claim the physical player.  Advancing next_seq alone must not let
        # a later AUIP stream skip over it.
        manager._normal_waiting_seq = 6
        claim = asyncio.create_task(manager._claim_stream_sequence(8, 0))
        await asyncio.sleep(0)
        assert claim.done() is False

        async with manager.play_condition:
            manager._normal_waiting_seq = None
            manager.play_condition.notify_all()
        assert await asyncio.wait_for(claim, timeout=1.0) is True
        assert manager._stream_claimed_seq == 8
        assert manager.next_seq_to_play == 9

        # The normal queue cannot consume sequence 9 until the streaming item
        # has released its physical-player claim.
        await manager._release_stream_sequence(8)
        assert manager._stream_claimed_seq is None

    asyncio.run(run())


def test_backend_stream_uses_shared_first_sentence_playback_path() -> None:
    from tts import pipeline

    class ClosingStream:
        def __init__(self) -> None:
            self._items = iter(
                [
                    (24000, np.ones(960, dtype=np.float32), "hello"),
                    (24000, np.ones(480, dtype=np.float32), ""),
                ]
            )
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._items)

        def close(self) -> None:
            self.closed = True

    class FakeRuntime:
        supports_streaming = True

        def __init__(self) -> None:
            self.stream = ClosingStream()

        def infer_stream(self, **_kwargs):
            return self.stream

    class FakePlaybackManager:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, int]] = []
            self.chunks: list[tuple[int, np.ndarray]] = []
            self.done = asyncio.Event()

        async def play_s1_stream(
            self,
            chunk_queue,
            sentence_id,
            text,
            playback_epoch=None,
        ) -> None:
            self.calls.append((sentence_id, text, playback_epoch))
            while True:
                item = await chunk_queue.get()
                if item is None:
                    break
                self.chunks.append(item)
            self.done.set()

    class EmptyCache:
        @staticmethod
        def lookup(*_args, **_kwargs):
            return None

        @staticmethod
        def store(*_args, **_kwargs) -> None:
            return None

    async def run() -> None:
        runtime = FakeRuntime()
        playback = FakePlaybackManager()
        executor = ThreadPoolExecutor(max_workers=1)
        previous = (
            pipeline._tts_runtime,
            pipeline._tts_executor,
            pipeline._playback_manager,
            pipeline._tts_interrupt_epoch,
        )
        try:
            pipeline._tts_runtime = runtime
            pipeline._tts_executor = executor
            pipeline._playback_manager = playback
            pipeline._tts_interrupt_epoch = 7
            with (
                patch.object(
                    pipeline,
                    "get_first_sentence_audio_cache",
                    return_value=EmptyCache(),
                ),
                patch.object(
                    pipeline,
                    "correct_pronunciation_for_tts",
                    side_effect=lambda value: value,
                ),
            ):
                await pipeline.speak_stream_enhanced_asyncio_queue(
                    "hello",
                    "sentence_1_remote",
                    is_first_sentence=True,
                    stream_to_player=True,
                    interrupt_epoch=7,
                )
                await asyncio.wait_for(playback.done.wait(), timeout=1.0)
        finally:
            (
                pipeline._tts_runtime,
                pipeline._tts_executor,
                pipeline._playback_manager,
                pipeline._tts_interrupt_epoch,
            ) = previous
            executor.shutdown(wait=True)

        assert playback.calls == [("sentence_1_remote", "hello", 7)]
        assert [len(audio) for _sample_rate, audio in playback.chunks] == [960, 480]
        assert runtime.stream.closed is True

    asyncio.run(run())


def test_audio_writer_publishes_mouth_envelope_before_each_physical_subwrite() -> None:
    from tts.playback import StreamPlayer

    events: list[tuple[str, float | int]] = []

    class MouthSink:
        @staticmethod
        def publish_mouth_value(value: float) -> None:
            events.append(("mouth", round(float(value), 4)))

    class FakeStream:
        @staticmethod
        def write(data: bytes) -> None:
            events.append(("write", len(data) // np.dtype(np.float32).itemsize))

    async def run() -> None:
        player = StreamPlayer(MouthSink())
        player.stream = FakeStream()
        player.is_playing = True
        player.chunk_size = 20
        player.send_interval = 0.05
        player.volume_multiplier = 1.0
        audio = np.concatenate(
            [
                np.full(50, 0.01, dtype=np.float32),
                np.full(50, 0.40, dtype=np.float32),
                np.full(50, 0.02, dtype=np.float32),
            ]
        )
        try:
            await player.write_audio_async(
                audio,
                mouth_envelope=True,
                sample_rate=1000,
                first_mouth_minimum=0.12,
            )
        finally:
            player._stop_audio_writer()

    asyncio.run(run())

    assert events == [
        ("mouth", 0.12),
        ("write", 50),
        ("mouth", 0.4),
        ("write", 50),
        ("mouth", 0.02),
        ("write", 50),
    ]


def test_shared_stream_playback_feeds_aec_and_mouth_signals() -> None:
    import tts.playback as playback_module
    from tts.playback import PlaybackManager

    class MouthSink:
        def __init__(self) -> None:
            self.values: list[float] = []

        def publish_mouth_value(self, value: float) -> None:
            self.values.append(float(value))

    class FakePlayer:
        def __init__(self) -> None:
            self._hooks = SimpleNamespace(
                subtitle_available=False,
                update_subtitle_display=None,
                check_and_display_pre_translation=None,
            )
            self.mouth_sink = MouthSink()
            self.last_send_time = 0.0
            self.send_interval = 0.0
            self.volume_multiplier = 1.0
            self.sample_rate = 0
            self.chunk_size = 512
            self.is_playing = True
            self.writes: list[np.ndarray] = []

        def initialize(self, sample_rate: int) -> None:
            self.sample_rate = sample_rate
            self.is_playing = True

        def _emit_mouth_value_for_audio(self, _audio, minimum: float = 0.0) -> None:
            self.mouth_sink.publish_mouth_value(minimum)

        async def write_audio_async(
            self,
            audio,
            *,
            loop,
            before_write=None,
            after_write=None,
            is_current=None,
            mouth_envelope=False,
            sample_rate=None,
            first_mouth_minimum=None,
        ) -> None:
            del loop
            if is_current is not None and not is_current():
                return
            if before_write is not None:
                before_write()
            if mouth_envelope:
                rate = int(sample_rate or self.sample_rate or 24000)
                window_samples = max(self.chunk_size, int(round(rate * self.send_interval)))
                for offset in range(0, len(audio), window_samples):
                    segment = audio[offset : offset + window_samples]
                    value = min(1.0, float(np.sqrt(np.mean(segment ** 2))) * self.volume_multiplier)
                    if offset == 0 and first_mouth_minimum is not None and value > 0.0:
                        value = max(float(first_mouth_minimum), value)
                    self.mouth_sink.publish_mouth_value(value)
                    self.writes.append(segment.copy())
            else:
                self.writes.append(audio.copy())
            if after_write is not None:
                after_write()

    class AECRecorder:
        def __init__(self) -> None:
            self.references: list[tuple[int, int]] = []

        def push_reference(self, audio, sample_rate, *_args) -> None:
            self.references.append((len(audio), int(sample_rate)))

        def start(self, *_args) -> None:
            return None

        def stop(self) -> None:
            return None

    async def run() -> None:
        player = FakePlayer()
        manager = PlaybackManager(player)
        realtime_aec = AECRecorder()
        debug_aec = AECRecorder()
        chunk_queue: asyncio.Queue = asyncio.Queue()
        await chunk_queue.put((24000, np.full(960, 0.25, dtype=np.float32)))
        await chunk_queue.put(None)
        with (
            patch.object(
                playback_module,
                "get_realtime_aec_processor",
                return_value=realtime_aec,
            ),
            patch.object(
                playback_module,
                "get_aec_debug_capture",
                return_value=debug_aec,
            ),
        ):
            await manager.play_s1_stream(
                chunk_queue,
                "sentence_1_remote",
                "hello",
                playback_epoch=manager.playback_epoch,
            )

        assert realtime_aec.references[0] == (960, 24000)
        assert debug_aec.references[0] == (960, 24000)
        assert any(value > 0 for value in player.mouth_sink.values)
        assert len(player.writes) >= 1

        reference_count = len(realtime_aec.references)
        write_count = len(player.writes)
        stale_queue: asyncio.Queue = asyncio.Queue()
        await stale_queue.put((24000, np.full(960, 0.5, dtype=np.float32)))
        await stale_queue.put(None)
        await manager.play_s1_stream(
            stale_queue,
            "sentence_2_stale_remote",
            "stale",
            playback_epoch=manager.playback_epoch - 1,
        )
        assert len(realtime_aec.references) == reference_count
        assert len(player.writes) == write_count

    asyncio.run(run())


def test_stream_player_stop_is_idempotent_for_an_already_closed_stream() -> None:
    from tts.playback import StreamPlayer

    class _MouthSink:
        @staticmethod
        def publish_mouth_value(_value):
            return None

    class _ClosedStream:
        closed = 0
        stopped = 0

        @staticmethod
        def is_active():
            return False

        def stop_stream(self):
            self.stopped += 1

        def close(self):
            self.closed += 1

    player = StreamPlayer(_MouthSink())
    stream = _ClosedStream()
    player.stream = stream
    player.is_playing = True

    player.stop()
    player.stop()

    assert player.stream is None
    assert player.is_playing is False
    assert stream.stopped == 0
    assert stream.closed == 1


def test_status_supersession_discards_only_matching_background_speech():
    from tts import pipeline

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(
        TTSRequest(
            sentence_id="sentence_10_progress",
            text="old progress",
            source="work_observer",
            metadata={"work_item_id": "work_1", "terminal": False},
        )
    )
    queue.put_nowait(
        TTSRequest(
            sentence_id="sentence_11_terminal",
            text="terminal truth",
            source="work_observer",
            metadata={"work_item_id": "work_1", "terminal": True},
        )
    )
    queue.put_nowait(
        TTSRequest(
            sentence_id="sentence_12_role",
            text="normal role speech",
            source="chat",
            metadata={"work_item_id": "work_1", "terminal": False},
        )
    )
    scheduler = TTSUtteranceScheduler()
    with (
        patch.object(pipeline, "_pending_sentence_items", queue),
        patch.object(pipeline, "_utterance_scheduler", scheduler),
    ):
        discarded = pipeline.discard_pending_tts(
            source="work_observer",
            work_item_id="work_1",
            nonterminal_only=True,
        )
    assert discarded == 1
    remaining = [queue.get_nowait(), queue.get_nowait()]
    assert [item.text for item in remaining] == [
        "terminal truth",
        "normal role speech",
    ]
    for _item in remaining:
        queue.task_done()


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all tts contract tests passed")


if __name__ == "__main__":
    _main()
