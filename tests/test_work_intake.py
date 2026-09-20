"""Contract tests for WorkItem/Operation/Attempt intake ownership."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_types import ProviderRunRequest
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from agent_host.work_ledger_types import RunAttemptRecord, WorkItemRecord
from server.work_intake import (
    persist_work_intake,
    plan_work_intake,
    resolve_intake_reference,
)
from server.work_ledger_coordinator import WorkLedgerCoordinator


def _item(*, state: str = "open") -> WorkItemRecord:
    now = time.time()
    return WorkItemRecord(
        work_item_id="work_existing",
        project_id="project_existing",
        title="Existing goal",
        goal="Build the existing goal.",
        state=state,  # type: ignore[arg-type]
        workspace_mode="local",
        workspace_path="C:/workspace",
        workspace_identity="workspace-identity",
        branch="",
        base_revision="",
        created_at=now,
        updated_at=now,
        last_activity_at=now,
    )


def _attempt(
    *,
    status: str = "succeeded",
    provider: str = "locus",
    mode: str = "agent",
    operation_id: str = "operation_existing",
) -> RunAttemptRecord:
    now = time.time()
    return RunAttemptRecord(
        attempt_id="attempt_existing",
        work_item_id="work_existing",
        operation_id=operation_id,
        attempt_number=1,
        provider=provider,
        provider_run_id="run_existing",
        task="Build the existing goal.",
        mode=mode,
        execution_status=status,  # type: ignore[arg-type]
        result="",
        error="",
        created_at=now,
        updated_at=now,
        started_at=now,
        finished_at=now,
    )


def _expect_conflict(fn, text: str) -> None:
    try:
        fn()
    except WorkLedgerConflict as exc:
        assert text in str(exc)
    else:
        raise AssertionError("invalid intake must fail closed")


def test_legacy_amend_reference_is_normalized_once_before_planning() -> None:
    promoted = resolve_intake_reference(
        continuation="new",
        intent="amend",
        work_item_id="",
        related_work_item_id="work_legacy",
        related_work_item_exists=True,
    )
    assert promoted.work_item_id == "work_legacy"
    assert promoted.continuation == "amend"
    assert promoted.legacy_amend_promoted is True

    missing = resolve_intake_reference(
        continuation="new",
        intent="amend",
        work_item_id="",
        related_work_item_id="work_missing",
        related_work_item_exists=False,
    )
    assert missing.work_item_id == ""
    assert missing.continuation == "new"
    assert missing.legacy_amend_promoted is False


def test_new_goal_and_amendment_append_distinct_operations() -> None:
    created = plan_work_intake(
        continuation="new",
        declared_intent="execute",
        existing_item=None,
        previous_attempt=None,
        request_provider="locus",
        request_mode="agent",
        predecessor_attempt_id="",
    )
    assert created.creates_operation is True
    assert created.operation_intent == "execute"
    assert created.operation_id == ""

    amended = plan_work_intake(
        continuation="amend",
        declared_intent="amend",
        existing_item=_item(),
        previous_attempt=_attempt(),
        request_provider="openclaw",
        request_mode="plan",
        predecessor_attempt_id="",
    )
    assert amended.creates_operation is True
    assert amended.operation_intent == "amend"
    assert amended.previous_operation_id == "operation_existing"
    # A new semantic Operation may select another compatible Provider/mode.
    assert amended.operation_id == ""


def test_retry_and_steer_replacement_reuse_the_predecessor_operation() -> None:
    retry = plan_work_intake(
        continuation="retry",
        declared_intent="execute",
        existing_item=_item(),
        previous_attempt=_attempt(status="failed"),
        request_provider="locus",
        request_mode="agent",
        predecessor_attempt_id="attempt_existing",
    )
    assert retry.creates_operation is False
    assert retry.operation_id == "operation_existing"
    assert retry.lineage_label == "Retry"

    replacement = plan_work_intake(
        continuation="steer_replacement",
        declared_intent="amend",
        existing_item=_item(),
        previous_attempt=_attempt(status="cancelled"),
        request_provider="locus",
        request_mode="agent",
        predecessor_attempt_id="attempt_existing",
    )
    assert replacement.creates_operation is False
    assert replacement.operation_id == "operation_existing"
    assert replacement.lineage_label == "steer replacement"


def test_continuation_planning_rejects_identity_and_capability_drift() -> None:
    _expect_conflict(
        lambda: plan_work_intake(
            continuation="retry",
            declared_intent="execute",
            existing_item=_item(),
            previous_attempt=_attempt(status="succeeded"),
            request_provider="locus",
            request_mode="agent",
            predecessor_attempt_id="attempt_existing",
        ),
        "failed or cancelled",
    )
    _expect_conflict(
        lambda: plan_work_intake(
            continuation="retry",
            declared_intent="execute",
            existing_item=_item(),
            previous_attempt=_attempt(status="failed"),
            request_provider="openclaw",
            request_mode="agent",
            predecessor_attempt_id="attempt_existing",
        ),
        "cannot change provider",
    )
    _expect_conflict(
        lambda: plan_work_intake(
            continuation="steer_replacement",
            declared_intent="amend",
            existing_item=_item(),
            previous_attempt=_attempt(status="cancelled"),
            request_provider="locus",
            request_mode="plan",
            predecessor_attempt_id="attempt_existing",
        ),
        "cannot change mode",
    )
    _expect_conflict(
        lambda: plan_work_intake(
            continuation="retry",
            declared_intent="execute",
            existing_item=_item(),
            previous_attempt=_attempt(status="failed"),
            request_provider="locus",
            request_mode="agent",
            predecessor_attempt_id="attempt_stale",
        ),
        "latest failed attempt",
    )


def test_invalid_continuation_cannot_manufacture_a_new_work_item() -> None:
    _expect_conflict(
        lambda: plan_work_intake(
            continuation="retry",
            declared_intent="execute",
            existing_item=None,
            previous_attempt=None,
            request_provider="locus",
            request_mode="agent",
            predecessor_attempt_id="attempt_missing",
        ),
        "requires an existing work item",
    )
    _expect_conflict(
        lambda: plan_work_intake(
            continuation="new",
            declared_intent="execute",
            existing_item=_item(),
            previous_attempt=_attempt(),
            request_provider="locus",
            request_mode="agent",
            predecessor_attempt_id="",
        ),
        "explicit amend, Retry, or steer replacement",
    )


def test_coordinator_rejects_invalid_continuation_before_persistence() -> None:
    with tempfile.TemporaryDirectory(prefix="work_intake_invalid_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            _expect_conflict(
                lambda: coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="fake",
                        task="Retry a goal that does not exist.",
                        cwd=str(root),
                        mode="agent",
                        metadata={
                            "continuation": "retry",
                            "retry_of": "attempt_missing",
                        },
                    )
                ),
                "requires an existing work item",
            )
            assert store.list_work_items(limit=10) == []
            coordinator.close()


def test_persistence_applies_the_plan_without_redeciding_identity() -> None:
    with tempfile.TemporaryDirectory(prefix="work_intake_") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            project = store.create_or_get_project(workspace)
            item = store.create_work_item(
                project.project_id,
                title="Stable goal",
                goal="Build a stable goal.",
                workspace_path=str(workspace),
            )
            first_plan = plan_work_intake(
                continuation="new",
                declared_intent="execute",
                existing_item=None,
                previous_attempt=None,
                request_provider="locus",
                request_mode="agent",
                predecessor_attempt_id="",
            )
            first_operation, first_attempt = persist_work_intake(
                store,
                item_id=item.work_item_id,
                plan=first_plan,
                original_task="Build a stable goal.",
                provider_task="Build a stable goal.",
                provider="locus",
                mode="agent",
                operation_metadata={"source": "test"},
                attempt_metadata={"continuation": "new"},
            )
            store.update_attempt(first_attempt.attempt_id, execution_status="failed")

            retry_plan = plan_work_intake(
                continuation="retry",
                declared_intent="execute",
                existing_item=item,
                previous_attempt=store.get_attempt(first_attempt.attempt_id),
                request_provider="locus",
                request_mode="agent",
                predecessor_attempt_id=first_attempt.attempt_id,
            )
            retry_operation, retry_attempt = persist_work_intake(
                store,
                item_id=item.work_item_id,
                plan=retry_plan,
                original_task="Build a stable goal.",
                provider_task="Build a stable goal.",
                provider="locus",
                mode="agent",
                operation_metadata={},
                attempt_metadata={"continuation": "retry"},
            )
            assert retry_operation.operation_id == first_operation.operation_id
            assert retry_attempt.operation_id == first_operation.operation_id
            assert len(store.list_operations(item.work_item_id)) == 1
            assert len(store.list_attempts(item.work_item_id)) == 2


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all work intake tests passed")


if __name__ == "__main__":
    _main()
