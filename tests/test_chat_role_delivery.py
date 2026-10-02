import asyncio
import ast
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

import pytest

from core.chat_runtime import ChatRuntime
from core.session_manager import ConversationHistory
from server.chat_role_delivery import ChatRoleDelivery
from server.cooperative_delivery import CooperativeHostDelivery, query_role_messages
from server.handlers.chat_handler import ChatHandler
from server.protocol import Method


@pytest.fixture(autouse=True)
def host_role_ownership(monkeypatch):
    monkeypatch.setattr("server.chat_role_delivery.sm.get_current_session_id", lambda: "A")
    monkeypatch.setattr(ChatHandler, "_turn_allows_visible_emit", AsyncMock(return_value=True))


@pytest.mark.parametrize("accept", [True, False])
async def test_renderer_receipt_requires_exact_message_and_session(monkeypatch, accept):
    delivery = ChatRoleDelivery()
    emitted = []
    async def emit(method, payload):
        emitted.append((method, payload))
        assert not (await delivery.handle(Method.CHAT_ROLE_RECEIVED,
            {"session_id":"other", "message_id":"run", "accepted":True}))["accepted"]
        assert (await delivery.handle(Method.CHAT_ROLE_RECEIVED,
            {"session_id":"A", "message_id":"run", "accepted":accept}))["accepted"]
    monkeypatch.setattr("server.chat_role_delivery.bus.emit", emit)
    assert await delivery.publish({"cause":"run", "session_id":"A", "text":"需要哪些东西？"}) is True
    assert emitted == [(Method.CHAT_ROLE_MESSAGE,
        {"session_id":"A", "message_id":"run", "text":"需要哪些东西？"})]
    assert delivery.receipts[-1]["state"] == ("accepted" if accept else "declined")


async def test_no_renderer_reply_does_not_block_publication_or_speech(monkeypatch):
    from unittest.mock import AsyncMock
    emit = AsyncMock()
    monkeypatch.setattr("server.chat_role_delivery.bus.emit", emit)
    delivery = ChatRoleDelivery()
    voice = AsyncMock(return_value={"status":"queued"})
    publication = CooperativeHostDelivery(session_id="A", display=delivery.publish, narration_sink=voice)
    assert await asyncio.wait_for(publication({"cause":"run", "text":"done"}), timeout=1)
    emit.assert_awaited_once()
    voice.assert_awaited_once()
    assert publication.receipts[-1]["published"] is True
    assert delivery.receipts[-1]["state"] == "published"
    assert (await delivery.handle(Method.CHAT_ROLE_RECEIVED,
        {"session_id":"A", "message_id":"run", "accepted":True}))["accepted"]


async def test_live_role_message_reuses_session_display_tag_projection(monkeypatch):
    delivery = ChatRoleDelivery()
    emitted = []
    async def emit(method, payload):
        emitted.append((method, payload))
        assert (await delivery.handle(Method.CHAT_ROLE_RECEIVED,
            {"session_id":"A", "message_id":"run-tags", "accepted":True}))["accepted"]
    monkeypatch.setattr("server.chat_role_delivery.bus.emit", emit)
    assert await delivery.publish({"cause":"run-tags", "session_id":"A",
        "text":"了解。[EMO preset=normal dur=4s] 完成したわ。"}) is True
    assert emitted == [(Method.CHAT_ROLE_MESSAGE,
        {"session_id":"A", "message_id":"run-tags", "text":"了解。 完成したわ。"})]


