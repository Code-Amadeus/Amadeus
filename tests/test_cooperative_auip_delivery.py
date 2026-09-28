"""AUIP facts keep their owner while one shared role publisher presents them."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import session_manager as sm
from server.auip_control_decision import AuipControlDecision
from server.cooperative_delivery import CooperativeHostDelivery
from server.protocol import Method
from test_cooperative_pending_turn import pending_host as pending_host


@pytest.fixture
def auip_host(pending_host):
    context = pending_host
    state = SimpleNamespace(action="read", turn_id="", fail="", events=[], systems=[],
        facts='game.ready=true\nscore=0', reply="盤面の準備ができていて、得点は0よ。")

    def capture(**_kwargs):
        if state.action == "after_work" and _kwargs.get("active_required"):
            return None
        return AuipControlDecision(status="ok", app_session_id="" if state.action == "after_work" else "app-current",
            action="none" if state.action == "read" else "launch" if state.action == "after_work" else state.action,
            timing="after_work" if state.action == "after_work" else "now",
            work_relation="subsumed" if state.action == "read" else "",
            read_facets=("state",) if state.action == "read" else (),
            active_work_attempt_ids=("attempt-current",) if state.action == "after_work" else ())

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] == "user":
            assert state.action == "after_work"
            return json.dumps({"action":{"op":"auip"}, "say":state.reply})
        state.events.append(frame)
        state.systems.append(messages[0]["content"])
        assert frame["source_kind"] == "host_receipt"
        if frame["current"]["state"] == "auip_step_pending":
            context.manager.auip_router.assert_not_awaited()
        else:
            receipt = context.manager.ingresses[context.session_id].receipts[state.turn_id]
            assert receipt["outcome"]["ok"] is True
            assert receipt["display_text"] == ""
        if state.fail == "expression":
            raise RuntimeError("role query unavailable")
        return state.reply

    def display(event):
        receipt = context.manager.ingresses[context.session_id].receipts[state.turn_id]
        assert receipt["outcome"]["ok"] is True
        if state.fail == "publication":
            raise RuntimeError("role publication unavailable")
        if state.fail == "declined":
            return False
        context.publications.append(event)
        return True

    async def voice(payload):
        context.spoken.append(payload)
        return {"status":"queued"}

    context.manager.query = AsyncMock(side_effect=query)
    context.manager.auip_decider = SimpleNamespace(capture=capture,
        render_read_only_answer=lambda _decision, **_kwargs:state.facts)
    context.manager.auip_router = AsyncMock(return_value={"ok":True, "deferred":True})
    context.manager.auip_entry_context = lambda _session:"成果ができたら既存の入口から開きます。"
    # Presentation tests receive a target already resolved by the Work owner.
    context.manager.resolve_after_work_targets = AsyncMock(return_value=(({
        "work_item_id":"work-current", "attempt_id":"attempt-current", "operation_id":"operation-current"},), ""))
    context.manager.active_work_for_recipient = lambda *_args:{"attempt_id":"attempt-current",
        "work_item_id":"work-current", "run_id":"run-current", "effect_id":"effect-current",
        "runtime_attached":True, "status":"running"} if state.action == "after_work" else None
    context.manager.publish_factory = lambda session:CooperativeHostDelivery(session_id=session,
        display=display, record_display=sm.append_session_message, narration_sink=voice)

    async def send():
        state.turn_id = "auip-" + state.action
        result = await context.handler.send_text("今の状況を教えて。", session_id=context.session_id,
            turn_id=state.turn_id, source="wake")
        assert result["status"] == "ok"
        await context.finish()
        return context.manager.ingresses[context.session_id].receipts.get(state.turn_id)

    return context, state, send


@pytest.mark.parametrize("action", ["read", "step", "leave", "after_work"])
async def test_accepted_auip_uses_one_natural_role_display_voice_and_history(auip_host, action):
    context, state, send = auip_host
    state.action = action
    requested_app_context = []
    if action == "read":
        ingress = await context.manager._ingress_for(context.session_id)
        ingress.loop.role_app_context = lambda app_session_id: (
            requested_app_context.append(app_session_id)
            or "[Current application]\nstate=active\n[AUIP Interaction Briefing]\nplace_black is available"
        )
    receipt = await send()
    assert receipt["state"] == {"read":"auip_read", "step":"auip_applied",
        "leave":"auip_applied", "after_work":"auip_after_work_deferred"}[action]
    assert receipt["display_text"] == state.reply
    assert context.manager.query.await_count == 1
    assert context.manager.auip_router.await_count == (0 if action == "read" else 1)
    assert len(context.publications) == len(context.spoken) == 1
    assert context.publications[0]["text"] == context.spoken[0]["display_text"] == state.reply
    history, _ = sm._read_session_history(context.session_id)
    assert [(row["role"], row["content"]) for row in history.dialog] == [
        ("user", "今の状況を教えて。"), ("assistant", state.reply)]
    loop_history = context.manager.ingresses[context.session_id].loop.history
    assert [row["source"] for row in loop_history] == ["user", "kurisu"]
    if action == "read":
        assert state.events[0]["current"]["question"] == "今の状況を教えて。"
        assert state.events[0]["current"]["facts"] == state.facts
        assert state.events[0]["current"]["app_session_id"] == "app-current"
        assert state.events[0]["app_context"] == (
            "[Current application]\nstate=active\n"
            "[AUIP Interaction Briefing]\nplace_black is available")
        assert requested_app_context == ["app-current"]
        assert state.facts != receipt["display_text"]
        assert "state=auip_read" in state.systems[0]
        assert "確認をすでに完了" in state.systems[0]
        assert "自然なドメイン表現" in state.systems[0]
        assert "Host・Controller・schema" in state.systems[0]
        assert state.facts not in state.systems[0]
    if action == "step":
        assert context.manager.auip_router.await_args.args[0]["_host_current_role_response"] == state.reply


@pytest.mark.parametrize(("action", "outcome", "presented"), [
    ("observe", {
        "ok":True, "changed":True, "app_session_id":"app-current",
        "app":{"id":"gomoku", "title":"Gomoku", "objective":"old history"},
        "status":"active", "stance":"spectator", "engagement_mode":"observe",
        "surface_close_status":"not_requested",
        "controller":{"status":"stopping", "lease":{"lease_id":"private"}, "reason":"mode_change"},
        "latest_verified_self_action":{"type":"gomoku.place_black",
            "effects":{"move":{"result":"pending_cpu"}}},
        "state":{"turn":"black"},
    }, {
        "ok":True, "changed":True, "app_session_id":"app-current",
        "app":{"id":"gomoku", "title":"Gomoku"},
        "status":"active", "stance":"spectator", "engagement_mode":"observe",
        "surface_close_status":"not_requested",
        "controller":{"status":"stopping", "reason":"mode_change"},
    }),
    ("leave", {
        "ok":True, "external_process_stopped":False, "host_surface_closed":False,
        "app_session_id":"app-current", "app":{"id":"gomoku", "title":"Gomoku"},
        "status":"closed", "surface_close_status":"pending", "surface_close_detail":"",
        "latest_verified_self_action":{"type":"gomoku.place_black",
            "effects":{"move":{"result":"pending_cpu"}}},
        "experience_capsule":{"verified_self_actions":[{"effects":{"move":{"result":"pending_cpu"}}}]},
        "state":{"turn":"black"},
    }, {
        "ok":True,
        "app_session_id":"app-current", "app":{"id":"gomoku", "title":"Gomoku"},
        "status":"closed", "surface_close_status":"pending", "surface_close_detail":"",
    }),
    ("leave", {
        "ok":True, "external_process_stopped":False, "host_surface_closed":True,
        "app_session_id":"app-current", "app":{"id":"gomoku", "title":"Gomoku"},
        "status":"closed", "surface_close_status":"closed", "surface_close_detail":"",
        "latest_verified_self_action":{"effects":{"move":{"result":"pending_cpu"}}},
    }, {
        "ok":True,
        "app_session_id":"app-current", "app":{"id":"gomoku", "title":"Gomoku"},
        "status":"closed", "surface_close_status":"closed", "surface_close_detail":"",
    }),
])
async def test_mode_and_leave_expression_uses_only_transition_current_facts(
        auip_host, action, outcome, presented):
    context, state, send = auip_host
    state.action = action
    ingress = await context.manager._ingress_for(context.session_id)
    current = dict(presented)
    if action == "observe":
        current["controller"] = {"status":"idle", "reason":""}
    elif action == "leave":
        current["host_surface_closed"] = True
        current["surface_close_status"] = "closed"
    current_context = ("[Current AUIP transition state]\n"
        + json.dumps(current, ensure_ascii=False, separators=(",", ":"))
        + "\n[/Current AUIP transition state]")
    requested_app_context = []

    def role_app_context(app_session_id, *, transition_action=""):
        requested_app_context.append((app_session_id, transition_action))
        return current_context

    ingress.loop.role_app_context = role_app_context
    context.manager.auip_router.return_value = outcome

    receipt = await send()

    assert receipt["state"] == "auip_applied"
    assert receipt["outcome"] == outcome
    frame, = state.events
    assert frame["current"] == {"source":"host_receipt", "state":"auip_applied",
        "action":action, "question":"今の状況を教えて。", "outcome":presented}
    assert frame["app_context"] == current_context
    assert requested_app_context == [("app-current", action)]
    assert "latest_verified_self_action" not in frame["current"]["outcome"]
    assert "experience_capsule" not in frame["current"]["outcome"]
    assert "state" not in frame["current"]["outcome"]
    assert "external_process_stopped" not in frame["current"]["outcome"]
    assert "host_surface_closed" not in frame["current"]["outcome"]
    assert "pending_cpu" not in json.dumps(frame, ensure_ascii=False)
    assert context.manager.query.await_count == 1
    assert context.manager.auip_router.await_count == 1


@pytest.mark.parametrize("outcome", [
    {"ok":False, "error":"mode_rejected", "reason":"unsupported"},
    {"ok":False, "uncertain":True, "error":"mode_result_unknown"},
])
async def test_mode_rejection_and_uncertainty_keep_exact_facts_without_old_receipt(
        auip_host, outcome):
    context, state, send = auip_host
    state.action = "observe"
    context.manager.auip_router.return_value = {
        **outcome,
        "app_session_id":"app-current",
        "latest_verified_self_action":{"effects":{"move":{"result":"pending_cpu"}}},
    }

    receipt = await send()

    assert receipt["state"] == "auip_rejected"
    assert receipt["outcome"]["latest_verified_self_action"]["effects"]["move"]["result"] == "pending_cpu"
    presented = state.events[0]["current"]["outcome"]
    assert presented == {**outcome, "app_session_id":"app-current"}
    assert "latest_verified_self_action" not in presented
    assert context.manager.query.await_count == 1
    assert context.manager.auip_router.await_count == 1


@pytest.mark.parametrize(("action", "failure"), [
    ("read", "expression"), ("leave", "expression"),
    ("read", "publication"), ("step", "publication"), ("leave", "publication"), ("after_work", "publication"),
])
async def test_postaccept_presentation_failure_preserves_receipt_and_never_reexecutes(auip_host, action, failure):
    context, state, send = auip_host
    state.action, state.fail = action, failure
    receipt = await send()
    assert receipt["outcome"]["ok"] is True and receipt["display_text"] == ""
    assert receipt["state"] == {"read":"auip_read", "step":"auip_applied",
        "leave":"auip_applied", "after_work":"auip_after_work_deferred"}[action]
    assert context.publications == context.spoken == []
    assert not any(method == Method.CHAT_ERROR for method, _ in context.visible)
    ingress = context.manager.ingresses[context.session_id]
    assert any(row["kind"] == "presentation_failed" for row in ingress.loop.trace)
    assert [row["source"] for row in ingress.loop.history] == ["user"]
    before = context.manager.auip_router.await_count
    replay = await context.handler.send_text("今の状況を教えて。", session_id=context.session_id,
        turn_id=state.turn_id, source="wake")
    assert replay["status"] == "replayed"
    assert context.manager.auip_router.await_count == before == (0 if action == "read" else 1)
    assert context.manager.query.await_count == 1


async def test_unpublished_auip_role_text_does_not_bypass_publisher_in_chat_completion(auip_host):
    context, state, send = auip_host
    state.fail = "declined"
    receipt = await send()
    assert receipt["state"] == "auip_read" and receipt["outcome"]["ok"] is True
    assert receipt["display_text"] == ""
    assert context.publications == context.spoken == []
    assert all(not payload.get("full_text") for method, payload in context.visible if method == Method.CHAT_COMPLETE)
    assert context.manager.query.await_count == 1


async def test_step_prospective_expression_failure_does_not_execute(auip_host):
    context, state, send = auip_host
    state.action, state.fail = "step", "expression"
    assert await send() is None
    context.manager.auip_router.assert_not_awaited()
    assert context.publications == context.spoken == []
    assert any(method == Method.CHAT_ERROR for method, _ in context.visible)
    assert context.manager.query.await_count == 1
