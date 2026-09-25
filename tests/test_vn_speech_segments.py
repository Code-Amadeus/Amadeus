"""First speech is independent of body completion; tails never replay or invent audio."""
import asyncio
import json

import pytest

from server.vn_tts_bridge import submit_vn_tts_confirmed
from vn_player.llm_client import _speech_prefix, _string_prefix
from vn_player.runtime import VNPlayerRuntime
from vn_player.schemas import default_response
from test_vn_llm_streaming import Stream, install


META = {"priority": "normal", "interrupt": False, "expires_after_lines": 3,
        "target_line_id": "", "target_script_id": "", "emotion_intent": "thinking"}
PREFIX = json.dumps({"decision": "speak", "importance": .8, "confidence": .9,
                     "speak": {**META, "text": ""}}, ensure_ascii=False)[:-3]
TAIL = ',"context_patches":[],"reason_label":"other"}'
FIRST, SECOND = "最初の話はここまでね。", "次の話を続けるわ。"


def content(text):
    return json.dumps(text, ensure_ascii=True)[1:-1]


async def make_runtime(path, monkeypatch, parts, submit, *, speech_epoch=None):
    sdk, _ = install(monkeypatch, [Stream(parts)])
    runtime = VNPlayerRuntime(path, speak_callback=submit, speech_epoch=speech_epoch)
    runtime._llm_enabled = runtime._immediate_llm_enabled = True
    await runtime.start({"session_id": "segments", "prompt_pack": "base", "script_path": "",
                         "output_language": "ja", "summary_llm_enabled": False,
                         "retrospective_llm_enabled": False})
    return runtime, sdk


def test_first_segment_plays_before_text_closes_and_later_segments_are_not_first(tmp_path, monkeypatch):
    async def run():
        body, tail = asyncio.Event(), asyncio.Event()
        first_queued, second_queued = asyncio.Event(), asyncio.Event()
        pending, spoken = asyncio.Queue(), []
        async def submit(payload):
            receipt = await submit_vn_tts_confirmed({**payload, "display_language": "japanese"}, pending_sentence_items=pending)
            spoken.append(payload)
            (first_queued if len(spoken) == 1 else second_queued).set()
            return receipt
        runtime, sdk = await make_runtime(tmp_path, monkeypatch,
            [PREFIX + content(FIRST), body, content(SECOND) + '"}', tail, TAIL], submit)
        task = asyncio.create_task(runtime.ingest_line({"text": "Displayed dialogue."}))
        await asyncio.wait_for(first_queued.wait(), 2)
        assert not task.done() and spoken[0]["text"] == FIRST
        assert spoken[0]["vn_speech_segment"] == 1
        assert pending.get_nowait().is_first
        pending.task_done()
        body.set()
        await asyncio.wait_for(second_queued.wait(), 2)
        assert not task.done(), "the verified memory tail may still be pending"
        assert spoken[1]["text"] == SECOND and spoken[1]["vn_speech_segment"] == 2
        assert not pending.get_nowait().is_first
        pending.task_done()
        tail.set()
        result = await task
        assert result["reaction"]["speak"]["text"] == FIRST + SECOND
        assert len(spoken) == 2 and len(runtime._recent_speaks) == 1
        sdk.chat.completions.create.assert_awaited_once()
        await runtime.stop()
    asyncio.run(run())


@pytest.mark.parametrize("first", ["", FIRST])
def test_broken_body_discards_only_the_unsent_fragment(tmp_path, monkeypatch, first):
    async def run():
        spoken = []
        runtime, sdk = await make_runtime(tmp_path, monkeypatch,
            [PREFIX + content(first + "未完成の")], lambda p: spoken.append(p))
        result = await runtime.ingest_line({"text": "Displayed line."})
        assert "".join(p["text"] for p in spoken) == first
        assert result["reaction"]["context_patches"] == []
        assert result["reaction"]["decision"] == ("speak" if first else "silence")
        if first:
            assert result["reaction"]["speak"]["text"] == first
        sdk.chat.completions.create.assert_awaited_once()
        await runtime.stop()
    asyncio.run(run())


