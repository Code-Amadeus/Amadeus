"""Contract tests for the read-only Work Ledger projection boundary."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.work_ledger_store import WorkLedgerStore
from agent_host.work_ledger_types import CompletionDecision
from server.work_read_model import WorkReadModel


def _model(
    store: WorkLedgerStore,
    *,
    now: float = 1000.0,
    unkept_draft: bool = False,
) -> WorkReadModel:
    return WorkReadModel(
        store,
        clock=lambda: now,
        is_unkept_draft=lambda _path: unkept_draft,
        is_desktop_export_permission=lambda _request: False,
        can_resume_authorized_export=lambda _request: False,
    )


@pytest.mark.parametrize(("session_metadata", "expected_session"), [
    ({"session_id":"direct", "provider_result":{"session_id":"fallback"}}, "direct"),
    ({"provider_result":{"session_id":"snake"}}, "snake"),
    ({"provider_result":{"sessionId":"camel"}}, "camel"),
])
def test_light_projection_matches_full_reads_and_refreshes_latest_facts(
        tmp_path, monkeypatch, session_metadata, expected_session):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with WorkLedgerStore(tmp_path / "projection.sqlite3", clock=lambda:10.0) as store:
        project = store.create_or_get_project(workspace)
        item = store.create_work_item(project.project_id, title="Current task",
            metadata={"presentation":{"image":"large" * 50_000}, "provider":"native"})
        _, first = store.create_operation_attempt(item.work_item_id, intent="execute",
            instruction="First", task="First", provider="native")
        store.update_attempt(first.attempt_id, execution_status="succeeded")
        _, second = store.create_operation_attempt(item.work_item_id, intent="amend",
            instruction="Second", task="Second", provider="native",
            attempt_metadata={**session_metadata, "provider_result":{
                **session_metadata.get("provider_result", {}),
                "provider_branch":{"screenshot":"large" * 50_000}}})
        for kind in ("business.export", "Business.export", "provider_payload"):
            store.register_artifact(item.work_item_id, kind=kind,
                metadata={"screenshot":"large" * 50_000})
        model = _model(store)
        with monkeypatch.context() as full:
            full.setattr(store, "latest_attempt", lambda work_id, **_kwargs:store.list_attempts(work_id)[-1])
            full.setattr(store, "artifact_counts", lambda work_id:{
                "business":sum(row.kind.startswith("business.") for row in store.list_artifacts(work_id)),
                "runtime":sum(not row.kind.startswith("business.") for row in store.list_artifacts(work_id))})
            expected = model.project_items(store.list_work_items())
        with monkeypatch.context() as light:
            def full_read_forbidden(*_args, **_kwargs):
                raise AssertionError("list projection must not hydrate all attempts or artifact payloads")
            light.setattr(store, "list_attempts", full_read_forbidden)
            light.setattr(store, "list_artifacts", full_read_forbidden)
            projected = model.project_items(store.list_work_items(include_presentation=False))
            assert projected == expected
            assert projected[0]["attemptId"] == second.attempt_id
            assert projected[0]["sessionId"] == expected_session
            store.update_attempt(first.attempt_id, metadata={"later_observation":True})
            store.update_attempt(second.attempt_id, execution_status="running")
            request = store.create_permission_request(item.work_item_id, attempt_id=second.attempt_id,
                capability="filesystem.write", action="write", scope_paths=[str(workspace)])
            current = model.project_items(store.list_work_items(include_presentation=False))[0]
            assert current["attemptId"] == second.attempt_id and current["execution"] == "running"
            assert current["pendingPermissionRequestId"] == request.request_id
            assert current["businessArtifactCount"] == 1 and current["runtimeArtifactCount"] == 2
        detail = model.detail(item.work_item_id)
        assert detail["attempts"][-1]["metadata"]["provider_result"]["provider_branch"]
        assert all(row["metadata"]["screenshot"] for row in detail["artifacts"])


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


def test_batch_projection_matches_single_items_and_rechecks_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    with WorkLedgerStore(tmp_path / "ledger.sqlite3", clock=lambda: 10.0) as store:
        project = store.create_or_get_project(workspace)
        items = [store.create_work_item(project.project_id, title=f"Task {i}") for i in range(2)]
        items.append(store.create_work_item(project.project_id, title="No workspace", workspace_mode="none"))
        model = _model(store)
        before = [_durable_facts(store, item.work_item_id) for item in items]
        assert model.project_items(items) == [model.project_item(item) for item in items]
        assert [_durable_facts(store, item.work_item_id) for item in items] == before

        workspace.rmdir()
        missing = model.project_items(items)
        assert all(row["workspaceExists"] is False for row in missing)
        assert [row["attention"] for row in missing] == ["error", "error", "none"]
        workspace.mkdir()
        restored = model.project_items(items)
        assert [row["workspaceExists"] for row in restored] == [True, True, False]


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


def test_orphaned_projection_is_unknown_not_terminal_or_retryable() -> None:
    with tempfile.TemporaryDirectory(prefix="work_read_orphaned_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            project = store.create_or_get_project(workspace, name="Unknown outcome")
            item = store.create_work_item(
                project.project_id,
                title="Reconcile native submission",
                workspace_path=workspace,
            )
            _, attempt = store.create_operation_attempt(
                item.work_item_id,
                intent="execute",
                instruction="Apply the requested change.",
                provider="codex_app_server",
                task="Apply the requested change.",
            )
            store.update_attempt(
                attempt.attempt_id,
                execution_status="orphaned",
                metadata={"runtime_resumable": False},
            )

            row = _model(store, unkept_draft=True).project_item(store.get_work_item(item.work_item_id))  # type: ignore[arg-type]

            assert row["execution"] == "orphaned"
            assert row["liveness"] == "orphaned"
            assert row["activity"]["phase"] == "orphaned"
            assert row["activity"]["uncertainty"] == "native_outcome_unknown"
            assert row["attention"] == "error"
            assert row["canRetry"] is False
            assert row["canResume"] is False
            assert row["canPromoteToProject"] is False

            store.update_attempt(
                attempt.attempt_id,
                metadata={
                    "runtime_resumable": True,
                    "provider_liveness": {"state": "cancel_pending"},
                },
            )
            cancelling = _model(store).project_item(store.get_work_item(item.work_item_id))  # type: ignore[arg-type]
            assert cancelling["canResume"] is False

            store.set_work_item_state(item.work_item_id, "archived")
            archived = _model(store).project_item(store.get_work_item(item.work_item_id))  # type: ignore[arg-type]
            assert archived["canReopen"] is False


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all work read model tests passed")


if __name__ == "__main__":
    _main()