async def test_partial_role_updates_use_cumulative_chat_tokens_until_one_final_receipt(monkeypatch):
    delivery = ChatRoleDelivery()
    emitted = []

    async def emit(method, payload):
        emitted.append((method, payload))

    monkeypatch.setattr("server.chat_role_delivery.bus.emit", emit)
    event = {"cause":"stream-role", "session_id":"A", "text":"一。"}
    assert await delivery.publish_partial(event)
    assert await delivery.publish_partial({**event, "text":"一。二。"})
    assert emitted == [
        (Method.CHAT_TOKEN,
            {"token":"一。", "turn_id":"stream-role", "session_id":"A"}),
        (Method.CHAT_TOKEN,
            {"token":"一。二。", "turn_id":"stream-role", "session_id":"A"}),
    ]
    assert delivery.receipts == []
    assert not (await delivery.handle(Method.CHAT_ROLE_RECEIVED,
        {"session_id":"A", "message_id":"stream-role", "accepted":True}))["accepted"]

    assert await delivery.publish({**event, "text":"一。二。三。"})
    assert emitted[-1] == (Method.CHAT_ROLE_MESSAGE,
        {"message_id":"stream-role", "session_id":"A", "text":"一。二。三。"})
    assert delivery.receipts == [{"message_id":"stream-role", "session_id":"A",
        "state":"published"}]
    assert (await delivery.handle(Method.CHAT_ROLE_RECEIVED,
        {"session_id":"A", "message_id":"stream-role", "accepted":True}))["accepted"]


@pytest.mark.parametrize("foreign_session", [True, False])
async def test_partial_role_update_rejects_foreign_or_interrupted_turn(
        monkeypatch, foreign_session):
    emit = AsyncMock()
    monkeypatch.setattr("server.chat_role_delivery.bus.emit", emit)
    if not foreign_session:
        monkeypatch.setattr(ChatHandler, "_turn_allows_visible_emit",
            AsyncMock(return_value=False))
    delivery = ChatRoleDelivery()
    assert not await delivery.publish_partial({"cause":"stale-role",
        "session_id":"B" if foreign_session else "A", "text":"古い途中。"})
    emit.assert_not_awaited()
    assert delivery.receipts == []


async def test_handler_callback_streams_past_slow_ui_and_preserves_interrupt_text(
        streaming_role, monkeypatch):
    host = streaming_role
    token_emit_started = asyncio.Event()
    release_token_emit = asyncio.Event()
    second_delta_returned = threading.Event()
    finish_transport = threading.Event()
    emitted = []

    async def slow_emit(method, payload):
        emitted.append((method, payload))
        if method == Method.CHAT_TOKEN:
            token_emit_started.set()
            await release_token_emit.wait()

    monkeypatch.setattr("server.handlers.chat_handler.bus.emit", slow_emit)
    monkeypatch.setattr("server.chat_role_delivery.bus.emit", slow_emit)
    chunks = ('{"action":null,"say":"前半', '。後半')
    raw = "".join(chunks) + '。"}'

    def transport(_messages, *, on_text):
        on_text(chunks[0])
        on_text(chunks[1])
        second_delta_returned.set()
        finish_transport.wait(2)
        on_text('。"}')
        return raw

    async def query(messages, *, on_text=None):
        return await query_role_messages(transport, messages, on_text=on_text)

    loop = role_loop(query, host.delivery)

    async def runner(text, *, gui_callback=None, turn_id="", **_kwargs):
        return await loop.submit(text, turn_id=turn_id, gui_callback=gui_callback)

    handler = ChatHandler()
    handler.configure(stream_llm_query=runner, pending_sentence_items=None)
    try:
        await handler.send_text("stream it", session_id="A", turn_id="handler-stream")
        await asyncio.wait_for(token_emit_started.wait(), 1)
        assert await asyncio.to_thread(second_delta_returned.wait, 1)
        assert host.queue.qsize() == 1
        assert host.queue.get_nowait().text == "前半。"
        assert handler._active_accumulated_text == "前半。後半"
        interrupted = await handler._handle_abort(
            {"turn_id":"handler-stream", "stop_execution":False})
        assert interrupted == {"status":"aborted", "turn_id":"handler-stream",
            "accumulated_text":"前半。後半"}
        assert not host.display.receipts
        assert not any(method == Method.CHAT_COMPLETE for method, _ in emitted)
    finally:
        release_token_emit.set()
        finish_transport.set()
        await asyncio.gather(handler._stream_task, return_exceptions=True)
        await handler.close()
        await loop.close()


