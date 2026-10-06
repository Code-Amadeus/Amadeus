"""Deterministic evidence checks for the shipping Cooperative J7 instrument."""

from argparse import Namespace
from copy import deepcopy
import json

import pytest

from tools.e2e_live_product_journey import (
    ROOT, TurnEvidence, _experience_history_evidence, _parser as product_parser,
    _b2_foreground_acceptance, _persisted_turn_dialog, _run,
)
from tools.e2e_real_work_conversation import EventRecord
from tools.run_semantic_journeys import _auip_experience
from server.auip_role_branch_experiment import AppSessionRoleBranch
from server.auip_runtime import AuipRuntime


def _facts():
    branch = AppSessionRoleBranch(app_session_id="app-one", app_title="Gomoku")
    branch.record_user("结束系列。")
    branch.record_receipt(accepted=True, action_type="game.finish_experience",
        payload={}, resulting_revision=21)
    capsule = {"app_session_id": "app-one", "verified_self_actions": [],
        "role_branch": branch.collapse(close_status="completed",
            terminal={"type": "game.experience_finished"})}
    session = {"app_session_id": "app-one", "status": "closed", "experience_capsule": capsule}
    events = [EventRecord(1.0, "auip.updated", {"app_session_id": "app-one",
        "receipt": {"accepted": True, "type": "game.finish_experience",
            "payload": {}, "resulting_revision": 21}})]
    turn = TurnEvidence("post_leave_chat", "聊聊今天。", 1,
        turn_id="after-leave", session_id="chat-one", reply="落ち着いた一日ね。")
    dialog = [{"role": "user", "turn_id": turn.turn_id, "content": turn.text},
        {"role": "assistant", "turn_id": turn.turn_id, "content": turn.reply}]
    return dict(app_session_id="app-one", session=session, events=events,
        post_leave_turn=turn, dialog=dialog)


def test_j7_keeps_actual_receipt_and_persisted_chat_evidence_without_raw_history_dump():
    evidence = _experience_history_evidence(**_facts())
    assert all(evidence["checks"].values())
    assert evidence["dialogue_rows"] == evidence["verified_action_rows"] == 1
    assert evidence["post_leave_turn_id"] == "after-leave"
    assert "dialog" not in evidence and "experience_capsule" not in evidence


