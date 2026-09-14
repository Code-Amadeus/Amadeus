"""App interpretation cannot suppress the independent Work proposal owner."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from test_auip_launch import _seed_app
from test_cooperative_independent_auip_work import composition as composition
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_work import planned


async def test_subsumed_app_no_work_uses_one_app_owned_response(composition):
    context, state, *_unused, app_line, publisher = composition
    state.action, state.relation = "step", "subsumed"
    text = "这步你来。"
    main_line = "この一手は私が進めるわ。"
    requests = []

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        requests.append(frame)
        if frame["source_kind"] == "user":
            return json.dumps({"action":{"op":"auip"}, "say":main_line}, ensure_ascii=False)
        assert frame["current"]["state"] in {"auip_step_pending", "auip_applied"}
        return app_line

    context.manager.query = query
    context.manager.work_planner = lambda *_args, **_kwargs:pytest.fail(
        "no coarse Work proposal may invoke the planner")
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="subsumed-app-only")
        await context.handler._stream_task
        ingress = context.manager.ingresses[context.session_id]
        receipt = ingress.receipts["subsumed-app-only"]
        assert receipt["work"]["state"] == "no_action"
        assert receipt["auip"]["state"] == "auip_applied"
        assert receipt["auip"]["outcome"]["ok"] is True
        context.manager.auip_router.assert_awaited_once()
        assert context.host.adapter.calls == 0
        assert len(publisher.receipts) == len(context.spoken) == 1
        assert context.spoken[0]["display_text"] == app_line
        assert len(requests) == 2
    finally:
        context.host.adapter.release.set()
        await context.finish()


@pytest.mark.parametrize("app_expression", ["empty", "failed"])
async def test_subsumed_app_without_display_keeps_the_only_main_response(
        composition, app_expression):
    context, state, *_unused, publisher = composition
    state.action, state.relation = "leave", "subsumed"
    main_line = "では、ゲームを閉じるわ。"

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] == "user":
            return json.dumps({"action":{"op":"auip"}, "say":main_line}, ensure_ascii=False)
        assert frame["current"]["state"] == "auip_applied"
        if app_expression == "failed":
            raise RuntimeError("app expression unavailable")
        return ""

    context.manager.query = query
    context.manager.work_planner = lambda *_args, **_kwargs:pytest.fail(
        "no coarse Work proposal may invoke the planner")
    try:
        await context.handler.send_text("这局先关掉吧。", session_id=context.session_id,
            turn_id="subsumed-no-app-display-" + app_expression)
        await context.handler._stream_task
        ingress = context.manager.ingresses[context.session_id]
        receipt = ingress.receipts["subsumed-no-app-display-" + app_expression]
        assert receipt["work"]["state"] == "no_action"
        assert receipt["auip"]["state"] == "auip_applied"
        context.manager.auip_router.assert_awaited_once()
        assert context.host.adapter.calls == 0
        assert len(publisher.receipts) == len(context.spoken) == 1
        assert context.spoken[0]["display_text"] == main_line
        if app_expression == "failed":
            assert any(row["kind"] == "presentation_failed" for row in ingress.loop.trace)
    finally:
        context.host.adapter.release.set()
        await context.finish()


@pytest.mark.parametrize("relation", ["independent", ""])
async def test_non_subsumed_app_no_work_keeps_main_and_app_responses(composition, relation):
    context, state, *_unused, app_line, publisher = composition
    state.action, state.relation = "step", relation
    main_line = "この一手は私が進めるわ。"

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] == "user":
            return json.dumps({"action":{"op":"auip"}, "say":main_line}, ensure_ascii=False)
        assert frame["current"]["state"] == "auip_step_pending"
        return app_line

    context.manager.query = query
    context.manager.work_planner = lambda *_args, **_kwargs:pytest.fail(
        "no coarse Work proposal may invoke the planner")
    try:
        await context.handler.send_text("这步你来。", session_id=context.session_id,
            turn_id="non-subsumed-app-only-" + (relation or "unknown"))
        await context.handler._stream_task
        assert context.manager.auip_router.await_count == 1
        assert context.host.adapter.calls == 0
        assert len(publisher.receipts) == len(context.spoken) == 1
        assert context.spoken[0]["display_text"] == main_line + "\n" + app_line
    finally:
        context.host.adapter.release.set()
        await context.finish()


async def test_subsumed_leave_failure_retains_receipt_and_presents_lifecycle_facts(composition):
    context, state, *_unused, publisher = composition
    state.action, state.relation = "leave", "subsumed"
    text = "这局先关掉吧。"
    main_line = "では、ゲームを閉じるわ。"
    failure_line = "アプリが終了要求を受理しなかったわ。"
    domain_outcome = {"ok":False, "reason":"surface refused",
        "receipt":{"accepted":False, "code":"surface_refused"}}
    host_events = []

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] == "user":
            return json.dumps({"action":{"op":"auip"}, "say":main_line}, ensure_ascii=False)
        host_events.append(frame["current"])
        assert frame["current"]["state"] == "auip_rejected"
        return failure_line

    context.manager.query = query
    context.manager.work_planner = lambda *_args, **_kwargs:pytest.fail(
        "no coarse Work proposal may invoke the planner")
    context.manager.auip_router = AsyncMock(return_value=domain_outcome)
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="subsumed-leave-failed")
        await context.handler._stream_task
        ingress = context.manager.ingresses[context.session_id]
        receipt = ingress.receipts["subsumed-leave-failed"]
        assert receipt["work"]["state"] == "no_action"
        assert receipt["auip"]["state"] == "auip_rejected"
        assert receipt["auip"]["outcome"] == domain_outcome
        context.manager.auip_router.assert_awaited_once()
        assert len(publisher.receipts) == len(context.spoken) == 1
        assert context.spoken[0]["display_text"] == failure_line
        # The Host keeps the full domain receipt above. Role presentation sees
        # lifecycle facts, not the unrelated raw application receipt/history.
        assert host_events == [{"source":"host_receipt", "state":"auip_rejected",
            "action":"leave", "question":text,
            "outcome":{"ok":False, "reason":"surface refused"},
            "role_scope":"この応答ではアプリ側の要求だけを扱います。独立したWorkは別の担当が説明します。"}]
    finally:
        context.host.adapter.release.set()
        await context.finish()


async def test_subsumed_app_keeps_unknown_work_response(composition):
    context, state, _, source, _, app_line, publisher = composition
    state.action, state.relation = "step", "subsumed"
    text = "这步你来，" + source
    main_line = "作業も確認して進めるわ。"
    failure_line = "作業の開始結果は確認できなかったわ。"

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] == "user":
            return json.dumps({"action":{"op":"work"}, "say":main_line}, ensure_ascii=False)
        if frame["current"].get("state") == "auip_step_pending":
            return app_line
        assert frame["current"]["state"] == "unknown"
        return failure_line

    async def planner(*_args, **_kwargs):
        return planned(context.manager.provider, text, source, "execute", None,
            one_off=True)

    context.manager.query = query
    context.manager.work_planner = planner
    context.manager.work_executor.dispatch = AsyncMock(
        side_effect=RuntimeError("handoff uncertain"))
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="subsumed-app-work-unknown")
        await context.handler._stream_task
        ingress = context.manager.ingresses[context.session_id]
        receipt = ingress.receipts["subsumed-app-work-unknown"]
        assert receipt["work"]["state"] == "unknown"
        assert receipt["auip"]["state"] == "auip_applied"
        context.manager.auip_router.assert_awaited_once()
        assert context.host.adapter.calls == 0
        assert len(publisher.receipts) == len(context.spoken) == 2
        assert context.spoken[0]["display_text"] == main_line
        assert context.spoken[1]["display_text"] == failure_line + "\n" + app_line
    finally:
        context.host.adapter.release.set()
        await context.finish()


@pytest.mark.parametrize("app_action", ["step", "none"])
@pytest.mark.parametrize("work_intent", ["execute", "retract", "report", "report_unavailable", None])
async def test_subsumed_app_cannot_veto_work_and_app_only_adds_no_planner(
        composition, tmp_path, app_action, work_intent):
    context, state, _, _, _, app_line, publisher = composition
    report_unavailable = work_intent == "report_unavailable"
    work_intent = "report" if report_unavailable else work_intent
    report_line = ("リストの状態は確認できなかったわ。" if report_unavailable
        else "リストの作成は完了しているわ。")
    report_result = "[report] answer pass unavailable" if report_unavailable else report_line
    state.action, state.relation = app_action, "subsumed"
    app_text = "这步你来，" if app_action == "step" else "现在多少分了？"
    work_text = {"execute":"再帮我做个清单页吧。",
        "retract":"清单先别做了。", "report":"清单做完了吗？", None:"今天真累啊。"}[work_intent]
    text = app_text + work_text
    item = None
    if work_intent in {"retract", "report"}:
        item, attempt, _ = _seed_app(context.host.work, context.host.project,
            tmp_path / "drafts", title="清单", turn_id="prior-list", goal="做个清单页")
        context.host.work.update_attempt(attempt.attempt_id,
            metadata={"session_id":context.session_id})
    requests, plans, report_calls = [], [], []

    async def report(source, attrs, *, publish):
        report_calls.append((source, dict(attrs)))
        if report_unavailable:
            return report_result
        await publish(report_line)
        return "[report] answered"

    context.manager.configure_work(context.host.control, context.host.executor,
        report_request=report)

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        requests.append(frame)
        if frame["source_kind"] == "user":
            assert frame["current"]["text"] == text
            assert "auip_context" not in frame
            return json.dumps({"action":{"op":"work"} if work_intent else None,
                "say":"確認するわ。" if work_intent else "少し休んでね。"})
        if frame["current"].get("state") == "work_auip_independent":
            assert frame["current"]["app_read_facts"] == "score=1"
            if work_intent == "report":
                assert frame["current"]["work"]["report_result"] == report_result
                return report_line + "今は1点よ。"
            return "今は1点よ。"
        if frame["current"].get("state") == "auip_read":
            assert frame["current"]["facts"] == "score=1"
            assert "work" not in frame["current"]
            return "今は1点よ。"
        if frame["current"].get("state") == "work_reported":
            assert frame["current"]["report_result"] == report_result
            return report_line
        if frame["current"].get("state") == "not_active":
            return "リストは既に完了していて、停止対象はないわ。"
        return app_line

    async def planner(ingress, turn_id, receipt, admission):
        plans.append(receipt)
        assert receipt["text"] == text
        assert receipt["auip_context"] == {
            "action":"read" if app_action == "none" else "step",
            "app_session_id":"current-app", "timing":"now", "instruction":""}
        assert not ingress.loop._foreground.locked()
        candidate = None
        if item is not None:
            candidates, complete, _ = context.manager.work_candidates_for_context(
                context.session_id, "")
            assert complete
            candidate = next(row for row in candidates if row.entity_id == item.work_item_id)
        return planned(context.manager.provider, text, work_text, work_intent,
            candidate, **({"one_off":True} if work_intent == "execute" else
                {"_host_workspace_access":"none"}))

    context.manager.query = query
    context.manager.work_planner = planner
    try:
        await context.handler.send_text(text, session_id=context.session_id, turn_id="mixed-plan")
        await asyncio.wait_for(context.handler._stream_task, 5)
        ingress = context.manager.ingresses[context.session_id]
        receipt = ingress.receipts["mixed-plan"]
        app_only_read = app_action == "none" and work_intent is None
        if app_only_read:
            assert receipt["state"] == "auip_read"
            assert "work" not in receipt and "auip" not in receipt
        else:
            assert receipt["auip"]["state"] == (
                "auip_read" if app_action == "none" else "auip_applied")
            assert receipt["work"]["state"] == {"execute":"work_started",
                "retract":"not_active", "report":"work_reported",
                None:"no_action"}[work_intent]
        assert len(plans) == int(work_intent is not None)
        assert context.host.adapter.calls == int(work_intent == "execute")
        if item is not None:
            assert receipt["work"].get("work_item_id",
                receipt["work"].get("report_work_item_id")) == item.work_item_id
            assert len(context.host.work.list_attempts(item.work_item_id)) == 1
        if work_intent == "report":
            assert len(report_calls) == 1
            assert report_calls[0][0] == work_text
            assert receipt["work"]["report_result"] == report_result
            assert report_line in context.spoken[-1]["display_text"]
            assert all("[report]" not in line["display_text"] for line in context.spoken)
        if work_intent == "execute":
            request = context.host.adapter.requests[0]["request"]
            assert request.task == work_text and request.metadata["source_user_text"] == text
        publication_count = 2
        assert len(publisher.receipts) == len(context.spoken) == publication_count
        if work_intent is not None:
            assert context.spoken[0]["display_text"] == "確認するわ。"
        else:
            assert [row["display_text"] for row in context.spoken] == [
                "少し休んでね。", app_line if app_action == "step" else "今は1点よ。"]
        if work_intent == "execute" and app_action == "step":
            assert context.spoken[-1]["display_text"] == app_line
        assert [row["text"] for row in ingress.loop.history if row["source"] == "user"] == [text]
        assert context.manager.auip_router.await_count == int(app_action == "step")
        request_count = len(requests)
        replay = await context.handler.send_text(text, session_id=context.session_id,
            turn_id="mixed-plan")
        assert replay["status"] == "replayed"
        assert len(requests) == request_count and len(plans) == int(work_intent is not None)
        assert len(publisher.receipts) == publication_count
    finally:
        context.host.adapter.release.set()
        await context.finish()
