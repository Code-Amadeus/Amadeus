"""SQLite WorkLedger persistence and identity tests.

Runs standalone through tools/run_tests.py and is also pytest-compatible.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.work_ledger_store import (
    SCHEMA_VERSION,
    WorkLedgerConflict,
    WorkLedgerStore,
)
from agent_host.work_ledger_types import CompletionDecision
from server.work_completion import CompletionEvidence, assess_completion


def _create_project_and_item(store: WorkLedgerStore, project_root: Path):
    project_root.mkdir(parents=True, exist_ok=True)
    project = store.create_or_get_project(project_root, metadata={"source": "test"})
    item = store.create_work_item(
        project.project_id,
        title="Persist work history",
        goal="Keep provider attempts distinguishable.",
    )
    return project, item


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
        seeded = WorkLedgerStore(db_path)
        seeded.close()
        connection = sqlite3.connect(db_path)
        connection.execute("DROP TABLE workspace_leases")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
        connection.close()

        upgraded = WorkLedgerStore(db_path)
        assert upgraded.schema_version == SCHEMA_VERSION
        assert upgraded.list_writer_leases() == []
        upgraded.close()


def test_version_two_migration_reconciles_duplicate_current_attempts() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_v2_upgrade_") as temp:
        root = Path(temp)
        db_path = root / "ledger.sqlite3"
        seeded = WorkLedgerStore(db_path)
        _, item = _create_project_and_item(seeded, root / "project")
        older = seeded.create_attempt(item.work_item_id, provider="locus", task="Older writer")
        seeded.acquire_writer_lease(
            item.work_item_id,
            older.attempt_id,
            workspace_path=root / "project",
        )
        seeded.close()

        connection = sqlite3.connect(db_path)
        connection.execute("DROP INDEX uq_work_item_active_attempt")
        connection.execute(
            """
            INSERT INTO run_attempts (
                attempt_id, work_item_id, attempt_number, provider, provider_run_id,
                task, mode, execution_status, created_at, updated_at, metadata_json
            ) VALUES ('attempt_newer', ?, 2, 'locus', '', 'Newer writer',
                      'agent', 'queued', 2, 2, '{}')
            """,
            (item.work_item_id,),
        )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
        connection.close()

        upgraded = WorkLedgerStore(db_path)
        assert upgraded.schema_version == SCHEMA_VERSION
        attempts = upgraded.list_attempts(item.work_item_id)
        assert [attempt.execution_status for attempt in attempts] == ["orphaned", "queued"]
        older_lease = upgraded.get_writer_lease(older.attempt_id)
        assert older_lease is not None and older_lease.status == "stale"
        try:
            upgraded.create_attempt(item.work_item_id, provider="locus", task="Third writer")
        except WorkLedgerConflict:
            pass
        else:
            raise AssertionError("the migrated active-attempt invariant must be enforced")
        upgraded.close()


def test_version_three_database_upgrades_permission_request_schema() -> None:
    with tempfile.TemporaryDirectory(prefix="work_ledger_v3_upgrade_") as temp:
        db_path = Path(temp) / "ledger.sqlite3"
        seeded = WorkLedgerStore(db_path)
        seeded.close()
        connection = sqlite3.connect(db_path)
        connection.execute("DROP TABLE permission_requests")
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
        connection.close()

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
        seeded = WorkLedgerStore(db_path)
        project, item = _create_project_and_item(seeded, root / "project")
        attempt = seeded.create_attempt(
            item.work_item_id,
            provider="locus",
            task="Historical instruction",
        )
        seeded.bind_conversation(
            "legacy-chat",
            project.project_id,
            anchor_work_item_id=item.work_item_id,
        )
        seeded.update_work_item_metadata(item.work_item_id, {"intent": "amend"})
        seeded.close()

        connection = sqlite3.connect(db_path)
        connection.execute("PRAGMA foreign_keys = OFF")
        # Imported/older ledgers may contain valid, non-compact JSON. The
        # migration must preserve intent semantically, not by byte pattern.
        connection.execute(
            "UPDATE work_items SET metadata_json = ? WHERE work_item_id = ?",
            ('{ "intent": "amend" }', item.work_item_id),
        )
        connection.execute("DROP INDEX idx_run_attempts_operation_number")
        connection.execute("ALTER TABLE run_attempts DROP COLUMN operation_id")
        connection.execute("DROP TABLE session_work_contexts")
        connection.execute("DROP TABLE work_operations")
        connection.execute("PRAGMA user_version = 6")
        connection.commit()
        connection.close()

        upgraded = WorkLedgerStore(db_path)
        assert upgraded.schema_version == SCHEMA_VERSION
        loaded_attempt = upgraded.get_attempt(attempt.attempt_id)
        operations = upgraded.list_operations(item.work_item_id)
        active = upgraded.get_session_work_context("legacy-chat")
        assert loaded_attempt is not None and loaded_attempt.operation_id
        assert len(operations) == 1
        assert loaded_attempt.operation_id == operations[0].operation_id
        assert operations[0].instruction == item.goal
        assert operations[0].intent == "amend"
        assert active is not None and active.active_work_item_id == item.work_item_id
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
