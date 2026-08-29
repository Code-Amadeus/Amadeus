"""Outcome verification is facet-driven and independent of provider ids."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_outcome import ProviderOutcomeEvidence
from agent_host.provider_contract import (
    ProviderCapabilities,
    ProviderManifest,
    ProviderOperation,
    ProviderRequirements,
)
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderRunRequest, ProviderRunResult
from agent_host.work_ledger_store import WorkLedgerStore
from server.outcome_verification import (
    assess_provider_outcome,
    observe_required_host_outcome,
)
from server.work_ledger_coordinator import WorkLedgerCoordinator
from server.work_observer import ObserverSession, WorkObserverCoordinator


def _declared_manifest(facet: str = "example.record_state") -> dict:
    return {
        "provider_id": "example",
        "capabilities": {
            "operations": [
                {
                    "operation_id": "publish",
                    "execution": "direct",
                    "atomic": True,
                    "outcome_facet": facet,
                }
            ]
        },
    }


def test_declared_outcome_is_required_even_when_evidence_is_missing() -> None:
    verdict = assess_provider_outcome(
        execution_status="succeeded",
        provider_report="The record was published.",
        metadata={
            "provider_manifest": _declared_manifest(),
            "action": "publish",
        },
    )
    assert verdict is not None
    assert verdict.facet == "example.record_state"
    assert verdict.completeness == "partial"
    assert verdict.verified is False
    assert verdict.provider_report_allowed is False


def test_provider_claim_cannot_mint_host_observation_authority() -> None:
    verdict = assess_provider_outcome(
        execution_status="succeeded",
        provider_report="The record was published.",
        metadata={
            "provider_manifest": _declared_manifest(),
            "action": "publish",
            "outcome_evidence": {
                "facet": "example.record_state",
                "operation": "publish",
                "expected": {"revision": 2},
                "observed": {"revision": 2},
                "observation_authority": "provider",
            },
        },
    )
    assert verdict is not None
    assert verdict.verified is False
    assert verdict.expected == {}
    assert verdict.observed == {}


def test_evidence_must_match_the_declared_operation_contract() -> None:
    manifest = _declared_manifest("browser.page_state")
    verdict = assess_provider_outcome(
        execution_status="succeeded",
        provider_report="The page is open.",
        metadata={
            "provider_manifest": manifest,
            "action": "publish",
            "outcome_evidence": ProviderOutcomeEvidence(
                facet="browser.page_state",
                operation="open",
                expected={"url": "https://example.com/"},
                observed={"url": "https://example.com/"},
            ).to_dict(),
        },
    )
    assert verdict is not None
    assert verdict.verified is False
    assert verdict.attention == "conflict"
    assert verdict.provider_report_allowed is False
    assert "does not match" in verdict.rationale


def test_browser_verifier_is_selected_by_facet_not_provider_name() -> None:
    verdict = assess_provider_outcome(
        execution_status="succeeded",
        provider_report="Opened it.",
        metadata={
            "outcome_evidence": ProviderOutcomeEvidence(
                facet="browser.page_state",
                operation="open",
                expected={"url": "https://example.com/"},
                observed={"url": "https://www.example.com/", "title": "Example"},
            ).to_dict()
        },
    )
    assert verdict is not None
    assert verdict.verified is True
    assert verdict.completeness == "complete"


def test_host_outcome_requirement_observes_artifacts_not_provider_identity() -> None:
    requirement = {
        "host_outcome_requirement": {
            "operation": "prepare",
            "facet": "auip.application",
            "expected": {"current_attempt_contribution": True},
        }
    }
    attempt = SimpleNamespace(work_item_id="work-game", attempt_id="attempt-current")

    with patch(
        "server.auip_app_source.discover_registered_auip_app",
        return_value=None,
    ):
        missing = observe_required_host_outcome(
            requirement,
            store=object(),
            attempt=attempt,
        )
    assert missing is not None
    missing_verdict = assess_provider_outcome(
        execution_status="succeeded",
        provider_report="Everything is ready.",
        metadata={**requirement, "outcome_evidence": missing.to_dict()},
    )
    assert missing_verdict is not None
    assert missing_verdict.verified is False
    assert missing_verdict.attention == "error"
    assert missing_verdict.provider_report_allowed is False

    with patch(
        "server.auip_app_source.discover_registered_auip_app",
        return_value={
            "contributing_attempt_ids": ["attempt-current"],
            "app": {"id": "gomoku", "title": "Gomoku"},
        },
    ):
        observed = observe_required_host_outcome(
            requirement,
            store=object(),
            attempt=attempt,
        )
    assert observed is not None
    verified = assess_provider_outcome(
        execution_status="succeeded",
        provider_report="Everything is ready.",
        metadata={**requirement, "outcome_evidence": observed.to_dict()},
    )
    assert verified is not None
    assert verified.verified is True
    assert verified.completeness == "complete"
    assert verified.observed["app_id"] == "gomoku"


def test_observer_replaces_optimistic_prose_for_any_unverified_facet() -> None:
    verdict = assess_provider_outcome(
        execution_status="succeeded",
        provider_report="The record was published.",
        metadata={
            "provider_manifest": _declared_manifest(),
            "action": "publish",
        },
    )
    assert verdict is not None
    note = {
        "provider": "example",
        "run_id": "run_example",
        "phase": "Result",
        "summary": verdict.summary,
        "metadata": {
            "execution_status": "succeeded",
            "outcome_verdict": verdict.to_dict(),
        },
    }
    session = ObserverSession(
        narration_id="run_example",
        run_id="run_example",
        session_id="session_example",
        provider="example",
    )
    session.add_note(note)
    decision = WorkObserverCoordinator()._merge_decision_defaults(
        {
            "action": "final_report",
            "terminal": True,
            "append_to_main_chat": True,
            "speak": True,
            "display_language": "english",
            "display_text": "The record was published.",
            "main_chat_entry": "The record was published.",
        },
        session,
        note,
    )
    assert "published" not in decision["display_text"].lower()
    assert "no outcome verifier" in decision["display_text"].lower()


def test_observer_localizes_verified_outcome_when_provider_prose_is_wrong_language() -> None:
    verdict = assess_provider_outcome(
        execution_status="succeeded",
        provider_report="页面已经打开。",
        metadata={
            "outcome_evidence": ProviderOutcomeEvidence(
                facet="browser.page_state",
                operation="open",
                expected={"url": "https://example.com/"},
                observed={"url": "https://example.com/", "title": "Example"},
            ).to_dict()
        },
        display_language="english",
    )
    assert verdict is not None and verdict.verified is True
    assert verdict.provider_report_allowed is False
    note = {
        "provider": "example",
        "run_id": "run_language",
        "phase": "Result",
        "metadata": {
            "execution_status": "succeeded",
            "outcome_verdict": verdict.to_dict(),
        },
    }
    session = ObserverSession(
        narration_id="run_language",
        run_id="run_language",
        session_id="session_language",
        provider="example",
    )
    session.add_note(note)
    decision = WorkObserverCoordinator()._merge_decision_defaults(
        {
            "action": "final_report",
            "terminal": True,
            "append_to_main_chat": True,
            "speak": True,
            "display_language": "japanese",
            "display_text": "页面已经打开。",
            "main_chat_entry": "页面已经打开。",
        },
        session,
        note,
    )
    assert "页面已经打开" not in decision["display_text"]
    assert "操作は完了したわ" in decision["display_text"]


class _LedgerOutcomeProbe:
    provider_id = "example_outcome"
    manifest = ProviderManifest(
        provider_id=provider_id,
        display_name="Outcome probe",
        capabilities=ProviderCapabilities(
            task_kinds=("general",),
            operations=(
                ProviderOperation(
                    "inspect",
                    outcome_facet="browser.page_state",
                ),
            ),
        ),
    )

    async def run(self, _request, _run_id, _emit):
        return ProviderRunResult(
            status="done",
            result="The requested page is open.",
            outcome_evidence=ProviderOutcomeEvidence(
                facet="browser.page_state",
                operation="inspect",
                expected={"url": "https://expected.example/"},
                observed={"url": "https://other.example/"},
            ),
        )

    async def cancel(self, _run_id):
        return {"confirmed": True, "cancelled": True}


def test_work_ledger_consumes_verdict_without_provider_specific_logic() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="provider_outcome_ledger_") as temp:
            store = WorkLedgerStore(Path(temp) / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(store)
            runtime = ProviderRuntime()
            runtime.register(_LedgerOutcomeProbe())
            runtime.set_request_preparer(coordinator.prepare_request)
            coordinator.configure()
            try:
                record = await runtime.start(
                    ProviderRunRequest(
                        provider="example_outcome",
                        task="Inspect the external page",
                        mode="inspect",
                        requirements=ProviderRequirements(
                            task_kind="general",
                            preferred_provider="example_outcome",
                            preference_policy="require",
                        ),
                        metadata={
                            "action": "inspect",
                            "session_id": "outcome-ledger-session",
                        },
                    )
                )
                assert record.task_handle is not None
                await record.task_handle
                work = record.metadata["work"]
                assessments = store.list_completions(work["work_item_id"])
                assert assessments
                assessment = assessments[-1]
                assert assessment.attention == "conflict"
                assert assessment.completeness != "complete"
                assert any(
                    "host-observed URL" in str(item)
                    for item in assessment.evidence.get("conflicts", [])
                )
                attempt = store.get_attempt(work["attempt_id"])
                assert attempt is not None
                assert attempt.metadata["outcome_verdict"]["attention"] == "conflict"
                assert attempt.metadata["outcome_verdict"]["provider_report_allowed"] is False
            finally:
                coordinator.close()

    asyncio.run(run())


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all provider outcome contract tests passed")


if __name__ == "__main__":
    _main()
