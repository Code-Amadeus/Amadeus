"""Provider-neutral work completion assessment tests.

Runs standalone through tools/run_tests.py and is also pytest-compatible.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.work_completion import CompletionEvidence, assess_completion, normalize_execution_status


def test_execution_status_aliases_are_normalized() -> None:
    assert normalize_execution_status("done") == "succeeded"
    assert normalize_execution_status("error") == "failed"
    assert normalize_execution_status("canceled") == "cancelled"
    try:
        normalize_execution_status("mystery")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown execution states must not be guessed")


def test_running_attempt_stays_open() -> None:
    decision = assess_completion(CompletionEvidence(execution_status="running"))
    assert decision.execution_status == "running"
    assert decision.completeness == "unknown"
    assert decision.attention == "none"
    assert decision.work_item_state == "open"
    assert decision.terminal is False


def test_succeeded_attempt_requires_review_and_never_auto_accepts() -> None:
    decision = assess_completion(CompletionEvidence(execution_status="succeeded"))
    assert decision.execution_status == "succeeded"
    assert decision.completeness == "partial"
    assert decision.attention == "review"
    assert decision.work_item_state == "review_ready"
    assert decision.work_item_state != "accepted"

    complete = assess_completion(
        CompletionEvidence(
            execution_status="done",
            explicit_complete=True,
            expected_artifact_count=1,
            registered_artifact_count=1,
            validation_statuses=("passed", "skipped"),
        )
    )
    assert complete.completeness == "complete"
    assert complete.work_item_state == "review_ready"
    assert complete.attention == "review"


def test_success_with_unresolved_obligations_stays_open() -> None:
    permission = assess_completion(
        CompletionEvidence(execution_status="succeeded", pending_permissions=1)
    )
    assert permission.work_item_state == "open"
    assert permission.attention == "permission"

    missing = assess_completion(
        CompletionEvidence(
            execution_status="succeeded",
            explicit_complete=True,
            expected_artifact_count=2,
            registered_artifact_count=1,
            missing_requirements=("requirements.txt",),
        )
    )
    assert missing.work_item_state == "open"
    assert missing.completeness == "partial"
    assert missing.attention == "review"

    conflict = assess_completion(
        CompletionEvidence(
            execution_status="succeeded",
            validation_statuses=("failed",),
            conflicts=("tool result and filesystem disagree",),
        )
    )
    assert conflict.work_item_state == "open"
    assert conflict.attention == "conflict"

    blocked = assess_completion(
        CompletionEvidence(
            execution_status="succeeded",
            blocking_errors=("host observed an error page",),
        )
    )
    assert blocked.work_item_state == "open"
    assert blocked.completeness == "incomplete"
    assert blocked.attention == "error"


def test_failed_cancelled_and_orphaned_are_not_complete() -> None:
    failed = assess_completion(CompletionEvidence(execution_status="failed"))
    assert failed.completeness == "incomplete"
    assert failed.attention == "error"
    assert failed.work_item_state == "open"

    cancelled = assess_completion(CompletionEvidence(execution_status="cancelled"))
    assert cancelled.completeness == "incomplete"
    assert cancelled.attention == "review"
    assert cancelled.work_item_state == "open"

    orphaned = assess_completion(CompletionEvidence(execution_status="orphaned"))
    assert orphaned.completeness == "unknown"
    assert orphaned.attention == "error"
    assert orphaned.terminal is False


def test_existing_user_disposition_is_preserved() -> None:
    accepted = assess_completion(
        CompletionEvidence(execution_status="succeeded", current_state="accepted")
    )
    archived = assess_completion(
        CompletionEvidence(execution_status="failed", current_state="archived")
    )
    assert accepted.work_item_state == "accepted"
    assert archived.work_item_state == "archived"


def _main() -> None:
    test_execution_status_aliases_are_normalized()
    test_running_attempt_stays_open()
    test_succeeded_attempt_requires_review_and_never_auto_accepts()
    test_success_with_unresolved_obligations_stays_open()
    test_failed_cancelled_and_orphaned_are_not_complete()
    test_existing_user_disposition_is_preserved()
    print("ok: work completion keeps execution success separate from user acceptance")


if __name__ == "__main__":
    _main()