def test_pause_after_first_segment_revokes_the_remainder(tmp_path, monkeypatch):
    async def run():
        release, emitted = asyncio.Event(), asyncio.Event()
        spoken = []
        def submit(payload):
            spoken.append(payload)
            emitted.set()
        runtime, _ = await make_runtime(tmp_path, monkeypatch,
            [PREFIX + content(FIRST), release, content(SECOND) + '"}' + TAIL], submit)
        task = asyncio.create_task(runtime.ingest_line({"text": "Displayed line."}))
        await asyncio.wait_for(emitted.wait(), 2)
        await runtime.set_preferences({"session_id": "segments", "commentary_paused": True})
        release.set()
        result = await task
        assert [p["text"] for p in spoken] == [FIRST]
        assert result["reaction"]["speak"]["text"] == FIRST
        assert len(runtime._recent_speaks) == 1
        await runtime.stop()
    asyncio.run(run())


@pytest.mark.parametrize("question", [False, True])
def test_host_audio_interrupt_revokes_old_segments_but_allows_a_new_answer(tmp_path, monkeypatch, question):
    async def run():
        from tts import pipeline
        monkeypatch.setattr(pipeline, "_tts_interrupt_epoch", 0)
        release, emitted = asyncio.Event(), asyncio.Event()
        pending, spoken = asyncio.Queue(), []
        async def submit(payload):
            receipt = await submit_vn_tts_confirmed(payload, pending_sentence_items=pending)
            spoken.append(payload)
            emitted.set()
            return receipt
        runtime, sdk = await make_runtime(tmp_path, monkeypatch,
            [PREFIX + content(FIRST), release, content(SECOND) + '"}' + TAIL], submit,
            speech_epoch=pipeline.current_tts_epoch)
        request = runtime.player_intervention("ask", {"text": "What happened?"}) if question else runtime.ingest_line({"text": "Displayed line."})
        task = asyncio.create_task(request)
        await asyncio.wait_for(emitted.wait(), 2)
        assert pending.get_nowait().tts_epoch == 0
        pending.task_done()
        # The existing Host interrupt invalidates its playback generation.
        # The later model chunk must not be stamped as new, post-interrupt audio.
        monkeypatch.setattr(pipeline, "_tts_interrupt_epoch", 1)
        release.set()
        result = await task
        assert [p["text"] for p in spoken] == [FIRST] and pending.empty()
        assert result["reaction"]["speak"]["text"] == FIRST
        sdk.chat.completions.create.side_effect = [Stream([PREFIX + content(SECOND) + '"}' + TAIL])]
        await runtime.player_intervention("ask", {"text": "Please answer now."})
        assert spoken[-1]["text"] == SECOND
        assert pending.get_nowait().tts_epoch == 1
        pending.task_done()
        await runtime.stop()
    asyncio.run(run())


def test_rejected_later_segment_keeps_only_accepted_speech(tmp_path, monkeypatch):
    async def run():
        accepted, attempts = [], []
        def submit(payload):
            attempts.append(payload)
            if len(attempts) == 1:
                accepted.append(payload)
                return {"status": "queued"}
            return {"status": "dropped", "reason": "test_queue_full"}
        runtime, sdk = await make_runtime(tmp_path, monkeypatch,
            [PREFIX + content(FIRST + SECOND) + '"}' + TAIL], submit)
        result = await runtime.ingest_line({"text": "Displayed line."})
        assert [p["text"] for p in accepted] == [FIRST]
        assert len(attempts) == 2 and result["reaction"]["speak"]["text"] == FIRST
        sdk.chat.completions.create.assert_awaited_once()
        await runtime.stop()
    asyncio.run(run())


def test_restart_after_first_segment_revokes_the_old_tail(tmp_path, monkeypatch):
    async def run():
        release, emitted = asyncio.Event(), asyncio.Event()
        spoken = []
        def submit(payload):
            spoken.append(payload)
            emitted.set()
        runtime, _ = await make_runtime(tmp_path, monkeypatch,
            [PREFIX + content(FIRST), release, content(SECOND) + '"}' + TAIL], submit)
        task = asyncio.create_task(runtime.ingest_line({"text": "Old session line."}))
        await asyncio.wait_for(emitted.wait(), 2)
        await runtime.start({"session_id": "new", "prompt_pack": "base", "script_path": ""})
        release.set()
        assert (await task)["status"] == "ignored"
        assert [p["text"] for p in spoken] == [FIRST]
        assert not runtime.store.short_memory() and not runtime.activity()
        await runtime.stop()
    asyncio.run(run())