@pytest.mark.parametrize("foreign_session", [True, False])
async def test_role_stream_callback_never_runs_after_session_or_turn_rejection(
        streaming_role, monkeypatch, foreign_session):
    host = streaming_role
    if foreign_session:
        monkeypatch.setattr("server.chat_role_delivery.sm.get_current_session_id",
            lambda:"B")
    else:
        monkeypatch.setattr(ChatHandler, "_turn_allows_visible_emit",
            AsyncMock(return_value=False))
    callback = Mock()
    stream = host.delivery.begin_stream("rejected-role", gui_callback=callback)
    with pytest.raises(asyncio.CancelledError):
        await stream.feed("見せない途中。")
    callback.assert_not_called()
    assert host.emitted == []
    assert host.queue.empty()
    assert host.display.receipts == []


@pytest.mark.parametrize("foreign_session", [True, False])
async def test_host_ownership_still_suppresses_foreign_or_interrupted_output(monkeypatch, foreign_session):
    emit = AsyncMock()
    monkeypatch.setattr("server.chat_role_delivery.bus.emit", emit)
    if not foreign_session:
        monkeypatch.setattr(ChatHandler, "_turn_allows_visible_emit", AsyncMock(return_value=False))
    delivery = ChatRoleDelivery()
    voice = AsyncMock()
    publication = CooperativeHostDelivery(session_id="B" if foreign_session else "A",
        display=delivery.publish, narration_sink=voice)
    assert not await publication({"cause":"run", "text":"old reply"})
    emit.assert_not_called()
    voice.assert_not_called()


async def test_renderer_decline_is_observation_not_a_speech_gate(monkeypatch):
    delivery = ChatRoleDelivery()
    async def emit(_method, payload):
        await delivery.handle(Method.CHAT_ROLE_RECEIVED, {**payload, "accepted":False})
    monkeypatch.setattr("server.chat_role_delivery.bus.emit", emit)
    voice = AsyncMock(return_value={"status":"queued"})
    publication = CooperativeHostDelivery(session_id="A", display=delivery.publish, narration_sink=voice)
    assert await publication({"cause":"run", "text":"ordinary reply"})
    voice.assert_awaited_once()
    assert delivery.receipts[-1]["state"] == "declined"


@pytest.mark.parametrize("session_id", ["", "A"])
async def test_cooperative_completion_cannot_overwrite_clean_role_text(monkeypatch, session_id):
    raw = "[EMO preset=normal dur=3s] あら、こんにちは。"
    emitted = AsyncMock()
    handler = ChatHandler()
    handler._chat_epoch = 7
    handler._active_turn_id = "turn-clean"
    handler._control_turn_runner = AsyncMock(return_value=raw)
    handler._turn_allows_visible_emit = AsyncMock(return_value=True)
    handler._notify_coordinator_finished = Mock()
    monkeypatch.setattr("server.handlers.chat_handler.bus.emit", emitted)

    await handler._run_stream(
        "hi",
        lambda _text: None,
        "turn-clean",
        session_id=session_id,
        chat_epoch=7,
        turn_admission=SimpleNamespace(authority_mode="turn_decision"),
        history_snapshot=ConversationHistory(),
    )

    emitted.assert_awaited_once_with(
        Method.CHAT_COMPLETE,
        {"turn_id": "turn-clean", "session_id": session_id, "full_text": "あら、こんにちは。"},
    )


async def test_chat_error_retains_its_originating_session(monkeypatch):
    handler = ChatHandler()
    handler._chat_epoch = 7
    handler._active_turn_id = "failed-turn"
    handler._control_turn_runner = AsyncMock(side_effect=RuntimeError("query failed"))
    handler._turn_allows_visible_emit = AsyncMock(return_value=True)
    handler._notify_coordinator_finished = Mock()
    emitted = AsyncMock()
    monkeypatch.setattr("server.handlers.chat_handler.bus.emit", emitted)
    await handler._run_stream("hi", lambda _text:None, "failed-turn", session_id="A",
        chat_epoch=7, turn_admission=SimpleNamespace(authority_mode="turn_decision", pending=False),
        history_snapshot=ConversationHistory())
    emitted.assert_awaited_once_with(Method.CHAT_ERROR,
        {"turn_id":"failed-turn", "session_id":"A", "error":"query failed"})


