"""VN observer speech receipts reflect the real TTS queue boundary."""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.vn_tts_bridge import _is_completed_display_subtitle, submit_vn_tts_confirmed
from tts.contract import TTSRequest


def test_confirmed_receipt_waits_for_the_sentence_queue() -> None:
    async def run() -> None:
        pending: asyncio.Queue = asyncio.Queue(maxsize=1)
        result = await submit_vn_tts_confirmed(
            {
                "display_text": "Progress",
                "voice_text_ja": "進捗です。",
                "source": "work_observer",
                "work_item_id": "work_1",
                "terminal": False,
                "speed": 0.95,
            },
            pending_sentence_items=pending,
        )
        assert result["status"] == "queued"
        assert str(result.get("sentence_id") or "").startswith("sentence_")
        queued = pending.get_nowait()
        assert isinstance(queued, TTSRequest)
        assert queued.text == "進捗です。"
        assert queued.source == "work_observer"
        assert queued.speed == 0.95
        assert queued.metadata["work_item_id"] == "work_1"
        assert queued.metadata["terminal"] is False
        assert queued.metadata["narration_complete_turn"] is False
        pending.task_done()

    asyncio.run(run())


def test_full_sentence_queue_never_returns_a_false_queued_receipt() -> None:
    async def run() -> None:
        pending: asyncio.Queue = asyncio.Queue(maxsize=1)
        pending.put_nowait(object())
        with patch.dict(
            os.environ,
            {
                "VN_TTS_QUEUE_PUT_TIMEOUT": "0.1",
                "VN_TTS_ENQUEUE_CONFIRM_TIMEOUT": "1.0",
            },
        ):
            result = await submit_vn_tts_confirmed(
                {"display_text": "Progress", "voice_text_ja": "進捗です。"},
                pending_sentence_items=pending,
            )
        assert result["status"] == "dropped"
        assert result["reason"] == "pending_sentence_queue_full"

    asyncio.run(run())


def test_complete_turn_receipt_identifies_the_real_last_sentence() -> None:
    async def run() -> None:
        pending: asyncio.Queue = asyncio.Queue()
        result = await submit_vn_tts_confirmed(
            {
                "display_text": "Status",
                "voice_text_ja": "一つ目が終わった。二つ目も終わった。",
                "source": "host_readonly_status",
                "complete_turn": True,
            },
            pending_sentence_items=pending,
        )
        assert result["status"] == "queued"
        queued = [pending.get_nowait(), pending.get_nowait()]
        assert result["last_sentence_id"] == queued[-1].sentence_id
        assert result["sentence_id"] == queued[0].sentence_id
        assert all(
            item.metadata["narration_complete_turn"] is True for item in queued
        )
        for _ in queued:
            pending.task_done()

    asyncio.run(run())


def test_direct_japanese_never_splits_compound_words_at_latency_cut() -> None:
    async def run() -> None:
        for phrase in (
            "エンドレスアーケードゲームを構築したわ。",
            "ゲームはステージング済みよ。",
            "デスクトップへのエクスポートが完了したわ。",
        ):
            pending: asyncio.Queue = asyncio.Queue()
            result = await submit_vn_tts_confirmed(
                {
                    "display_text": phrase,
                    "display_language": "japanese",
                    "voice_text_ja": phrase,
                },
                pending_sentence_items=pending,
            )
            assert result["status"] == "queued"
            queued = pending.get_nowait()
            assert isinstance(queued, TTSRequest)
            assert queued.text == phrase
            assert pending.empty()
            pending.task_done()

    asyncio.run(run())


def test_japanese_role_text_is_not_mistaken_for_a_translated_caption() -> None:
    assert not _is_completed_display_subtitle("作業は終わったわ。", "japanese")
    assert _is_completed_display_subtitle("工作已经完成。", "simplified_chinese")