def test_real_host_capsule_bounds_match_the_journey_instrument():
    branch = AppSessionRoleBranch(app_session_id="app-one", app_title="Gomoku")
    facts = _facts()
    for index in range(20):
        branch.record_strategy_directive(f"Policy {index}")
        branch.record_user("Dialogue " + str(index))
        branch.record_receipt(accepted=True, action_type="game.place_stone",
            payload={"x": index % 9, "y": index // 9}, resulting_revision=index + 1)
        facts["events"].append(EventRecord(float(index), "auip.updated", {
            "app_session_id": "app-one", "receipt": {"accepted": True,
                "type": "game.place_stone", "payload": {"x": index % 9, "y": index // 9},
                "resulting_revision": index + 1}}))
    produced = branch.collapse(close_status="completed", terminal={"type": "game.experience_finished"})
    facts["session"]["experience_capsule"]["role_branch"] = produced
    assert len(produced["dialogue_tail"]) == len(produced["verified_actions"]) == 4
    assert len(produced["strategy_directives"]) == 3
    assert sum(len(row["content"]) for row in produced["dialogue_tail"]) <= branch.max_chars
    assert all(_experience_history_evidence(**facts)["checks"].values())


def test_host_leave_preserves_the_completed_role_capsule_after_experience_terminal():
    runtime = AuipRuntime(role_branch_mode="b2")
    registered = runtime.register(conversation_id="chat-one", manifest=json.loads(
        (ROOT / "examples" / "auip-gomoku" / "auip.manifest.json").read_text(encoding="utf-8")))
    identity = dict(app_session_id=registered["app_session_id"], bridge_token=registered["bridge_token"])
    terminal = runtime.publish_event(**identity, event_id="finished", type="game.experience_finished",
        actor="app", revision=0, payload={"winner": "black"})
    assert terminal["status"] == "completed"
    collapsed = terminal["experience_capsule"]["role_branch"]
    left = runtime.close(**identity, reason="user_left")
    assert left["status"] == "closed"
    assert left["experience_capsule"]["role_branch"] == collapsed
    assert collapsed["close_status"] == "completed"


def test_history_reader_checks_the_current_persistence_and_visible_projection_contract(tmp_path, monkeypatch):
    from core import session_manager as sm
    from core.chat_history_projection import project_completed_role_history

    run_root = tmp_path / "run"
    monkeypatch.setattr(sm, "_SESSION_DIR", str(run_root / "state" / "sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    facts = _facts()
    turn = facts["post_leave_turn"]
    sm.create_session(turn.session_id)
    assert sm.append_session_message(turn.session_id, role="user", content=turn.text, turn_id=turn.turn_id)
    assert sm.append_session_message(turn.session_id, role="assistant", turn_id=turn.turn_id,
        content=project_completed_role_history("[EMO thinking]" + turn.reply))
    facts["dialog"] = _persisted_turn_dialog(run_root, turn.session_id)
    assert all(_experience_history_evidence(**facts)["checks"].values())


@pytest.mark.parametrize("change", ["missing", "other_app", "active", "wrong_terminal"])
def test_j7_cannot_score_missing_or_uncollapsed_foreign_experience(change):
    facts = _facts()
    role = facts["session"]["experience_capsule"]["role_branch"]
    if change == "missing":
        facts["session"]["experience_capsule"] = None
    elif change == "other_app":
        role["app_session_id"] = "app-two"
    elif change == "active":
        role["close_status"] = "active"
    else:
        role["terminal"] = {"type": "game.round_finished"}
    assert not all(_experience_history_evidence(**facts)["checks"].values())


@pytest.mark.parametrize("change", ["other_app", "rejected", "revision", "payload", "absent"])
def test_capsule_claim_needs_the_same_accepted_application_receipt(change):
    facts = _facts()
    event = facts["events"][0]
    if change == "other_app":
        event.params["app_session_id"] = "app-two"
    elif change == "rejected":
        event.params["receipt"]["accepted"] = False
    elif change == "revision":
        event.params["receipt"]["resulting_revision"] = 20
    elif change == "payload":
        event.params["receipt"]["payload"] = {"unrelated": True}
    else:
        facts["events"] = []
    assert _experience_history_evidence(**facts)["checks"][
        "experience_capsule_actions_match_accepted_app_receipts"] is False


@pytest.mark.parametrize("field,oversized", [
    ("dialogue_tail", [{"role": "user", "content": "x"}] * 5),
    ("dialogue_tail", [{"role": "assistant", "content": "x" * 2601}]),
    ("strategy_directives", ["x"] * 4),
    ("verified_actions", [{"accepted": True}] * 5),
])
def test_capsule_must_keep_the_existing_host_history_bounds(field, oversized):
    facts = _facts()
    facts["session"]["experience_capsule"]["role_branch"][field] = deepcopy(oversized)
    assert not all(_experience_history_evidence(**facts)["checks"].values())


def test_missing_revision_on_both_sides_cannot_link_a_capsule_claim():
    facts = _facts()
    facts["session"]["experience_capsule"]["role_branch"]["verified_actions"][0].pop("resulting_revision")
    facts["events"][0].params["receipt"].pop("resulting_revision")
    assert _experience_history_evidence(**facts)["checks"][
        "experience_capsule_actions_match_accepted_app_receipts"] is False


def test_truthful_rejected_receipt_does_not_erase_verified_participation():
    facts = _facts()
    facts["session"]["experience_capsule"]["role_branch"]["verified_actions"].append(
        {"accepted": False, "action_type": "game.place_stone", "payload": {"x": 1, "y": 1},
            "resulting_revision": 20})
    facts["events"].append(EventRecord(0.5, "auip.updated", {"app_session_id": "app-one",
        "receipt": {"accepted": False, "type": "game.place_stone", "payload": {"x": 1, "y": 1},
            "resulting_revision": 20}}))
    assert all(_experience_history_evidence(**facts)["checks"].values())


@pytest.mark.parametrize("change", ["missing", "different_turn", "different_reply", "missing_user"])
def test_visible_reply_does_not_prove_persisted_post_leave_history(change):
    facts = _facts()
    if change == "missing":
        facts["dialog"] = []
    elif change == "different_turn":
        facts["dialog"][1]["turn_id"] = "other-turn"
    elif change == "different_reply":
        facts["dialog"][1]["content"] = "Other reply"
    else:
        facts["dialog"] = facts["dialog"][1:]
    assert _experience_history_evidence(**facts)["checks"][
        "post_leave_turn_retained_in_session_history"] is False


@pytest.mark.parametrize("route", ["basic", "professional", "inherit"])
def test_registry_runs_the_mature_shipping_driver_with_all_j7_stages(tmp_path, route):
    command = _auip_experience(Namespace(chat_route=route, provider="deepseek", model="test-model"), tmp_path)
    args = product_parser().parse_args(command[5:])
    assert args.chat_route == route and args.journey_layer == "interaction"
    assert args.scenario == "gomoku" and args.engagement_mode == "collaborate"
    assert args.require_b2 and args.complete_gomoku_round and args.exercise_gomoku_post_round
    assert args.exercise_post_leave_chat and args.require_experience_history
    assert args.no_tts is False


async def test_history_requirement_cannot_skip_the_experience_lifecycle():
    args = product_parser().parse_args(["--require-experience-history"])
    with pytest.raises(ValueError, match="requires the Gomoku post-round lifecycle"):
        await _run(args)


def _foreground_facts():
    turn = TurnEvidence("step", "You play first.", 0, turn_id="turn-one",
        session_id="chat-one", reply="I placed one stone.")
    requested = EventRecord(1, "auip.action.requested", {"app_session_id": "app-one",
        "candidate_id": "candidate-one", "decision_path": "b2", "action": {
            "action_id": "action-one", "proposal_id": "b2f:r1:candidate-one",
            "type": "game.place_stone", "payload": {"x": 4, "y": 4}}})
    receipt = EventRecord(2, "auip.updated", {"app_session_id": "app-one", "receipt": {
        **requested.params["action"], "accepted": True, "resolved_at": 100.0}})
    narration = {"event_id": "action-one", "text": turn.reply, "delivered_at": 101.0}
    events = [EventRecord(0, "chat.token", {"turn_id": turn.turn_id, "token": "All right."}),
        requested, receipt,
        EventRecord(3, "auip.updated", {"app_session_id": "app-one", "latest_delivered_narration": narration}),
        EventRecord(4, "chat.complete", {"turn_id": turn.turn_id, "session_id": turn.session_id,
            "full_text": turn.reply})]
    return dict(events=events, turn=turn, app_session_id="app-one",
        requested=requested, receipt_event=receipt, latest_narration=narration)


def test_b2_result_follows_accepted_action_while_generic_acknowledgement_can_be_early():
    # Generic CHAT_COMPLETE has no action/candidate metadata in Cooperative Chat.
    assert all(_b2_foreground_acceptance(**_foreground_facts()).values())


@pytest.mark.parametrize("method", ["chat.role_message", "chat.token"])
def test_action_specific_result_cannot_be_visible_before_its_receipt(method):
    facts = _foreground_facts()
    turn = facts["turn"]
    payload = {"turn_id": turn.turn_id, "session_id": turn.session_id,
        "text" if method == "chat.role_message" else "token": turn.reply}
    facts["events"].insert(1, EventRecord(0.5, method, payload))
    assert _b2_foreground_acceptance(**facts)["b2_receipt_precedes_visible_chat"] is False


@pytest.mark.parametrize("field", ["app_session_id", "action_id", "proposal_id", "payload", "accepted"])
def test_b2_result_needs_the_exact_accepted_application_action_receipt(field):
    facts = _foreground_facts()
    receipt = facts["receipt_event"].params
    if field == "app_session_id":
        receipt[field] = "app-two"
    elif field == "payload":
        receipt["receipt"][field] = {"x": 1, "y": 1}
    elif field == "accepted":
        receipt["receipt"][field] = False
    else:
        receipt["receipt"][field] = "unrelated"
    checks = _b2_foreground_acceptance(**facts)
    assert checks["b2_candidate_receipt_linked"] is False
    assert checks["b2_receipt_precedes_visible_chat"] is False


@pytest.mark.parametrize("change", ["other_action", "premature", "other_text"])
def test_b2_completion_needs_action_bound_delivery_facts(change):
    facts = _foreground_facts()
    narration = facts["latest_narration"]
    if change == "other_action":
        narration["event_id"] = "action-two"
    elif change == "premature":
        narration["delivered_at"] = 99.0
    else:
        narration["text"] = "Unrelated output."
    checks = _b2_foreground_acceptance(**facts)
    assert checks["b2_visible_delivery_recorded"] is False
    assert checks["b2_receipt_precedes_visible_chat"] is False


@pytest.mark.parametrize("change", ["missing_result", "other_turn"])
def test_recorded_narration_still_needs_this_turns_visible_result(change):
    facts = _foreground_facts()
    if change == "missing_result":
        facts["events"].pop()
    else:
        facts["events"][-1].params["turn_id"] = "other-turn"
    checks = _b2_foreground_acceptance(**facts)
    assert checks["b2_visible_delivery_recorded"] is True
    assert checks["b2_receipt_precedes_visible_chat"] is False
