"""Focused app effects and independent Work retain their own admission owners."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import session_manager as sm
from server.chat_role_delivery import ChatRoleDelivery
from server.compound_control import (
    CompoundControlOperation,
    CompoundControlPlan,
    SourceClause,
)
from server.control_decision import CONTROL_REFERENCE_CANDIDATES_ATTR
from server.cooperative_chat_ingress import _suppress_ambiguous_retracts
from server.cooperative_delivery import CooperativeHostDelivery
from server.auip_control_decision import AuipControlDecisionResolver
from server.auip_b2 import AuipB2Coordinator
from server.event_bus import bus
from server.protocol import Method
from test_auip_control_decision import _Runtime, _Catalog
from test_auip_b2 import _runtime
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_work import planned


@pytest.fixture
def composition(pending_host):
    context = pending_host
    state = SimpleNamespace(relation="independent", action="step", app_ok=True, work_expected=True,
        slow=False, app_started=asyncio.Event(), release_app=asyncio.Event(), frames=[])
    state.release_app.set()
    app_line, work_line = "盤面を一手進めるわ。", "メモを作成するわ。"
    work_source = "再帮我做个便签页吧。"
    text = "这一步你来，" + work_source
    publisher = ChatRoleDelivery()
    context.manager.publish_factory = lambda session:CooperativeHostDelivery(
        session_id=session, display=publisher.publish, record_display=sm.append_session_message,
        narration_sink=lambda payload:context.spoken.append(payload) or {"status":"queued"})

    def capture(**kwargs):
        if kwargs["user_text"] == "谢啦":
            return None
        return SimpleNamespace(status="ok", action=state.action, timing="now",
            app_session_id="current-app", work_relation=state.relation,
            read_facets=("state",) if state.action == "none" else (),
            control_attrs=lambda:{"action":state.action, "target":"current-app"})

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        state.frames.append(frame)
        if frame["source_kind"] == "user":
            if frame["current"]["text"] == "谢啦":
                return json.dumps({"action":None, "say":"どういたしまして。"})
            if "auip_context" in frame:
                assert frame["auip_context"]["work_relation"] == "independent"
            if not state.work_expected:
                return json.dumps({"action":None, "say":""})
            return json.dumps({"action":{"op":"work", "intent":"execute", "source":work_source},
                "say":("以前のスコアは0ね。" + work_line if state.action == "none" else work_line)})
        if frame["current"].get("state") == "work_auip_independent":
            assert frame["current"]["app_read_facts"] == "score=1"
            work_state = frame["current"]["work"]["state"]
            return (work_line if work_state == "work_started" else
                "" if work_state == "no_action" else "作業は開始できなかったわ。") + "スコアは1点よ。"
        if frame["current"].get("state") == "auip_read":
            return "スコアは1点よ。"
        return app_line if frame["current"].get("state", "").startswith("auip_") else "作業は開始できなかったわ。"

    async def route(*_args, **_kwargs):
        state.app_started.set()
        await state.release_app.wait()
        return {"ok":state.app_ok, "pending":state.app_ok}

    context.manager.query = query
    context.manager.auip_decider = SimpleNamespace(capture=capture,
        render_read_only_answer=lambda *_args, **_kwargs:"score=1")
    context.manager.auip_router = AsyncMock(side_effect=route)
    context.host.adapter.release.clear()
    return context, state, text, work_source, work_line, app_line, publisher


@pytest.mark.parametrize("failure", ["none", "work_refused", "work_unknown", "app_refused", "presentation"])
async def test_independent_effects_use_exact_work_clause_and_one_real_publication(composition, failure):
    context, state, text, source, work_line, app_line, publisher = composition
    if failure == "work_refused":
        context.manager.work_control = context.manager.work_executor = None
    elif failure == "work_unknown":
        context.manager.work_executor.dispatch = AsyncMock(side_effect=RuntimeError("handoff uncertain"))
    elif failure == "app_refused":
        state.app_ok = False
    elif failure == "presentation":
        publisher.publish = AsyncMock(side_effect=RuntimeError("display unavailable"))
    try:
        await context.handler.send_text(text, session_id=context.session_id, turn_id="compound")
        await context.handler._stream_task
        ingress = context.manager.ingresses[context.session_id]
        receipt = ingress.receipts["compound"]
        assert receipt["state"] == "work_auip_independent"
        assert receipt["work"]["state"] == ("rejected" if failure == "work_refused" else
            "unknown" if failure == "work_unknown" else "work_started")
        assert receipt["auip"]["state"] == ("auip_rejected" if failure == "app_refused" else "auip_applied")
        context.manager.auip_router.assert_awaited_once()
        assert context.host.adapter.calls == (0 if failure in {"work_refused", "work_unknown"} else 1)
        if failure not in {"work_refused", "work_unknown"}:
            request = context.host.adapter.requests[0]["request"]
            assert request.task == source
            assert request.metadata["source_user_text"] == text
            assert len(context.host.work.list_work_items()) == 1
        assert [row["text"] for row in ingress.loop.history if row["source"] == "user"] == [text]
        history, _ = sm._read_session_history(context.session_id)
        assert [row["content"] for row in history.dialog if row["role"] == "user"] == [text]
        if failure == "presentation":
            assert not context.spoken
        else:
            assert len(publisher.receipts) == len(context.spoken) == 1
            assert publisher.receipts[0]["message_id"] == "compound"
            if failure == "none":
                assert context.spoken[0]["display_text"] == work_line + "\n" + app_line
        replay = await context.handler.send_text(text, session_id=context.session_id, turn_id="compound")
        assert replay["status"] == "replayed"
        context.manager.auip_router.assert_awaited_once()
        if failure not in {"work_refused", "work_unknown"}:
            context.manager._stop_active_work = AsyncMock(return_value={"state":"stopped"})
            await context.manager.abort_turn("compound", context.session_id)
            assert context.manager._stop_active_work.call_args.args[2]["run_id"] == receipt["work"]["run_id"]
    finally:
        context.host.adapter.release.set()
        await context.finish()


async def test_slow_app_does_not_hold_work_acceptance_or_later_chat(composition):
    context, state, text, source, _, _, publisher = composition
    state.release_app.clear()
    try:
        await context.handler.send_text(text, session_id=context.session_id, turn_id="slow-compound")
        original_stream = context.handler._stream_task
        await asyncio.wait_for(state.app_started.wait(), 2)
        await asyncio.wait_for(context.host.adapter.started.wait(), 2)
        ingress = context.manager.ingresses[context.session_id]
        work = dict(ingress.receipts["slow-compound"])
        assert work["state"] == "work_started"
        assert not ingress.loop._foreground.locked()
        assert not publisher.receipts
        await context.handler.send_text("谢啦", session_id=context.session_id, turn_id="later")
        await asyncio.wait_for(context.handler._stream_task, 2)
        assert any(row["cause"] == "later" for row in ingress.loop.history if row["source"] == "kurisu")
        # The old presentation may be discarded, but accepted domain work survives.
        state.release_app.set()
        await asyncio.gather(original_stream, return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(*tuple(ingress.loop._monitors)), 2)
        result = ingress.receipts["slow-compound"]
        assert result["work"]["run_id"] == work["run_id"]
        assert result["auip"]["outcome"]["ok"] is True
        assert not any(row["message_id"] == "slow-compound" for row in publisher.receipts)
        assert context.host.adapter.calls == context.manager.auip_router.await_count == 1
        assert context.host.runtime.get_run(work["run_id"]).status == "running"
    finally:
        state.release_app.set()
        context.host.adapter.release.set()
        await context.finish()


@pytest.mark.parametrize(("action", "text", "raw"), [
    ("step", "这步你来，便签先别做了。",
        '{"action":{"op":"work","intent":"amend","target":"便签页"},'
        '"say":"了解、このステップは私がやるわ。便箋の作業は一旦止めておく。'
        '[EMO preset=normal dur=3s] 今から2048の操作を進めるわね。"}'),
    ("none", "现在多少分了？便签先别做了。",
        '{"action":{"op":"report","target":"2048のスコア"},'
        '"say":"スコアの確認ね。[EMO preset=normal dur=3s] 今の状態を台帳から読み取るわ。'
        '便箋の作業は止めておく。少し待ってて。"}'),
    ("step", "这步你来。",
        '{"action":null,"say":"この一手は任せて。"}]'),
    ("none", "现在多少分了？",
        '{"action":null,"say":"今の状態を答えるわ。"}]'),
])
async def test_recorded_work_parse_failure_preserves_independent_app(composition, action, text, raw):
    """Replay the two 2026-09-08 oral probe errors through actual ingress."""
    context, state, _, _, _, app_line, publisher = composition
    state.action = action
    original_query = context.manager.query

    async def query(messages, **kwargs):
        if json.loads(messages[-1]["content"])["source_kind"] == "user":
            return raw
        return await original_query(messages, **kwargs)

    context.manager.query = query
    try:
        await context.handler.send_text(text, session_id=context.session_id, turn_id="parse-failure")
        await context.handler._stream_task
        ingress = context.manager.ingresses[context.session_id]
        receipt = ingress.receipts["parse-failure"]
        assert receipt["state"] == "work_auip_independent"
        assert receipt["work"] == {"state":"not_accepted",
            "reason":"role_decision_unavailable", "input_id":"parse-failure"}
        assert receipt["auip"]["state"] == ("auip_read" if action == "none" else "auip_applied")
        assert context.host.adapter.calls == 0
        assert context.host.work.list_work_items() == []
        assert len(publisher.receipts) == len(context.spoken) == 1
        spoken = context.spoken[0]["display_text"]
        assert "止めておく" not in spoken
        assert ("スコアは1点よ。" if action == "none" else app_line) in spoken
        expression_frames = [frame["current"] for frame in state.frames
            if frame["source_kind"] == "host_receipt"]
        assert not any(frame.get("state") == "unknown" for frame in expression_frames)
        assert "work_dispatch_failed" not in json.dumps(expression_frames)
        if action == "none":
            combined = next(frame for frame in expression_frames
                if frame.get("state") == "work_auip_independent")
            assert combined["work"]["reason"] == "role_decision_unavailable"
        else:
            assert any(frame.get("state") == "not_accepted"
                and frame.get("reason") == "role_decision_unavailable"
                for frame in expression_frames)
        assert [row["text"] for row in ingress.loop.history if row["source"] == "user"] == [text]
        replay = await context.handler.send_text(text, session_id=context.session_id, turn_id="parse-failure")
        assert replay["status"] == "replayed"
        assert context.manager.auip_router.await_count == int(action == "step")
    finally:
        context.host.adapter.release.set()
        await context.finish()


async def test_focused_auip_role_acknowledgement_does_not_become_a_second_action(composition):
    """Replay the 2026-09-10 physical note action at the failed role boundary."""
    context, state, *_rest, app_line, publisher = composition
    text = "记一下：周五买牛奶。"
    role_line = "金曜日に牛乳を買う、とメモしておくわ。"
    original_query = context.manager.query

    async def query(messages, **kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] == "user" and frame["current"]["text"] == text:
            state.frames.append(frame)
            return json.dumps({"action":{"op":"auip"}, "say":role_line}, ensure_ascii=False)
        return await original_query(messages, **kwargs)

    context.manager.query = query
    state.work_expected = False
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="focused-auip-ack")
        await context.handler._stream_task
        ingress = context.manager.ingresses[context.session_id]
        receipt = ingress.receipts["focused-auip-ack"]
        assert receipt["state"] == "work_auip_independent"
        assert receipt["work"]["state"] == "no_action"
        assert receipt["auip"]["state"] == "auip_applied"
        context.manager.auip_router.assert_awaited_once()
        assert context.host.adapter.calls == 0
        assert context.host.work.list_work_items() == []
        host_frames = [frame for frame in state.frames
            if frame["source_kind"] == "host_receipt"]
        assert len(host_frames) == 1
        assert host_frames[0]["current"]["state"].startswith("auip_")
        assert not any(row.get("state") == "not_accepted" for row in ingress.loop.history)
        assert len(publisher.receipts) == len(context.spoken) == 1
        assert context.spoken[0]["display_text"] == role_line + "\n" + app_line
    finally:
        context.host.adapter.release.set()
        await context.finish()


@pytest.mark.parametrize("supersede", [False, True])
async def test_independent_app_does_not_wait_for_work_interpretation(composition, supersede):
    context, state, text, _, _, _, publisher = composition
    queried, release_query = asyncio.Event(), asyncio.Event()
    original_query = context.manager.query

    async def query(messages, **kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] == "user" and frame["current"]["text"] == text:
            queried.set()
            await release_query.wait()
        return await original_query(messages, **kwargs)

    context.manager.query = query
    try:
        await context.handler.send_text(text, session_id=context.session_id, turn_id="slow-work")
        original_stream = context.handler._stream_task
        await asyncio.wait_for(queried.wait(), 2)
        await asyncio.wait_for(state.app_started.wait(), 2)
        assert context.host.adapter.calls == 0
        if supersede:
            await context.handler.send_text("谢啦", session_id=context.session_id, turn_id="later-work")
            await asyncio.wait_for(context.handler._stream_task, 2)
        release_query.set()
        await asyncio.gather(original_stream, return_exceptions=True)
        ingress = context.manager.ingresses[context.session_id]
        await asyncio.wait_for(asyncio.gather(*tuple(ingress.loop._monitors)), 2)
        receipt = ingress.receipts["slow-work"]
        assert receipt["auip"]["state"] == "auip_applied"
        assert context.host.adapter.calls == int(not supersede)
        assert context.manager.auip_router.await_count == 1
        assert any(row["message_id"] == "slow-work" for row in publisher.receipts) is not supersede
    finally:
        release_query.set()
        context.host.adapter.release.set()
        await context.finish()


async def test_professional_focused_control_and_role_start_without_gating_each_other(
        composition):
    context, state, text, source, work_line, app_line, publisher = composition
    control_started, release_control = asyncio.Event(), asyncio.Event()
    role_started, first_sentence, release_role = (
        asyncio.Event(), asyncio.Event(), asyncio.Event())
    role_line = "アプリを進めて、メモも作るわ。"
    planner_receipts = []
    original_query = context.manager.query

    async def capture():
        control_started.set()
        await release_control.wait()
        return SimpleNamespace(status="ok", action="step", timing="now",
            app_session_id="current-app", work_relation="independent",
            read_facets=(), instruction="这一步你来",
            control_attrs=lambda:{"action":"step", "target":"current-app"})

    context.manager.auip_decider = SimpleNamespace(
        capture=lambda **_kwargs:capture(),
        render_read_only_answer=lambda *_args, **_kwargs:"")

    async def query(messages, *, on_text=None, **kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] != "user":
            return await original_query(messages, **kwargs)
        state.frames.append(frame)
        role_started.set()
        raw = json.dumps({"action":{"op":"work"}, "say":role_line},
            ensure_ascii=False, separators=(",", ":"))
        if on_text is not None:
            split = raw.index(role_line) + len(role_line)
            await on_text(raw[:split])
            first_sentence.set()
            await release_role.wait()
            await on_text(raw[split:])
        else:
            first_sentence.set()
            await release_role.wait()
        return raw

    context.manager.query = query
    def plan_work(_ingress, _turn, receipt, _admission):
        planner_receipts.append(dict(receipt))
        return planned(context.manager.provider, receipt["text"], source,
            "execute", one_off=True)

    context.manager.work_planner = plan_work
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="parallel-focused-control")
        stream = context.handler._stream_task
        await asyncio.wait_for(control_started.wait(), 2)
        await asyncio.wait_for(role_started.wait(), 2)
        await asyncio.wait_for(first_sentence.wait(), 2)
        assert not release_control.is_set() and not stream.done()
        assert publisher.receipts == []

        release_control.set()
        await asyncio.wait_for(state.app_started.wait(), 2)
        assert not release_role.is_set() and context.host.adapter.calls == 0

        release_role.set()
        await asyncio.wait_for(stream, 4)
        ingress = context.manager.ingresses[context.session_id]
        receipt = ingress.receipts["parallel-focused-control"]
        assert receipt["work"]["state"] == "work_started"
        assert receipt["auip"]["state"] == "auip_applied"
        assert context.host.adapter.calls == context.manager.auip_router.await_count == 1
        user_frames = [frame for frame in state.frames
            if frame["source_kind"] == "user"
            and frame["current"]["text"] == text]
        assert len(user_frames) == 1
        assert "auip_context" not in user_frames[0]
        assert planner_receipts[0]["auip_context"] == {
            "action":"step", "app_session_id":"current-app", "timing":"now",
            "instruction":"这一步你来"}
        request = context.host.adapter.requests[0]["request"]
        assert request.task == source
        assert request.metadata["source_user_text"] == text
        assert len(publisher.receipts) == len(context.spoken) == 2
        assert [row["display_text"] for row in context.spoken] == [
            role_line, app_line]
    finally:
        release_control.set()
        release_role.set()
        context.host.adapter.release.set()
        await context.finish()


@pytest.mark.parametrize("ending", ["superseded", "role_cancelled"])
async def test_unaccepted_deferred_focused_turn_drains_control_and_role(
        composition, ending):
    context, state, text, *_ = composition
    control_started, control_cancelled = asyncio.Event(), asyncio.Event()
    role_started, role_cancelled = asyncio.Event(), asyncio.Event()
    release_role = asyncio.Event()
    original_query = context.manager.query

    async def capture():
        control_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            control_cancelled.set()
            raise

    def capture_turn(**kwargs):
        return None if kwargs["user_text"] == "谢啦" else capture()

    context.manager.auip_decider = SimpleNamespace(capture=capture_turn,
        render_read_only_answer=lambda *_args, **_kwargs:"")

    async def query(messages, **kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] != "user" or frame["current"]["text"] != text:
            return await original_query(messages, **kwargs)
        role_started.set()
        if ending == "role_cancelled":
            raise asyncio.CancelledError()
        try:
            await release_role.wait()
        except asyncio.CancelledError:
            role_cancelled.set()
            raise
        return json.dumps({"action":None, "say":"遅い応答"}, ensure_ascii=False)

    context.manager.query = query
    context.manager.work_planner = lambda *_args, **_kwargs: pytest.fail(
        "an unaccepted role must not reach the Work planner")
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="cancelled-focused")
        old_stream = context.handler._stream_task
        await asyncio.wait_for(control_started.wait(), 2)
        await asyncio.wait_for(role_started.wait(), 2)
        if ending == "superseded":
            await context.handler.send_text("谢啦", session_id=context.session_id,
                turn_id="later-focused")
            await asyncio.wait_for(context.handler._stream_task, 3)
            await asyncio.gather(old_stream, return_exceptions=True)
            await asyncio.wait_for(role_cancelled.wait(), 2)
        else:
            await asyncio.wait_for(asyncio.gather(old_stream, return_exceptions=True), 3)
        await asyncio.wait_for(control_cancelled.wait(), 2)
        await asyncio.sleep(0)

        ingress = context.manager.ingresses[context.session_id]
        assert "cancelled-focused" not in ingress.receipts
        assert not state.app_started.is_set()
        assert context.host.adapter.calls == 0
        assert context.host.work.list_work_items() == []
        source = context.manager.ledger.find_admission(
            "chat:" + context.session_id, "cancelled-focused")
        assert source is not None and source["plan_id"] is None
        assert not [task for task in ingress.loop._monitors
            if task.get_name() in {"role:cancelled-focused",
                "auip-focused:cancelled-focused"}]
    finally:
        release_role.set()
        context.host.adapter.release.set()
        await context.finish()


async def test_failed_role_cannot_cancel_pending_independent_app_authority(composition):
    context, state, text, *_ = composition
    role_started, release_control = asyncio.Event(), asyncio.Event()
    capture = context.manager.auip_decider.capture
    query = context.manager.query

    async def delayed_control(**kwargs):
        await release_control.wait()
        return capture(**kwargs)

    async def failed_role(messages, **kwargs):
        if json.loads(messages[-1]["content"])["source_kind"] == "user":
            role_started.set()
            raise RuntimeError("role transport unavailable")
        return await query(messages, **kwargs)

    context.manager.auip_decider.capture = delayed_control
    context.manager.query = failed_role
    context.manager.work_planner = lambda *_args: pytest.fail("no valid role Work proposal")
    turn = "focused-role-failure"
    try:
        await context.handler.send_text(text, session_id=context.session_id, turn_id=turn)
        await asyncio.wait_for(role_started.wait(), 3)
        ingress = context.manager.ingresses[context.session_id]
        input_task = ingress.loop._inputs[turn][1]
        await asyncio.wait_for(asyncio.gather(input_task, return_exceptions=True), 3)
        assert not state.app_started.is_set()
        release_control.set()
        await asyncio.wait_for(context.handler._stream_task, 3)
        receipt = ingress.receipts[turn]
        assert receipt["work"]["state"] == "not_accepted"
        assert receipt["work"]["reason"] == "role_decision_unavailable"
        assert receipt["auip"]["state"] == "auip_applied"
        context.manager.auip_router.assert_awaited_once()
        assert context.host.adapter.calls == 0
        assert context.host.work.list_work_items() == []
    finally:
        release_control.set()
        context.host.adapter.release.set()
        await context.finish()


@pytest.mark.parametrize("role_action", [
    {"op":"send"},
    {"op":"delegate", "provider":"loop-test"},
])
async def test_deferred_focused_owner_preserves_only_work_proposals(
        composition, role_action):
    context, state, text, *_unused, app_line, publisher = composition
    role_line = "アプリ側を進めるわ。"
    original_query = context.manager.query

    async def query(messages, **kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] == "user":
            state.frames.append(frame)
            return json.dumps({"action":role_action, "say":role_line},
                ensure_ascii=False)
        return await original_query(messages, **kwargs)

    context.manager.query = query
    context.manager.work_planner = lambda *_args, **_kwargs: pytest.fail(
        "an app-owned role proposal must not reach the Work planner")
    state.work_expected = False
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="focused-nonwork-proposal")
        await asyncio.wait_for(context.handler._stream_task, 4)
        ingress = context.manager.ingresses[context.session_id]
        receipt = ingress.receipts["focused-nonwork-proposal"]
        assert receipt["work"]["state"] == "no_action"
        assert receipt["auip"]["state"] == "auip_applied"
        assert context.host.adapter.calls == 0
        assert context.host.work.list_work_items() == []
        admission = context.manager.ledger.find_admission(
            "chat:" + context.session_id, "focused-nonwork-proposal")
        assert admission is not None and admission["plan_id"]
        assert len(publisher.receipts) == len(context.spoken) == 1
        assert context.spoken[0]["display_text"] == role_line + "\n" + app_line
    finally:
        context.host.adapter.release.set()
        await context.finish()


async def test_deferred_focused_non_owner_keeps_ordinary_chat_effect_free(
        composition):
    context, state, *_unused, publisher = composition
    text = "今日は少し疲れた。"
    role_line = "少し休みながら話しましょう。"
    context.manager.auip_decider = SimpleNamespace(capture=lambda **_kwargs:
        SimpleNamespace(status="ok", action="none", timing="now",
            app_session_id="current-app", work_relation="subsumed",
            read_facets=(), instruction=""),
        render_read_only_answer=lambda *_args, **_kwargs:"")

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        state.frames.append(frame)
        return json.dumps({"action":None, "say":role_line}, ensure_ascii=False)

    context.manager.query = query
    context.manager.work_planner = lambda *_args, **_kwargs: pytest.fail(
        "ordinary chat must not reach the Work planner")
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="focused-ordinary-chat")
        await asyncio.wait_for(context.handler._stream_task, 3)
        ingress = context.manager.ingresses[context.session_id]
        assert ingress.receipts["focused-ordinary-chat"]["state"] == "no_action"
        assert context.manager.auip_router.await_count == 0
        assert context.host.adapter.calls == 0
        assert context.host.work.list_work_items() == []
        admission = context.manager.ledger.find_admission(
            "chat:" + context.session_id, "focused-ordinary-chat")
        assert admission is not None and admission["plan_id"]
        assert len(publisher.receipts) == len(context.spoken) == 1
        assert context.spoken[0]["display_text"] == role_line
    finally:
        context.host.adapter.release.set()
        await context.finish()


@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("parse_failure", [False, True])
async def test_pending_independent_domains_share_voice_confirmation(composition, confirm, parse_failure):
    context, state, text, *_ = composition
    queried = asyncio.Event()
    original_query = context.manager.query

    async def query(messages, **kwargs):
        if json.loads(messages[-1]["content"])["source_kind"] == "user":
            queried.set()
            if parse_failure:
                return "malformed Work JSON"
        return await original_query(messages, **kwargs)

    context.manager.query = query
    try:
        assert await context.launcher.launch(text)
        turn_id = context.launcher._slot_turn_id
        await asyncio.wait_for(queried.wait(), 2)
        assert context.host.adapter.calls == 0
        context.manager.auip_router.assert_not_awaited()
        assert not context.spoken
        if confirm:
            assert await context.launcher.resolve(text)
        else:
            await context.launcher.abandon("test_discard")
        await asyncio.gather(context.handler._stream_task, return_exceptions=True)
        ingress = context.manager.ingresses[context.session_id]
        await asyncio.wait_for(asyncio.gather(*tuple(ingress.loop._monitors)), 2)
        assert context.host.adapter.calls == int(confirm and not parse_failure)
        assert context.manager.auip_router.await_count == int(confirm)
        assert len(context.spoken) == int(confirm)
        if confirm:
            assert ingress.receipts[turn_id]["work"]["state"] == (
                "not_accepted" if parse_failure else "work_started")
            if parse_failure:
                assert ingress.receipts[turn_id]["work"]["reason"] == (
                    "role_decision_unavailable")
            assert [row["text"] for row in ingress.loop.history if row["source"] == "user"] == [text]
            history, _ = sm._read_session_history(context.session_id)
            assert [row["content"] for row in history.dialog if row["role"] == "user"] == [text]
    finally:
        context.host.adapter.release.set()
        await context.finish()


async def test_subsumed_app_read_preserves_running_work_and_does_not_create_more(composition):
    context, state, text, *_ = composition
    try:
        await context.handler.send_text(text, session_id=context.session_id, turn_id="start")
        await context.handler._stream_task
        ingress = context.manager.ingresses[context.session_id]
        work = ingress.receipts["start"]["work"]
        binding = (ingress.loop.bound_context_id, ingress.loop._binding.token)
        state.action, state.relation = "none", "subsumed"
        before = len([frame for frame in state.frames if frame["source_kind"] == "user"])
        await context.handler.send_text("现在多少分了？", session_id=context.session_id, turn_id="read")
        await context.handler._stream_task
        assert ingress.receipts["read"]["state"] == "auip_read"
        assert len([frame for frame in state.frames if frame["source_kind"] == "user"]) == before
        assert context.host.runtime.get_run(work["run_id"]).status == "running"
        assert len(context.host.work.list_work_items()) == context.host.adapter.calls == 1
        assert (ingress.loop.bound_context_id, ingress.loop._binding.token) == binding
    finally:
        context.host.adapter.release.set()
        await context.finish()


@pytest.mark.parametrize(("work_expected", "failure"), [(True, "none"), (False, "none"), (True, "work_refused")])
async def test_app_read_and_independent_work_keep_both_domains(composition, work_expected, failure):
    context, state, _, source, work_line, _, publisher = composition
    state.action, state.work_expected = "none", work_expected
    text = "现在多少分了？" + (source if work_expected else "")
    app_runtime = _Runtime({"status":"active", "app_session_id":"current-app",
        "app":{"title":"2048"}, "available_modes":["observe", "collaborate"]}, read_answer="score=1")
    context.manager.auip_decider = AuipControlDecisionResolver(
        query=AsyncMock(return_value='{"action":"none","work_relation":"independent","read":["state"]}'),
        app_runtime=app_runtime, launch_catalog=_Catalog())
    if failure == "work_refused":
        context.manager.work_control = context.manager.work_executor = None
    try:
        await context.handler.send_text(text, session_id=context.session_id, turn_id="read-and-work")
        await context.handler._stream_task
        ingress = context.manager.ingresses[context.session_id]
        receipt = ingress.receipts["read-and-work"]
        assert receipt["auip"]["state"] == "auip_read"
        assert receipt["auip"]["outcome"]["ok"] is True
        assert receipt["work"]["state"] == ("rejected" if failure == "work_refused" else
            "work_started" if work_expected else "no_action")
        started = work_expected and failure == "none"
        assert context.host.adapter.calls == int(started)
        assert len(context.host.work.list_work_items()) == int(started)
        context.manager.auip_router.assert_not_awaited()
        assert app_runtime.read_calls == [{"app_session_id":"current-app", "facets":("state",),
            "state_paths":(), "language":"ja"}]
        if started:
            request = context.host.adapter.requests[0]["request"]
            assert request.task == source and request.metadata["source_user_text"] == text
        assert len(publisher.receipts) == len(context.spoken) == 1
        assert context.spoken[0]["display_text"] == (
            ("作業は開始できなかったわ。" if failure == "work_refused" else
                work_line if work_expected else "") + "スコアは1点よ。")
        expressions = [frame["current"] for frame in state.frames if frame["source_kind"] == "host_receipt"]
        assert len(expressions) == 1 and expressions[0]["state"] == "work_auip_independent"
        assert expressions[0]["question"] == text
        assert [row["text"] for row in ingress.loop.history if row["source"] == "user"] == [text]
    finally:
        context.host.adapter.release.set()
        await context.finish()


async def test_unclassified_app_read_keeps_main_history_and_original_work_owner(composition):
    context, state, _, source, work_line, *_ = composition
    text = "冷却调好了吗？" + source
    prior = [
        {"source":"user", "text":"把之前的便签目标保留下来。", "input_id":"prior-user"},
        {"source":"kurisu", "text":"ええ、同じ目標で続けるわ。", "cause":"prior-role"},
    ]
    ingress = await context.manager._ingress_for(context.session_id)
    assert sm.append_session_message(context.session_id, role="user",
        content=prior[0]["text"], turn_id=prior[0]["input_id"])
    assert sm.append_session_message(context.session_id, role="assistant",
        content=prior[1]["text"], turn_id=prior[1]["cause"])
    app_runtime = _Runtime({"status":"active", "app_session_id":"current-app",
        "app":{"title":"Reactor Controls"}, "available_modes":["observe", "collaborate"],
        "state":{"cooling":2}}, read_answer="cooling=2")
    context.manager.auip_decider = AuipControlDecisionResolver(
        query=AsyncMock(return_value='{"action":"none","read":["state"]}'),
        app_runtime=app_runtime, launch_catalog=_Catalog())

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        state.frames.append(frame)
        if frame["source_kind"] == "user":
            assert frame["current"]["text"] == text
            assert "work_relation" not in frame["auip_context"]
            assert frame["auip_context"]["action"] == "read"
            assert frame["history"][-2:] == prior
            return json.dumps({"action":{"op":"work", "intent":"execute", "source":source},
                "say":work_line}, ensure_ascii=False)
        assert frame["current"]["state"] == "work_auip_independent"
        assert frame["current"]["app_read_facts"] == "cooling=2"
        return work_line + "冷却は二段階よ。"

    context.manager.query = query
    ingress.loop.query = query
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="unclassified-read-work")
        await context.handler._stream_task
        receipt = ingress.receipts["unclassified-read-work"]
        assert receipt["state"] == "work_auip_independent"
        assert receipt["auip"]["state"] == "auip_read"
        assert receipt["work"]["state"] == "work_started"
        assert context.host.adapter.calls == 1
        context.manager.auip_router.assert_not_awaited()
        request = context.host.adapter.requests[0]["request"]
        assert request.task == source
        assert request.metadata["source_user_text"] == text
        assert request.metadata["source_user_context"].endswith(
            'Main Chat: "ええ、同じ目標で続けるわ。"')
        assert app_runtime.read_calls == [{"app_session_id":"current-app",
            "facets":("state",), "state_paths":(), "language":"ja"}]
        user_frame = next(frame for frame in state.frames if frame["source_kind"] == "user")
        assert user_frame["current"]["text"] == text
        assert user_frame["history"][-2:] == prior
    finally:
        context.host.adapter.release.set()
        await context.finish()


async def test_unclassified_app_read_performs_no_app_mutation_or_work(composition):
    context, state, *_ = composition
    text = "冷却调好了吗？"
    app_runtime = _Runtime({"status":"active", "app_session_id":"current-app",
        "app":{"title":"Reactor Controls"}, "available_modes":["observe", "collaborate"],
        "state":{"cooling":2}}, read_answer="cooling=2")
    context.manager.auip_decider = AuipControlDecisionResolver(
        query=AsyncMock(return_value='{"action":"none","read":["state"]}'),
        app_runtime=app_runtime, launch_catalog=_Catalog())

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        state.frames.append(frame)
        if frame["source_kind"] == "user":
            assert frame["current"]["text"] == text
            assert frame["auip_context"]["action"] == "read"
            assert "work_relation" not in frame["auip_context"]
            return json.dumps({"action":None, "say":""})
        assert frame["current"]["state"] == "work_auip_independent"
        assert frame["current"]["work"]["state"] == "no_action"
        return "冷却は二段階よ。"

    context.manager.query = query
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="unclassified-read-only")
        await context.handler._stream_task
        ingress = context.manager.ingresses[context.session_id]
        receipt = ingress.receipts["unclassified-read-only"]
        assert receipt["state"] == "work_auip_independent"
        assert receipt["work"]["state"] == "no_action"
        assert receipt["auip"]["state"] == "auip_read"
        assert context.host.adapter.calls == 0
        assert context.host.work.list_work_items() == []
        context.manager.auip_router.assert_not_awaited()
        assert app_runtime.read_calls == [{"app_session_id":"current-app",
            "facets":("state",), "state_paths":(), "language":"ja"}]
        assert context.spoken[0]["display_text"] == "冷却は二段階よ。"
    finally:
        context.host.adapter.release.set()
        await context.finish()


async def test_professional_app_read_ignores_null_action_root_metadata(composition):
    context, state, *_unused, publisher = composition
    text = "现在多少分了？"
    app_runtime = _Runtime({"status":"active", "app_session_id":"current-app",
        "app":{"title":"2048"}, "available_modes":["observe", "collaborate"]},
        read_answer="score=1")
    context.manager.auip_decider = AuipControlDecisionResolver(
        query=AsyncMock(return_value=(
            '{"action":"none","work_relation":"subsumed","read":["state"]}')),
        app_runtime=app_runtime, launch_catalog=_Catalog())
    planner_calls = []

    async def planner(*args, **kwargs):
        planner_calls.append((args, kwargs))
        pytest.fail("null action metadata cannot propose professional Work")

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        state.frames.append(frame)
        if frame["source_kind"] == "user":
            return json.dumps({"action":None, "say":"状態を答えるわ。",
                "type":"json_object", "metadata":{"action":{"op":"work"},
                    "provider":"codex", "task":"do not execute"}}, ensure_ascii=False)
        assert frame["current"]["state"] == "auip_read"
        assert frame["current"]["question"] == text
        assert frame["current"]["facts"] == "score=1"
        assert "work" not in frame["current"]
        assert "app_read_facts" not in frame["current"]
        return "スコアは1点よ。"

    context.manager.query = query
    context.manager.work_planner = planner
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="read-null-root-metadata")
        await context.handler._stream_task
        ingress = context.manager.ingresses[context.session_id]
        receipt = ingress.receipts["read-null-root-metadata"]
        assert receipt["state"] == "auip_read"
        assert "work" not in receipt and "auip" not in receipt
        assert planner_calls == []
        assert context.host.adapter.calls == 0
        assert context.host.work.list_work_items() == []
        context.manager.auip_router.assert_not_awaited()
        assert app_runtime.read_calls == [{"app_session_id":"current-app",
            "facets":("state",), "state_paths":(), "language":"ja"}]
        assert len(publisher.receipts) == len(context.spoken) == 2
        assert [row["display_text"] for row in context.spoken] == [
            "状態を答えるわ。", "スコアは1点よ。"]
        assert [row["text"] for row in ingress.loop.history
            if row["source"] == "user"] == [text]
        assert not any(row.get("state") in {"not_accepted", "unknown"}
            for row in ingress.loop.history)
    finally:
        context.host.adapter.release.set()
        await context.finish()


async def test_subsumed_read_after_explicit_empty_professional_plan_uses_app_receipt(
        composition):
    context, state, *_unused = composition
    text = "刚才接管了吗？局面怎样？"
    app_runtime = _Runtime({"status":"active", "app_session_id":"current-app",
        "app":{"title":"Current game"}, "available_modes":["observe", "collaborate"],
        "state":{"phase":"gameover"}}, read_answer="controller=idle; phase=gameover")
    context.manager.auip_decider = AuipControlDecisionResolver(
        query=AsyncMock(return_value=(
            '{"action":"none","work_relation":"subsumed",'
            '"read":["receipt","state"]}')),
        app_runtime=app_runtime, launch_catalog=_Catalog())
    planner = AsyncMock(return_value=CompoundControlPlan(status="ok"))
    context.manager.work_planner = planner
    ingress = await context.manager._ingress_for(context.session_id)
    requested_app_context = []
    ingress.loop.role_app_context = lambda app_session_id: (
        requested_app_context.append(app_session_id)
        or "[Current application]\ncontroller=idle\n[AUIP Interaction Briefing]\nphase=gameover"
    )

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        state.frames.append(frame)
        if frame["source_kind"] == "user":
            return json.dumps({"action":{"op":"work"},
                "say":"確認するわ。"}, ensure_ascii=False)
        assert frame["source_kind"] == "host_receipt"
        assert frame["current"] == {
            "source":"host_receipt",
            "state":"auip_read",
            "question":text,
            "facts":"controller=idle; phase=gameover",
            "app_session_id":"current-app",
        }
        assert frame["app_context"] == (
            "[Current application]\ncontroller=idle\n"
            "[AUIP Interaction Briefing]\nphase=gameover")
        return "今は操作していなくて、局面はゲームオーバーよ。"

    context.manager.query = query
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="subsumed-read-empty-work")
        await context.handler._stream_task
        ingress = context.manager.ingresses[context.session_id]
        receipt = ingress.receipts["subsumed-read-empty-work"]
        assert receipt["state"] == "auip_read"
        assert "work" not in receipt and "auip" not in receipt
        planner.assert_awaited_once()
        host_frames = [frame for frame in state.frames
            if frame["source_kind"] == "host_receipt"]
        assert len(host_frames) == 1
        assert requested_app_context == ["", "current-app"]
        assert [row["text"] for row in ingress.loop.history
            if row["source"] == "user"] == [text]
        assert context.host.adapter.calls == 0
        assert context.host.work.list_work_items() == []
    finally:
        context.host.adapter.release.set()
        await context.finish()


@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize("result", ["accepted", "rejected", "superseded", "hidden"])
async def test_existing_b2_owner_chooses_executes_and_publishes_once(composition, independent, result):
    context, state, mixed_text, source, work_line, _, publisher = composition
    text = mixed_text if independent else "这步你来吧。"
    state.relation = "independent" if independent else "subsumed"
    runtime, registered = _runtime(conversation_id=context.session_id)
    sid = registered["app_session_id"]
    source_query = AsyncMock(return_value=json.dumps({"action":"step",
        "instruction":"这步你来吧。", "work_relation":state.relation}))
    decider = AuipControlDecisionResolver(query=source_query, app_runtime=runtime, launch_catalog=_Catalog())
    selected, release_choice = asyncio.Event(), asyncio.Event()
    spoken_choice = "右下に置いたわ。"
    requested = []

    async def choose(**kwargs):
        assert kwargs["user_instruction"] == "这步你来吧。"
        candidate = next(item for item in kwargs["candidates"].values()
            if item.action_type == "game.place" and item.payload == {"x":1, "y":1})
        assert candidate.revision == 1
        assert kwargs["speech_required"] is True
        selected.set()
        if result == "superseded":
            await release_choice.wait()
        return {"candidate_id":candidate.candidate_id, "instruction_relation":"follows",
            "speech":spoken_choice, "choice_reason":"the selected point is empty"}

    chooser = AsyncMock(side_effect=choose)
    b2 = AuipB2Coordinator(runtime=runtime, control_decider=decider, role_chooser=chooser,
        stage_decision=lambda *_args:pytest.fail("accepted B2 step must not enter legacy Chat staging"))
    context.manager.configure_auip(decider, context.manager.auip_router,
        step_request=b2.execute_user_decision)
    planner_receipts = []
    if independent:
        def plan_work(_ingress, _turn, receipt, _admission):
            planner_receipts.append(dict(receipt))
            return planned(context.manager.provider, receipt["text"], source,
                "execute", one_off=True)
        context.manager.work_planner = plan_work
    if result == "hidden":
        publisher.publish = AsyncMock(return_value=False)

    async def application_receipt(_method, payload):
        if payload.get("app_session_id") != sid:
            return
        if independent and result != "hidden":
            assert [row["display_text"] for row in context.spoken] == [work_line]
        else:
            assert not context.spoken
        action = payload["action"]
        requested.append(action)
        snapshot = runtime.get(sid)["state"]
        if result != "rejected":
            snapshot["board"]["rows"] = ["B.", ".B"]
        receipt = runtime.resolve_action(app_session_id=sid, bridge_token=registered["bridge_token"],
            action_id=action["action_id"], accepted=result != "rejected",
            resulting_revision=1 if result == "rejected" else 2, state=snapshot,
            reason="application declined" if result == "rejected" else "")
        await bus.emit(Method.AUIP_UPDATED, receipt)

    bus.on(Method.AUIP_ACTION_REQUESTED, application_receipt)
    try:
        await context.handler.send_text(text, session_id=context.session_id, turn_id="b2-source")
        original_stream = context.handler._stream_task
        if result == "superseded":
            await asyncio.wait_for(selected.wait(), 3)
            if independent:
                await asyncio.wait_for(context.host.adapter.started.wait(), 3)
            # The app decision is still private. A new turn must invalidate its invocation.
            decider.capture = lambda **_kwargs:None
            await context.handler.send_text("谢啦", session_id=context.session_id, turn_id="later-b2")
            await asyncio.wait_for(context.handler._stream_task, 3)
            release_choice.set()
        await asyncio.gather(original_stream, return_exceptions=True)
        ingress = context.manager.ingresses[context.session_id]
        await asyncio.gather(*tuple(ingress.loop._monitors), return_exceptions=True)
        assert source_query.await_count == chooser.await_count == 1
        context.manager.auip_router.assert_not_awaited()
        assert len(requested) == (0 if result == "superseded" else 1)
        assert context.host.adapter.calls == int(independent)
        if independent:
            request = context.host.adapter.requests[0]["request"]
            assert request.task == source and request.metadata["source_user_text"] == text
            work_frame = next(frame for frame in state.frames if frame["source_kind"] == "user")
            assert "auip_context" not in work_frame
            assert planner_receipts[0]["auip_context"]["instruction"] == "这步你来吧。"
            assert context.host.runtime.get_run(context.manager.ingresses[context.session_id]
                .receipts["b2-source"]["work"]["run_id"]).status == "running"
        role_frames = [frame for frame in state.frames if frame["source_kind"] == "host_receipt"]
        assert not any(frame["current"].get("state") == "auip_step_pending" for frame in role_frames)
        projection = runtime.get(sid)
        assert projection["revision"] == (2 if result in {"accepted", "hidden"} else 1)
        if result == "superseded":
            assert projection["operator_status"] == "idle"
        if result == "accepted":
            assert [row["display_text"] for row in context.spoken] == (
                [work_line, spoken_choice] if independent else [spoken_choice])
            assert projection["latest_delivered_narration"]["text"] == spoken_choice
            assert role_frames == []
        else:
            assert projection["latest_delivered_narration"] is None
            assert not any(spoken_choice in line["display_text"] for line in context.spoken)
        if result != "superseded":
            replay = await context.handler.send_text(text, session_id=context.session_id, turn_id="b2-source")
            assert replay["status"] == "replayed"
            assert len(requested) == 1 and chooser.await_count == 1
    finally:
        release_choice.set()
        bus.off(Method.AUIP_ACTION_REQUESTED, application_receipt)
        context.host.adapter.release.set()
        await context.finish()


def _active_work_ambiguity_decider(attempt_id):
    return AuipControlDecisionResolver(
        query=AsyncMock(return_value=(
            '{"action":"none","ambiguity":"work_or_app"}')),
        app_runtime=_Runtime({"status":"active", "app_session_id":"current-app",
            "app":{"title":"Current game"},
            "available_modes":["observe", "collaborate"]}),
        launch_catalog=_Catalog(),
        has_active_work=lambda _session_id:(attempt_id,))


async def _seed_professional_ambiguity_work(context, text, source):
    context.manager.work_planner = lambda _ingress, _turn, receipt, _admission: planned(
        context.manager.provider, receipt["text"], source, "execute", one_off=True)
    await context.handler.send_text(text, session_id=context.session_id,
        turn_id="ambiguity-seed")
    await context.handler._stream_task
    ingress = context.manager.ingresses[context.session_id]
    work = ingress.receipts["ambiguity-seed"]["work"]
    assert context.host.runtime.get_run(work["run_id"]).status == "running"
    return ingress, work


@pytest.mark.parametrize("presentation_failure", [False, True])
async def test_active_app_work_ambiguity_blocks_direct_role_interrupt(
        composition, presentation_failure):
    context, _state, seed_text, source, *_unused = composition
    ingress, work = await _seed_professional_ambiguity_work(
        context, seed_text, source)
    context.manager.auip_decider = _active_work_ambiguity_decider(work["attempt_id"])
    expressions, reference_queries = [], []

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame.get("source_kind") == "user":
            return json.dumps({"say":"確認するわ。",
                "action":{"op":"interrupt", "target":"便签"}}, ensure_ascii=False)
        if frame.get("source_kind") == "host_receipt":
            expressions.append(frame)
            if presentation_failure:
                raise RuntimeError("presentation unavailable")
            return "どちらを止めるか教えてちょうだい。"
        reference_queries.append(messages)
        return json.dumps({"references":[]})

    context.manager.query = ingress.loop.query = query
    context.host.runtime.cancel = AsyncMock(wraps=context.host.runtime.cancel)
    context.host.adapter.cancel = AsyncMock(wraps=context.host.adapter.cancel)
    before_app = context.manager.auip_router.await_count
    try:
        await context.handler.send_text("先停一下。", session_id=context.session_id,
            turn_id="ambiguous-direct-stop")
        await context.handler._stream_task

        result = ingress.receipts["ambiguous-direct-stop"]
        assert result["state"] == "no_action"
        assert result["reason"] == "work_or_app_ambiguous"
        assert result["auip_context"]["ambiguity"] == "work_or_app"
        assert result["auip_context"]["app_session_id"] == ""
        assert "resolution=ambiguous_between_application_and_work" in (
            result["auip_context"]["role_grounding"])
        assert len(expressions) == 1
        assert expressions[0]["current"]["auip_context"] == result["auip_context"]
        assert bool([row for row in ingress.loop.trace
            if row.get("kind") == "presentation_failed"
            and row.get("cause") == "ambiguous-direct-stop"]) is presentation_failure
        assert reference_queries == []
        context.host.runtime.cancel.assert_not_awaited()
        context.host.adapter.cancel.assert_not_awaited()
        assert context.host.runtime.get_run(work["run_id"]).status == "running"
        assert context.manager.auip_router.await_count == before_app
    finally:
        context.host.adapter.release.set()
        await context.finish()


@pytest.mark.parametrize("mixed", [False, True])
async def test_active_app_work_ambiguity_filters_planned_retract_only(
        composition, mixed):
    context, _state, seed_text, source, *_unused = composition
    ingress, work = await _seed_professional_ambiguity_work(
        context, seed_text, source)
    context.manager.auip_decider = _active_work_ambiguity_decider(work["attempt_id"])
    candidate = next(candidate for candidate in
        context.manager.work_candidates_for_context(context.session_id, "")[0]
        if candidate.entity_id == work["work_item_id"])
    stop_clause = "先停一下。"
    create_clause = "另做一个清单页。"
    text = create_clause + stop_clause if mixed else stop_clause
    stop_start = text.index(stop_clause)
    operations = [CompoundControlOperation(0, stop_clause, {
        "provider":context.manager.provider, "intent":"retract", "task":stop_clause,
        "subject":"work_item", "_host_workspace_access":"none",
        CONTROL_REFERENCE_CANDIDATES_ATTR:(candidate,)})]
    clauses = [SourceClause(stop_clause, stop_start, stop_start + len(stop_clause))]
    if mixed:
        operations.insert(0, CompoundControlOperation(0, create_clause, {
            "provider":context.manager.provider, "intent":"execute", "task":create_clause,
            "one_off":True, "_host_workspace_access":"write"}))
        operations[1] = CompoundControlOperation(1, stop_clause, operations[1].action)
        clauses.insert(0, SourceClause(create_clause, 0, len(create_clause)))
    plan = CompoundControlPlan(status="ok", operations=tuple(operations),
        clauses=tuple(clauses), raw_reply="canonical-professional-plan")
    planner = AsyncMock(return_value=plan)
    context.manager.work_planner = planner
    expressions = []

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame.get("source_kind") == "user":
            return json.dumps({"say":"確認するわ。", "action":{"op":"work"}},
                ensure_ascii=False)
        expressions.append(frame)
        return "新しい作業は進めるけど、止める対象は確認させて。"

    context.manager.query = ingress.loop.query = query
    context.host.runtime.cancel = AsyncMock(wraps=context.host.runtime.cancel)
    context.host.adapter.cancel = AsyncMock(wraps=context.host.adapter.cancel)
    before_calls = context.host.adapter.calls
    if mixed:
        context.host.adapter.started.clear()
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="ambiguous-planned-" + str(mixed).lower())
        await context.handler._stream_task
        if mixed:
            await asyncio.wait_for(context.host.adapter.started.wait(), 2)

        result = ingress.receipts["ambiguous-planned-" + str(mixed).lower()]
        planner.assert_awaited_once()
        planner_receipt = planner.await_args.args[2]
        assert planner_receipt["auip_context"]["ambiguity"] == "work_or_app"
        assert planner_receipt["auip_context"]["app_session_id"] == ""
        assert result["auip_context"] == planner_receipt["auip_context"]
        assert len(expressions) == 1
        assert expressions[0]["current"]["auip_context"] == result["auip_context"]
        context.host.runtime.cancel.assert_not_awaited()
        context.host.adapter.cancel.assert_not_awaited()
        assert context.host.runtime.get_run(work["run_id"]).status == "running"
        assert context.host.adapter.calls == before_calls + int(mixed)
        if mixed:
            assert result["state"] == "work_started"
            request = context.host.adapter.requests[-1]["request"]
            assert request.task == create_clause
            assert request.metadata["source_user_text"] == text
        else:
            assert result["state"] == "no_action"
    finally:
        context.host.adapter.release.set()
        await context.finish()


def test_ambiguous_retract_filter_preserves_other_plan_operations_and_raw_reply():
    intents = ("execute", "amend", "message", "report", "retract")
    clauses = tuple(SourceClause(intent, index * 10, index * 10 + len(intent))
        for index, intent in enumerate(intents))
    references = (object(),)
    operations = tuple(CompoundControlOperation(index, intent, {
        "intent":intent, "provider":"codex", "task":intent,
        CONTROL_REFERENCE_CANDIDATES_ATTR:references})
        for index, intent in enumerate(intents))
    plan = CompoundControlPlan(status="ok", operations=operations,
        clauses=clauses, raw_reply="unaltered canonical reply")

    filtered, suppressed = _suppress_ambiguous_retracts(plan)

    assert [operation.action["intent"] for operation in filtered.operations] == [
        "execute", "amend", "message", "report"]
    assert [operation.operation_index for operation in filtered.operations] == [0, 1, 2, 3]
    assert filtered.clauses == clauses[:-1]
    assert filtered.raw_reply == plan.raw_reply
    assert all(operation.action[CONTROL_REFERENCE_CANDIDATES_ATTR] is references
        for operation in filtered.operations)
    assert suppressed == (operations[-1],)
