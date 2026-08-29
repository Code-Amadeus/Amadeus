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
            },
            pending_sentence_items=pending,
        )
        assert result["status"] == "queued"
        assert str(result.get("sentence_id") or "").startswith("sentence_")
        queued = pending.get_nowait()
        assert isinstance(queued, TTSRequest)
        assert queued.text == "進捗です。"
        assert queued.source == "work_observer"
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
