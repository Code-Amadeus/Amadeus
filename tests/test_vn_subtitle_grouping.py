"""Full captions span audio chunks without delaying speech or crossing playback IDs."""
import asyncio

import pytest

from server import vn_tts_bridge as bridge
from test_vn_speech_segments import PREFIX, TAIL, content, make_runtime
from test_vn_overlay_subtitle_order import overlay_without_window


FIRST = "その時間に扉が開いていたなら、"
SECOND = "別の入口から入った可能性があるわ。"
NEXT = "次の証言も確認しましょう。"


@pytest.fixture(autouse=True)
def isolated_captions(monkeypatch):
    for name in ("_SUBTITLE_STREAMS", "_SENTENCE_META", "_VN_SUBTITLE_CACHE", "_SUBTITLE_SEMAPHORES"):
        monkeypatch.setattr(bridge, name, {})
    monkeypatch.setattr(bridge, "_SUBTITLE_TASKS", set())


def test_whole_caption_follows_both_audio_chunks_while_first_audio_precedes_body(tmp_path, monkeypatch):
    async def run():
        body, tail = asyncio.Event(), asyncio.Event()
        first_audio, second_audio = asyncio.Event(), asyncio.Event()
        translating, translated = asyncio.Event(), asyncio.Event()
        source_shown, caption_shown = asyncio.Event(), asyncio.Event()
        pending, audio, calls, shown = asyncio.Queue(), [], [], []
        async def translate(text):
            calls.append(text)
            translating.set()
            await translated.wait()
            return "完整的第一句字幕。" if text == FIRST + SECOND else "下一句字幕。"
        monkeypatch.setattr(bridge, "_translate_ja_to_zh", translate)
        overlay = overlay_without_window()
        monkeypatch.setattr(bridge, "_post_json", lambda url, payload, timeout: overlay.apply_reaction(payload))
        async def submit(payload):
            receipt = await bridge.submit_vn_tts_confirmed(
                {**payload, "overlay_url": "http://overlay.test/reaction"}, pending_sentence_items=pending)
            while not pending.empty():
                audio.append(pending.get_nowait())
                pending.task_done()
            (first_audio if len(audio) == 1 else second_audio).set()
            return receipt
        runtime, _ = await make_runtime(tmp_path, monkeypatch,
            [PREFIX + content(FIRST), body, content(SECOND + NEXT) + '"}', tail, TAIL], submit)
        task = asyncio.create_task(runtime.ingest_line({"text": "Displayed dialogue."}))
        await asyncio.wait_for(first_audio.wait(), 2)
        first = audio[0]
        assert first.text == FIRST and not task.done() and not calls
        assert (await bridge.get_vn_subtitle(first.sentence_id, FIRST))["status"] == "buffering"
        current = first.sentence_id
        await bridge.publish_overlay_playback(current, True)
        async def display(identity, source, chinese):
            shown.append((identity, source, chinese))
            await bridge.publish_overlay_subtitle(identity, source, chinese)
            (caption_shown if chinese else source_shown).set()
        watcher = asyncio.create_task(bridge.display_vn_subtitle(current, first.text,
            display=display, is_current=lambda identity: identity == current))
        body.set()
        await asyncio.wait_for(second_audio.wait(), 2)
        await asyncio.wait_for(translating.wait(), 2)
        await asyncio.wait_for(source_shown.wait(), 2)
        assert shown[0][1:] == (FIRST + SECOND, "")
        assert calls.count(FIRST + SECOND) == 1 and FIRST not in calls and SECOND not in calls
        current = audio[1].sentence_id
        await bridge.publish_overlay_playback(current, True)
        next_watcher = asyncio.create_task(bridge.display_vn_subtitle(current, audio[1].text,
            display=display, is_current=lambda identity: identity == current))
        translated.set()
        await asyncio.wait_for(caption_shown.wait(), 2)
        await asyncio.gather(watcher, next_watcher)
        assert overlay.text_var.get() == "完整的第一句字幕。"
        assert all(source == FIRST + SECOND for _, source, _ in shown)
        # The next completed model sentence can be prefetched, never displayed early.
        assert (await bridge.get_vn_subtitle(audio[2].sentence_id, NEXT))["japanese"] == NEXT
        await bridge.publish_overlay_subtitle(first.sentence_id, "stale", "过期片段")
        assert overlay.text_var.get() == "完整的第一句字幕。"
        tail.set()
        await task
        assert not bridge._SUBTITLE_STREAMS
        await runtime.stop()
    asyncio.run(run())


@pytest.mark.parametrize("completed", [False, True])
def test_end_of_body_flushes_only_a_normally_completed_caption(tmp_path, monkeypatch, completed):
    async def run():
        released, emitted = asyncio.Event(), asyncio.Event()
        pending, audio, calls = asyncio.Queue(), [], []
        async def translate(text):
            calls.append(text)
            return "整句字幕"
        monkeypatch.setattr(bridge, "_translate_ja_to_zh", translate)
        async def submit(payload):
            receipt = await bridge.submit_vn_tts_confirmed(payload, pending_sentence_items=pending)
            audio.append(pending.get_nowait())
            pending.task_done()
            emitted.set()
            return receipt
        ending = '"}' + TAIL if completed else RuntimeError("broken body")
        runtime, _ = await make_runtime(tmp_path, monkeypatch, [PREFIX + content(FIRST), released, ending], submit)
        task = asyncio.create_task(runtime.ingest_line({"text": "Displayed line."}))
        await asyncio.wait_for(emitted.wait(), 2)
        assert not calls
        released.set()
        await task
        await asyncio.gather(*bridge._SUBTITLE_TASKS)
        caption = await bridge.get_vn_subtitle(audio[0].sentence_id, FIRST)
        assert caption["status"] == ("completed" if completed else "cancelled")
        assert calls == ([FIRST] if completed else [])
        assert len(audio) == 1 and not bridge._SUBTITLE_STREAMS
        await runtime.stop()
    asyncio.run(run())