async def test_completed_cooperative_role_reuses_main_chat_tts_contract(monkeypatch):
    queue = asyncio.Queue()
    playback = SimpleNamespace(mark_turn_last_sentence=Mock())
    expression = SimpleNamespace(register_sentence_actions=Mock())
    runtime = ChatRuntime()
    runtime.configure(
        pending_sentence_items=queue,
        playback_manager=playback,
    )
    sentence_ids = iter(("sentence_1_role", "sentence_2_role", "sentence_3_role"))
    monkeypatch.setattr(
        "core.chat_runtime.sentence_state_manager.create_sentence",
        lambda _text: next(sentence_ids),
    )
    monkeypatch.setattr("core.chat_runtime._pre_translation_enabled", lambda: False)
    monkeypatch.setattr("core.chat_runtime._get_expr_ctrl", lambda: expression)

    result = await runtime.enqueue_completed_role_text(
        "[EMO preset=normal dur=3s] 一。二。三。",
        turn_id="role-turn",
    )

    items = [queue.get_nowait(), queue.get_nowait(), queue.get_nowait()]
    assert [item.text for item in items] == ["一。", "二。", "三。"]
    assert [item.stream_tts for item in items] == [True, False, False]
    assert all(item.source == "chat" and item.turn_id == "role-turn" for item in items)
    assert result == {
        "status": "queued",
        "sentence_id": "sentence_3_role",
        "last_sentence_id": "sentence_3_role",
        "sentence_count": 3,
    }
    playback.mark_turn_last_sentence.assert_called_once_with(
        "sentence_3_role",
        "role-turn",
    )
    expression.register_sentence_actions.assert_called_once()


@pytest.mark.parametrize("visible", [True, False])
async def test_bootstrap_role_publisher_uses_chat_runtime_speech(monkeypatch, visible):
    """Exercise the actual bootstrap wiring without starting devices or Providers."""
    queue = asyncio.Queue()
    playback = SimpleNamespace(mark_turn_last_sentence=Mock())
    runtime = ChatRuntime()
    runtime.configure(pending_sentence_items=queue, playback_manager=playback)
    ids = iter(f"sentence_{i}_role" for i in range(1, 20))
    monkeypatch.setattr("core.chat_runtime.sentence_state_manager.create_sentence",
        lambda _text: next(ids))
    monkeypatch.setattr("core.chat_runtime._pre_translation_enabled", lambda: False)
    monkeypatch.setattr("core.session_manager.append_session_message", Mock(return_value=True))
    vn_voice = AsyncMock(return_value={"status": "queued"})
    scope = {
        "CooperativeHostDelivery": CooperativeHostDelivery,
        "chat_role_delivery": SimpleNamespace(publish=AsyncMock(return_value=visible),
            publish_partial=AsyncMock(return_value=visible), allows=AsyncMock(return_value=visible)),
        "cooperative_deliveries": {}, "chat_runtime": runtime,
        "work_observer": SimpleNamespace(finish_external_presentation=AsyncMock(),
            begin_external_result=AsyncMock()),
        "e2e_no_tts": False, "logger": Mock(), "_speak_vn_reaction": vn_voice,
    }
    path = Path(__file__).resolve().parents[1] / "server/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bootstrap = next(node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "bootstrap")
    definitions = [node for node in ast.walk(bootstrap)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_cooperative_publisher", "_speak_cooperative_chat"}]
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(path), "exec"), scope)
    delivery = scope["_cooperative_publisher"]("role-session")

    assert await delivery({"cause": "role-turn", "text": "一。二。三。"}) is visible

    vn_voice.assert_not_called()
    if not visible:
        assert queue.empty()
        playback.mark_turn_last_sentence.assert_not_called()
        return
    items = [queue.get_nowait() for _ in range(queue.qsize())]
    assert [item.text for item in items] == ["一。", "二。", "三。"]
    assert [(item.source, item.turn_id, item.stream_tts) for item in items] == [
        ("chat", "role-turn", True), ("chat", "role-turn", False),
        ("chat", "role-turn", False)]
    playback.mark_turn_last_sentence.assert_called_once_with(items[-1].sentence_id, "role-turn")
    assert delivery.receipts[-1]["narration"]["accepted"] is True

    # The real shared scheduler can aggregate the tail, unlike an all-streaming VN line.
    from tts.utterance_scheduler import TTSUtteranceScheduler
    monkeypatch.setenv("ENABLE_TTS_UTTERANCE_SCHEDULER", "1")
    monkeypatch.setenv("TTS_UTTERANCE_MIN_START_SEQ", "2")
    monkeypatch.setenv("TTS_UTTERANCE_MAX_SENTENCES", "2")
    for item in items:
        queue.put_nowait(item)
    scheduler = TTSUtteranceScheduler()
    first = await scheduler.next_job(queue)
    tail = await scheduler.next_job(queue)
    assert first.is_first and first.consumed_count == 1
    assert not tail.is_first and tail.consumed_count == 2
    assert tail.turn_id == "role-turn" and tail.text == "二。三。"


