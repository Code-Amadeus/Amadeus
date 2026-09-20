"""Contract tests for the read-only Work Ledger projection boundary."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.work_ledger_store import WorkLedgerStore
from agent_host.work_ledger_types import CompletionDecision
from server.work_read_model import WorkReadModel


def _model(store: WorkLedgerStore, *, now: float = 1000.0) -> WorkReadModel:
    return WorkReadModel(
        store,
        clock=lambda: now,
        is_unkept_draft=lambda _path: False,
        is_desktop_export_permission=lambda _request: False,
        can_resume_authorized_export=lambda _request: False,
    )


def _durable_facts(store: WorkLedgerStore, work_item_id: str) -> dict:
    return {
        "items": [row.to_dict() for row in store.list_work_items()],
        "operations": [row.to_dict() for row in store.list_operations(work_item_id)],
        "attempts": [row.to_dict() for row in store.list_attempts(work_item_id)],
        "artifacts": [row.to_dict() for row in store.list_artifacts(work_item_id)],
        "completions": [row.to_dict() for row in store.list_completions(work_item_id)],
        "permissions": [
            row.to_dict() for row in store.list_permission_requests(work_item_id)
        ],
    }


def test_projection_reads_durable_facts_without_mutating_them() -> None:
    with tempfile.TemporaryDirectory(prefix="work_read_model_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3", clock=lambda: 10.0) as store:
            project = store.create_or_get_project(workspace, name="Read model")
            item = store.create_work_item(
                project.project_id,
                title="Explain status",
                goal="Keep reads side-effect free.",
                workspace_path=workspace,
            )
            before = _durable_facts(store, item.work_item_id)
            model = _model(store)

            row = model.project_item(item)
            detail = model.detail(item.work_item_id)
            project_row = model.project_status_snapshot(project.project_id)

            assert row["execution"] == "idle"
            assert row["workspaceExists"] is True
            assert detail["operations"] == []
            assert project_row is not None
            assert project_row["counts"] == {
                "current": 1,
                "running": 0,
                "needsYou": 0,
                "history": 0,
            }
            assert _durable_facts(store, item.work_item_id) == before


def test_latest_attempt_never_inherits_an_older_completion() -> None:
    with tempfile.TemporaryDirectory(prefix="work_read_completion_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            project = store.create_or_get_project(workspace, name="Completion")
            item = store.create_work_item(
                project.project_id,
                title="Continue one goal",
                workspace_path=workspace,
            )
            _, first = store.create_operation_attempt(
                item.work_item_id,
                intent="execute",
                instruction="Create the first version.",
                provider="locus",
                task="Create the first version.",
            )
            store.update_attempt(first.attempt_id, execution_status="succeeded")
            store.record_completion(
                item.work_item_id,
                CompletionDecision(
                    execution_status="succeeded",
                    completeness="partial",
                    attention="review",
                    work_item_state="review_ready",
                    rationale="First version needs review.",
                    terminal=True,
                ),
                attempt_id=first.attempt_id,
            )
            _, second = store.create_operation_attempt(
                item.work_item_id,
                intent="amend",
                instruction="Add the requested fourth point.",
                provider="locus",
                task="Add the requested fourth point.",
            )

            row = _model(store).project_item(store.get_work_item(item.work_item_id))  # type: ignore[arg-type]
            assert row["attemptId"] == second.attempt_id
            assert row["execution"] == "queued"
            assert row["completion"] == "unknown"
            assert row["attention"] == "none"
            assert row["completionRationale"] == ""


def test_projection_exposes_existing_attempt_session_identity() -> None:
    with tempfile.TemporaryDirectory(prefix="work_read_session_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            project = store.create_or_get_project(workspace, name="Session identity")
            item = store.create_work_item(
                project.project_id,
                title="Keep this task in its conversation",
                workspace_path=workspace,
            )
            store.create_operation_attempt(
                item.work_item_id,
                intent="execute",
                instruction="Record the existing session fact.",
                provider="locus",
                task="Record the existing session fact.",
                attempt_metadata={"session_id": "session-current"},
            )

            row = _model(store).project_item(store.get_work_item(item.work_item_id))  # type: ignore[arg-type]
            assert row["sessionId"] == "session-current"


def test_pending_permission_is_attention_not_execution_truth() -> None:
    with tempfile.TemporaryDirectory(prefix="work_read_permission_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            project = store.create_or_get_project(workspace, name="Permission")
            item = store.create_work_item(
                project.project_id,
                title="Await one decision",
                workspace_path=workspace,
            )
            _, attempt = store.create_operation_attempt(
                item.work_item_id,
                intent="execute",
                instruction="Perform the bounded action.",
                provider="locus",
                task="Perform the bounded action.",
            )
            store.update_attempt(attempt.attempt_id, execution_status="running")
            request = store.create_permission_request(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                capability="shell",
                action="run",
                scope_paths=[str(workspace)],
                reason="Needs explicit approval.",
                options=["allow_once", "deny"],
            )

            row = _model(store).project_item(store.get_work_item(item.work_item_id))  # type: ignore[arg-type]
            assert row["execution"] == "running"
            assert row["attention"] == "permission"
            assert row["pendingPermissionRequestId"] == request.request_id
            assert row["pendingPermissionCount"] == 1
            assert row["canRetry"] is False


def test_projection_keeps_reported_direction_distinct_from_semantic_results() -> None:
    with tempfile.TemporaryDirectory(prefix="work_read_direction_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3", clock=lambda: 900.0) as store:
            project = store.create_or_get_project(workspace, name="Direction")
            item = store.create_work_item(
                project.project_id,
                title="Connect the app",
                workspace_path=workspace,
            )
            _, attempt = store.create_operation_attempt(
                item.work_item_id,
                intent="amend",
                instruction="Connect the existing app.",
                provider="codex",
                task="Connect the existing app.",
            )
            store.update_attempt(
                attempt.attempt_id,
                execution_status="running",
                metadata={
                    "activity_snapshot": {
                        "phase": "working",
                        "lastDirectionalUpdateAt": 990.0,
                        "latestCandidateSummary": (
                            "Mapping the existing state before connected-mode validation."
                        ),
                        "candidateSource": "codex_native_agent_message",
                    }
                },
            )

            row = _model(store).project_item(store.get_work_item(item.work_item_id))  # type: ignore[arg-type]
            activity = row["activity"]
            assert activity["directionSummary"].startswith("Mapping the existing state")
            assert activity["directionSource"] == "codex_native_agent_message"
            assert activity["semanticSummary"] == ""
            assert activity["silentSeconds"] == 10.0


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all work read model tests passed")


if __name__ == "__main__":
    _main()
