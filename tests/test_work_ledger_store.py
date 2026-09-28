"""SQLite WorkLedger persistence and identity tests.

Runs standalone through tools/run_tests.py and is also pytest-compatible.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.work_ledger_store import (
    SCHEMA_VERSION,
    WorkLedgerConflict,
    WorkLedgerStore,
)
from agent_host import work_ledger_store as work_ledger_store_module
from agent_host.work_ledger_types import CompletionDecision
from server.work_completion import CompletionEvidence, assess_completion
from work_ledger_fixtures import create_historical_schema, seed_historical_work


def test_lightweight_reads_trim_json_before_decode_without_changing_full_records(tmp_path, monkeypatch):
    with WorkLedgerStore(tmp_path / "lightweight.sqlite3", clock=lambda:10.0) as store:
        project, item = _create_project_and_item(store, tmp_path / "project")
        presentation = {"text":"presentation-payload-" + "p" * 1_000_000}
        store.update_work_item_metadata(item.work_item_id,
            {"presentation":presentation, "provider":"native", "keep":{"nested":True}})
        _, first = store.create_operation_attempt(item.work_item_id, intent="execute",
            instruction="First version", provider="native", task="First version")
        store.update_attempt(first.attempt_id, execution_status="succeeded")
        _, second = store.create_operation_attempt(item.work_item_id, intent="amend",
            instruction="Current version", provider="native", task="Current version",
            attempt_metadata={"provider_result":{"provider_branch":{"text":"branch-payload-" + "b" * 1_000_000},
                "session_id":"session-snake", "sessionId":"session-camel", "keep":True},
                "provider_session":{"session_id":"native-context"}})
        full_item = store.get_work_item(item.work_item_id)
        full_attempt = store.get_attempt(second.attempt_id)
        assert store.list_work_items(project_id=project.project_id) == [full_item]
        assert store.list_attempts(item.work_item_id)[-1] == full_attempt
        assert store.latest_attempt(item.work_item_id) == full_attempt

        decoded = []
        original_load = work_ledger_store_module._load_json
        def observe_load(value):
            decoded.append(value)
            return original_load(value)
        monkeypatch.setattr(work_ledger_store_module, "_load_json", observe_load)
        light_item = store.list_work_items(project_id=project.project_id,
            states=["open"], include_presentation=False)[0]
        light_attempt = store.latest_attempt(item.work_item_id, include_provider_branch=False)
        assert light_item == replace(full_item,
            metadata={key:value for key, value in full_item.metadata.items() if key != "presentation"})
        assert light_attempt == replace(full_attempt, metadata={**full_attempt.metadata,
            "provider_result":{key:value for key, value in full_attempt.metadata["provider_result"].items()
                if key != "provider_branch"}})
        assert all("presentation-payload-" not in raw and "branch-payload-" not in raw for raw in decoded)
        assert store.get_work_item(item.work_item_id) == full_item
        assert store.get_attempt(second.attempt_id) == full_attempt
        assert store.latest_attempt("missing-work", include_provider_branch=False) is None


def test_artifact_counts_preserve_case_sensitive_business_prefix_and_scope(tmp_path):
    with WorkLedgerStore(tmp_path / "counts.sqlite3") as store:
        project, item = _create_project_and_item(store, tmp_path / "project")
        other = store.create_work_item(project.project_id, title="Other scope")
        assert store.artifact_counts(item.work_item_id) == {"business":0, "runtime":0}
        kinds = ("business.export", "business.", "business.Δ", "Business.export",
            "BUSINESS.export", "business", "businesses.export", "business．export", "provider_payload")
        for kind in kinds:
            store.register_artifact(item.work_item_id, kind=kind, metadata={"screenshot":"opaque" * 20_000})
        store.register_artifact(other.work_item_id, kind="business.export")
        expected = sum(kind.startswith("business.") for kind in kinds)
        assert store.artifact_counts(item.work_item_id) == {
            "business":expected, "runtime":len(kinds) - expected}
        assert store.artifact_counts(other.work_item_id) == {"business":1, "runtime":0}
        store.register_artifact(item.work_item_id, kind="business.new")
        assert store.artifact_counts(item.work_item_id)["business"] == expected + 1


def _create_project_and_item(store: WorkLedgerStore, project_root: Path):
    project_root.mkdir(parents=True, exist_ok=True)
    project = store.create_or_get_project(project_root, metadata={"source": "test"})
    item = store.create_work_item(
        project.project_id,
        title="Persist work history",
        goal="Keep provider attempts distinguishable.",
    )
    return project, item


def test_session_roster_preserves_legacy_metadata_and_latest_attempt_scope(tmp_path):
    with WorkLedgerStore(tmp_path / "session-roster.sqlite3") as store:
        project, first = _create_project_and_item(store, tmp_path / "project")
        items = [first, *(store.create_work_item(project.project_id, title=f"Work {n}")
            for n in range(4))]
        metadata = [
            {"session_id":"current", "provider_result":{"sessionId":"other"}},
            {"provider_result":{"session_id":"current"}},
            {"provider_result":{"sessionId":"current"}},
            {"session_id":" current "},
            {"session_id":"current"},
        ]
        for item, fields in zip(items, metadata, strict=True):
            _, attempt = store.create_operation_attempt(item.work_item_id, intent="execute",
                instruction="First version", provider="native", task="First version",
                attempt_metadata=fields)
            store.update_attempt(attempt.attempt_id, execution_status="succeeded")
        store.create_operation_attempt(items[-1].work_item_id, intent="amend",
            instruction="Continue elsewhere", provider="native", task="Continue elsewhere",
            attempt_metadata={"session_id":"other"})
        assert {item.work_item_id for item in store.list_work_items(session_id="current")} == {
            item.work_item_id for item in items[:-1]}
        assert [item.work_item_id for item in store.list_work_items(session_id="other")] == [
            items[-1].work_item_id]
        assert len(store.list_work_items()) == 5


def test_schema_migration_is_idempotent_and_records_survive_restart() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_migration_") as temp:
        root = Path(temp)
        db_path = root / "runtime" / "work_ledger.sqlite3"
        first = WorkLedgerStore(db_path)
        project, item = _create_project_and_item(first, root / "project")
        first.close()

        second = WorkLedgerStore(db_path)
        assert second.schema_version == SCHEMA_VERSION
        assert second.get_project(project.project_id) is not None
        loaded = second.get_work_item(item.work_item_id)
        assert loaded is not None
        assert loaded.title == "Persist work history"
        assert len(second.list_projects()) == 1
        second.close()


def test_conversation_binding_persists_and_validates_work_item_project() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_binding_") as temp:
        root = Path(temp)
        db_path = root / "ledger.sqlite3"
        store = WorkLedgerStore(db_path)
        project, item = _create_project_and_item(store, root / "project")
        other, _ = _create_project_and_item(store, root / "other")

        bound = store.bind_conversation(
            "chat-1",
            project.project_id,
            anchor_work_item_id=item.work_item_id,
            metadata={"source": "slice"},
        )
        assert bound.binding_kind == "work_item"
        assert bound.anchor_work_item_id == item.work_item_id
        store.close()

        reopened = WorkLedgerStore(db_path)
        loaded = reopened.get_conversation_binding("chat-1")
        assert loaded is not None
        assert loaded.project_id == project.project_id
        assert loaded.metadata["source"] == "slice"
        try:
            reopened.bind_conversation(
                "chat-2",
                other.project_id,
                anchor_work_item_id=item.work_item_id,
            )
        except WorkLedgerConflict:
            pass
        else:
            raise AssertionError("a WorkItem anchor cannot cross Project ownership")
        assert reopened.clear_conversation_binding("chat-1") is True
        assert reopened.get_conversation_binding("chat-1") is None
        reopened.close()


def test_attempt_metadata_compare_and_set_is_atomic_and_monotonic() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_attempt_cas_") as temp:
        root = Path(temp)
        store = WorkLedgerStore(root / "ledger.sqlite3")
        _project, item = _create_project_and_item(store, root / "project")
        attempt = store.create_attempt(
            item.work_item_id,
            provider="codex",
            task="Preserve one recovery winner.",
        )
        initial = {"state": "unclaimed", "ordinal": 1}
        persisted, initialized = store.compare_and_set_attempt_metadata(
            attempt.attempt_id,
            key="recovery",
            expected_present=False,
            value=initial,
        )
        assert initialized is True
        assert persisted.metadata["recovery"] == initial

        stale, replaced = store.compare_and_set_attempt_metadata(
            attempt.attempt_id,
            key="recovery",
            expected_present=False,
            value={"state": "stale"},
        )
        assert replaced is False
        assert stale.metadata["recovery"] == initial

        started = {"state": "started", "ordinal": 1}
        current, replaced = store.compare_and_set_attempt_metadata(
            attempt.attempt_id,
            key="recovery",
            expected_present=True,
            expected_value=initial,
            value=started,
        )
        assert replaced is True
        assert current.metadata["recovery"] == started

        stale, replaced = store.compare_and_set_attempt_metadata(
            attempt.attempt_id,
            key="recovery",
            expected_present=True,
            expected_value=initial,
            value={"state": "unclaimed"},
        )
        assert replaced is False
        assert stale.metadata["recovery"] == started
        store.close()


def test_version_one_database_upgrades_writer_lease_schema() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_v1_upgrade_") as temp:
        db_path = Path(temp) / "ledger.sqlite3"
        create_historical_schema(db_path, 1).close()

        upgraded = WorkLedgerStore(db_path)
        assert upgraded.schema_version == SCHEMA_VERSION
        assert upgraded.list_writer_leases() == []
        upgraded.close()


def test_version_two_migration_reconciles_duplicate_current_attempts() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_v2_upgrade_") as temp:
        root = Path(temp)
        db_path = root / "ledger.sqlite3"
        connection = create_historical_schema(db_path, 2)
        seeded = seed_historical_work(connection, root / "project")
        connection.execute(
            "INSERT INTO workspace_leases(lease_id,workspace_path,workspace_identity,work_item_id,"
            "attempt_id,status,acquired_at,heartbeat_at) VALUES ('lease_historical',?,?,?,?,'active',1,1)",
            (seeded["workspace_path"], seeded["workspace_identity"], seeded["work_item_id"], seeded["attempt_id"]),
        )
        connection.execute(
            """
            INSERT INTO run_attempts (
                attempt_id, work_item_id, attempt_number, provider, provider_run_id,
                task, mode, execution_status, created_at, updated_at, metadata_json
            ) VALUES ('attempt_newer', ?, 2, 'locus', '', 'Newer writer',
                      'agent', 'queued', 2, 2, '{}')
            """,
            (seeded["work_item_id"],),
        )
        connection.close()

        upgraded = WorkLedgerStore(db_path)
        assert upgraded.schema_version == SCHEMA_VERSION
        attempts = upgraded.list_attempts(seeded["work_item_id"])
        assert [attempt.execution_status for attempt in attempts] == ["orphaned", "queued"]
        older_lease = upgraded.get_writer_lease(seeded["attempt_id"])
        assert older_lease is not None and older_lease.status == "stale"
        try:
            upgraded.create_attempt(seeded["work_item_id"], provider="locus", task="Third writer")
        except WorkLedgerConflict:
            pass
        else:
            raise AssertionError("the migrated active-attempt invariant must be enforced")
        upgraded.close()


def test_version_ten_writer_lease_migrates_to_explicit_work_owner() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_v10_writer_upgrade_") as temp:
        root = Path(temp)
        db_path = root/"ledger.sqlite3"
        connection = create_historical_schema(db_path, 7)
        seeded = seed_historical_work(connection, root/"project")
        for migration in (
                work_ledger_store_module._MIGRATION_8,
                work_ledger_store_module._MIGRATION_9,
                work_ledger_store_module._MIGRATION_10):
            connection.execute("BEGIN IMMEDIATE")
            for statement in migration:
                connection.execute(statement)
            connection.execute("COMMIT")
        connection.execute("""INSERT INTO workspace_leases(
            lease_id,workspace_path,workspace_identity,work_item_id,attempt_id,
            status,acquired_at,heartbeat_at,metadata_json)
            VALUES ('lease-v10',?,?,?,?, 'active',1,1,'{}')""",
            (seeded["workspace_path"], seeded["workspace_identity"],
             seeded["work_item_id"], seeded["attempt_id"]))
        connection.close()

        upgraded = WorkLedgerStore(db_path)
        lease = upgraded.get_writer_lease(seeded["attempt_id"])
        assert upgraded.schema_version == 11
        assert lease is not None and lease.owner_kind == "work_attempt"
        assert lease.work_item_id == seeded["work_item_id"]
        assert not lease.session_id and not lease.context_id
        assert not lease.provider_effect_id and not lease.provider_run_id
        upgraded.close()


def test_version_three_database_upgrades_permission_request_schema() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_v3_upgrade_") as temp:
        db_path = Path(temp) / "ledger.sqlite3"
        create_historical_schema(db_path, 3).close()

        upgraded = WorkLedgerStore(db_path)
        assert upgraded.schema_version == SCHEMA_VERSION
        _, item = _create_project_and_item(upgraded, Path(temp) / "project")
        attempt = upgraded.create_attempt(item.work_item_id, provider="locus", task="Migrate")
        request = upgraded.create_permission_request(
            item.work_item_id,
            attempt_id=attempt.attempt_id,
            capability="filesystem.write",
            action="Write file",
        )
        assert request.status == "pending"
        upgraded.close()


def test_project_identity_resolves_relative_and_real_path_aliases() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_identity_") as temp:
        root = Path(temp)
        project_root = root / "real-project"
        project_root.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            first = store.create_or_get_project(project_root)
            dot_alias = project_root / "subdir" / ".."
            second = store.create_or_get_project(dot_alias, metadata={"alias": True})
            assert first.project_id == second.project_id
            assert len(store.list_projects()) == 1

            link = root / "project-link"
            try:
                link.symlink_to(project_root, target_is_directory=True)
            except (OSError, NotImplementedError):
                link = project_root
            via_real_alias = store.create_or_get_project(link)
            assert via_real_alias.project_id == first.project_id
            assert store.get_project_by_path(link).project_id == first.project_id  # type: ignore[union-attr]


def test_continue_attempt_numbers_and_provider_binding_are_persistent() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_attempts_") as temp:
        root = Path(temp)
        db_path = root / "ledger.sqlite3"
        store = WorkLedgerStore(db_path)
        _, item = _create_project_and_item(store, root / "project")
        first = store.create_attempt(
            item.work_item_id,
            provider="locus",
            task="First pass",
            metadata={"work": {"reason": "new"}},
        )
        bound = store.bind_provider_run(first.attempt_id, "locus_provider_run_1")
        assert bound.provider_run_id == "locus_provider_run_1"
        running = store.update_attempt(first.attempt_id, execution_status="running")
        assert running.started_at is not None
        done = store.update_attempt(
            first.attempt_id,
            execution_status="succeeded",
            result="Generated the requested file.",
        )
        assert done.finished_at is not None
        # Repeated terminal evidence is idempotent; contradictory terminal
        # evidence is rejected instead of silently rewriting history.
        store.update_attempt(first.attempt_id, execution_status="succeeded")
        try:
            store.update_attempt(first.attempt_id, execution_status="failed")
        except WorkLedgerConflict:
            pass
        else:
            raise AssertionError("terminal attempt must not change terminal status")
        second = store.create_attempt(
            item.work_item_id,
            provider="locus",
            task="Continue the same objective",
        )
        assert (first.attempt_number, second.attempt_number) == (1, 2)
        store.close()

        reopened = WorkLedgerStore(db_path)
        attempts = reopened.list_attempts(item.work_item_id)
        assert [attempt.attempt_number for attempt in attempts] == [1, 2]
        assert reopened.get_attempt_by_provider_run("locus_provider_run_1").attempt_id == first.attempt_id  # type: ignore[union-attr]
        reopened.close()


def test_operations_distinguish_amendment_from_retry() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_operations_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            _, item = _create_project_and_item(store, root / "project")
            first_operation, first_attempt = store.create_operation_attempt(
                item.work_item_id,
                intent="execute",
                instruction="Build a game",
                provider="locus",
                task="Build a game",
            )
            store.update_attempt(first_attempt.attempt_id, execution_status="succeeded")
            second_operation, second_attempt = store.create_operation_attempt(
                item.work_item_id,
                intent="amend",
                instruction="Make it two-player",
                provider="codex",
                task="Make it two-player",
            )
            store.update_attempt(second_attempt.attempt_id, execution_status="failed")
            retry = store.create_attempt(
                item.work_item_id,
                operation_id=second_operation.operation_id,
                provider="codex",
                task="Make it two-player",
            )

            operations = store.list_operations(item.work_item_id)
            attempts = store.list_attempts(item.work_item_id)
            assert [operation.intent for operation in operations] == ["execute", "amend"]
            assert [operation.operation_number for operation in operations] == [1, 2]
            assert [attempt.attempt_number for attempt in attempts] == [1, 2, 3]
            assert first_attempt.operation_id == first_operation.operation_id
            assert second_attempt.operation_id == retry.operation_id
            assert second_attempt.operation_id == second_operation.operation_id


def test_new_operation_reopens_accepted_work_but_archived_requires_reopen() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_operation_reopen_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            _, item = _create_project_and_item(store, root / "project")
            store.set_work_item_state(item.work_item_id, "accepted")
            operation, attempt = store.create_operation_attempt(
                item.work_item_id,
                intent="amend",
                instruction="Add another mode",
                provider="locus",
                task="Add another mode",
            )
            assert operation.operation_number == 1
            assert attempt.attempt_number == 1
            assert store.get_work_item(item.work_item_id).state == "open"  # type: ignore[union-attr]
            store.update_attempt(attempt.attempt_id, execution_status="cancelled")
            store.set_work_item_state(item.work_item_id, "archived")
            try:
                store.create_operation_attempt(
                    item.work_item_id,
                    intent="amend",
                    instruction="Change it again",
                    provider="locus",
                    task="Change it again",
                )
            except WorkLedgerConflict:
                pass
            else:
                raise AssertionError("archived work must be explicitly reopened")


def test_schema_v6_migration_backfills_operations_and_session_active_work() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_v6_upgrade_") as temp:
        root = Path(temp)
        db_path = root / "ledger.sqlite3"
        connection = create_historical_schema(db_path, 6)
        # Imported/older ledgers may contain valid, non-compact JSON. The
        # migration must preserve intent semantically, not by byte pattern.
        seeded = seed_historical_work(connection, root / "project", metadata_json='{ "intent": "amend" }')
        connection.execute(
            "INSERT INTO conversation_bindings(session_id,project_id,anchor_work_item_id,"
            "binding_kind,created_at,updated_at) VALUES ('legacy-chat',?,?,'work_item',1,1)",
            (seeded["project_id"], seeded["work_item_id"]),
        )
        connection.close()

        upgraded = WorkLedgerStore(db_path)
        assert upgraded.schema_version == SCHEMA_VERSION
        loaded_attempt = upgraded.get_attempt(seeded["attempt_id"])
        operations = upgraded.list_operations(seeded["work_item_id"])
        active = upgraded.get_session_work_context("legacy-chat")
        assert loaded_attempt is not None and loaded_attempt.operation_id
        assert len(operations) == 1
        assert loaded_attempt.operation_id == operations[0].operation_id
        assert operations[0].instruction == seeded["goal"]
        assert operations[0].intent == "amend"
        assert active is not None and active.active_work_item_id == seeded["work_item_id"]
        upgraded.close()


def test_presentation_metadata_does_not_reorder_activity_by_default() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_metadata_") as temp:
        root = Path(temp)
        current_time = [100.0]
        with WorkLedgerStore(root / "ledger.sqlite3", clock=lambda: current_time[0]) as store:
            _, item = _create_project_and_item(store, root / "project")
            assert item.last_activity_at == 100.0

            current_time[0] = 200.0
            projected = store.update_work_item_metadata(
                item.work_item_id,
                {"presentation": {"mode": "diff", "title": "Current review"}},
            )
            assert projected.updated_at == 200.0
            assert projected.last_activity_at == 100.0
            assert projected.metadata["presentation"]["mode"] == "diff"

            current_time[0] = 300.0
            semantic = store.update_work_item_metadata(
                item.work_item_id,
                {"checkpoint": "reviewed"},
                touch_activity=True,
            )
            assert semantic.last_activity_at == 300.0
            assert semantic.metadata["presentation"]["title"] == "Current review"
            assert semantic.metadata["checkpoint"] == "reviewed"


def test_concurrent_attempt_creation_allows_only_one_current_attempt() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_concurrency_") as temp:
        root = Path(temp)
        db_path = root / "ledger.sqlite3"
        first_store = WorkLedgerStore(db_path)
        _, item = _create_project_and_item(first_store, root / "project")
        second_store = WorkLedgerStore(db_path)
        stores = (first_store, second_store)

        def reserve(index: int) -> tuple[bool, int]:
            try:
                attempt = stores[index % 2].create_attempt(
                    item.work_item_id,
                    provider="locus",
                    task=f"Continue pass {index}",
                )
            except WorkLedgerConflict:
                return False, 0
            return True, attempt.attempt_number

        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(reserve, range(8)))
        winners = [number for succeeded, number in outcomes if succeeded]
        assert winners == [1]

        current = first_store.list_attempts(item.work_item_id)
        assert len(current) == 1
        first_store.update_attempt(current[0].attempt_id, execution_status="succeeded")
        continued = second_store.create_attempt(
            item.work_item_id,
            provider="locus",
            task="Continue after the current attempt ended",
        )
        assert continued.attempt_number == 2
        first_store.close()
        second_store.close()


def test_pending_terminal_receipt_query_filters_before_limit_and_uses_status_index() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_terminal_receipts_") as temp:
        root = Path(temp)
        now = [1.0]
        with WorkLedgerStore(root / "ledger.sqlite3", clock=lambda: now[0]) as store:
            project = store.create_or_get_project(root / "project")

            def terminal_attempt(ordinal: int, state: str, updated_at: float, run_id: str):
                item = store.create_work_item(
                    project.project_id,
                    title=f"Terminal receipt {ordinal}",
                    work_item_id=f"work-terminal-receipt-{ordinal}",
                )
                attempt = store.create_attempt(
                    item.work_item_id,
                    provider="locus",
                    task=f"Terminal receipt task {ordinal}",
                    provider_run_id=run_id,
                    attempt_id=f"attempt-terminal-receipt-{ordinal}",
                )
                now[0] = updated_at
                return store.update_attempt(
                    attempt.attempt_id,
                    execution_status="succeeded",
                    metadata={"provider_terminal_pipeline": {"state": state}},
                )

            terminal_attempt(1, "completed", 1.0, "run-completed-oldest")
            pending_oldest = terminal_attempt(2, "pending", 2.0, "run-pending-oldest")
            terminal_attempt(3, "pending", 3.0, "run-pending-newest")
            terminal_attempt(4, "pending", 0.5, "")

            selected = store.list_pending_terminal_provider_attempts(limit=1)
            plan = " ".join(
                str(row[3])
                for row in store._connection.execute(  # noqa: SLF001
                    "EXPLAIN QUERY PLAN SELECT * FROM run_attempts "
                    "WHERE execution_status IN ('succeeded', 'failed', 'cancelled') "
                    "AND provider_run_id <> '' "
                    "AND json_extract(metadata_json, "
                    "'$.provider_terminal_pipeline.state') = 'pending' "
                    "ORDER BY updated_at ASC, attempt_id ASC LIMIT ?",
                    (1,),
                )
            )

            assert selected == [pending_oldest]
            assert "idx_run_attempts_status_updated" in plan
            assert store.list_pending_terminal_provider_attempts(limit=0) == []


def test_artifacts_are_deduplicated_and_external_outputs_stay_pending() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_artifacts_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            _, item = _create_project_and_item(store, root / "project")
            attempt = store.create_attempt(
                item.work_item_id,
                provider="locus",
                task="Generate a file",
            )
            internal_path = root / "project" / "output.py"
            first = store.register_artifact(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                kind="file",
                path=internal_path,
                sha256="abc",
                metadata={"event": "artifact.created"},
            )
            duplicate = store.register_artifact(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                kind="file",
                path=internal_path,
                size_bytes=42,
                metadata={"verified": True},
            )
            assert duplicate.artifact_id == first.artifact_id
            assert duplicate.location == "workspace"
            assert duplicate.status == "registered"
            assert duplicate.size_bytes == 42
            assert duplicate.metadata == {"event": "artifact.created", "verified": True}

            external = store.register_artifact(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                kind="file",
                path=root / "Desktop" / "chess_game.py",
            )
            assert external.location == "external"
            assert external.status == "pending"
            assert len(store.list_artifacts(item.work_item_id)) == 2


def test_permission_request_upsert_resolution_and_restart_are_persistent() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_permission_restart_") as temp:
        root = Path(temp)
        db_path = root / "ledger.sqlite3"
        current_time = [100.0]
        store = WorkLedgerStore(db_path, clock=lambda: current_time[0])
        _, item = _create_project_and_item(store, root / "project")
        attempt = store.create_attempt(
            item.work_item_id,
            provider="locus",
            task="Write a chess game to Desktop",
        )
        created = store.create_permission_request(
            item.work_item_id,
            attempt_id=attempt.attempt_id,
            request_id="permission_write_desktop",
            idempotency_key="claude_tool_write_1",
            capability="filesystem.write.external",
            action="Write chess_game.py",
            scope_paths=[
                str(root / "Desktop" / "chess_game.py"),
                "",
                str(root / "Desktop" / "chess_game.py"),
            ],
            reason="The requested output is outside the project workspace.",
            reversibility="Delete the exported file.",
            options=["allow_once", "deny", "allow_once"],
            metadata={"provider": "locus"},
        )
        assert created.status == "pending"
        assert created.scope_paths == [str(root / "Desktop" / "chess_game.py")]
        assert created.options == ["allow_once", "deny"]
        assert created.resolved_at is None
        assert created.id == created.request_id
        assert created.to_dict()["id"] == created.request_id
        assert created.to_dict()["scope"] == created.scope_paths

        # Exact provider event replay returns the one immutable pending record
        # without changing its revision material.
        current_time[0] = 200.0
        replayed = store.upsert_permission_request(
            item.work_item_id,
            attempt_id=attempt.attempt_id,
            capability="filesystem.write.external",
            action="Write chess_game.py",
            idempotency_key="claude_tool_write_1",
            metadata={"provider": "locus"},
        )
        assert replayed.request_id == created.request_id
        assert replayed.created_at == 100.0
        assert replayed.updated_at == 100.0
        assert replayed.reason == created.reason
        assert replayed.scope_paths == created.scope_paths
        assert replayed.metadata == {"provider": "locus"}
        exact_replay = store.create_permission_request(
            item.work_item_id,
            attempt_id=attempt.attempt_id,
            capability="filesystem.write.external",
            action="Write chess_game.py",
            idempotency_key="claude_tool_write_1",
            metadata={"provider": "locus"},
        )
        assert exact_replay.updated_at == 100.0
        assert len(store.list_permission_requests(item.work_item_id, status="pending")) == 1
        store.close()

        reopened = WorkLedgerStore(db_path, clock=lambda: 300.0)
        pending = reopened.get_permission_request(created.request_id)
        assert pending is not None and pending.status == "pending"
        resolved = reopened.resolve_permission_request(
            created.request_id,
            "allowed",
            metadata={"surface": "wallpaper.slice", "decision": "allow_once"},
        )
        assert resolved.status == "allowed"
        assert resolved.resolved_at == 300.0
        assert resolved.metadata["provider"] == "locus"
        assert resolved.metadata["decision"] == "allow_once"
        assert reopened.list_permission_requests(
            item.work_item_id,
            attempt_id=attempt.attempt_id,
            status="pending",
        ) == []
        assert (
            reopened.list_permission_requests(item.work_item_id, status="allowed")[0].request_id
            == created.request_id
        )
        reopened.close()

        verified = WorkLedgerStore(db_path)
        persisted = verified.get_permission_request(created.request_id)
        assert persisted is not None and persisted.status == "allowed"
        assert persisted.resolved_at == 300.0
        verified.close()


def test_cooperative_permission_owner_is_persistent_without_creating_work() -> None:
    with tempfile.TemporaryDirectory(prefix="cooperative_permission_restart_") as temp:
        path = Path(temp) / "host.sqlite3"
        store = WorkLedgerStore(path)
        with store._transaction() as cursor:
            cursor.execute("""CREATE TABLE cooperative_contexts (
                session_id TEXT NOT NULL,context_id TEXT NOT NULL,closed INTEGER NOT NULL,
                run_id TEXT NOT NULL,run_status TEXT NOT NULL,
                PRIMARY KEY(session_id,context_id))""")
            cursor.execute("INSERT INTO cooperative_contexts VALUES (?,?,?,?,?)",
                ("session-a", "context-a", 0, "run-a", "running"))
        request = store.create_cooperative_permission_request(
            session_id="session-a", context_id="context-a", provider_run_id="run-a",
            capability="shell.execute", action="execute_command",
            scope_paths=["workspace"], reason="Needs approval", options=["deny"],
            idempotency_key="provider:codex:run-a:native-request",
            metadata={"provider":"codex", "provider_request_id":"native-request"})
        assert request.owner_kind == "cooperative_run"
        assert (request.work_item_id, request.attempt_id) == ("", "")
        assert (request.session_id, request.context_id, request.provider_run_id) == (
            "session-a", "context-a", "run-a")
        replay = store.create_cooperative_permission_request(
            session_id="session-a", context_id="context-a", provider_run_id="run-a",
            capability="shell.execute", action="execute_command",
            idempotency_key="provider:codex:run-a:native-request",
            metadata={"provider":"codex", "provider_request_id":"native-request"})
        assert replay.request_id == request.request_id
        assert store.list_work_items() == [] and store.list_projects() == []
        denied = store.resolve_permission_request(request.request_id, "denied",
            metadata={"resolution":"policy_denied"})
        assert denied.status == "denied"
        with store._transaction() as cursor:
            cursor.execute("""UPDATE cooperative_contexts SET run_status='done'
                WHERE session_id='session-a' AND context_id='context-a'""")
        terminal_replay = store.create_cooperative_permission_request(
            session_id="session-a", context_id="context-a", provider_run_id="run-a",
            capability="shell.execute", action="execute_command",
            idempotency_key="provider:codex:run-a:native-request",
            metadata={"provider":"codex", "provider_request_id":"native-request"})
        assert terminal_replay.status == "denied"
        store.close()

        reopened = WorkLedgerStore(path)
        persisted = reopened.list_cooperative_permission_requests(
            "session-a", context_id="context-a", provider_run_id="run-a",
            status="denied")
        assert len(persisted) == 1 and persisted[0].request_id == request.request_id
        assert persisted[0].metadata["resolution"] == "policy_denied"
        reopened.close()


def test_v9_permission_rows_migrate_to_explicit_work_owner() -> None:
    with tempfile.TemporaryDirectory(prefix="permission_v9_migration_") as temp:
        root = Path(temp)
        path = root / "ledger.sqlite3"
        store = WorkLedgerStore(path)
        _, item = _create_project_and_item(store, root / "project")
        attempt = store.create_attempt(item.work_item_id, provider="codex", task="Build")
        request = store.create_permission_request(item.work_item_id,
            attempt_id=attempt.attempt_id, request_id="permission-v9",
            idempotency_key="native-v9", capability="shell.execute",
            action="execute_command", options=["allow_once", "deny"],
            metadata={"provider":"codex"})
        store.close()

        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("""
                DROP INDEX uq_permission_request_attempt_key;
                DROP INDEX uq_permission_request_cooperative_key;
                DROP INDEX idx_permission_requests_item_status;
                DROP INDEX idx_permission_requests_attempt_status;
                DROP INDEX idx_permission_requests_cooperative_status;
                ALTER TABLE permission_requests RENAME TO permission_requests_v10;
                CREATE TABLE permission_requests (
                    request_id TEXT PRIMARY KEY,
                    work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id) ON DELETE CASCADE,
                    attempt_id TEXT NOT NULL REFERENCES run_attempts(attempt_id) ON DELETE CASCADE,
                    idempotency_key TEXT NOT NULL DEFAULT '', capability TEXT NOT NULL,
                    action TEXT NOT NULL, scope_paths_json TEXT NOT NULL DEFAULT '[]',
                    reason TEXT NOT NULL DEFAULT '', reversibility TEXT NOT NULL DEFAULT 'unknown',
                    status TEXT NOT NULL CHECK (status IN ('pending','allowed','denied','expired')),
                    options_json TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, resolved_at REAL,
                    metadata_json TEXT NOT NULL DEFAULT '{}');
                INSERT INTO permission_requests SELECT request_id,work_item_id,attempt_id,
                    idempotency_key,capability,action,scope_paths_json,reason,reversibility,
                    status,options_json,created_at,updated_at,resolved_at,metadata_json
                    FROM permission_requests_v10;
                DROP TABLE permission_requests_v10;
                CREATE UNIQUE INDEX uq_permission_request_attempt_key
                    ON permission_requests(attempt_id,idempotency_key)
                    WHERE idempotency_key<>'';
                CREATE INDEX idx_permission_requests_item_status
                    ON permission_requests(work_item_id,status,updated_at DESC);
                CREATE INDEX idx_permission_requests_attempt_status
                    ON permission_requests(attempt_id,status,updated_at DESC);
                PRAGMA user_version=9;
            """)

        migrated = WorkLedgerStore(path)
        loaded = migrated.get_permission_request(request.request_id)
        assert loaded is not None and loaded.owner_kind == "work_attempt"
        assert loaded.work_item_id == item.work_item_id
        assert loaded.attempt_id == attempt.attempt_id
        assert not loaded.session_id and not loaded.context_id and not loaded.provider_run_id
        assert loaded.options == ["allow_once", "deny"]
        assert migrated.schema_version == SCHEMA_VERSION
        migrated.close()


def test_permission_request_rejects_invalid_identity_and_repeated_decisions() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_permission_invalid_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            project, item = _create_project_and_item(store, root / "project")
            attempt = store.create_attempt(
                item.work_item_id,
                provider="locus",
                task="Request a gated write",
            )
            other_item = store.create_work_item(
                project.project_id,
                title="Other task",
                workspace_path=root / "other-project",
            )
            try:
                store.create_permission_request(
                    other_item.work_item_id,
                    attempt_id=attempt.attempt_id,
                    capability="filesystem.write",
                    action="Write file",
                )
            except WorkLedgerConflict:
                pass
            else:
                raise AssertionError("attempt/work item ownership must be enforced")

            request = store.create_permission_request(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                request_id="permission_stable_id",
                capability="filesystem.write",
                action="Write file",
            )
            try:
                store.create_permission_request(
                    item.work_item_id,
                    attempt_id=attempt.attempt_id,
                    request_id=request.request_id,
                    capability="shell.execute",
                    action="Run command",
                )
            except WorkLedgerConflict:
                pass
            else:
                raise AssertionError("one request identity must not be reused for another action")

            for invalid_status in ("pending", "approved", ""):
                try:
                    store.resolve_permission_request(request.request_id, invalid_status)  # type: ignore[arg-type]
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"invalid resolution should fail: {invalid_status!r}")
            try:
                store.resolve_permission_request(
                    request.request_id,
                    "allowed",
                    expected_status="allowed",
                )
            except ValueError:
                pass
            else:
                raise AssertionError("resolution must compare-and-set from pending")

            denied = store.resolve_permission_request(request.request_id, "denied")
            assert denied.status == "denied"
            try:
                store.resolve_permission_request(request.request_id, "denied")
            except WorkLedgerConflict:
                pass
            else:
                raise AssertionError("a repeated decision must not rewrite resolution history")
            terminal_replay = store.create_permission_request(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                request_id=request.request_id,
                capability="filesystem.write",
                action="Write file",
                reason="A stale provider replay must not reopen the card.",
            )
            assert terminal_replay.status == "denied"
            assert terminal_replay.reason == request.reason


def test_pending_permission_idempotency_contract_is_immutable() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_permission_contract_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            _, item = _create_project_and_item(store, root / "project")
            attempt = store.create_attempt(
                item.work_item_id,
                provider="locus",
                task="Export one reviewed file",
            )
            original = store.create_permission_request(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                idempotency_key="stable_export_request",
                capability="filesystem.export",
                action="copy_to_desktop",
                scope_paths=[str(root / "Desktop" / "result.txt")],
                reason="Export the reviewed result.",
                reversibility="Delete the exported file.",
                options=["allow_once", "deny"],
                metadata={
                    "kind": "desktop_export",
                    "entries": [{"source_path": "approved.txt", "sha256": "aaa"}],
                    "preview_patch": "+approved",
                },
            )
            changed_contracts = (
                {"scope_paths": [str(root / "Desktop" / "other.txt")]},
                {"reason": "Export a different result."},
                {"reversibility": "This cannot be reversed."},
                {"options": ["allow_always", "deny"]},
                {
                    "metadata": {
                        "kind": "desktop_export",
                        "entries": [{"source_path": "swapped.txt", "sha256": "bbb"}],
                        "preview_patch": "+swapped",
                    }
                },
            )
            for changed in changed_contracts:
                try:
                    store.create_permission_request(
                        item.work_item_id,
                        attempt_id=attempt.attempt_id,
                        idempotency_key="stable_export_request",
                        capability="filesystem.export",
                        action="copy_to_desktop",
                        **changed,
                    )
                except WorkLedgerConflict:
                    pass
                else:
                    raise AssertionError(
                        f"idempotent replay changed authority-bearing contract: {changed}"
                    )
                persisted = store.get_permission_request(original.request_id)
                assert persisted is not None
                assert persisted.scope_paths == original.scope_paths
                assert persisted.reason == original.reason
                assert persisted.reversibility == original.reversibility
                assert persisted.options == original.options
                assert persisted.metadata == original.metadata


def test_permission_request_resolution_is_atomic_across_connections() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_permission_race_") as temp:
        root = Path(temp)
        db_path = root / "ledger.sqlite3"
        seed = WorkLedgerStore(db_path)
        _, item = _create_project_and_item(seed, root / "project")
        attempt = seed.create_attempt(
            item.work_item_id,
            provider="locus",
            task="Gated write",
        )
        request = seed.create_permission_request(
            item.work_item_id,
            attempt_id=attempt.attempt_id,
            idempotency_key="provider_event_1",
            capability="filesystem.write.external",
            action="Write desktop file",
        )
        seed.close()

        first = WorkLedgerStore(db_path)
        second = WorkLedgerStore(db_path)

        def resolve(store_and_status) -> tuple[bool, str]:
            store, status = store_and_status
            try:
                result = store.resolve_permission_request(request.request_id, status)
            except WorkLedgerConflict:
                return False, status
            return True, result.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(resolve, [(first, "allowed"), (second, "denied")]))
        assert sum(1 for succeeded, _ in outcomes if succeeded) == 1
        winner = next(status for succeeded, status in outcomes if succeeded)
        persisted = first.get_permission_request(request.request_id)
        assert persisted is not None and persisted.status == winner
        assert persisted.resolved_at is not None
        first.close()
        second.close()


def test_single_writer_lease_is_atomic_across_connections() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_writer_lease_") as temp:
        root = Path(temp)
        db_path = root / "ledger.sqlite3"
        seed = WorkLedgerStore(db_path)
        _, first_item = _create_project_and_item(seed, root / "project")
        project = seed.get_project(first_item.project_id)
        assert project is not None
        second_item = seed.create_work_item(
            project.project_id,
            title="Concurrent writer",
            workspace_path=root / "project",
        )
        first_attempt = seed.create_attempt(first_item.work_item_id, provider="locus", task="Writer A")
        second_attempt = seed.create_attempt(second_item.work_item_id, provider="locus", task="Writer B")
        seed.close()

        def acquire(work_item_id: str, attempt_id: str) -> bool:
            store = WorkLedgerStore(db_path)
            try:
                store.acquire_writer_lease(
                    work_item_id,
                    attempt_id,
                    workspace_path=root / "project",
                )
                return True
            except WorkLedgerConflict:
                return False
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(
                pool.map(
                    lambda args: acquire(*args),
                    [
                        (first_item.work_item_id, first_attempt.attempt_id),
                        (second_item.work_item_id, second_attempt.attempt_id),
                    ],
                )
            )
        assert sorted(outcomes) == [False, True]
        verified = WorkLedgerStore(db_path)
        active = verified.list_writer_leases(active_only=True)
        assert len(active) == 1
        released = verified.release_writer_lease(active[0].attempt_id)
        assert released is not None and released.status == "released"
        assert verified.list_writer_leases(active_only=True) == []
        reacquired = verified.acquire_writer_lease(
            active[0].work_item_id,
            active[0].attempt_id,
            workspace_path=root / "project",
            metadata={"reason": "resume"},
        )
        assert reacquired.status == "active"
        assert reacquired.lease_id == active[0].lease_id
        verified.release_writer_lease(active[0].attempt_id)
        verified.close()


def test_cooperative_writer_lease_shares_work_slot_without_work_identity() -> None:
    with tempfile.TemporaryDirectory(prefix="cooperative_writer_lease_") as temp:
        root = Path(temp)
        workspace = root/"project"
        store = WorkLedgerStore(root/"ledger.sqlite3")
        _, item = _create_project_and_item(store, workspace)
        attempt = store.create_attempt(item.work_item_id, provider="codex", task="Work writer")

        cooperative = store.acquire_cooperative_writer_lease(
            "session-a", "context-a", "effect-a", workspace_path=workspace,
            metadata={"source":"cooperative"})
        assert cooperative.owner_kind == "cooperative_run"
        assert not cooperative.work_item_id and not cooperative.attempt_id
        assert (cooperative.session_id, cooperative.context_id,
            cooperative.provider_effect_id) == ("session-a", "context-a", "effect-a")
        assert not cooperative.provider_run_id
        assert store.acquire_cooperative_writer_lease(
            "session-a", "context-a", "effect-a",
            workspace_path=workspace).lease_id == cooperative.lease_id
        try:
            store.acquire_writer_lease(item.work_item_id, attempt.attempt_id,
                workspace_path=workspace)
        except WorkLedgerConflict as exc:
            assert "active writer" in str(exc)
        else:
            raise AssertionError("Work cannot bypass an active cooperative writer")

        bound = store.bind_cooperative_writer_run("effect-a", "provider-run-a")
        assert bound.provider_run_id == "provider-run-a"
        assert store.bind_cooperative_writer_run(
            "effect-a", "provider-run-a").provider_run_id == "provider-run-a"
        released = store.release_cooperative_writer_lease("effect-a",
            metadata={"terminal":"done"})
        assert released is not None and released.status == "released"
        assert released.metadata == {"source":"cooperative", "terminal":"done"}
        try:
            store.acquire_cooperative_writer_lease("session-a", "context-a",
                "effect-a", workspace_path=workspace)
        except WorkLedgerConflict as exc:
            assert "cannot reactivate" in str(exc)
        else:
            raise AssertionError("a terminal effect cannot reacquire its old lease")

        work_lease = store.acquire_writer_lease(item.work_item_id,
            attempt.attempt_id, workspace_path=workspace)
        assert work_lease.owner_kind == "work_attempt"
        assert not work_lease.session_id and not work_lease.provider_effect_id
        try:
            store.acquire_cooperative_writer_lease("session-b", "context-b",
                "effect-b", workspace_path=workspace)
        except WorkLedgerConflict as exc:
            assert "active writer" in str(exc)
        else:
            raise AssertionError("cooperative writes cannot bypass a Work writer")
        store.release_writer_lease(attempt.attempt_id)
        store.close()


def test_work_and_cooperative_writer_acquisition_is_atomic_across_connections() -> None:
    with tempfile.TemporaryDirectory(prefix="cross_owner_writer_lease_") as temp:
        root = Path(temp)
        workspace = root/"project"
        database = root/"ledger.sqlite3"
        seed = WorkLedgerStore(database)
        _, item = _create_project_and_item(seed, workspace)
        attempt = seed.create_attempt(item.work_item_id, provider="codex", task="Work")
        seed.close()

        def acquire(kind: str) -> bool:
            store = WorkLedgerStore(database)
            try:
                if kind == "work":
                    store.acquire_writer_lease(item.work_item_id,
                        attempt.attempt_id, workspace_path=workspace)
                else:
                    store.acquire_cooperative_writer_lease(
                        "session", "context", "effect", workspace_path=workspace)
                return True
            except WorkLedgerConflict:
                return False
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(acquire, ("work", "cooperative")))
        assert sorted(outcomes) == [False, True]
        verified = WorkLedgerStore(database)
        active, = verified.list_writer_leases(active_only=True)
        assert active.owner_kind in {"work_attempt", "cooperative_run"}
        if active.owner_kind == "work_attempt":
            verified.release_writer_lease(active.attempt_id)
        else:
            verified.release_cooperative_writer_lease(active.provider_effect_id)
        verified.close()


def test_completion_history_and_surface_focus_are_persistent() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_focus_") as temp:
        root = Path(temp)
        db_path = root / "ledger.sqlite3"
        store = WorkLedgerStore(db_path)
        _, item = _create_project_and_item(store, root / "project")
        attempt = store.create_attempt(
            item.work_item_id,
            provider="locus",
            task="Finish a task",
        )
        decision = assess_completion(
            CompletionEvidence(
                execution_status="succeeded",
                explicit_complete=True,
                validation_statuses=("passed",),
            )
        )
        assessment = store.record_completion(
            item.work_item_id,
            decision,
            attempt_id=attempt.attempt_id,
            evidence={"validation": ["passed"]},
        )
        assert assessment.work_item_state == "review_ready"
        assert assessment.terminal is True
        assert store.get_work_item(item.work_item_id).state == "review_ready"  # type: ignore[union-attr]
        assert store.latest_completion(item.work_item_id).assessment_id == assessment.assessment_id  # type: ignore[union-attr]

        slice_focus = store.set_focus("wallpaper.slice", item.work_item_id, mode="pinned")
        work_focus = store.set_focus("electron.work", item.work_item_id, mode="auto")
        assert slice_focus.mode == "pinned"
        assert work_focus.mode == "auto"
        store.close()

        reopened = WorkLedgerStore(db_path)
        assert reopened.get_focus("wallpaper.slice").work_item_id == item.work_item_id  # type: ignore[union-attr]
        assert reopened.get_focus("electron.work").mode == "auto"  # type: ignore[union-attr]
        cleared = reopened.clear_focus("wallpaper.slice")
        assert cleared.work_item_id == ""
        assert cleared.mode == "auto"
        reopened.close()


def test_acceptance_is_explicit_and_continue_requires_reopen() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_acceptance_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            _, item = _create_project_and_item(store, root / "project")
            accepted_decision = CompletionDecision(
                execution_status="succeeded",
                completeness="complete",
                attention="none",
                work_item_state="accepted",
                rationale="The user accepted the reviewed result.",
                terminal=True,
            )
            try:
                store.record_completion(item.work_item_id, accepted_decision, source="host")
            except WorkLedgerConflict:
                pass
            else:
                raise AssertionError("host completion must not accept work")
            store.record_completion(item.work_item_id, accepted_decision, source="user")
            assert store.get_work_item(item.work_item_id).state == "accepted"  # type: ignore[union-attr]
            try:
                store.create_attempt(
                    item.work_item_id,
                    provider="locus",
                    task="Continue without reopening",
                )
            except WorkLedgerConflict:
                pass
            else:
                raise AssertionError("accepted work must be reopened before Continue")
            store.set_work_item_state(item.work_item_id, "open", expected_state="accepted")
            continued = store.create_attempt(
                item.work_item_id,
                provider="locus",
                task="Continue after explicit reopen",
            )
            assert continued.attempt_number == 1


def _main() -> None:
    test_schema_migration_is_idempotent_and_records_survive_restart()
    test_attempt_metadata_compare_and_set_is_atomic_and_monotonic()
    test_version_one_database_upgrades_writer_lease_schema()
    test_version_two_migration_reconciles_duplicate_current_attempts()
    test_version_three_database_upgrades_permission_request_schema()
    test_project_identity_resolves_relative_and_real_path_aliases()
    test_continue_attempt_numbers_and_provider_binding_are_persistent()
    test_operations_distinguish_amendment_from_retry()
    test_new_operation_reopens_accepted_work_but_archived_requires_reopen()
    test_schema_v6_migration_backfills_operations_and_session_active_work()
    test_presentation_metadata_does_not_reorder_activity_by_default()
    test_concurrent_attempt_creation_allows_only_one_current_attempt()
    test_artifacts_are_deduplicated_and_external_outputs_stay_pending()
    test_permission_request_upsert_resolution_and_restart_are_persistent()
    test_permission_request_rejects_invalid_identity_and_repeated_decisions()
    test_pending_permission_idempotency_contract_is_immutable()
    test_permission_request_resolution_is_atomic_across_connections()
    test_single_writer_lease_is_atomic_across_connections()
    test_completion_history_and_surface_focus_are_persistent()
    test_acceptance_is_explicit_and_continue_requires_reopen()
    print("ok: work ledger persists work, permissions, artifacts, completion, and focus")


if __name__ == "__main__":
    _main()