@pytest.fixture
def streaming_role(monkeypatch):
    queue = asyncio.Queue()
    runtime = ChatRuntime()
    playback = SimpleNamespace(mark_turn_last_sentence=Mock())
    runtime.configure(pending_sentence_items=queue, playback_manager=playback)
    monkeypatch.setattr("core.chat_runtime._pre_translation_enabled", lambda: False)
    monkeypatch.setattr("core.chat_runtime._get_expr_ctrl",
        lambda: SimpleNamespace(register_sentence_actions=Mock()))
    emitted = []

    async def emit(method, payload):
        emitted.append((method, payload))

    monkeypatch.setattr("server.chat_role_delivery.bus.emit", emit)
    display = ChatRoleDelivery()
    history = Mock(return_value=True)

    async def narrate(payload):
        return await runtime.enqueue_completed_role_text(payload["voice_text_ja"], turn_id=payload["turn_id"])

    narration = AsyncMock(side_effect=narrate)
    delivery = CooperativeHostDelivery(session_id="A", display=display.publish,
        partial_display=display.publish_partial, allows=display.allows,
        role_stream_factory=lambda cause, gui_callback=None,
            auip_background_capture_release=None:runtime.begin_role_text_stream(
                turn_id=cause, gui_callback=gui_callback,
                auip_background_capture_release=auip_background_capture_release),
        narration_sink=narration, record_display=history)
    return SimpleNamespace(queue=queue, runtime=runtime, playback=playback, emitted=emitted,
        display=display, history=history, narration=narration, delivery=delivery)


def role_loop(query, delivery):
    from agent_host.provider_runtime import ProviderRuntime
    from server.cooperative_provider_loop import CooperativeProviderLoop

    return CooperativeProviderLoop(ProviderRuntime(), query, Mock(side_effect=AssertionError("no execution")),
        provider="unavailable", context_requirements={}, publish=delivery)


async def test_fragmented_json_streams_first_sentence_then_records_and_finishes_once(streaming_role):
    host = streaming_role
    text = '[EMO preset=normal dur=3s] 一😀。二"尾\\'
    raw = json.dumps({"action":None, "say":text}, ensure_ascii=True)
    calls = []

    async def query(messages, *, on_text=None):
        calls.append(messages)
        for char in raw:
            await on_text(char)
            if host.queue.qsize():
                assert host.history.call_count == 0
                assert not host.display.receipts
        assert host.queue.qsize() == 1  # Tail has not reached completion.
        return raw

    loop = role_loop(query, host.delivery)
    try:
        receipt = await loop.submit("hi", input_id="input", turn_id="reply")
        assert receipt["state"] == "no_action"
        items = [host.queue.get_nowait(), host.queue.get_nowait()]
        assert [item.text for item in items] == ["一😀。", '二"尾\\']
        assert [item.stream_tts for item in items] == [True, False]
        assert all(item.turn_id == "reply" for item in items)
        host.playback.mark_turn_last_sentence.assert_called_once_with(items[-1].sentence_id, "reply")
        host.history.assert_called_once_with("A", role="assistant",
            content=' 一😀。二"尾\\', turn_id="reply", message_id=ANY)
        host.narration.assert_not_called()
        assert len(host.display.receipts) == len(host.delivery.receipts) == len(calls) == 1
        assert host.delivery.receipts[0]["narration"]["accepted"] is True
        assert len([row for row in loop.history if row["source"] == "kurisu"]) == 1
        partials = [payload for method, payload in host.emitted
            if method == Method.CHAT_TOKEN]
        finals = [payload for method, payload in host.emitted
            if method == Method.CHAT_ROLE_MESSAGE]
        assert partials and all("EMO" not in payload["token"] for payload in partials)
        assert partials[-1]["token"] == '一😀。二"尾\\'
        assert finals == [{"message_id":"reply", "session_id":"A",
            "text":'一😀。二"尾\\'}]
    finally:
        await loop.close()


