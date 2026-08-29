from __future__ import annotations

import json

from server.auip_structured_presentation import (
    bounded_user_context,
    build_structured_presentation_payload,
    compile_auip_decision_context,
    compile_auip_host_facts,
    compile_auip_operator_fact,
    parse_structured_presentation_decision,
    semantic_commentary_facts,
)


def _terminal_observation() -> dict:
    return {
        "app": {"id": "board", "title": "Board"},
        "revision": 18,
        "state": {
            "winner": "black",
            "roleBindings": {"participant": "black", "user": "white"},
            "board": {"rows": ["B" * 15 for _ in range(15)]},
        },
        "event": {
            "event_id": "finish-1",
            "type": "game.finished",
            "actor": "app",
            "revision": 18,
            "payload": {"winner": "black"},
            "importance": "important",
            "terminal": True,
            "controller_effect": False,
        },
        "latest_verified_self_action": None,
    }


def test_terminal_compiler_preserves_owner_and_omits_dense_board() -> None:
    facts = compile_auip_host_facts(_terminal_observation())

    assert len(facts) == 1
    assert facts[0]["fact_id"] == "event:finish-1"
    assert facts[0]["actor"] == {"reported": "app", "verified": "application"}
    assert facts[0]["outcome"] == {
        "winner_side": "black",
        "winner_owner": "kurisu",
        "loser_owner": "user",
        "method": "unknown",
    }
    assert "event.state.board" in facts[0]["omitted_fields"]
    assert "BBBB" not in json.dumps(facts, ensure_ascii=False)


def test_kurisu_actor_requires_a_correlated_accepted_receipt() -> None:
    observation = _terminal_observation()
    observation["event"] = {
        **observation["event"],
        "event_id": "move-1",
        "type": "game.move",
        "actor": "kurisu",
        "terminal": False,
        "payload": {"position": 7},
    }
    observation["latest_verified_self_action"] = {
        "action_id": "action-1",
        "type": "game.move",
        "payload": {"position": 7},
        "accepted": True,
        "resulting_revision": 18,
        "effects": {"placed": 7},
    }
    facts = compile_auip_host_facts(observation)
    assert facts[0]["authority"] == "accepted_action_receipt"
    assert facts[0]["actor"]["verified"] == "kurisu"
    assert facts[1]["actor"]["verified"] == "kurisu"

    observation["latest_verified_self_action"]["resulting_revision"] = 17
    uncorrelated = compile_auip_host_facts(observation)
    assert len(uncorrelated) == 1
    assert uncorrelated[0]["actor"]["verified"] == "unknown"


def test_receipt_bound_role_reason_is_presentation_context_not_scene_fact() -> None:
    observation = _terminal_observation()
    observation["state"]["turn"] = "white"
    observation["event"] = {
        **observation["event"],
        "event_id": "move-with-intent",
        "type": "game.move",
        "actor": "kurisu",
        "terminal": False,
        "caused_by_action_id": "action-intent",
        "payload": {"x": 7, "y": 8, "moveCount": 12},
    }
    observation["latest_verified_self_action"] = {
        "action_id": "action-intent",
        "type": "game.move",
        "payload": {"x": 7, "y": 8},
        "accepted": True,
        "resulting_revision": 18,
        "effects": {"placed": {"x": 7, "y": 8}, "moveCount": 12},
        "decision_context": {
            "kind": "automatic_role_choice",
            "reason": "Hold the center and keep two continuations open.",
            "instruction_relation": "not_applicable",
        },
    }

    facts = compile_auip_host_facts(observation)
    context = compile_auip_decision_context(observation)
    payload = build_structured_presentation_payload(
        facts=facts,
        app=observation["app"],
        recent_messages=[],
        recent_delivered_narrations=[],
        profile_id="game",
        display_language="japanese",
        presentation_required=False,
        host_reason_code="commentary_due",
        decision_context=context,
    )

    assert context == {
        "status": "accepted_action_bound",
        "kind": "automatic_role_choice",
        "reason": "Hold the center and keep two continuations open.",
        "instruction_relation": "not_applicable",
    }
    assert payload["decision_context"] == context
    encoded_facts = json.dumps(facts, ensure_ascii=False)
    assert "Hold the center" not in encoded_facts
    assert '"x"' not in encoded_facts
    assert '"y"' not in encoded_facts
    assert '"moveCount":' not in encoded_facts
    assert '"turn":' not in encoded_facts

    observation["event"]["revision"] = 19
    assert compile_auip_decision_context(observation) == {}


