"""Japanese speech readings must not alter the original reply text."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest

from tools.tts_text_processor import correct_pronunciation_for_tts
from tts.contract import TTSRequest


@pytest.fixture(autouse=True)
def isolate_local_pronunciation_resources(monkeypatch):
    # Public tests must not depend on an installation's private user dictionary.
    from tools import tts_text_processor as processor

    processor._english_word_to_katakana.cache_clear()
    monkeypatch.setattr(processor, "_japanese_user_dictionary", lambda: None)
    yield
    processor._english_word_to_katakana.cache_clear()


def test_local_dictionary_reading_precedes_general_conversion(monkeypatch):
    from tools import tts_text_processor as processor

    def general_conversion():
        pytest.fail("a recognized dictionary word must not run general conversion")

    monkeypatch.setattr(processor, "_japanese_user_dictionary", lambda: (
        lambda word: "ブラウザー" if word == "browser" else None
    ))
    monkeypatch.setattr(processor, "_english_kana_resources", general_conversion)

    assert correct_pronunciation_for_tts("BrowserとMLP") == "ブラウザーとエムエルピー"


@pytest.mark.parametrize("features,expected", [
    ([{"string": "ｌｅａｄｅｒ", "read": "リーダー"}], "リーダー"),
    ([{"string": "ｌｅａｄ", "read": "リード"}, {"string": "ｅｒ", "read": "アー"}], None),
    ([{"string": "ｌｅａｄｅｒ", "read": "*"}], None),
])
def test_native_dictionary_requires_a_complete_word(monkeypatch, features, expected):
    pytest.importorskip("pyopenjtalk")
    from GPT_SoVITS.text import japanese

    monkeypatch.setattr(japanese, "pyopenjtalk", SimpleNamespace(run_frontend=lambda _word: features))

    assert japanese.dictionary_word_reading("Leader") == expected


def test_incomplete_dictionary_word_uses_whole_word_conversion(monkeypatch):
    from tools import tts_text_processor as processor

    monkeypatch.setattr(processor, "_japanese_user_dictionary", lambda: lambda _word: None)

    assert correct_pronunciation_for_tts("ProposerとMLP") == "プロポーザーとエムエルピー"


@pytest.mark.parametrize("word,reading", [
    ("Leader", "リーダー"),
    ("leader", "リーダー"),
    ("Follower", "フォロワー"),
    ("Learner", "ラーナー"),
    ("Proposer", "プロポーザー"),
    ("Preview", "プレビュー"),
    ("DeepSeek", "ディープシーク"),
    ("MLP", "エムエルピー"),
    ("MLP2", "エムエルピー2"),
    ("GPT4o", "ジーピーティー4オー"),
    ("API", "エーピーアイ"),
])
def test_words_and_initialisms_have_distinct_japanese_readings(word, reading):
    assert correct_pronunciation_for_tts(word, output_language="ja") == reading


def test_japanese_context_and_number_suffixes_are_preserved():
    text = "申請を受け付けるLeaderとMLP、model2の結果を取り込む。"

    assert correct_pronunciation_for_tts(text) == (
        "申請を受け付けるリーダーとエムエルピー、モデル2の結果を取り込む。"
    )


def test_pronunciation_overrides_do_not_replace_substrings():
    # The old replace loop turned API inside CAPITAL into Japanese text.
    assert correct_pronunciation_for_tts("CAPITAL") == "シーエーピーアイティーエーエル"
    assert correct_pronunciation_for_tts("GitHub") == "ギットハブ"


def test_english_output_keeps_english_words_and_initialisms():
    text = "Leader and MLP use the OpenAI API."

    assert correct_pronunciation_for_tts(text, output_language="en") == text


@pytest.mark.parametrize("cancel", [False, True])
def test_local_group_prediction_keeps_voice_responsive_and_restores_on_cancel(monkeypatch, cancel):
    from tts.utterance_scheduler import TTSUtteranceScheduler

    monkeypatch.setenv("ENABLE_TTS_UTTERANCE_SCHEDULER", "1")
    monkeypatch.setenv("TTS_UTTERANCE_MIN_START_SEQ", "2")
    monkeypatch.setenv("TTS_UTTERANCE_FLUSH_TIMEOUT_MS", "350")

    async def run():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()

        def predict(_text):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "prediction blocked the voice event loop"
            return .2

        scheduler = TTSUtteranceScheduler(
            cover_seconds_getter=lambda: 20,
            synthesis_seconds_getter=predict, deadline_enabled=True,
        )
        queue = asyncio.Queue()
        queue.put_nowait(TTSRequest(sentence_id="sentence_2_prediction", text="完了。"))
        task = asyncio.create_task(scheduler.next_job(queue))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "asr-ready"), 1) == "asr-ready"
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                release.set()
                job = await scheduler.next_job(queue)
            else:
                release.set()
                job = await task
            assert job.utterance_id == "sentence_2_prediction"
            queue.task_done()
            await asyncio.wait_for(queue.join(), 1)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("synthesis", ["enhanced", "queued"])
def test_synthesis_uses_a_pronunciation_copy_but_playback_keeps_the_reply(monkeypatch, synthesis):
    from tts import pipeline

    original = "LeaderとMLPを確認する。"
    expected_speech = "リーダーとエムエルピーを確認する。"
    request = TTSRequest(sentence_id="sentence_1_pronunciation", text=original, is_first=True)
    synthesized, displayed, cached = [], [], []

    def infer_stream(**kwargs):
        synthesized.append(kwargs["text"])
        for _ in range(3):
            yield 24000, np.ones(100, dtype=np.float32), kwargs["text"]

    async def display(_audio, _rate, _sentence_id, text, **_kwargs):
        displayed.append(text)

    def lookup(text, _params):
        cached.append(text)
        return None

    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(pipeline, "_tts_executor", executor)
        monkeypatch.setattr(pipeline, "_tts_runtime", SimpleNamespace(
            infer_stream=infer_stream, backend_id="gpt_sovits", supports_streaming=True,
        ))
        monkeypatch.setattr(pipeline, "_playback_manager", SimpleNamespace(
            add_streaming_chunk=display, add_to_playlist=display,
        ))
        monkeypatch.setattr(pipeline, "_tts_interrupt_epoch", 7)
        monkeypatch.setattr(pipeline, "TTS_OUTPUT_LANGUAGE", "日文")
        monkeypatch.setattr(pipeline, "get_first_sentence_audio_cache", lambda: SimpleNamespace(
            lookup=lookup, store=lambda *_args, **_kwargs: None,
        ))
        if synthesis == "enhanced":
            asyncio.run(pipeline.speak_stream_enhanced(
                request.text, request.sentence_id, is_first_sentence=True, interrupt_epoch=7,
            ))
        else:
            asyncio.run(pipeline.speak_stream_enhanced_asyncio_queue(
                request.text, request.sentence_id, is_first_sentence=True, interrupt_epoch=7,
            ))

    assert synthesized == [expected_speech]
    assert [text for text in displayed if text] == [original]
    assert request.text == original
    if synthesis == "queued":
        assert cached == [expected_speech]


@pytest.mark.parametrize("synthesis", ["enhanced", "queued"])
@pytest.mark.parametrize("cancel", [False, True])
def test_pronunciation_preparation_keeps_voice_responsive_and_releases_its_permit(
    monkeypatch, synthesis, cancel,
):
    from tts import pipeline

    async def run():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        inferred, completed = [], []

        def prepare(_text, **_kwargs):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "the voice event loop did not release pronunciation preparation"
            return "リーダー。"

        def infer_stream(**kwargs):
            inferred.append(kwargs["text"])
            yield 24000, np.ones(100, dtype=np.float32), kwargs["text"]

        async def display(*_args, **_kwargs):
            pass

        with ThreadPoolExecutor(max_workers=1) as executor:
            monkeypatch.setattr(pipeline, "_tts_executor", executor)
            monkeypatch.setattr(pipeline, "_tts_runtime", SimpleNamespace(
                infer_stream=infer_stream, backend_id="fake",
            ))
            monkeypatch.setattr(pipeline, "_playback_manager", SimpleNamespace(
                add_streaming_chunk=display, add_to_playlist=display,
            ))
            monkeypatch.setattr(pipeline, "_tts_interrupt_epoch", 7)
            monkeypatch.setattr(pipeline, "correct_pronunciation_for_tts", prepare)
            semaphore = asyncio.BoundedSemaphore(1)
            await semaphore.acquire()
            kwargs = dict(is_first_sentence=True, interrupt_epoch=7, task_semaphore=semaphore)
            if synthesis == "enhanced":
                coroutine = pipeline.speak_stream_enhanced("Leader。", "sentence_1_test", **kwargs)
            else:
                coroutine = pipeline.speak_stream_enhanced_asyncio_queue(
                    "Leader。", "sentence_1_test", on_synthesis_done=lambda: completed.append(True),
                    **kwargs,
                )
            task = asyncio.create_task(coroutine)
            try:
                await asyncio.wait_for(entered.wait(), 1)
                assert await asyncio.wait_for(asyncio.to_thread(lambda: "asr-ready"), 1) == "asr-ready"
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert inferred == []
                else:
                    release.set()
                    await asyncio.wait_for(task, 2)
                    assert inferred == ["リーダー。"]
                assert not semaphore.locked()
                if synthesis == "queued":
                    assert completed == [True]
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