@pytest.mark.parametrize("legacy", [True, False])
async def test_old_query_or_say_first_shape_keeps_single_completed_delivery(streaming_role, legacy):
    host = streaming_role
    raw = json.dumps({"say":"完成。", "action":None})

    async def old_query(messages):
        assert not host.emitted
        return raw

    async def query(messages, *, on_text=None):
        await on_text(raw)
        assert not host.emitted and host.queue.empty()
        return raw

    loop = role_loop(old_query if legacy else query, host.delivery)
    try:
        await loop.submit("hi", turn_id="reply")
        assert host.queue.qsize() == len(host.emitted) == 1
        host.history.assert_called_once()
        host.narration.assert_awaited_once()
    finally:
        await loop.close()


async def test_pending_reply_never_streams_before_confirmation(streaming_role):
    host = streaming_role
    ready, confirmed = asyncio.Event(), asyncio.Event()

    async def query(messages, *, on_text=None):
        assert on_text is None
        ready.set()
        return json.dumps({"action":None, "say":"确认后回答。"})

    async def accept():
        await confirmed.wait()
        return SimpleNamespace(pending=False)

    loop = role_loop(query, host.delivery)
    task = asyncio.create_task(loop.submit("pending", turn_id="reply",
        turn_admission=SimpleNamespace(pending=True), acceptance_check=accept))
    try:
        await ready.wait()
        assert not host.emitted and host.queue.empty()
        host.history.assert_not_called()
        confirmed.set()
        await task
        assert len(host.emitted) == host.queue.qsize() == 1
        host.history.assert_called_once()
        host.narration.assert_awaited_once()
    finally:
        confirmed.set()
        await loop.close()


async def test_detailed_work_reply_waits_for_full_json_and_ingress_owner(streaming_role):
    host = streaming_role
    raw = json.dumps({"action":{"op":"work", "intent":"execute"}, "say":"开始。"})

    async def query(messages, *, on_text=None):
        await on_text(raw)
        assert not host.emitted and host.queue.empty()
        return raw

    loop = role_loop(query, host.delivery)
    try:
        receipt = await loop.submit("create a report", turn_id="reply")
        assert receipt["state"] == "work_required"
        assert not host.emitted and host.queue.empty()
        host.history.assert_not_called()
        host.narration.assert_not_awaited()
    finally:
        await loop.close()


@pytest.mark.parametrize("session_switch", [True, False])
async def test_interrupted_stream_cannot_publish_or_enqueue_tail(streaming_role, monkeypatch, session_switch):
    host = streaming_role

    async def query(messages, *, on_text=None):
        await on_text('{"action":null,"say":"前句。')
        assert host.queue.qsize() == 1
        if session_switch:
            monkeypatch.setattr("server.chat_role_delivery.sm.get_current_session_id", lambda:"B")
        else:
            monkeypatch.setattr(ChatHandler, "_turn_allows_visible_emit", AsyncMock(return_value=False))
        await on_text('旧尾句。"}')
        raise AssertionError("cancelled stream resumed")

    loop = role_loop(query, host.delivery)
    try:
        with pytest.raises(asyncio.CancelledError):
            await loop.submit("hi", turn_id="reply")
        assert host.queue.qsize() == 1
        assert host.emitted
        assert all(method == Method.CHAT_TOKEN
            and payload["token"] == "前句。" for method, payload in host.emitted)
        host.history.assert_not_called()
        host.playback.mark_turn_last_sentence.assert_not_called()
        assert not host.display.receipts
    finally:
        await loop.close()


