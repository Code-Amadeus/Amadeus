"""Provider-neutral WorkItem, Operation, and Attempt intake planning.

This module owns one bounded decision: whether a Provider request starts a new
goal, appends a semantic Operation to an existing goal, or retries the latest
Operation with another Attempt.  It does not choose a workspace, Provider,
permission, export, or UI projection.

Planning is pure.  Persistence applies the resulting plan through the existing
``WorkLedgerStore`` transaction methods without reinterpreting identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_host.work_ledger_store import (
    WorkLedgerConflict,
    WorkLedgerStore,
)
from agent_host.work_ledger_types import (
    RunAttemptRecord,
    WorkItemRecord,
    WorkOperationRecord,
)


EXISTING_ITEM_CONTINUATIONS = frozenset(
    {"amend", "retry", "steer_replacement"}
)
_SUPPORTED_CONTINUATIONS = frozenset({"new", *EXISTING_ITEM_CONTINUATIONS})


@dataclass(frozen=True, slots=True)
class WorkIntakeReference:
    """The normalized durable target before WorkItem lookup."""

    work_item_id: str
    continuation: str
    legacy_amend_promoted: bool = False


@dataclass(frozen=True, slots=True)
class WorkIntakePlan:
    """One immutable instruction for Operation/Attempt persistence."""

    continuation: str
    creates_operation: bool
    operation_intent: str
    operation_id: str = ""
    previous_operation_id: str = ""
    previous_attempt_id: str = ""
    lineage_label: str = ""


def resolve_intake_reference(
    *,
    continuation: str,
    intent: str,
    work_item_id: str,
    related_work_item_id: str,
    related_work_item_exists: bool,
) -> WorkIntakeReference:
    """Normalize the one schema-v6 amendment edge still accepted at intake."""

    clean_continuation = str(continuation or "new").strip().lower() or "new"
    clean_work_item_id = str(work_item_id or "").strip()
    clean_related_id = str(related_work_item_id or "").strip()
    if (
        not clean_work_item_id
        and clean_continuation == "new"
        and str(intent or "").strip().lower() == "amend"
        and clean_related_id
        and related_work_item_exists
    ):
        return WorkIntakeReference(
            work_item_id=clean_related_id,
            continuation="amend",
            legacy_amend_promoted=True,
        )
    return WorkIntakeReference(
        work_item_id=clean_work_item_id,
        continuation=clean_continuation,
    )


def plan_work_intake(
    *,
    continuation: str,
    declared_intent: str,
    existing_item: WorkItemRecord | None,
    previous_attempt: RunAttemptRecord | None,
    request_provider: str,
    request_mode: str,
    predecessor_attempt_id: str,
) -> WorkIntakePlan:
    """Choose Operation append versus Attempt retry from durable identity.

    Workspace availability, pending permissions, active-run exclusion, and
    amendment text validation remain at their existing owners.  This planner
    only decides the WorkItem/Operation/Attempt relationship.
    """

    clean_continuation = str(continuation or "new").strip().lower() or "new"
    if clean_continuation not in _SUPPORTED_CONTINUATIONS:
        raise WorkLedgerConflict(
            f"unsupported work continuation: {clean_continuation}"
        )

    if existing_item is None:
        if clean_continuation != "new":
            raise WorkLedgerConflict(
                f"{clean_continuation} requires an existing work item"
            )
        operation_intent = (
            "amend"
            if str(declared_intent or "").strip().lower() == "amend"
            else "execute"
        )
        return WorkIntakePlan(
            continuation="new",
            creates_operation=True,
            operation_intent=operation_intent,
        )

    if clean_continuation not in EXISTING_ITEM_CONTINUATIONS:
        raise WorkLedgerConflict(
            "existing work items require explicit amend, Retry, or "
            "steer replacement semantics"
        )

    if clean_continuation == "amend":
        return WorkIntakePlan(
            continuation="amend",
            creates_operation=True,
            operation_intent="amend",
            previous_operation_id=(
                str(previous_attempt.operation_id or "").strip()
                if previous_attempt is not None
                else ""
            ),
            previous_attempt_id=(
                str(previous_attempt.attempt_id or "").strip()
                if previous_attempt is not None
                else ""
            ),
        )

    if previous_attempt is None:
        raise WorkLedgerConflict(
            f"work item {existing_item.work_item_id} has no attempt to Retry"
        )

    if clean_continuation == "retry":
        if previous_attempt.execution_status not in {"failed", "cancelled"}:
            raise WorkLedgerConflict(
                "Retry is only valid after a failed or cancelled attempt"
            )
        invalid_predecessor = "Retry must reference the latest failed attempt"
        lineage_label = "Retry"
    else:
        if previous_attempt.execution_status != "cancelled":
            raise WorkLedgerConflict(
                "steer replacement requires a confirmed cancelled predecessor"
            )
        invalid_predecessor = (
            "steer replacement must reference the latest cancelled attempt"
        )
        lineage_label = "steer replacement"

    if str(predecessor_attempt_id or "").strip() != previous_attempt.attempt_id:
        raise WorkLedgerConflict(invalid_predecessor)
    if (
        str(request_provider or "").strip().lower()
        != previous_attempt.provider.strip().lower()
    ):
        raise WorkLedgerConflict(
            "Retry cannot change provider; create a new WorkItem"
        )
    if (
        str(request_mode or "").strip().lower()
        != previous_attempt.mode.strip().lower()
    ):
        raise WorkLedgerConflict("Retry cannot change mode; create a new WorkItem")

    operation_id = str(previous_attempt.operation_id or "").strip()
    if not operation_id:
        raise WorkLedgerConflict(
            "continuation predecessor has no durable operation binding"
        )
    return WorkIntakePlan(
        continuation=clean_continuation,
        creates_operation=False,
        operation_intent="",
        operation_id=operation_id,
        previous_operation_id=operation_id,
        previous_attempt_id=previous_attempt.attempt_id,
        lineage_label=lineage_label,
    )


def persist_work_intake(
    store: WorkLedgerStore,
    *,
    item_id: str,
    plan: WorkIntakePlan,
    original_task: str,
    provider_task: str,
    provider: str,
    mode: str,
    operation_metadata: dict[str, Any],
    attempt_metadata: dict[str, Any],
) -> tuple[WorkOperationRecord, RunAttemptRecord]:
    """Persist exactly one previously planned Operation/Attempt relationship."""

    if plan.creates_operation:
        return store.create_operation_attempt(
            item_id,
            intent=plan.operation_intent,
            instruction=original_task,
            provider=provider,
            task=provider_task,
            mode=mode,
            operation_metadata=operation_metadata,
            attempt_metadata=attempt_metadata,
        )

    operation = store.get_operation(plan.operation_id)
    if operation is None:
        raise WorkLedgerConflict(
            "continuation predecessor has no durable operation binding"
        )
    attempt = store.create_attempt(
        item_id,
        provider=provider,
        task=provider_task,
        mode=mode,
        operation_id=operation.operation_id,
        metadata=attempt_metadata,
    )
    return operation, attempt
