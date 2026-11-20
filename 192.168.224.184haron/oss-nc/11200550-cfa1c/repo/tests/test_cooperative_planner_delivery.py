"""Coarse Work intent is visible without lending execution authority to speech."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from server.compound_control import CompoundControlPlan
from server.cooperative_delivery import CooperativeHostDelivery, ConversationSayDecoder
from server.cooperative_delivery import DelegateRoleDecoder
from test_auip_launch import _seed_app
from test_chat_role_delivery import streaming_role as streaming_role
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_work import planned
from test_cooperative_independent_auip_work import composition as composition


def configure(context, planner, *, work_texts, coarse="引き受けるわ。"):
    context.manager.work_planner = planner

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] == "user":
            action = {"op":"work"} if frame["current"]["text"] in work_texts else None
            return json.dumps({"action":action,
                "say":coarse if action else "話を聞いているわ。"}, ensure_ascii=False)
        state = str(frame["current"].get("state") or "")
        return ("今回はWorkを始めないわ。" if state == "no_action" else
            "開始できなかったわ。" if state in {"rejected", "unknown"} else "確認したわ。")

    context.manager.query = query


async def test_coarse_work_line_publishes_while_planner_waits_and_is_not_repeated(
        pending_host):
    context = pending_host
    text, clause = "这步你来，帮我另做个清单页吧。", "帮我另做个清单页吧。"
    started, release = asyncio.Event(), asyncio.Event()

    async def planner(ingress, *_args):
        assert not ingress.loop._foreground.locked()
        started.set()
        await release.wait()
        return planned(context.manager.provider, text, clause, "execute", one_off=True)

    configure(context, planner, work_texts={text})
    await context.handler.send_text(text, session_id=context.session_id,
        turn_id="early-success")
    await asyncio.wait_for(started.wait(), 3)
    assert context.host.adapter.calls == 0
    assert [row["text"] for row in context.publications
        if row["cause"] == "early-success"] == ["引き受けるわ。"]
    assert [row["display_text"] for row in context.spoken] == ["引き受けるわ。"]
    release.set()
    await asyncio.wait_for(context.handler._stream_task, 3)
    result = context.manager.ingresses[context.session_id].receipts["early-success"]
    assert result["state"] == "work_started"
    assert [row["text"] for row in context.publications
        if row["cause"] == "early-success"].count("引き受けるわ。") == 1
    context.host.adapter.release.set()
    await context.finish()


async def test_pending_coarse_work_stays_private_until_confirmation(pending_host):
    context = pending_host
    text = "帮我做个确认后的页面。"
    started, release = asyncio.Event(), asyncio.Event()

    async def planner(*_args):
        started.set()
        await release.wait()
        return planned(context.manager.provider, text, text, "execute", one_off=True)

    configure(context, planner, work_texts={text})
    assert await context.launcher.launch(text)
    turn_id = context.launcher._slot_turn_id
    await asyncio.sleep(0.05)
    assert not started.is_set() and not context.publications and not context.spoken
    assert await context.launcher.resolve(text)
    await asyncio.wait_for(started.wait(), 3)
    assert [row["text"] for row in context.publications] == ["引き受けるわ。"]
    release.set()
    await asyncio.wait_for(context.handler._stream_task, 3)
    assert context.manager.ingresses[context.session_id].receipts[turn_id]["state"] == "work_started"
    context.host.adapter.release.set()
    await context.finish()


async def test_planner_starts_while_shared_speech_is_still_blocked(pending_host):
    context = pending_host
    text = "Build while speech is pending."
    planner_started = asyncio.Event()
    planner_release = asyncio.Event()
    speech_started = asyncio.Event()
    speech_release = asyncio.Event()

    async def planner(*_args):
        planner_started.set()
        await planner_release.wait()
        return planned(context.manager.provider, text, text, "execute", one_off=True)

    async def speech(payload):
        context.spoken.append(payload)
        speech_started.set()
        await speech_release.wait()
        return {"status":"queued"}

    context.manager.publish_factory = lambda session:CooperativeHostDelivery(
        session_id=session, display=lambda event:context.publications.append(event) or True,
        narration_sink=speech)
    configure(context, planner, work_texts={text})
    await context.handler.send_text(text, session_id=context.session_id,
        turn_id="parallel-speech")
    await asyncio.wait_for(asyncio.gather(
        planner_started.wait(), speech_started.wait()), 3)
    assert not speech_release.is_set() and context.host.adapter.calls == 0
    speech_release.set()
    await asyncio.sleep(0)
    assert not context.handler._stream_task.done()
    planner_release.set()
    await asyncio.wait_for(context.handler._stream_task, 3)
    assert context.manager.ingresses[context.session_id].receipts[
        "parallel-speech"]["state"] == "work_started"
    context.host.adapter.release.set()
    await context.finish()


async def test_coarse_work_stream_uses_shared_role_feed_before_role_and_planner_finish(
        pending_host, streaming_role):
    context, shared = pending_host, streaming_role
    text = "Build through the shared role feed."
    role_partial = asyncio.Event()
    role_release = asyncio.Event()
    planner_started = asyncio.Event()
    planner_release = asyncio.Event()
    raw = json.dumps({"action":{"op":"work"},
        "say":"最初の文。次の文。"}, ensure_ascii=False)

    async def query(messages, *, on_text=None, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] != "user":
            return "確認したわ。"
        split = raw.index("次の文")
        await on_text(raw[:split])
        role_partial.set()
        await role_release.wait()
        await on_text(raw[split:])
        return raw

    async def planner(*_args):
        planner_started.set()
        await planner_release.wait()
        return planned(context.manager.provider, text, text, "execute", one_off=True)

    delivery = CooperativeHostDelivery(session_id=context.session_id,
        display=shared.display.publish, partial_display=shared.display.publish_partial,
        allows=shared.display.allows,
        role_stream_factory=lambda cause, gui_callback=None,
            auip_background_capture_release=None:
            shared.runtime.begin_role_text_stream(
                turn_id=cause, gui_callback=gui_callback,
                auip_background_capture_release=auip_background_capture_release),
        narration_sink=shared.narration, record_display=shared.history)
    context.manager.publish_factory = lambda _session:delivery
    context.manager.query = query
    context.manager.work_planner = planner
    context.host.adapter.release.clear()
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="coarse-stream")
        await asyncio.wait_for(role_partial.wait(), 3)
        assert shared.queue.qsize() == 1 and not planner_started.is_set()
        assert shared.queue._queue[0].text == "最初の文。"
        role_release.set()
        await asyncio.wait_for(planner_started.wait(), 3)
        assert shared.queue.qsize() == 2
        planner_release.set()
        await asyncio.wait_for(context.handler._stream_task, 3)
        assert context.manager.ingresses[context.session_id].receipts[
            "coarse-stream"]["state"] == "work_started"
        assert [item.text for item in shared.queue._queue] == ["最初の文。", "次の文。"]
        shared.history.assert_called_once()
        shared.narration.assert_not_awaited()
    finally:
        role_release.set()
        planner_release.set()
        context.host.adapter.release.set()
        await context.finish()


async def test_active_auip_coarse_work_streams_before_role_and_planner_finish(
        composition, streaming_role):
    context, state, text, work_source, _work_line, app_line, _publisher = composition
    shared = streaming_role
    state.relation = "subsumed"
    role_partial = asyncio.Event()
    role_release = asyncio.Event()
    planner_started = asyncio.Event()
    planner_release = asyncio.Event()
    raw = json.dumps({"action":{"op":"work"},
        "say":"最初の文。次の文。"}, ensure_ascii=False)

    async def query(messages, *, on_text=None, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] != "user":
            return app_line
        assert "auip_context" not in frame
        split = raw.index("次の文")
        if on_text is not None:
            await on_text(raw[:split])
        role_partial.set()
        await role_release.wait()
        if on_text is not None:
            await on_text(raw[split:])
        return raw

    async def planner(_ingress, _turn, receipt, _admission):
        assert receipt["auip_context"] == {"action":"step",
            "app_session_id":"current-app", "timing":"now",
            "instruction":""}
        planner_started.set()
        await planner_release.wait()
        return planned(context.manager.provider, text, work_source,
            "execute", one_off=True)

    delivery = CooperativeHostDelivery(session_id=context.session_id,
        display=shared.display.publish, partial_display=shared.display.publish_partial,
        allows=shared.display.allows,
        role_stream_factory=lambda cause, gui_callback=None,
            auip_background_capture_release=None:
            shared.runtime.begin_role_text_stream(
                turn_id=cause, gui_callback=gui_callback,
                auip_background_capture_release=auip_background_capture_release),
        narration_sink=shared.narration,
        record_display=shared.history)
    context.manager.publish_factory = lambda _session:delivery
    context.manager.query = query
    context.manager.work_planner = planner
    context.host.adapter.release.clear()
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="mixed-coarse-stream")
        await asyncio.wait_for(role_partial.wait(), 3)
        assert shared.queue.qsize() == 1
        assert shared.queue._queue[0].text == "最初の文。"
        assert not planner_started.is_set() and context.host.adapter.calls == 0
        role_release.set()
        await asyncio.wait_for(planner_started.wait(), 3)
        assert [item.text for item in shared.queue._queue][:2] == [
            "最初の文。", "次の文。"]
        planner_release.set()
        await asyncio.wait_for(context.handler._stream_task, 5)
        receipt = context.manager.ingresses[context.session_id].receipts[
            "mixed-coarse-stream"]
        assert receipt["state"] == "work_auip_independent"
        assert receipt["work"]["state"] == "work_started"
        assert receipt["auip"]["state"] == "auip_applied"
        history_texts = [call.args[2] if len(call.args) > 2 else
            call.kwargs.get("content") for call in shared.history.call_args_list]
        assert history_texts.count("最初の文。次の文。") == 1
        assert context.manager.auip_router.await_count == 1
    finally:
        role_release.set()
        planner_release.set()
        context.host.adapter.release.set()
        await context.finish()


async def test_new_turn_expires_visible_intent_without_executing_old_plan(pending_host):
    context = pending_host
    old, new = "Build the obsolete page.", "Just chatting now."
    started, release = asyncio.Event(), asyncio.Event()

    async def planner(*_args):
        started.set()
        await release.wait()
        return planned(context.manager.provider, old, old, "execute", one_off=True)

    configure(context, planner, work_texts={old})
    await context.handler.send_text(old, session_id=context.session_id, turn_id="visible-old")
    old_stream = context.handler._stream_task
    await asyncio.wait_for(started.wait(), 3)
    assert [row["text"] for row in context.publications] == ["引き受けるわ。"]
    await context.handler.send_text(new, session_id=context.session_id, turn_id="visible-new")
    await asyncio.wait_for(context.handler._stream_task, 3)
    release.set()
    await asyncio.gather(old_stream, return_exceptions=True)
    assert context.host.adapter.calls == 0 and context.host.work.list_work_items() == []
    assert context.manager.ingresses[context.session_id].receipts["visible-new"]["state"] == "no_action"


async def test_zero_plan_and_rejection_publish_final_fact_after_coarse_intent(pending_host):
    context = pending_host
    zero, rejected = "今天有点累。", "Build the unavailable page."
    plans = {zero:CompoundControlPlan(status="ok"),
        rejected:planned(context.manager.provider, rejected, rejected, "execute", one_off=True)}

    async def planner(_ingress, _turn_id, receipt, _admission):
        if receipt["text"] == rejected:
            context.manager.work_control = context.manager.work_executor = None
        return plans[receipt["text"]]

    configure(context, planner, work_texts={zero, rejected})
    await context.handler.send_text(zero, session_id=context.session_id, turn_id="zero-final")
    await asyncio.wait_for(context.handler._stream_task, 3)
    assert context.manager.ingresses[context.session_id].receipts["zero-final"]["state"] == "no_action"
    assert [row["text"] for row in context.publications
        if row["cause"] == "zero-final"] == ["引き受けるわ。"]
    await context.handler.send_text(rejected, session_id=context.session_id,
        turn_id="reject-final")
    await asyncio.wait_for(context.handler._stream_task, 3)
    assert context.manager.ingresses[context.session_id].receipts["reject-final"]["state"] == "rejected"
    assert [row["text"] for row in context.publications
        if row["cause"] == "reject-final"] == ["引き受けるわ。", "開始できなかったわ。"]
    assert context.host.adapter.calls == 0 and context.host.work.list_work_items() == []


async def test_planned_report_publishes_coarse_intent_then_canonical_fact_once(
        pending_host, tmp_path):
    context = pending_host
    item, attempt, _ = _seed_app(context.host.work, context.host.project,
        tmp_path / "drafts", title="Memo", turn_id="delivery-report", goal="Memo")
    context.host.work.update_attempt(attempt.attempt_id,
        metadata={"session_id":context.session_id})
    text = "Memo 做完了吗？"
    candidate = None

    async def planner(*_args):
        assert candidate is not None
        return planned(context.manager.provider, text, text, "report", candidate,
            _host_workspace_access="none")

    report = AsyncMock()

    async def report_side_effect(_source, _attrs, *, publish=None):
        await publish("Memo 已经完成。")
        return "canonical"

    report.side_effect = report_side_effect
    context.manager.configure_work(context.host.control, context.host.executor,
        report_request=report)
    configure(context, planner, work_texts={text})
    await context.manager._ingress_for(context.session_id)
    candidate = next(row for row in context.manager.work_candidates_for_context(
        context.session_id, "")[0] if row.entity_id == item.work_item_id)
    await context.handler.send_text(text, session_id=context.session_id,
        turn_id="report-final")
    await asyncio.wait_for(context.handler._stream_task, 3)
    assert [row["text"] for row in context.publications
        if row["cause"] == "report-final"] == ["引き受けるわ。", "Memo 已经完成。"]
    report.assert_awaited_once()
    assert context.host.adapter.calls == 0


def test_conversation_decoder_streams_only_null_or_exact_coarse_work_shape():
    default = ConversationSayDecoder()
    assert default.feed('{"action":{"op":"work"},"say":"引き受けるわ。"}') == ""
    assert not default.started
    decoder = ConversationSayDecoder(allow_work_proposal=True)
    assert decoder.feed('{"action":{"op":"work"},"say":"引き') == "引き"
    assert decoder.feed('受けるわ。"}') == "受けるわ。"
    decoder.finish({"action":{"op":"work"}, "say":"引き受けるわ。"})
    detailed = ConversationSayDecoder()
    assert detailed.feed(
        '{"action":{"op":"work","intent":"execute"},"say":"始めるわ。"}') == ""
    assert not detailed.started


@pytest.mark.parametrize("tag, action", [
    ('[DELEGATE op=work]', {"op":"work"}),
    ('[DELEGATE op=send_to target="先ほどの調査"]',
        {"op":"send_to", "target":"先ほどの調査"}),
    ('[DELEGATE op=interrupt target="前の作業"]',
        {"op":"interrupt", "target":"前の作業"}),
])
def test_inline_role_reuses_parser_and_commits_only_complete_handoff(tag, action):
    decoder = DelegateRoleDecoder()
    opener = "ええ、[EMO thinking] 確認するわ。"
    assert "".join(decoder.feed(c) for c in opener) == opener
    for c in tag[:-1]:
        assert decoder.feed(c) == ""
        assert not decoder.handoff
    assert decoder.feed(tag[-1] + "生成を待つ必要のない後半。") == ""
    assert decoder.handoff
    decoder.finish({"say":opener, "action":action})


@pytest.mark.parametrize("raw", [
    "調べるわ。[DELEGATE op=work", "調べるわ。[DELEGATE]",
    "調べるわ。[PARAM id=MouthOpen value=1]",
])
def test_inline_role_rejects_incomplete_or_unsupported_control(raw):
    decoder = DelegateRoleDecoder()
    with pytest.raises(ValueError):
        decoder.feed(raw)
        decoder.value()


async def test_inline_delegate_starts_work_without_waiting_for_model_tail(
        pending_host, streaming_role):
    context, shared = pending_host, streaming_role
    text = "帮我做一个简单的清单页吧。"
    prefix_ready, send_tag = asyncio.Event(), asyncio.Event()
    query_closed, planner_started = asyncio.Event(), asyncio.Event()

    async def query(messages, *, on_text=None, **_kwargs):
        if json.loads(messages[-1]["content"])["source_kind"] != "user":
            return "確認したわ。"
        try:
            await on_text("ええ、[EMO thinking] 作ってみるわ。")
            prefix_ready.set()
            await send_tag.wait()
            await on_text("[DELEGATE op=work]")
            pytest.fail("the completed DELEGATE must stop reading the model tail")
        finally:
            query_closed.set()

    async def planner(*_args):
        assert query_closed.is_set()
        planner_started.set()
        return planned(context.manager.provider, text, text, "execute", one_off=True)

    delivery = CooperativeHostDelivery(session_id=context.session_id,
        display=shared.display.publish, partial_display=shared.display.publish_partial,
        allows=shared.display.allows,
        role_stream_factory=lambda cause, **kwargs:shared.runtime.begin_role_text_stream(
            turn_id=cause, **kwargs),
        narration_sink=shared.narration, record_display=shared.history)
    context.manager.publish_factory = lambda _session:delivery
    context.manager.query, context.manager.work_planner = query, planner
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="inline-delegate")
        await asyncio.wait_for(prefix_ready.wait(), 3)
        assert shared.queue.qsize() >= 1
        assert not planner_started.is_set() and context.host.adapter.calls == 0
        send_tag.set()
        await asyncio.wait_for(context.handler._stream_task, 3)
        assert planner_started.is_set()
        assert context.manager.ingresses[context.session_id].receipts[
            "inline-delegate"]["state"] == "work_started"
        shared.history.assert_called_once()
        assert [item.text for item in shared.queue._queue] == ["ええ、", " 作ってみるわ。".strip()]
    finally:
        send_tag.set()
        context.host.adapter.release.set()
        await context.finish()


@pytest.mark.parametrize(("raw", "value", "allow_work", "say"), [
    # Recorded turn ca94d3bf-ac30-46a8-9aa8-16ac4169f811 repeated
    # its identical action member after the already-streamed say.
    ('{"action":null,"say":"別の話ね、いいわよ。","action":null}',
        {"action":None, "say":"別の話ね、いいわよ。"}, False, "別の話ね、いいわよ。"),
    ('{"action":{"op":"work"},"say":"進めるわ。",'
        '"action":{"op":"work"}}',
        {"action":{"op":"work"}, "say":"進めるわ。"}, True, "進めるわ。"),
    ('{"action":null,"say":"話すわ。","say":"話すわ。"}',
        {"action":None, "say":"話すわ。"}, False, "話すわ。"),
    ('{"action":null,"say":"話すわ。","type":"json_object",'
        '"nested":{"action":{"op":"work"},"task":"do not run"}}',
        {"action":None, "say":"話すわ。"}, False, "話すわ。"),
])
def test_conversation_decoder_accepts_only_consistent_duplicate_members(
        raw, value, allow_work, say):
    decoder = ConversationSayDecoder(allow_work_proposal=allow_work)
    cuts = (len(raw) // 3, len(raw) * 2 // 3)
    visible = "".join((decoder.feed(raw[:cuts[0]]),
        decoder.feed(raw[cuts[0]:cuts[1]]), decoder.feed(raw[cuts[1]:])))
    assert visible == say
    decoder.finish(value)


@pytest.mark.parametrize("raw", [
    '{"action":null,"say":"話すわ。","action":{"op":"work"}}',
    '{"action":null,"say":"話すわ。","action":{"op":"work"},"action":null}',
    '{"action":null,"say":"話すわ。","say":"違うわ。"}',
    '{"action":null,"say":"話すわ。","say":"違うわ。","say":"話すわ。"}',
    '{"action":null,"say":"話すわ。"} trailing',
    '{"action":null,"say":"話すわ。"',
    '{"action":null,"say":"話すわ。","action":}',
])
def test_conversation_decoder_rejects_conflicts_unknowns_and_incomplete_json(raw):
    decoder = ConversationSayDecoder(allow_work_proposal=True)
    decoder.feed(raw)
    with pytest.raises(ValueError, match="changed or was incomplete"):
        decoder.finish({"action":None, "say":"話すわ。"})


@pytest.mark.parametrize("value", [
    {"action":None, "say":"変えたわ。"},
    {"action":{"op":"work"}, "say":"話すわ。"},
    {"action":None, "say":"話すわ。", "unknown":None},
])
def test_conversation_decoder_rejects_normalized_value_that_changed_after_stream(value):
    decoder = ConversationSayDecoder(allow_work_proposal=True)
    assert decoder.feed('{"action":null,"say":"話すわ。"}') == "話すわ。"
    with pytest.raises(ValueError, match="changed or was incomplete"):
        decoder.finish(value)