@pytest.mark.parametrize("raw", [
    '{"action":null,"say":"暂时。","action":{"op":"send"}}',
    '{"action":null,"say":"暂时。","action":{"op":"work"},"action":null}',
])
async def test_streamed_null_cannot_be_replaced_by_a_duplicate_action(streaming_role, raw):
    from server.cooperative_provider_loop import LoopConflict, RoleDecisionUnavailable

    host = streaming_role

    async def query(messages, *, on_text=None):
        await on_text(raw)
        return raw

    loop = role_loop(query, host.delivery)
    try:
        with pytest.raises(RoleDecisionUnavailable,
                match="role decision unavailable") as raised:
            await loop.submit("hi", turn_id="reply")
        assert isinstance(raised.value.__cause__, LoopConflict)
        assert str(raised.value.__cause__) == "invalid coordination JSON"
        assert isinstance(raised.value.__cause__.__cause__, ValueError)
        assert not loop.children and not loop.receipts
        host.history.assert_not_called()
    finally:
        await loop.close()


async def test_streamed_identical_duplicate_null_settles_once_without_action(streaming_role):
    host = streaming_role
    say = "別の話ね、いいわよ。"
    raw = '{"action":null,"say":"' + say + '","action":null}'

    async def query(messages, *, on_text=None):
        for chunk in (raw[:19], raw[19:37], raw[37:]):
            await on_text(chunk)
        return raw

    loop = role_loop(query, host.delivery)
    try:
        receipt = await loop.submit("顺便聊点别的。", turn_id="duplicate-null")
        assert receipt["state"] == "no_action"
        assert not loop.children
        queued = []
        while not host.queue.empty():
            queued.append(host.queue.get_nowait())
        assert "".join(item.text for item in queued) == say
        assert all(item.turn_id == "duplicate-null" for item in queued)
        host.history.assert_called_once_with(
            "A", role="assistant", content=say, turn_id="duplicate-null", message_id=ANY)
        assert len([row for row in loop.history if row["source"] == "kurisu"]) == 1
        assert host.emitted[-1][1]["text"] == say
        assert len(host.display.receipts) == len(host.delivery.receipts) == 1
        host.narration.assert_not_called()
    finally:
        await loop.close()


async def test_streamed_null_ignores_extra_root_data_and_never_executes(streaming_role):
    host = streaming_role
    say = "別の話をしましょう。"
    raw = json.dumps({"action":None, "say":say, "type":"json_object",
        "nested":{"action":{"op":"send"}, "provider":"codex",
            "task":"this is data"}}, ensure_ascii=False)

    async def query(messages, *, on_text=None):
        for chunk in (raw[:23], raw[23:61], raw[61:]):
            await on_text(chunk)
        return raw

    loop = role_loop(query, host.delivery)
    try:
        receipt = await loop.submit("雑談しよう。", turn_id="null-extra-data")
        assert receipt["state"] == "no_action"
        assert not loop.children
        queued = []
        while not host.queue.empty():
            queued.append(host.queue.get_nowait())
        assert "".join(item.text for item in queued) == say
        assert all(item.turn_id == "null-extra-data" for item in queued)
        host.history.assert_called_once_with(
            "A", role="assistant", content=say, turn_id="null-extra-data", message_id=ANY)
        assert len([row for row in loop.history if row["source"] == "kurisu"]) == 1
        assert len(host.display.receipts) == len(host.delivery.receipts) == 1
    finally:
        await loop.close()