@pytest.mark.parametrize("revocation", ["pause", "interrupt", "queue_rejected"])
def test_revoked_or_rejected_tail_cannot_complete_the_old_caption(tmp_path, monkeypatch, revocation):
    async def run():
        from tts import pipeline
        monkeypatch.setattr(pipeline, "_tts_interrupt_epoch", 0)
        released, emitted = asyncio.Event(), asyncio.Event()
        pending, receipts, calls = asyncio.Queue(maxsize=1), [], []
        async def translate(text):
            calls.append(text)
            return "should not be translated"
        monkeypatch.setattr(bridge, "_translate_ja_to_zh", translate)
        monkeypatch.setenv("VN_TTS_QUEUE_PUT_TIMEOUT", "0.1")
        async def submit(payload):
            receipt = await bridge.submit_vn_tts_confirmed(payload, pending_sentence_items=pending)
            receipts.append(receipt)
            emitted.set()
            return receipt
        runtime, _ = await make_runtime(tmp_path, monkeypatch,
            [PREFIX + content(FIRST), released, content(SECOND) + '"}' + TAIL], submit,
            speech_epoch=pipeline.current_tts_epoch)
        task = asyncio.create_task(runtime.ingest_line({"text": "Displayed line."}))
        await asyncio.wait_for(emitted.wait(), 2)
        if revocation == "pause":
            await runtime.set_preferences({"session_id": "segments", "commentary_paused": True})
        elif revocation == "interrupt":
            monkeypatch.setattr(pipeline, "_tts_interrupt_epoch", 1)
        released.set()
        await task
        item = pending.get_nowait()
        pending.task_done()
        assert (await bridge.get_vn_subtitle(item.sentence_id, item.text))["status"] == "cancelled"
        assert not calls and not bridge._SUBTITLE_STREAMS
        if revocation == "queue_rejected":
            assert receipts[-1]["status"] == "dropped"
        await runtime.stop()
    asyncio.run(run())


def test_caption_identity_and_spaces_survive_distinct_utterances(monkeypatch):
    async def run():
        async def translate(text):
            return "translated: " + text
        monkeypatch.setattr(bridge, "_translate_ja_to_zh", translate)
        queue, items = asyncio.Queue(), []
        for utterance, segment, text in (("a", 1, "The first part "), ("b", 1, "Another sentence."),
                                         ("a", 2, "continues here.")):
            await bridge.submit_vn_tts_confirmed({"text": text, "vn_speech_id": utterance,
                "vn_speech_segment": segment, "line_id": "same-game-line"}, pending_sentence_items=queue)
            items.append(queue.get_nowait())
            queue.task_done()
        bridge.finish_vn_speech("a", True)
        bridge.finish_vn_speech("b", True)
        await asyncio.gather(*bridge._SUBTITLE_TASKS)
        captions = [await bridge.get_vn_subtitle(item.sentence_id, item.text) for item in items]
        assert captions[0]["japanese"] == captions[2]["japanese"] == "The first part continues here."
        assert captions[1]["japanese"] == "Another sentence."
        assert not bridge._SUBTITLE_STREAMS
    asyncio.run(run())


def test_complete_response_uses_the_same_caption_lifecycle(tmp_path, monkeypatch):
    async def run():
        calls, queue = [], asyncio.Queue()
        async def translate(text):
            calls.append(text)
            return "完整字幕"
        monkeypatch.setattr(bridge, "_translate_ja_to_zh", translate)
        async def submit(payload):
            return await bridge.submit_vn_tts_confirmed(payload, pending_sentence_items=queue)
        runtime, _ = await make_runtime(tmp_path, monkeypatch, [], submit)
        text = "実際に" + FIRST + SECOND
        await runtime._speak({"text": "[EMO preset=thinking dur=8s] " + text}, {})
        await asyncio.gather(*bridge._SUBTITLE_TASKS)
        assert queue.qsize() == 2 and calls == [text]
        while not queue.empty():
            item = queue.get_nowait()
            queue.task_done()
            assert (await bridge.get_vn_subtitle(item.sentence_id, item.text))["japanese"] == text
        assert not bridge._SUBTITLE_STREAMS
        await runtime.stop()
    asyncio.run(run())


@pytest.mark.parametrize("mode,expected", [("streamed_vn", []), ("legacy_vn", [(FIRST, "")]), ("chat", [(FIRST, "")])])
def test_raw_playback_hook_cannot_replace_a_grouped_caption_with_a_fragment(mode, expected):
    if mode == "streamed_vn":
        bridge._SENTENCE_META["playing"] = {"_subtitle": {"japanese": FIRST + SECOND}}
    elif mode == "legacy_vn":
        bridge._SENTENCE_META["playing"] = {"display_language": "japanese"}
    updates = []
    bridge.update_playback_subtitle("playing", FIRST, "", update=lambda source, chinese: updates.append((source, chinese)))
    assert updates == expected
