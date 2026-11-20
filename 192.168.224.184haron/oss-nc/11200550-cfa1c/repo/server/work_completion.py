"""Pure completion assessment for provider-neutral work attempts.

Process success and user acceptance are intentionally different facts.  A
provider exiting successfully can make a work item ready for review, but this
module never turns an open item into ``accepted``.  Acceptance remains an
explicit user/disposition transition owned by the future work coordinator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from agent_host.work_ledger_types import (
    CompletionDecision,
    ExecutionStatus,
    WorkItemState,
)


_EXECUTION_ALIASES: dict[str, ExecutionStatus] = {
    "queued": "queued",
    "pending": "queued",
    "running": "running",
    "active": "running",
    "working": "running",
    "done": "succeeded",
    "complete": "succeeded",
    "completed": "succeeded",
    "succeeded": "succeeded",
    "success": "succeeded",
    "error": "failed",
    "failed": "failed",
    "failure": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "interrupted": "cancelled",
    "orphaned": "orphaned",
}

_VALIDATION_ALIASES = {
    "ok": "passed",
    "success": "passed",
    "succeeded": "passed",
    "pass": "passed",
    "passed": "passed",
    "failure": "failed",
    "error": "failed",
    "fail": "failed",
    "failed": "failed",
    "queued": "pending",
    "running": "pending",
    "pending": "pending",
    "unknown": "pending",
    "skip": "skipped",
    "skipped": "skipped",
}


@dataclass(frozen=True, slots=True)
class CompletionEvidence:
    """Auditable inputs used to assess one run attempt.

    ``explicit_complete`` is an Amadeus-level criteria result, not the mere
    presence of words such as "done" in a provider response.  Callers should
    set it only after checking the task's completion criteria.
    """

    execution_status: str
    current_state: WorkItemState = "open"
    explicit_complete: bool = False
    expected_artifact_count: int = 0
    registered_artifact_count: int = 0
    validation_statuses: tuple[str, ...] = ()
    missing_requirements: tuple[str, ...] = ()
    pending_permissions: int = 0
    pending_inputs: int = 0
    blocking_errors: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


def normalize_execution_status(value: str) -> ExecutionStatus:
    text = str(value or "").strip().lower()
    try:
        return _EXECUTION_ALIASES[text]
    except KeyError as exc:
        raise ValueError(f"unsupported execution status: {value!r}") from exc


def _normalize_validations(values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        text = str(value or "").strip().lower()
        if not text:
            continue
        normalized.append(_VALIDATION_ALIASES.get(text, "pending"))
    return tuple(normalized)


def _preserve_explicit_disposition(current: WorkItemState, proposed: WorkItemState) -> WorkItemState:
    # Completion assessment must not undo an explicit accept/archive action.
    # More importantly, it never *creates* accepted from open/review_ready.
    if current in {"accepted", "archived"}:
        return current
    return proposed


def assess_completion(evidence: CompletionEvidence) -> CompletionDecision:
    """Assess execution, completeness, attention, and review disposition.

    The function is deterministic and side-effect free.  In particular:

    * ``succeeded`` means the process ended normally, not that the goal is met;
    * unresolved permission/input/conflict evidence keeps the item ``open``;
    * an unblocked successful attempt becomes ``review_ready``;
    * an open item is never automatically changed to ``accepted``.
    """

    execution = normalize_execution_status(evidence.execution_status)
    current = evidence.current_state
    validations = _normalize_validations(evidence.validation_statuses)
    failed_validation = "failed" in validations
    pending_validation = "pending" in validations
    missing_artifacts = max(0, int(evidence.expected_artifact_count)) > max(
        0, int(evidence.registered_artifact_count)
    )

    if execution in {"queued", "running"}:
        state = _preserve_explicit_disposition(current, "open")
        return CompletionDecision(
            execution_status=execution,
            completeness="unknown",
            attention="none",
            work_item_state=state,
            rationale="The provider attempt is still active; completion has not been assessed.",
            terminal=False,
        )

    if execution == "failed":
        state = _preserve_explicit_disposition(current, "open")
        return CompletionDecision(
            execution_status=execution,
            completeness="incomplete",
            attention="error",
            work_item_state=state,
            rationale="The provider attempt failed before the work could be accepted.",
            terminal=True,
        )

    if execution == "orphaned":
        state = _preserve_explicit_disposition(current, "open")
        return CompletionDecision(
            execution_status=execution,
            completeness="unknown",
            attention="error",
            work_item_state=state,
            rationale="The provider attempt lost its live owner and needs recovery or review.",
            terminal=False,
        )

    if execution == "cancelled":
        state = _preserve_explicit_disposition(current, "open")
        return CompletionDecision(
            execution_status=execution,
            completeness="incomplete",
            attention="review",
            work_item_state=state,
            rationale="The provider attempt was cancelled; any partial changes need review.",
            terminal=True,
        )

    # From here the execution itself succeeded.  Resolve blocking evidence
    # before deciding whether its result is ready for review.
    if evidence.blocking_errors:
        state = _preserve_explicit_disposition(current, "open")
        return CompletionDecision(
            execution_status=execution,
            completeness="incomplete",
            attention="error",
            work_item_state=state,
            rationale=(
                "The process exited successfully, but the host observed a blocking "
                "business-level error."
            ),
            terminal=True,
        )

    if evidence.conflicts or failed_validation:
        reasons = []
        if evidence.conflicts:
            reasons.append("recorded facts conflict")
        if failed_validation:
            reasons.append("validation failed")
        state = _preserve_explicit_disposition(current, "open")
        return CompletionDecision(
            execution_status=execution,
            completeness="partial",
            attention="conflict",
            work_item_state=state,
            rationale="The process exited successfully, but " + " and ".join(reasons) + ".",
            terminal=True,
        )

    if evidence.pending_permissions > 0:
        state = _preserve_explicit_disposition(current, "open")
        return CompletionDecision(
            execution_status=execution,
            completeness="partial",
            attention="permission",
            work_item_state=state,
            rationale="The process exited successfully, but a permission decision is still pending.",
            terminal=True,
        )

    if evidence.pending_inputs > 0:
        state = _preserve_explicit_disposition(current, "open")
        return CompletionDecision(
            execution_status=execution,
            completeness="partial",
            attention="input",
            work_item_state=state,
            rationale="The process exited successfully, but user input is still required.",
            terminal=True,
        )

    if evidence.missing_requirements or missing_artifacts or pending_validation:
        reasons = []
        if evidence.missing_requirements:
            reasons.append("requirements remain unresolved")
        if missing_artifacts:
            reasons.append("expected artifacts are missing")
        if pending_validation:
            reasons.append("validation is pending")
        state = _preserve_explicit_disposition(current, "open")
        return CompletionDecision(
            execution_status=execution,
            completeness="partial",
            attention="review",
            work_item_state=state,
            rationale="The process exited successfully, but " + ", ".join(reasons) + ".",
            terminal=True,
        )

    completeness = "complete" if evidence.explicit_complete else "partial"
    state = _preserve_explicit_disposition(current, "review_ready")
    rationale = (
        "Completion criteria are satisfied; user review is required before acceptance."
        if evidence.explicit_complete
        else "The process exited successfully; goal completeness still needs user review."
    )
    return CompletionDecision(
        execution_status=execution,
        completeness=completeness,
        attention="review",
        work_item_state=state,
        rationale=rationale,
        terminal=True,
    )
