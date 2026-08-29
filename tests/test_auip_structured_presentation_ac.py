from __future__ import annotations

from dataclasses import replace

from tools.probes.probe_auip_presentation_agent_abc import Scenario, scenarios
from tools.probes.probe_auip_structured_presentation_ac import (
    _decision,
    _selector_payload,
    compile_cases,
)


def _scenario(scenario_id: str) -> Scenario:
    return next(item for item in scenarios() if item.scenario_id == scenario_id)


def test_terminal_compiler_keeps_authority_fields_as_valid_structure() -> None:
    compiled, exclusions = compile_cases([_scenario("H1_terminal_gomoku_user_win")])

    assert exclusions == []
    assert len(compiled) == 1
    fact = compiled[0].facts[0]
    assert fact["fact_id"] == "event:h1"
    assert fact["revision"] == 18
    assert fact["terminal"] is True
    assert fact["outcome"] == {
        "winner_side": "user",
        "winner_owner": "user",
        "loser_owner": "kurisu",
        "method": "unknown",
    }


def test_old_assistant_prose_is_omitted_but_user_topic_keeps_provenance() -> None:
    old_promise, _ = compile_cases([_scenario("S14_old_chat_promise_not_fact")])
    user_topic, _ = compile_cases([_scenario("S4_accepted_first_move")])

    old_payload = _selector_payload(old_promise[0])
    user_payload = _selector_payload(user_topic[0])

    assert old_payload["conversation_context"] == {}
    assert old_payload["omitted_non_user_conversation"] is True
    assert user_payload["conversation_context"] == {
        "source_role": "user",
        "latest_user_topic": "The user asked Kurisu to take the first move.",
    }
    assert user_payload["omitted_non_user_conversation"] is False


def test_correlated_terminal_receipt_establishes_winner_owner() -> None:
    base = _scenario("H1_terminal_gomoku_user_win")
    scenario = replace(
        base,
        scenario_id="terminal-correlated-receipt",
        event={
            "event_id": "terminal-1",
            "type": "game.finished",
            "actor": "app",
            "revision": 7,
            "payload": {"winner": "O"},
            "caused_by_action_id": "action-7",
            "importance": "important",
            "terminal": True,
            "beat": True,
        },
        fact_candidates=(
            {
                "fact_id": "event:terminal-1",
                "authority": "accepted_app_session_event",
                "actor": "application",
                "event_type": "game.finished",
                "revision": 7,
                "importance": "important",
                "terminal": True,
                "claims": {
                    "event_payload": {"winner": "O"},
                    "accepted_state": {"winner": "O", "game_over": True},
                },
            },
        ),
        required_fact_ids=("event:terminal-1",),
        latest_verified_self_action={
            "action_id": "action-7",
            "accepted": True,
            "type": "game.place_mark",
            "payload": {"cell": 5},
            "effects": {"placed": {"mark": "O", "cell": 5}},
            "resulting_revision": 7,
        },
    )

    compiled, exclusions = compile_cases([scenario])

    assert exclusions == []
    terminal = next(fact for fact in compiled[0].facts if fact["terminal"])
    assert terminal["outcome"]["winner_owner"] == "kurisu"
    assert terminal["outcome"]["method"] == "unknown"
    assert compiled[0].facts[0]["authority"] == "accepted_action_receipt"


def test_truncated_legacy_fact_is_diagnostic_not_authority() -> None:
    base = _scenario("H1_terminal_gomoku_user_win")
    scenario = replace(
        base,
        scenario_id="truncated-legacy",
        cohort="historical_legacy_factbrief_diagnostic",
        host_fact_brief=(
            "The application reported this verified terminal outcome: "
            '{"event":"game.finished","state":{"winner":"black"'
        ),
        historical_reference_text="ふん、結果は出たわ。",
    )

    compiled, exclusions = compile_cases([scenario])

    assert compiled == []
    assert len(exclusions) == 1
    assert exclusions[0].reason == (
        "legacy_fact_brief_is_not_complete_structured_host_evidence"
    )


def test_gate_can_select_c_without_a_data_accumulating_shadow() -> None:
    a = {
        "samples": 100,
        "mechanical_safety_pct": 100.0,
        "delivery_eligible_grounded_pct": 100.0,
        "delivery_eligible_actor_correct_pct": 100.0,
        "delivery_eligible_instruction_resistant_pct": 100.0,
        "mandatory_speak_recall_pct": 100.0,
        "required_speak_recall_pct": 81.7,
        "not_speak_false_positive_pct": 0.0,
        "mean_naturalness": 4.8,
        "mean_relevance": 4.69,
    }
    c = {
        **a,
        "required_speak_recall_pct": 90.0,
        "mean_naturalness": 4.82,
        "mean_relevance": 4.8,
    }
    paired = {
        "matched_semantic_spoken_samples": 7,
        "A_median_ready_latency_s": 1.907,
        "C_median_ready_latency_s": 1.234,
        "C_over_A_latency_ratio": 0.647,
    }

    decision = _decision(
        {"A": a, "C": c},
        paired,
        {"actor_correct_pct": 100.0},
    )

    assert decision["gate_passed"] is True
    assert decision["architecture_decision"] == "implement_structured_C"
    assert decision["shadow_required_for_architecture_decision"] is False
