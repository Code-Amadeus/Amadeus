"""Regression from real colloquial ChatPage: blocked AUIP step must not crash Chat."""
from dataclasses import replace
import json

import pytest

from server.auip_control_decision import parse_auip_control_decision
from server.protocol import Method
from test_cooperative_independent_auip_work import composition as composition
from test_cooperative_pending_turn import pending_host as pending_host


@pytest.mark.parametrize("role_action", [None, {"op": "auip"},
    {"op": "send_to", "target": "an older research task"}])
@pytest.mark.parametrize("app_action", ["step", "collaborate", "delegate"])
async def test_blocked_step_reports_unexecuted_action_and_keeps_chat_usable(composition, role_action, app_action):
    context, *_ = composition
    source = "点一下点赞按钮。"
    decision = parse_auip_control_decision(json.dumps({"action": app_action,
        **({"instruction": source} if app_action == "step" else {}),
        "work_relation": "subsumed"}), has_active=True, candidate_titles=set(),
        active_title="Comparison", allow_after_work=False, active_modes={"observe"})
    assert decision.status == "blocked" and decision.control_attrs() is None
    decision = replace(decision, app_session_id="current-app")
    context.manager.auip_decider.capture = lambda **kwargs: decision if kwargs["user_text"] == source else None

    async def query(messages, **kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] == "user":
            action = role_action if frame["current"]["text"] == source else None
            return json.dumps({"action": action, "say": "確認するわ。"})
        return "確認するわ。"

    context.manager.query = query
    context.manager.work_planner = lambda *args, **kwargs: pytest.fail("No Work was requested")
    try:
        await context.handler.send_text(source, session_id=context.session_id, turn_id="blocked-step")
        await context.handler._stream_task
        errors = [payload for method, payload in context.visible if method == Method.CHAT_ERROR]
        receipt = context.manager.ingresses[context.session_id].receipts["blocked-step"]
        assert receipt["auip"]["state"] == "auip_rejected"
        assert receipt["auip"]["outcome"]["reason"] == decision.reason
        context.manager.auip_router.assert_not_awaited()
        await context.handler.send_text("先放着，聊点别的。", session_id=context.session_id, turn_id="chat-after-blocked")
        await context.handler._stream_task
        assert context.manager.ingresses[context.session_id].receipts["chat-after-blocked"]["state"] == "no_action"
        assert context.host.adapter.calls == 0
        assert not errors, errors
    finally:
        context.host.adapter.release.set()
        await context.finish()


async def test_blocked_app_operation_does_not_veto_independent_work(composition):
    context, _state, text, *_ = composition
    decision = parse_auip_control_decision(json.dumps({"action":"step", "instruction":"这一步你来",
        "work_relation":"independent"}), has_active=True, candidate_titles=set(),
        active_title="Application", allow_after_work=False, active_modes={"observe"})
    assert decision.status == "blocked"
    context.manager.auip_decider.capture = lambda **kwargs: replace(decision, app_session_id="current-app")
    try:
        await context.handler.send_text(text, session_id=context.session_id, turn_id="independent-with-blocked-app")
        await context.handler._stream_task
        receipt = context.manager.ingresses[context.session_id].receipts["independent-with-blocked-app"]
        assert receipt["auip"]["state"] == "auip_rejected"
        assert receipt["work"]["state"] == "work_started"
        context.manager.auip_router.assert_not_awaited()
        assert context.host.adapter.calls == 1
    finally:
        context.host.adapter.release.set()
        await context.finish()