async def test_query_bridge_cancels_inflight_callback_and_closes_transport():
    import threading
    from server.cooperative_delivery import query_role_messages

    entered = asyncio.Event()
    closed = threading.Event()
    callback_cancelled = asyncio.Event()

    async def callback(_text):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            callback_cancelled.set()

    def query(messages, *, on_text):
        try:
            on_text("first")
            raise AssertionError("cancelled transport resumed")
        finally:
            closed.set()

    task = asyncio.create_task(query_role_messages(query, [], on_text=callback))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(callback_cancelled.wait(), 1)
    assert await asyncio.to_thread(closed.wait, 1)


def production_query(query):
    scope = {"json":json, "_llm_client_mod":SimpleNamespace(remote_llm_messages_query=query),
        "settings":SimpleNamespace(COOPERATIVE_CHAT_QUERY_MAX_TOKENS=900,
            COOPERATIVE_CHAT_QUERY_TIMEOUT_S=45)}
    path = Path(__file__).resolve().parents[1] / "server/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    definition = next(node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_query_cooperative_chat")
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(path), "exec"), scope)
    return scope["_query_cooperative_chat"]


@pytest.mark.parametrize("source_kind", ["user", "provider", "host_receipt"])
async def test_production_role_query_keeps_shared_language_contract_after_coordination(source_kind):
    from llm.prompts import get_language_lock_prompt, wrap_user_message_for_language_lock

    reply = '{"action":null,"say":"こんにちは。"}' if source_kind == "user" else "こんにちは。"
    query = Mock(return_value=reply)
    assembled = production_query(query)
    original = [{"role":"system", "content":"Persona\nCoordination JSON contract"},
        {"role":"user", "content":json.dumps({"source_kind":source_kind,
            "current":{"text":"你好"}}, ensure_ascii=False)}]

    assert await assembled(original) == reply

    query.assert_called_once()
    assert query.call_args.kwargs["json_output"] is (source_kind == "user")
    sent = query.call_args.args[0]
    assert sent[0]["content"].startswith(original[0]["content"])
    assert sent[0]["content"].endswith(get_language_lock_prompt().strip())
    assert sent[1]["content"] == (wrap_user_message_for_language_lock(original[1]["content"])
        if source_kind == "user" else original[1]["content"])
    assert json.loads(original[1]["content"])["current"]["text"] == "你好"
    assert original[0]["content"] == "Persona\nCoordination JSON contract"


async def test_production_inline_role_query_disables_json_mode_explicitly():
    query = Mock(return_value="ええ、作るわ。[DELEGATE op=work]")
    messages = [{"role":"system", "content":"Role contract"},
        {"role":"user", "content":json.dumps({"source_kind":"user"})}]
    assert await production_query(query)(messages, json_output=False) == query.return_value
    assert query.call_args.kwargs["json_output"] is False


async def test_production_query_preserves_real_typed_reference_requests():
    from server.reference_catalog import TypedReferenceCandidate
    from server.reference_clarification import build_reference_messages, resolve_typed_reference

    candidate = TypedReferenceCandidate(kind="work_item", entity_id="work-avatar",
        label="紹介ページ", scope="session_draft")
    utterance = "そのページを修正して。"
    history = [{"role":"assistant", "content":"紹介ページを作成したわ。"}]
    expected = build_reference_messages(utterance, [candidate], history=history)
    query = Mock(return_value=json.dumps({"references":[candidate.token]}))
    resolution = await resolve_typed_reference(utterance, [candidate], complete=True,
        query=production_query(query), history=history)

    assert resolution.status == "unique" and resolution.candidate == candidate
    query.assert_called_once()
    assert query.call_args.kwargs["json_output"] is True
    assert query.call_args.args[0][1:] == expected[1:]


@pytest.mark.parametrize("content", [
    "Resolve this reference.", "[]", "42", '{"references":[]}', '{"source_kind":"reference"}',
])
async def test_generic_queries_keep_json_default_without_role_wrapping(content):
    query = Mock(return_value='{"references":[]}')
    messages = [{"role":"system", "content":"Return a reference decision."},
        {"role":"user", "content":content}]
    assert await production_query(query)(messages) == '{"references":[]}'
    query.assert_called_once()
    assert query.call_args.kwargs["json_output"] is True
    assert query.call_args.args[0][-1] == messages[-1]
