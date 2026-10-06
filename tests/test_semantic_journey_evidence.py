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
from tools.semantic_release_gate import discover_evidence, evaluate_gate
from tools.run_semantic_journeys import JOURNEYS, _active_steer, _auip_experience


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


def test_j7_uses_shipping_cooperative_entry_and_requires_complete_experience_evidence() -> None:
    journey = JOURNEYS['J7']
    assert journey.implemented is True
    assert journey.build is _auip_experience
    command = journey.build(argparse.Namespace(chat_route="basic", provider="deepseek", model="test-model"),
        ROOT / "runtime" / "e2e_reports" / "semantic_journeys" / "J7")
    assert command[4].endswith("e2e_live_product_journey.py")
    assert command[command.index("--journey-layer") + 1] == "interaction"
    assert command[command.index("--chat-route") + 1] == "basic"
    assert command[command.index("--seed") + 1] == str(ROOT / "examples" / "auip-gomoku")
    assert {"--require-b2", "--complete-gomoku-round", "--exercise-gomoku-post-round",
        "--exercise-post-leave-chat", "--require-experience-history"}.issubset(command)


def main() -> None:
    test_evidence_embeds_exact_code_identity_and_assertions()
    test_passed_evidence_cannot_hide_a_failed_assertion()
    test_gate_counts_only_matching_l3_and_keeps_manual_separate()
    test_discovery_ignores_legacy_json_and_rejects_malformed_canonical_json()
    test_j3_runner_uses_the_default_codex_control_journey()
    test_j7_uses_shipping_cooperative_entry_and_requires_complete_experience_evidence()


if __name__ == "__main__":
    main()
