"""Contract tests for canonical Provider event ownership."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.work_ledger_store import WorkLedgerStore
from server.provider_event_ingestion import ProviderEventIngestor
from server.work_activity_snapshot import ACTIVITY_METADATA_KEY


def _prepared_attempt(store: WorkLedgerStore, workspace: Path):
    project = store.create_or_get_project(workspace, name="Events")
    item = store.create_work_item(
        project.project_id,
        title="Track one run",
        workspace_path=workspace,
    )
    _, attempt = store.create_operation_attempt(
        item.work_item_id,
        intent="execute",
        instruction="Track one run.",
        provider="locus",
        task="Track one run.",
    )
    return item, attempt


def _event(attempt_id: str, event_type: str, *, run_id: str = "run-one") -> dict:
    return {
        "provider": "locus",
        "run_id": run_id,
        "type": event_type,
        "payload": {},
        "metadata": {"work": {"attempt_id": attempt_id}},
    }


def test_run_identity_is_bound_once_and_mismatches_fail_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="provider_ingestion_identity_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            item, attempt = _prepared_attempt(store, workspace)
            ingestor = ProviderEventIngestor(
                store,
                clock=lambda: 100.0,
                default_surface="test",
            )
            created = ingestor.ingest_event(_event(attempt.attempt_id, "run.created"))
            assert created is not None
            assert created.accepted is True
            assert created.attempt.provider_run_id == "run-one"
            assert len(store.list_work_items()) == 1

            mismatched_run = ingestor.ingest_event(
                _event(attempt.attempt_id, "run.started", run_id="run-other")
            )
            mismatched_provider = ingestor.ingest_event(
                {
                    **_event(attempt.attempt_id, "run.started"),
                    "provider": "openclaw",
                }
            )
            unknown_explicit = ingestor.ingest_result(
                {
                    "provider": "locus",
                    "run_id": "run-unknown",
                    "attempt_id": "attempt-does-not-exist",
                    "status": "done",
                }
            )
            assert mismatched_run is None
            assert mismatched_provider is None
            assert unknown_explicit is None
            assert len(store.list_work_items()) == 1
            assert store.get_work_item(item.work_item_id) is not None


def test_terminal_result_consumes_run_evidence_and_projects_activity() -> None:
    with tempfile.TemporaryDirectory(prefix="provider_ingestion_result_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            _, attempt = _prepared_attempt(store, workspace)
            ingestor = ProviderEventIngestor(
                store,
                clock=lambda: 200.0,
                default_surface="test",
            )
            ingestor.ingest_event(_event(attempt.attempt_id, "run.created"))
            ingestor.ingest_event(_event(attempt.attempt_id, "run.started"))
            ingestor.event_fact("run-one")["pending_inputs"] = 1

            result = ingestor.ingest_result(
                {
                    "provider": "locus",
                    "run_id": "run-one",
                    "status": "done",
                    "result": "Finished.",
                    "metadata": {"provider_session": {"id": "opaque"}},
                }
            )
            assert result is not None
            assert result.status == "succeeded"
            assert result.evidence["pending_inputs"] == 1
            assert ingestor.event_fact("run-one")["pending_inputs"] == 0
            stored = store.get_attempt(attempt.attempt_id)
            assert stored is not None
            assert stored.execution_status == "succeeded"
            assert stored.result == "Finished."
            assert stored.metadata["provider_session"] == {"id": "opaque"}
            assert stored.metadata[ACTIVITY_METADATA_KEY]["phase"] == "review"


def test_late_contradictory_result_cannot_reinterpret_terminal_truth() -> None:
    with tempfile.TemporaryDirectory(prefix="provider_ingestion_terminal_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            _, attempt = _prepared_attempt(store, workspace)
            ingestor = ProviderEventIngestor(
                store,
                clock=lambda: 300.0,
                default_surface="test",
            )
            ingestor.ingest_event(_event(attempt.attempt_id, "run.created"))
            first = ingestor.ingest_result(
                {
                    "provider": "locus",
                    "run_id": "run-one",
                    "status": "done",
                    "result": "Canonical success.",
                }
            )
            late = ingestor.ingest_result(
                {
                    "provider": "locus",
                    "run_id": "run-one",
                    "status": "failed",
                    "error": "Late contradiction.",
                }
            )
            assert first is not None and first.status == "succeeded"
            assert late is not None and late.status == "succeeded"
            stored = store.get_attempt(attempt.attempt_id)
            assert stored is not None
            assert stored.execution_status == "succeeded"
            assert stored.result == "Canonical success."
            assert stored.error == ""

            cancelled = ingestor.ingest_event(
                _event(attempt.attempt_id, "run.cancelled")
            )
            assert cancelled is not None
            assert cancelled.accepted is False
            assert cancelled.attempt.execution_status == "succeeded"


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all provider event ingestion tests passed")


if __name__ == "__main__":
    _main()
