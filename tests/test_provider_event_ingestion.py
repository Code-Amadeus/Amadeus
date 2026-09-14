"""Contract tests for canonical Provider event ownership."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.work_ledger_store import WorkLedgerStore
from agent_host.provider_identity import (
    PARENT_CONTEXT_DELIVERED_EVENT,
    PARENT_CONTEXT_DELIVERY_METADATA_KEY,
    parent_context_delivery_receipt,
)
from server.provider_event_ingestion import (
    PROVIDER_TERMINAL_PIPELINE_METADATA_KEY,
    ProviderEventIngestor,
)
from server.work_activity_snapshot import ACTIVITY_METADATA_KEY


def test_work_item_title_uses_display_label_with_task_fallback() -> None:
    assert ProviderEventIngestor.work_item_title(
        "就按刚才说的继续。", "  实现会话恢复方案  "
    ) == "实现会话恢复方案"
    assert ProviderEventIngestor.work_item_title(
        "就按刚才说的继续。", ""
    ) == "就按刚才说的继续。"
    assert ProviderEventIngestor.work_item_title(
        "原始任务", "\ud800"
    ) == "原始任务"


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
            assert result.pipeline_required is True
            assert result.pipeline_receipt["state"] == "pending"
            assert result.pipeline_receipt["facts"]["pending_inputs"] == 1
            assert ingestor.event_fact("run-one")["pending_inputs"] == 0
            stored = store.get_attempt(attempt.attempt_id)
            assert stored is not None
            assert stored.execution_status == "succeeded"
            assert stored.result == "Finished."
            assert stored.metadata["provider_session"] == {"id": "opaque"}
            assert stored.metadata[ACTIVITY_METADATA_KEY]["phase"] == "review"


def test_terminal_event_does_not_commit_status_before_canonical_result() -> None:
    with tempfile.TemporaryDirectory(prefix="provider_ingestion_terminal_event_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            _, attempt = _prepared_attempt(store, workspace)
            ingestor = ProviderEventIngestor(
                store,
                clock=lambda: 210.0,
                default_surface="test",
            )
            ingestor.ingest_event(_event(attempt.attempt_id, "run.created"))
            ingestor.ingest_event(_event(attempt.attempt_id, "run.started"))

            terminal_event = _event(attempt.attempt_id, "run.failed")
            terminal_event["payload"] = {
                "status": "error",
                "error": "terminal event arrived before result",
            }
            observed = ingestor.ingest_event(terminal_event)
            before_result = store.get_attempt(attempt.attempt_id)

            assert observed is not None and observed.accepted is True
            assert before_result is not None
            assert before_result.execution_status == "running"
            assert before_result.error == ""
            assert PROVIDER_TERMINAL_PIPELINE_METADATA_KEY not in before_result.metadata

            terminal_result = ingestor.ingest_result(
                {
                    "provider": "locus",
                    "run_id": "run-one",
                    "status": "error",
                    "error": "terminal event arrived before result",
                }
            )
            assert terminal_result is not None
            assert terminal_result.pipeline_required is True
            assert terminal_result.status == "failed"


def test_orphaned_result_retains_evidence_and_cannot_grant_resume() -> None:
    with tempfile.TemporaryDirectory(prefix="provider_ingestion_orphaned_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            _, attempt = _prepared_attempt(store, workspace)
            ingestor = ProviderEventIngestor(
                store,
                clock=lambda: 225.0,
                default_surface="test",
            )
            ingestor.ingest_event(_event(attempt.attempt_id, "run.created"))
            ingestor.event_fact("run-one")["pending_inputs"] = 1

            orphaned = ingestor.ingest_result(
                {
                    "provider": "locus",
                    "run_id": "run-one",
                    "status": "orphaned",
                    "error": "native outcome is unknown",
                    "metadata": {"runtime_resumable": True},
                }
            )

            assert orphaned is not None and orphaned.status == "orphaned"
            assert orphaned.evidence == {}
            assert orphaned.pipeline_required is False
            assert orphaned.pipeline_receipt == {}
            assert ingestor.event_fact("run-one")["pending_inputs"] == 1
            stored = store.get_attempt(attempt.attempt_id)
            assert stored is not None
            assert stored.execution_status == "orphaned"
            assert stored.metadata["runtime_resumable"] is False
            assert PROVIDER_TERMINAL_PIPELINE_METADATA_KEY not in stored.metadata

            terminal = ingestor.ingest_result(
                {
                    "provider": "locus",
                    "run_id": "run-one",
                    "status": "done",
                    "result": "Reconciled terminal result.",
                }
            )
            assert terminal is not None and terminal.status == "succeeded"
            assert terminal.evidence["pending_inputs"] == 1
            assert terminal.pipeline_required is True
            assert ingestor.event_fact("run-one")["pending_inputs"] == 0


def test_terminal_receipt_exact_replay_is_noop_after_completion() -> None:
    with tempfile.TemporaryDirectory(prefix="provider_ingestion_terminal_receipt_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        now = [100.0]
        with WorkLedgerStore(root / "ledger.sqlite3", clock=lambda: now[0]) as store:
            _, attempt = _prepared_attempt(store, workspace)
            ingestor = ProviderEventIngestor(
                store,
                clock=lambda: now[0],
                default_surface="test",
            )
            ingestor.ingest_event(_event(attempt.attempt_id, "run.created"))
            payload = {
                "provider": "locus",
                "run_id": "run-one",
                "status": "done",
                "result": "Canonical success.",
                "metadata": {"bounded": "evidence"},
            }

            first = ingestor.ingest_result(payload)
            assert first is not None and first.pipeline_required is True
            pending = store.get_attempt(attempt.attempt_id)
            assert pending is not None
            pending_payload = pending.to_dict()

            now[0] = 200.0
            replay_pending = ingestor.ingest_result(payload)
            assert replay_pending is not None
            assert replay_pending.pipeline_required is True
            assert store.get_attempt(attempt.attempt_id).to_dict() == pending_payload  # type: ignore[union-attr]

            assert ingestor.complete_terminal_pipeline(
                attempt.attempt_id,
                first.pipeline_receipt,
            ) is True
            completed = store.get_attempt(attempt.attempt_id)
            assert completed is not None
            completed_payload = completed.to_dict()
            assert completed.metadata[PROVIDER_TERMINAL_PIPELINE_METADATA_KEY]["state"] == (
                "completed"
            )

            now[0] = 300.0
            replay_completed = ingestor.ingest_result(payload)
            contradiction = ingestor.ingest_result(
                {**payload, "result": "Contradictory replacement."}
            )

            assert replay_completed is not None
            assert replay_completed.pipeline_required is False
            assert contradiction is not None
            assert contradiction.pipeline_required is False
            assert store.get_attempt(attempt.attempt_id).to_dict() == completed_payload  # type: ignore[union-attr]
            assert store.list_pending_terminal_provider_attempts() == []

            damaged_receipt = dict(
                completed.metadata[PROVIDER_TERMINAL_PIPELINE_METADATA_KEY]
            )
            damaged_receipt["state"] = "pending"
            damaged_receipt["receipt_sha256"] = "0" * 64
            store.update_attempt(
                attempt.attempt_id,
                metadata={
                    PROVIDER_TERMINAL_PIPELINE_METADATA_KEY: damaged_receipt,
                },
            )
            damaged = store.get_attempt(attempt.attempt_id)
            assert damaged is not None
            assert store.list_pending_terminal_provider_attempts(limit=1) == [damaged]
            assert ingestor.terminal_replay_payload(damaged) is None


def test_stale_lifecycle_events_cannot_reopen_an_orphaned_attempt() -> None:
    with tempfile.TemporaryDirectory(prefix="provider_ingestion_sequence_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            _, attempt = _prepared_attempt(store, workspace)
            ingestor = ProviderEventIngestor(
                store,
                clock=lambda: 250.0,
                default_surface="test",
            )
            created = _event(attempt.attempt_id, "run.created")
            created["sequence"] = 1
            ingestor.ingest_event(created)
            orphaned = {
                **_event(attempt.attempt_id, "run.status"),
                "sequence": 4,
                "payload": {"status": "orphaned"},
            }
            accepted = ingestor.ingest_event(orphaned)
            assert accepted is not None and accepted.accepted is True
            before = store.get_attempt(attempt.attempt_id)
            assert before is not None and before.execution_status == "orphaned"

            stale_events = [
                {
                    **_event(attempt.attempt_id, "run.status"),
                    "sequence": 3,
                    "payload": {"status": "running", "liveness": "active"},
                },
                {**_event(attempt.attempt_id, "run.started"), "sequence": 2},
                {**_event(attempt.attempt_id, "run.created"), "sequence": 1},
                {
                    **_event(attempt.attempt_id, "run.failed"),
                    "sequence": 3,
                    "payload": {"error": "stale failure"},
                },
            ]
            for event in stale_events:
                rejected = ingestor.ingest_event(event)
                assert rejected is not None and rejected.accepted is False

            after = store.get_attempt(attempt.attempt_id)
            assert after is not None
            assert after.execution_status == "orphaned"
            assert after.error == before.error
            assert after.metadata[ACTIVITY_METADATA_KEY]["eventSequence"] == 4


def test_only_native_context_acceptance_persists_the_delivery_cursor() -> None:
    with tempfile.TemporaryDirectory(prefix="provider_ingestion_context_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        now = [100.0]
        with WorkLedgerStore(root / "ledger.sqlite3", clock=lambda: now[0]) as store:
            _, attempt = _prepared_attempt(store, workspace)
            ingestor = ProviderEventIngestor(
                store,
                clock=lambda: now[0],
                default_surface="test",
            )
            ingestor.ingest_event(_event(attempt.attempt_id, "run.created"))

            planned = store.get_attempt(attempt.attempt_id)
            assert planned is not None
            assert PARENT_CONTEXT_DELIVERY_METADATA_KEY not in planned.metadata
            item_before = store.get_work_item(planned.work_item_id)
            assert item_before is not None
            attempt_updated_at = planned.updated_at
            item_updated_at = item_before.updated_at
            item_last_activity_at = item_before.last_activity_at
            now[0] = 250.0

            source_metadata = {
                "work": {"attempt_id": attempt.attempt_id},
                "turn_id": "turn-delivered",
                "source_user_text": "Apply the delivered constraint.",
                "source_context_scope": "chat:chat-events",
                "source_context_mode": "snapshot",
            }
            receipt = parent_context_delivery_receipt(source_metadata)
            delivered = ingestor.ingest_event(
                {
                    "provider": "locus",
                    "run_id": "run-one",
                    "type": PARENT_CONTEXT_DELIVERED_EVENT,
                    "payload": {},
                    "metadata": {
                        **source_metadata,
                        PARENT_CONTEXT_DELIVERY_METADATA_KEY: receipt,
                    },
                }
            )
            assert delivered is not None and delivered.accepted is True
            stored = store.get_attempt(attempt.attempt_id)
            assert stored is not None
            assert stored.metadata[PARENT_CONTEXT_DELIVERY_METADATA_KEY] == receipt
            assert stored.metadata["source_context_cursor_turn_id"] == (
                "turn-delivered"
            )
            item_after = store.get_work_item(stored.work_item_id)
            assert item_after is not None
            assert stored.updated_at == attempt_updated_at
            assert item_after.updated_at == item_updated_at
            assert item_after.last_activity_at == item_last_activity_at


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


def test_unbound_provider_events_do_not_accept_work() -> None:
    with tempfile.TemporaryDirectory(prefix="provider_unbound_events_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            ingestor = ProviderEventIngestor(store, clock=lambda: 100.0, default_surface="test")
            # Absence of Work ownership is sufficient. No provider/mode opt-out
            # marker should be necessary to keep a conversation out of Work.
            for metadata in ({}, {"conversation_mode":"cooperative"}):
                event = {"provider":"locus", "run_id":"unowned-run",
                    "payload":{"cwd":str(root), "task":"Answer a question"}, "metadata":metadata}
                for kind in ("run.created", "run.started", "run.completed", "permission.requested"):
                    assert ingestor.ingest_event({**event, "type":kind}) is None
                assert ingestor.ingest_result({**event, "status":"done", "result":"An answer"}) is None
            assert store.list_work_items() == []
            assert store.list_projects() == []
            assert store.get_focus("test") is None


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all provider event ingestion tests passed")


if __name__ == "__main__":
    _main()
