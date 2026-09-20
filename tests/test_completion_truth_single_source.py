"""One assessment, one narration: the character stops signing off on claims.

2026-07-31, session 20260731-191318: every tool call in a Locus run was denied,
nothing was written to disk, and the process still exited 0. Two verdicts were
computed from that. The ledger cross-checked the recorded tool failures against
an empty git delta and wrote attention=conflict with the rationale "The process
exited successfully, but recorded facts conflict." The adapter mapped the exit
code to "done". The pill head showed the first; the Report panel and the spoken
report carried the second, so Kurisu told the user the chess game was finished
and saved to the Desktop.

The honest sentence already existed. What was missing was that it owned the
telling.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config.settings as settings
from server.handlers.work_activity_handler import WorkActivityCoordinator
from server.work_completion import CompletionEvidence, assess_completion
from server.work_ledger_coordinator import WorkLedgerCoordinator


def _decision(**kw):
    base = dict(
        execution_status="succeeded",
        explicit_complete=False,
        expected_artifact_count=0,
        registered_artifact_count=0,
        validation_statuses=(),
        missing_requirements=(),
        pending_permissions=0,
        pending_inputs=0,
        conflicts=(),
    )
    base.update(kw)
    return assess_completion(CompletionEvidence(**base))


def test_the_chess_verdict_is_what_gets_said() -> None:
    """The exact evidence shape from the incident, through the summary picker."""

    decision = _decision(
        conflicts=(
            "PowerShell reported failure",
            "Bash reported failure",
            "Write reported failure",
        )
    )
    assert decision.attention == "conflict"
    assert decision.completeness == "partial"

    spoken = WorkLedgerCoordinator._terminal_narration_summary(
        decision,
        "I've built the complete chess game and saved it to your Desktop.",
        "",
    )
    assert spoken == decision.rationale
    assert "conflict" in spoken
    assert "chess" not in spoken.lower(), "the provider's claim must not be repeated"


def test_an_agreeing_verdict_still_lets_the_provider_word_it() -> None:
    """Nothing is gained by paraphrasing a report the evidence supports.

    Every finished attempt lands on attention=review on its way to the Accept
    gate, so review is a disposition, not a disagreement. Reading it as one
    would swap the actual report for "completeness still needs user review" on
    every successful run.
    """

    decision = _decision(explicit_complete=True)
    assert decision.completeness == "complete"
    assert decision.attention == "review"

    spoken = WorkLedgerCoordinator._terminal_narration_summary(
        decision, "Created theme.txt and wrote color=blue.", ""
    )
    assert spoken == "Created theme.txt and wrote color=blue."

    # A plain success with no completion criteria is partial/review, and still
    # not a contradiction.
    plain = _decision()
    assert (plain.completeness, plain.attention) == ("partial", "review")
    assert (
        WorkLedgerCoordinator._terminal_narration_summary(plain, "Wrote the file.", "")
        == "Wrote the file."
    )

    # With nothing reported, the assessment still supplies a sentence.
    assert WorkLedgerCoordinator._terminal_narration_summary(decision, "", "")


def test_blocked_endings_are_not_narrated_as_finished() -> None:
    for kw in ({"pending_permissions": 1}, {"pending_inputs": 1}):
        decision = _decision(**kw)
        spoken = WorkLedgerCoordinator._terminal_narration_summary(
            decision, "All set!", ""
        )
        assert spoken == decision.rationale, kw
        assert "All set" not in spoken


def test_a_cancelled_attempt_is_not_narrated_as_the_provider_left_it() -> None:
    decision = _decision(execution_status="cancelled")
    spoken = WorkLedgerCoordinator._terminal_narration_summary(
        decision, "All done!", ""
    )
    assert spoken == decision.rationale
    assert "cancelled" in spoken.lower()


def test_only_a_run_the_ledger_assesses_hands_over_its_narration() -> None:
    tracked = {
        "provider": "locus",
        "metadata": {"work": {"work_item_id": "w1", "attempt_id": "a1"}},
    }
    untracked = {"provider": "openclaw", "metadata": {}}

    with patch.object(settings, "WORK_LEDGER_OWNS_TERMINAL_NARRATION", True):
        assert WorkActivityCoordinator._ledger_owns_terminal_note(tracked) is True
        # No assessment is coming for this one, so silence would be the only
        # other outcome.
        assert WorkActivityCoordinator._ledger_owns_terminal_note(untracked) is False

    with patch.object(settings, "WORK_LEDGER_OWNS_TERMINAL_NARRATION", False):
        assert WorkActivityCoordinator._ledger_owns_terminal_note(tracked) is False
        recovering = {
            "metadata": {
                **tracked["metadata"],
                "provider_completion": {
                    "classification": "progress_only_completion",
                },
            }
        }
        assert WorkActivityCoordinator._ledger_owns_terminal_note(recovering) is True
        # The Desktop export hand-off predates the flag and does not depend on it.
        export = {
            "provider": "locus",
            "metadata": {
                "work": {"work_item_id": "w1", "attempt_id": "a1"},
                "export_plan": {"staged": True},
            },
        }
        assert WorkActivityCoordinator._ledger_owns_terminal_note(export) is True


def test_the_hand_off_is_keyed_on_assessment_not_on_a_provider_name() -> None:
    """A provider name here would say Locus and mean "has a tracked attempt"."""

    browser = {
        "provider": "browser",
        "metadata": {"work": {"work_item_id": "w9", "attempt_id": "a9"}},
    }
    with patch.object(settings, "WORK_LEDGER_OWNS_TERMINAL_NARRATION", True):
        assert WorkActivityCoordinator._ledger_owns_terminal_note(browser) is True


if __name__ == "__main__":
    test_the_chess_verdict_is_what_gets_said()
    print("ok: the chess verdict is what gets said")
    test_an_agreeing_verdict_still_lets_the_provider_word_it()
    print("ok: an agreeing verdict still lets the provider word it")
    test_blocked_endings_are_not_narrated_as_finished()
    print("ok: blocked endings are not narrated as finished")
    test_a_cancelled_attempt_is_not_narrated_as_the_provider_left_it()
    print("ok: a cancelled attempt is not narrated as the provider left it")
    test_only_a_run_the_ledger_assesses_hands_over_its_narration()
    print("ok: only a run the ledger assesses hands over its narration")
    test_the_hand_off_is_keyed_on_assessment_not_on_a_provider_name()
    print("ok: the hand-off is keyed on assessment, not on a provider name")
    print("all completion truth tests passed")
