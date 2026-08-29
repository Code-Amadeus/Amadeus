"""Canonical semantic Journey evidence does not overclaim test levels."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.semantic_journey_evidence import (
    EvidenceError,
    build_evidence,
    evidence_from_report,
    validate_evidence,
)
from tools.e2e_auip_semantic_journey import _checks as _auip_checks
from tools.semantic_release_gate import discover_evidence, evaluate_gate
from tools.run_semantic_journeys import _active_steer, _auip_experience


def _evidence(journey_id: str = "J3", *, level: str = "L3") -> dict:
    return build_evidence(
        root=ROOT,
        journey_id=journey_id,
        status="passed",
        test_level=level,
        provider="locus",
        model="test-model",
        report_path=ROOT / "runtime" / "test-report.json",
        isolation_root=ROOT / "runtime" / "isolated",
        checks={"same_work_item": True, "final_artifact_verified": True},
        started_at="2026-08-13T00:00:00+00:00",
        finished_at="2026-08-13T00:01:00+00:00",
        manual_acceptance="pending",
    )


def test_evidence_embeds_exact_code_identity_and_assertions() -> None:
    evidence = _evidence()
    assert evidence["code_identity"]["commit_sha"]
    assert len(evidence["code_identity"]["workspace_fingerprint"]) == 64
    assert evidence["failed_assertions"] == []
    assert evidence_from_report({"semantic_evidence": evidence}) == evidence
    print("ok: semantic evidence carries exact code identity and hard assertions")


def test_passed_evidence_cannot_hide_a_failed_assertion() -> None:
    evidence = _evidence()
    evidence["hard_assertions"][0]["passed"] = False
    evidence["failed_assertions"] = [evidence["hard_assertions"][0]["name"]]
    try:
        validate_evidence(evidence)
    except EvidenceError as exc:
        assert "passed evidence" in str(exc)
    else:
        raise AssertionError("failed hard assertion was accepted as passed evidence")
    print("ok: failed assertions cannot be packaged as passed evidence")


def test_gate_counts_only_matching_l3_and_keeps_manual_separate() -> None:
    automatic = _evidence("J3", level="L3")
    host_only = _evidence("J4", level="L2")
    identity = automatic["code_identity"]
    report = evaluate_gate(
        [automatic, host_only],
        identity=identity,
        required_repeats=1,
        require_manual=True,
    )
    rows = {row["journey_id"]: row for row in report["journeys"]}
    assert rows["J3"]["automatic_passes"] == 1
    assert rows["J3"]["status"] == "missing"  # manual UX is still pending
    assert rows["J4"]["automatic_passes"] == 0  # L2 never upgrades to L3
    assert report["status"] == "incomplete"
    print("ok: release gate separates host, real-provider, and manual evidence")


def test_discovery_ignores_legacy_json_and_rejects_malformed_canonical_json() -> None:
    evidence = _evidence("J1")
    with tempfile.TemporaryDirectory(prefix="semantic_evidence_") as temp:
        root = Path(temp)
        (root / "legacy.json").write_text(json.dumps({"status": "passed"}), encoding="utf-8")
        (root / "valid.json").write_text(
            json.dumps({"semantic_evidence": evidence}), encoding="utf-8"
        )
        malformed = dict(evidence)
        malformed["journey_id"] = "J99"
        (root / "bad.json").write_text(json.dumps(malformed), encoding="utf-8")
        found, rejected = discover_evidence([root])
        assert len(found) == 1 and found[0]["journey_id"] == "J1"
        assert len(rejected) == 1 and rejected[0]["path"].endswith("bad.json")
    print("ok: discovery ignores legacy reports and exposes malformed canonical evidence")


def test_j3_runner_uses_the_default_codex_control_journey() -> None:
    command = _active_steer(
        argparse.Namespace(provider="deepseek"),
        ROOT / "runtime" / "e2e_reports" / "semantic_journeys" / "J3",
    )
    assert command[4].endswith("e2e_codex_app_server_control.py")
    assert command[5:7] == ["--chat-provider", "deepseek"]
    print("ok: J3 exercises the default Codex App Server control path")


def test_j7_runner_uses_the_canonical_auip_journey() -> None:
    command = _auip_experience(
        argparse.Namespace(model="deepseek-v4-flash"),
        ROOT / "runtime" / "e2e_reports" / "semantic_journeys" / "J7",
    )
    assert command[5:7] == ["--model", "deepseek-v4-flash"]
    assert command[4].endswith("e2e_auip_semantic_journey.py")
    print("ok: J7 runs one canonical AUIP AppSession journey")


def test_j7_requires_scene_commentary_and_a_grounded_terminal_summary() -> None:
    result = {
        "browser": "chromium-headless",
        "transport": "real_websocket",
        "identity_preserved": True,
        "routed_controls": [
            {"decided": action}
            for action in ("observe", "collaborate", "step", "none", "step")
        ],
        "natural_step": {
            "decided": "step",
            "receipt_type": "game.take_first_move",
            "status_decided": "none",
            "status_preserved_revision": True,
            "participant_role": "black",
        },
        "negotiated_step": {
            "decided": "step",
            "agreed_instruction": "Place one stone at x=5, y=4.",
            "participant_instruction": "Place one stone at x=5, y=4.",
            "receipt_type": "game.place_stone",
            "receipt_payload": {"x": 5, "y": 4},
        },
        "role_authorizer_transport": "real_explicit_turns+deterministic_fixture",
        "real_role_authorization_count": 1,
        "role_consensus": {
            "decided": "step",
            "participant_was_consulted": True,
            "action_suppressed": True,
        },
        "narration_trace": [
            {
                "source": "auip_operator_outcome",
                "terminal": False,
                "display_text": "blocked",
                "voice_text_matches": True,
                "fact_brief": "operator failure",
            },
            {
                "source": "auip_narrator",
                "terminal": False,
                "display_text": "move comment",
                "voice_text_matches": True,
                "fact_brief": "accepted action",
            },
            {
                "source": "auip_narrator",
                "terminal": True,
                "display_text": "terminal summary",
                "voice_text_matches": True,
                "fact_brief": 'verified terminal outcome {"winner":"black"}',
            },
        ],
        "attached_final_revision": 13,
        "retained_kurisu_actions": 4,
        "winner": "black",
        "terminal_event": "game.experience_finished",
        "lifecycle_step": {"decided": "step"},
        "close_reason": "journey_complete",
        "context_bounded": True,
        "shared_state_chars": 200,
        "standalone_move_count": 1,
        "console_errors": [],
        "page_errors": [],
        "diverse_games": {
            "samples": {
                "2048": {
                    "natural_step": {
                        "decided": "step",
                        "receipt_type": "game.slide",
                        "status_decided": "none",
                    },
                    "console_errors": [],
                    "page_errors": [],
                },
                "reactor": {
                    "natural_step": {
                        "decided": "step",
                        "receipt_type": "reactor.set_cooling",
                        "status_decided": "none",
                    },
                    "console_errors": [],
                    "page_errors": [],
                },
            }
        },
    }

    rows = {item["name"]: item["ok"] for item in _auip_checks(result)}
    assert rows["natural-step-crosses-participant-app-and-receipt"] is True
    assert rows["negotiated-step-binds-agreement-proposal-and-receipt"] is True
    assert rows["polite-proposal-respects-visible-role-alternative"] is True
    assert rows["real-observer-narrator-delivers-an-intermediate-comment"] is True
    assert rows["terminal-summary-is-grounded-delivered-and-retained"] is True
    assert rows[
        "round-result-remains-active-before-explicit-experience-terminal"
    ] is True

    speculative_question = json.loads(json.dumps(result))
    speculative_question["role_consensus"]["decided"] = "none"
    speculative_question["role_consensus"]["participant_was_consulted"] = False
    rows = {
        item["name"]: item["ok"]
        for item in _auip_checks(speculative_question)
    }
    assert rows["polite-proposal-respects-visible-role-alternative"] is False

    mismatched_negotiation = json.loads(json.dumps(result))
    mismatched_negotiation["negotiated_step"]["receipt_payload"] = {
        "x": 3,
        "y": 4,
    }
    rows = {
        item["name"]: item["ok"]
        for item in _auip_checks(mismatched_negotiation)
    }
    assert rows["negotiated-step-binds-agreement-proposal-and-receipt"] is False

    without_scene_comment = json.loads(json.dumps(result))
    without_scene_comment["narration_trace"] = [
        item
        for item in without_scene_comment["narration_trace"]
        if item["source"] != "auip_narrator" or item["terminal"] is True
    ]
    rows = {
        item["name"]: item["ok"]
        for item in _auip_checks(without_scene_comment)
    }
    assert rows["real-observer-narrator-delivers-an-intermediate-comment"] is False

    wrong_terminal = json.loads(json.dumps(result))
    wrong_terminal["narration_trace"][-1]["fact_brief"] = (
        'verified terminal outcome {"winner":"white"}'
    )
    rows = {item["name"]: item["ok"] for item in _auip_checks(wrong_terminal)}
    assert rows["terminal-summary-is-grounded-delivered-and-retained"] is False
    print("ok: J7 separates scene commentary from control errors and terminal truth")


def main() -> None:
    test_evidence_embeds_exact_code_identity_and_assertions()
    test_passed_evidence_cannot_hide_a_failed_assertion()
    test_gate_counts_only_matching_l3_and_keeps_manual_separate()
    test_discovery_ignores_legacy_json_and_rejects_malformed_canonical_json()
    test_j3_runner_uses_the_default_codex_control_journey()
    test_j7_runner_uses_the_canonical_auip_journey()
    test_j7_requires_scene_commentary_and_a_grounded_terminal_summary()


if __name__ == "__main__":
    main()