def test_stream_enqueues_first_phrase_before_generation_finishes() -> None:
    async def run() -> None:
        pending = asyncio.Queue()
        continue_generation = asyncio.Event()
        async def voice():
            yield "確認してほしいことがあるの。"
            await continue_generation.wait()
            yield "次の項目も見てもらえる？"
        task = asyncio.create_task(submit_vn_tts_confirmed(
            {"display_text": "原始问题", "complete_turn": True},
            pending_sentence_items=pending, voice_stream=voice(),
        ))
        first = await asyncio.wait_for(pending.get(), 1)
        assert first.text == "確認してほしいことがあるの。"
        assert not task.done(), "Speech must enter TTS while the model is still generating"
        continue_generation.set()
        result = await task
        last = pending.get_nowait()
        assert result["sentence_id"] == first.sentence_id
        assert result["last_sentence_id"] == last.sentence_id
        assert last.text == "次の項目も見てもらえる？"
    asyncio.run(run())


def test_model_stream_is_not_cancelled_by_the_direct_enqueue_deadline() -> None:
    async def run() -> None:
        pending = asyncio.Queue()
        async def voice():
            # A model can still be preparing its first phrase after the
            # direct, already-generated text's queue deadline has elapsed.
            await asyncio.sleep(0.6)
            yield "元の解析はまだ移せていないわ。"
        with patch.dict(os.environ, {"VN_TTS_ENQUEUE_CONFIRM_TIMEOUT": "0.5"}):
            result = await submit_vn_tts_confirmed(
                {"display_text": "原来的分析尚未迁移", "complete_turn": True},
                pending_sentence_items=pending, voice_stream=voice(),
            )
        assert result["status"] == "queued"
        sentence = pending.get_nowait()
        assert sentence.text == "元の解析はまだ移せていないわ。"
        assert result["last_sentence_id"] == sentence.sentence_id
    asyncio.run(run())


def test_cancel_before_first_model_phrase_stops_late_enqueue() -> None:
    async def run() -> None:
        pending = asyncio.Queue()
        generating, closed = asyncio.Event(), asyncio.Event()
        async def voice():
            try:
                generating.set()
                await asyncio.Event().wait()
                yield "古いお知らせ。"
            finally:
                closed.set()
        task = asyncio.create_task(submit_vn_tts_confirmed(
            {"display_text": "原始问题", "complete_turn": True},
            pending_sentence_items=pending, voice_stream=voice(),
        ))
        await asyncio.wait_for(generating.wait(), 1)
        task.cancel()
        try:
            await task
            assert False, "Cancellation must propagate during model generation"
        except asyncio.CancelledError:
            pass
        await asyncio.wait_for(closed.wait(), 1)
        assert pending.empty()
    asyncio.run(run())


def test_stream_late_failure_and_cancellation_do_not_confirm_a_complete_turn() -> None:
    async def run() -> None:
        for cancel in (False, True):
            pending = asyncio.Queue()
            continue_generation = asyncio.Event()
            closed = asyncio.Event()
            async def voice():
                try:
                    yield "確認してほしいことがあるの。"
                    await continue_generation.wait()
                    raise RuntimeError("invalid later speech fragment")
                finally:
                    closed.set()
            task = asyncio.create_task(submit_vn_tts_confirmed(
                {"display_text": "原始问题", "complete_turn": True},
                pending_sentence_items=pending, voice_stream=voice(),
            ))
            await asyncio.wait_for(pending.get(), 1)
            if cancel:
                task.cancel()
                try:
                    await task
                    assert False, "Cancellation must propagate"
                except asyncio.CancelledError:
                    pass
            else:
                continue_generation.set()
                result = await task
                assert result["status"] == "error"
                assert "last_sentence_id" not in result
            await asyncio.wait_for(closed.wait(), 1)
            assert pending.empty()
    asyncio.run(run())


if __name__ == "__main__":
    test_confirmed_receipt_waits_for_the_sentence_queue()
    print("ok: confirmed VN receipt follows the real sentence queue")
    test_full_sentence_queue_never_returns_a_false_queued_receipt()
    print("ok: a full sentence queue is never reported as queued")
    test_complete_turn_receipt_identifies_the_real_last_sentence()
    print("ok: completed VN turn receipts identify the real last sentence")
    test_direct_japanese_never_splits_compound_words_at_latency_cut()
    print("ok: direct Japanese compound words are not split at the latency cut")
    test_japanese_role_text_is_not_mistaken_for_a_translated_caption()
    print("ok: Japanese role text is not accepted as a translated caption")