@pytest.mark.parametrize("same", [False, True])
def test_repeat_guard_waits_for_a_distinguishable_prefix(tmp_path, monkeypatch, same):
    async def run():
        release, started = asyncio.Event(), asyncio.Event()
        spoken = []
        ending = SECOND if same else "別の展開になったわ。"
        runtime, _ = await make_runtime(tmp_path, monkeypatch,
            [PREFIX + content(FIRST), started, release, content(ending) + '"}' + TAIL], lambda p: spoken.append(p))
        prior_line = runtime.store.record_line({"text": "Earlier displayed text."}, None)
        runtime.store.record_reaction({**default_response("speak"), "speak": {**META, "text": FIRST + SECOND}}, prior_line)
        task = asyncio.create_task(runtime.ingest_line({"text": "A new displayed line."}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not spoken
        started.set()
        release.set()
        result = await task
        if same:
            assert not spoken and result["reaction"]["reason_label"] == "repeated_speech"
        else:
            assert "".join(p["text"] for p in spoken) == FIRST + ending
        await runtime.stop()
    asyncio.run(run())


def test_split_emoji_escape_never_reaches_speech_as_a_surrogate():
    encoded = json.dumps('風🌸、"引用"と\\記号。', ensure_ascii=True)
    for index in range(1, len(encoded) + 1):
        value, _, complete = _string_prefix(encoded[:index], 0)
        assert '風🌸、"引用"と\\記号。'.startswith(value)
        value.encode("utf-8")
        assert complete == (index == len(encoded))


def test_partial_body_requires_confirmed_playback_metadata():
    prefix = _speech_prefix(PREFIX + content(FIRST))
    assert prefix[0]["speak"]["text"] == FIRST and prefix[1] is False
    assert _speech_prefix('{"decision":"speak","importance":0.8,"confidence":0.9,"speak":{"text":"' + FIRST) is None


def test_partial_emotion_tag_is_never_spoken_or_replaced_with_filler(tmp_path, monkeypatch):
    async def run():
        release, started = asyncio.Event(), asyncio.Event()
        spoken = []
        runtime, _ = await make_runtime(tmp_path, monkeypatch,
            [PREFIX + '[EMO preset=thi', started, release,
             'nking dur=8s] ' + content(FIRST) + '"}' + TAIL], lambda p: spoken.append(p))
        task = asyncio.create_task(runtime.ingest_line({"text": "Displayed line."}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not spoken
        started.set()
        release.set()
        result = await task
        assert "".join(p["text"] for p in spoken) == FIRST
        assert result["reaction"]["speak"]["text"] == FIRST
        await runtime.stop()
    asyncio.run(run())


def test_utterance_lock_keeps_an_answer_out_of_a_comment_body(tmp_path, monkeypatch):
    async def run():
        from vn_player.speech_stream import VNSpeechStream
        lock, spoken = asyncio.Lock(), []
        active = lambda: True
        authorize = lambda header, closed: header
        async def submit(payload):
            spoken.append(payload["text"])
        def stream():
            return VNSpeechStream(authorize=authorize, active=active, submit=submit, normalize=lambda t: t, lock=lock)
        one, two = stream(), stream()
        def header(text):
            return {**default_response("speak"), "speak": {**META, "text": text}}
        await one.update(header(FIRST), False)
        await asyncio.sleep(0)
        await two.update(header("質問の答えよ。"), True)
        await asyncio.sleep(0)
        assert spoken == [FIRST]
        await one.update(header(FIRST + SECOND), True)
        await asyncio.gather(one.finish(), two.finish())
        assert spoken == [FIRST, SECOND, "質問の答えよ。"]
    asyncio.run(run())