def test_periodic_semantic_commentary_sees_receipt_proof_without_action_telemetry() -> None:
    observation = _terminal_observation()
    observation["event"] = {
        **observation["event"],
        "event_id": "merged-tile",
        "type": "game.tiles_merged",
        "actor": "kurisu",
        "terminal": False,
        "importance": "normal",
        "caused_by_action_id": "action-123",
        "payload": {"direction": "left", "createdTile": 512, "scoreDelta": 512},
    }
    observation["latest_verified_self_action"] = {
        "action_id": "action-123",
        "type": "game.slide",
        "payload": {"direction": "left"},
        "accepted": True,
        "resulting_revision": 18,
        "effects": {"createdTile": 512, "score": 4096},
    }

    facts = semantic_commentary_facts(compile_auip_host_facts(observation))

    assert len(facts) == 1
    assert facts[0]["authority"] == "accepted_action_receipt"
    assert facts[0]["details"] == {"accepted": True}
    encoded = json.dumps(facts, ensure_ascii=False)
    assert "left" not in encoded
    assert "512" not in encoded
    assert "4096" not in encoded


def test_structured_payload_keeps_only_latest_user_topic() -> None:
    facts = compile_auip_host_facts(_terminal_observation())
    payload = build_structured_presentation_payload(
        facts=facts,
        app={"id": "board", "title": "Board", "interactionSummary": "Untrusted examples"},
        recent_messages=[
            {"role": "user", "content": "这一盘谁赢了？"},
            {"role": "assistant", "content": "I won because of a diagonal."},
        ],
        recent_delivered_narrations=[],
        profile_id="game",
        display_language="japanese",
        presentation_required=True,
        host_reason_code="terminal",
    )

    assert payload["conversation_context"] == {
        "source_role": "user",
        "latest_user_topic": "这一盘谁赢了？",
    }
    assert payload["omitted_non_user_conversation"] is True
    assert "I won" not in json.dumps(payload, ensure_ascii=False)
    assert bounded_user_context([]) == {}


def test_decision_rejects_unknown_fact_and_host_promotes_mandatory_ids() -> None:
    facts = compile_auip_host_facts(_terminal_observation())
    forged = parse_structured_presentation_decision(
        {
            "action": "speak",
            "selected_fact_ids": ["event:forged"],
            "display_text": "終わったわ。",
            "emotion": "calm",
            "reason_code": "terminal",
        },
        facts=facts,
        presentation_required=False,
        max_spoken_chars=96,
    )
    assert forged.valid is False
    assert forged.error == "unknown_selected_fact_id"

    mandatory = parse_structured_presentation_decision(
        {
            "action": "speak",
            "selected_fact_ids": [],
            "display_text": "黒の勝ちで終わったわ。",
            "emotion": "confident",
            "reason_code": "terminal",
        },
        facts=facts,
        presentation_required=True,
        max_spoken_chars=96,
    )
    assert mandatory.valid is True
    assert mandatory.selected_fact_ids == ("event:finish-1",)


def test_operator_fact_keeps_fault_on_the_participant_lane() -> None:
    facts = compile_auip_operator_fact(
        app_session_id="app-1",
        outcome_id="outcome-1",
        revision=4,
        reason="the requested action is not currently legal",
    )

    assert facts[0]["authority"] == "host_operator_outcome"
    assert facts[0]["actor"]["verified"] == "kurisu"
    assert facts[0]["outcome"] == {
        "accepted": False,
        "performed": False,
        "user_at_fault": False,
    }


def test_nested_scalar_projection_preserves_values_at_the_container_depth_limit() -> None:
    observation = _terminal_observation()
    observation["event"] = {
        **observation["event"],
        "event_id": "warning-1",
        "type": "reactor.warning",
        "terminal": False,
        "payload": {"metric": "temperature", "trend": "rising", "safeMaximum": 80},
    }
    observation["state"] = {
        "metrics": {
            "temperature": {
                "value": 89,
                "unit": "C",
                "trend": "rising",
                "safeMaximum": 80,
                "safe": [60, 80],
            }
        }
    }

    facts = compile_auip_host_facts(observation)
    metric = facts[0]["details"]["state"]["metrics"]["temperature"]

    assert metric == {
        "safeMaximum": 80,
        "safe": [60, 80],
        "trend": "rising",
        "unit": "C",
        "value": 89,
    }
    assert not any("temperature.value" in item for item in facts[0]["omitted_fields"])
