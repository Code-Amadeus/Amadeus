"""Route-independent history, device preparation and terminal scene ownership."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.chat_runtime import ChatRuntime
from server.cooperative_delivery import CooperativeHostDelivery
from server.work_observer import WorkObserverCoordinator
from test_chat_role_delivery import role_loop, streaming_role as streaming_role
from test_chat_role_delivery import host_role_ownership as host_role_ownership


@pytest.mark.parametrize("policy, expected", [
    ("strip", "ええ、考えるわ。結論よ。"),
    ("expressive_only", "ええ、[EMO thinking]考えるわ。結論よ。"),
    ("preserve", "ええ、[EMO thinking]考えるわ。[EMO normal]結論よ。"),
])
async def test_completed_role_history_policy_does_not_change_display_or_voice(monkeypatch, policy, expected):
    monkeypatch.setattr("core.chat_history_projection.EMO_HISTORY_POLICY", policy)
    text = "ええ、[EMO thinking]考えるわ。[EMO normal]結論よ。"
    display, history = Mock(return_value=True), Mock(return_value=True)
    voice = AsyncMock(return_value={"status":"queued"})
    delivery = CooperativeHostDelivery(session_id="s", display=display,
        record_display=history, narration_sink=voice)
    assert await delivery({"cause":"t", "text":text})
    assert display.call_args.args[0]["text"] == text
    assert history.call_args.kwargs["content"] == expected
    assert voice.call_args.args[0]["voice_text_ja"] == text


async def test_role_prepares_audio_before_query_without_blocking_loop(
        streaming_role, monkeypatch, host_role_ownership):
    import core.turn_coordinator as tc
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    host = streaming_role
    entered, release = threading.Event(), threading.Event()
    initialized = []
    loop_thread = threading.get_ident()

    def initialize(rate):
        initialized.append((rate, threading.get_ident()))
        entered.set()
        assert release.wait(3)

    host.runtime._playback_manager.player = SimpleNamespace(initialize=initialize)
    monkeypatch.delenv("AMADEUS_E2E_NO_TTS", raising=False)
    queried = asyncio.Event()

    async def query(_messages, *, on_text=None):
        assert release.is_set()
        queried.set()
        raw = '{"action":null,"say":"ええ。"}'
        await on_text(raw)
        return raw

    loop = role_loop(query, host.delivery)
    task = asyncio.create_task(loop.submit("hi", turn_id="prepared"))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        # Reaching this line while initialize is blocked proves the UI loop runs.
        assert not queried.is_set() and host.queue.empty()
        assert initialized == [(24000, initialized[0][1])]
        assert initialized[0][1] != loop_thread
        release.set()
        await asyncio.wait_for(task, 3)
        assert queried.is_set() and host.queue.qsize() == 1
    finally:
        release.set()
        await loop.close()


async def test_silent_role_does_not_open_audio_device(monkeypatch):
    runtime = ChatRuntime()
    initialize = Mock()
    runtime.configure(pending_sentence_items=asyncio.Queue(),
        playback_manager=SimpleNamespace(player=SimpleNamespace(initialize=initialize)))
    await runtime.begin_role_text_stream(turn_id="silent", speech=False).prepare()
    initialize.assert_not_called()


@pytest.mark.parametrize("cancel", [False, True])
async def test_external_result_uses_existing_bounded_wait_and_releases_on_cancel(cancel):
    observer = WorkObserverCoordinator()
    entered, voice_done = asyncio.Event(), asyncio.Event()
    async def wait_for_voice():
        entered.set()
        await voice_done.wait()
        return True
    observer._wait_for_terminal_output_idle = wait_for_voice
    release = AsyncMock()
    observer.configure(release_work=release)
    task = asyncio.create_task(observer.finish_external_presentation("native-run"))
    await entered.wait()
    release.assert_not_called()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        voice_done.set()
        await task
    release.assert_awaited_once_with("native-run")


async def test_native_progress_uses_original_narrator_then_retires_before_external_result(monkeypatch):
    from test_work_narration_priority import _note, _settle_narration
    observer = WorkObserverCoordinator()
    line = "ゲームの入力処理まで確認できたわ。"
    model = AsyncMock(return_value={"action":"speak", "speak":True,
        "append_to_main_chat":True, "display_text":line, "main_chat_entry":line,
        "display_language":"japanese"})
    voice, history = AsyncMock(return_value={"status":"queued"}), Mock()
    observer.configure(observer_llm=model, narrate=voice, append_to_main_chat=history,
        is_chat_busy=lambda:False, is_tts_busy=lambda:False)
    observer._wait_for_output_idle = AsyncMock()
    note = _note("Game input handling has been checked.", "semantic_progress", milestone="validation")
    note["metadata"]["cooperative_context_id"] = "native-context"
    try:
        await observer._handle_note(note)
        await _settle_narration(observer)
        model.assert_awaited_once()
        voice.assert_awaited_once()
        assert voice.call_args.args[0]["run_id"] == note["run_id"]
        assert voice.call_args.args[0]["voice_text_ja"] == line
        history.assert_called_once()
        await observer.begin_external_result(note["run_id"])
        assert observer.get_session(note["run_id"]) is None
        await observer._handle_note({**note, "summary":"stale progress"})
        assert model.await_count == voice.await_count == 1
    finally:
        await observer.close()


def test_native_progress_queue_cleanup_keeps_other_runs_and_terminal_speech(monkeypatch):
    from tts import pipeline
    from tts.contract import TTSRequest
    queue = asyncio.Queue()
    for ident, source, terminal in (("a","work_observer",False),
            ("b","work_observer",False), ("a","work_observer",True), ("a","chat",False)):
        queue.put_nowait(TTSRequest(sentence_id="sentence_1_"+ident,
            text=ident, source=source, metadata={"run_id":ident, "terminal":terminal}))
    monkeypatch.setattr(pipeline, "_pending_sentence_items", queue)
    from tts.utterance_scheduler import TTSUtteranceScheduler
    monkeypatch.setattr(pipeline, "_utterance_scheduler", TTSUtteranceScheduler())
    assert pipeline.discard_pending_tts(source="work_observer", run_id="a", nonterminal_only=True) == 1
    assert queue.qsize() == 3
