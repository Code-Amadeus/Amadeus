from __future__ import annotations

import json

from tools.probes.probe_auip_presentation_agent_abc import (
    ArmResult,
    _gate_decision,
    _paired_metrics,
    _score_result,
    load_historical_scenarios,
    scenarios,
)


def _scenario(scenario_id: str):
    return next(item for item in scenarios() if item.scenario_id == scenario_id)


def _result(scenario_id: str, *, arm: str, origin: str, selected: list[str]):
    scenario = _scenario(scenario_id)
    return ArmResult(
        scenario_id=scenario.scenario_id,
        cohort=scenario.cohort,
        category=scenario.category,
        route=scenario.route,
        repeat=1,
        arm=arm,
        origin=origin,
        expected_action=scenario.expected_action,
        mandatory_speech=scenario.mandatory_speech,
        replacement_window_ms=scenario.replacement_window_ms,
        action="speak",
        selected_fact_ids=selected,
        fact_brief=scenario.host_fact_brief,
        display_text="ふん、結果は確認できたわ。",
        emotion="thinking",
        reason_code="consequence",
        schema_ok=True,
    )


def test_host_promoted_c_does_not_require_model_selected_fact_ids() -> None:
    scenario = _scenario("H5_commentary_debt")
    result = _result(
        scenario.scenario_id,
        arm="C",
        origin="host_promoted_mandatory_narrator",
        selected=[],
    )

    scored = _score_result(result, scenario)

    assert scored.selected_fact_ids_ok is True
    assert scored.mechanical_safety_ok is True


def test_integrated_line_may_select_the_event_without_an_unused_receipt() -> None:
    scenario = _scenario("S8_2048_created_512")
    result = _result(
        scenario.scenario_id,
        arm="B",
        origin="integrated_all_admitted",
        selected=["event:s8"],
    )

    scored = _score_result(result, scenario)

    assert scored.selected_fact_ids_ok is True


def test_convergence_can_pass_while_shared_fact_projection_blocks_production() -> None:
    baseline = {
        "samples": 80,
        "mechanical_safety_pct": 100.0,
        "delivery_eligible_grounded_pct": 100.0,
        "delivery_eligible_actor_correct_pct": 100.0,
        "delivery_eligible_instruction_resistant_pct": 100.0,
        "mandatory_speak_recall_pct": 100.0,
        "required_speak_recall_pct": 60.0,
        "not_speak_false_positive_pct": 0.0,
        "mean_naturalness": 4.5,
        "mean_relevance": 4.5,
    }
    candidate = {
        **baseline,
        "required_speak_recall_pct": 90.0,
        "mean_naturalness": 4.7,
        "mean_relevance": 4.7,
    }
    summary = {"A": baseline, "B": candidate, "C": candidate}
    paired = {
        "matched_semantic_spoken_samples": 12,
        "A_median_ready_latency_s": 2.0,
        "C_median_ready_latency_s": 1.0,
        "C_over_A_latency_ratio": 0.5,
    }
    diagnostic = {
        "A": {"delivery_eligible_actor_correct_pct": 87.5},
        "B": {"delivery_eligible_actor_correct_pct": 95.0},
        "C": {
            "delivery_eligible_grounded_pct": 90.0,
            "delivery_eligible_actor_correct_pct": 87.5,
        },
    }

    decision = _gate_decision(summary, paired, diagnostic)

    assert decision["convergence_gate_passed"] is True
    assert decision["stage_0_gate_passed"] is False
    assert decision["selected_candidate_shape"] == "C"
    assert decision["recommended_next_step"] == (
        "repair_shared_fact_projection_then_shadow_C"
    )


def test_historical_full_flow_keeps_revision_window_and_receipt(tmp_path) -> None:
    report = tmp_path / "run.json"
    report.write_text(
        json.dumps(
            {
                "ok": True,
                "delivered_narration": ["終わったわ。"],
                "auip_events": [
                    {
                        "params": {
                            "app": {"id": "sample", "title": "Sample"},
                            "state": {"turn": "user"},
                            "event": {
                                "event_id": "move-1",
                                "type": "game.move_committed",
                                "actor": "kurisu",
                                "revision": 2,
                                "payload": {"position": "A1"},
                                "observed_at": 10.0,
                                "beat": True,
                                "importance": "normal",
                                "terminal": False,
                            },
                            "latest_verified_self_action": {
                                "accepted": True,
                                "type": "game.place",
                                "payload": {"position": "A1"},
                                "effects": {"placed": "A1"},
                                "resulting_revision": 2,
                            },
                        }
                    },
                    {
                        "params": {
                            "app": {"id": "sample", "title": "Sample"},
                            "state": {"winner": "kurisu"},
                            "event": {
                                "event_id": "terminal-1",
                                "type": "game.finished",
                                "actor": "app",
                                "revision": 2,
                                "payload": {"winner": "kurisu"},
                                "observed_at": 10.05,
                                "beat": True,
                                "importance": "important",
                                "terminal": True,
                            },
                            "latest_verified_self_action": {
                                "accepted": True,
                                "type": "game.place",
                                "payload": {"position": "A1"},
                                "effects": {"placed": "A1"},
                                "resulting_revision": 2,
                            },
                        }
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    loaded, evidence = load_historical_scenarios([str(report)], trace_limit=0)

    assert len(loaded) == 2
    assert loaded[0].cohort == "historical_full_flow"
    assert abs(float(loaded[0].replacement_window_ms or 0.0) - 50.0) < 0.001
    assert loaded[0].required_fact_ids[0].startswith("historical-receipt:")
    assert loaded[1].mandatory_speech is True
    assert evidence[0]["sha256"]


def test_paired_latency_uses_only_same_sample_spoken_pairs() -> None:
    scenario = _scenario("S3_gomoku_double_threat")
    a = _score_result(
        _result(scenario.scenario_id, arm="A", origin="shipping", selected=[]),
        scenario,
    )
    c = _score_result(
        _result(
            scenario.scenario_id,
            arm="C",
            origin="integrated",
            selected=["event:s3"],
        ),
        scenario,
    )
    a.calls = []
    c.calls = []

    metrics = _paired_metrics([a, c])

    assert metrics["matched_semantic_spoken_samples"] == 1
