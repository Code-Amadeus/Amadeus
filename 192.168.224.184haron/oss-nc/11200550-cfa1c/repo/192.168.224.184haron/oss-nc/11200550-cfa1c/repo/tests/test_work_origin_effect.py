"""Storage correlation, not Control acceptance or native execution evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_host.work_ledger_store import SCHEMA_VERSION, WorkLedgerConflict, WorkLedgerStore
from server.control_ledger import ControlLedgerStore, _SCHEMA_VERSION as CONTROL_SCHEMA_VERSION
from work_ledger_fixtures import HISTORICAL_MIGRATIONS, create_historical_schema, seed_historical_work


TABLES = ("work_items", "work_operations", "run_attempts")


def _initial(store, project_id, origin="effect-create", **kwargs):
    return store.create_work_item_with_attempt(
        project_id, title="Game", goal="Original goal", intent="execute",
        instruction="Create the requested game", provider="fake", task="Provider task",
        origin_effect_id=origin, **kwargs,
    )


def _amend(store, work_item_id, origin="effect-amend", **kwargs):
    return store.create_operation_attempt(
        work_item_id, intent="amend", instruction="Add a restart button",
        provider="fake", task="Amend provider task", origin_effect_id=origin, **kwargs,
    )


def _rows(store):
    return {table: [dict(row) for row in store._connection.execute(f"SELECT * FROM {table}")]
            for table in TABLES}


def _expected(origin, work, operation, attempt, run=""):
    return dict(origin_effect_id=origin, work_item_id=work.work_item_id,
                operation_id=operation.operation_id, attempt_id=attempt.attempt_id,
                provider_run_id=run)


def test_initial_origin_is_explicit_durable_and_not_metadata(tmp_path):
    database = tmp_path / "work.sqlite3"
    with WorkLedgerStore(database) as store:
        project = store.create_or_get_project(tmp_path)
        records = _initial(store, project.project_id, "Host:Exact-1",
                           metadata={"origin_effect_id": "untrusted"},
                           operation_metadata={"turn_id": "not-an-effect"},
                           attempt_metadata={"origin_effect_id": "also-untrusted"})
        assert [record.origin_effect_id for record in records] == ["Host:Exact-1"] * 3
        assert [record.to_dict()["origin_effect_id"] for record in records] == ["Host:Exact-1"] * 3
        expected = _expected("Host:Exact-1", *records)
        assert store.get_origin_effect_binding("Host:Exact-1") == expected
        assert store.get_origin_effect_binding("untrusted") is None
    with WorkLedgerStore(database) as reopened:
        assert reopened.get_origin_effect_binding("Host:Exact-1") == expected
        assert reopened.get_work_item(records[0].work_item_id).metadata == {"origin_effect_id": "untrusted"}


def test_amendment_has_its_own_origin_without_rewriting_work_origin(tmp_path):
    with WorkLedgerStore(tmp_path / "work.sqlite3") as store:
        project = store.create_or_get_project(tmp_path)
        work, operation, attempt = _initial(store, project.project_id)
        store.update_attempt(attempt.attempt_id, execution_status="succeeded")
        amended, next_attempt = _amend(store, work.work_item_id)
        assert store.get_work_item(work.work_item_id).origin_effect_id == "effect-create"
        assert amended.origin_effect_id == next_attempt.origin_effect_id == "effect-amend"
        assert store.get_origin_effect_binding("effect-create") == _expected("effect-create", work, operation, attempt)
        assert store.get_origin_effect_binding("effect-amend") == _expected("effect-amend", work, amended, next_attempt)


@pytest.mark.parametrize("replay", ["new_work", "new_operation"])
def test_duplicate_effect_rejects_without_writes_even_after_terminal(tmp_path, replay):
    with WorkLedgerStore(tmp_path / "work.sqlite3") as store:
        project = store.create_or_get_project(tmp_path)
        work, _, attempt = _initial(store, project.project_id)
        store.update_attempt(attempt.attempt_id, execution_status="succeeded")
        before = _rows(store)
        with pytest.raises(WorkLedgerConflict, match="origin effect"):
            if replay == "new_work":
                _initial(store, project.project_id)
            else:
                _amend(store, work.work_item_id, "effect-create")
        assert _rows(store) == before


@pytest.mark.parametrize("replay", ["new_work", "same_work", "other_work"])
def test_amendment_origin_cannot_be_reused_as_another_creation_or_amendment(tmp_path, replay):
    with WorkLedgerStore(tmp_path / "work.sqlite3") as store:
        project = store.create_or_get_project(tmp_path)
        work = store.create_work_item(project.project_id, title="Existing Work")
        other = store.create_work_item(project.project_id, title="Other Work")
        _, attempt = _amend(store, work.work_item_id)
        store.update_attempt(attempt.attempt_id, execution_status="succeeded")
        store.set_work_item_state(work.work_item_id, "review_ready")
        before = _rows(store)
        with pytest.raises(WorkLedgerConflict, match="origin effect"):
            if replay == "new_work":
                _initial(store, project.project_id, "effect-amend")
            else:
                _amend(store, work.work_item_id if replay == "same_work" else other.work_item_id)
        assert _rows(store) == before


def test_empty_origin_is_legacy_unknown_and_retry_does_not_inherit(tmp_path):
    with WorkLedgerStore(tmp_path / "work.sqlite3") as store:
        project = store.create_or_get_project(tmp_path)
        records = _initial(store, project.project_id, "", attempt_metadata={"origin_effect_id": "fake"})
        assert all(record.origin_effect_id == "" and "origin_effect_id" not in record.to_dict() for record in records)
        assert store.get_origin_effect_binding("fake") is None
        work, operation, attempt = _initial(store, project.project_id)
        store.update_attempt(attempt.attempt_id, execution_status="failed")
        retry = store.create_attempt(work.work_item_id, operation_id=operation.operation_id,
                                     provider="fake", task="Retry", metadata={"origin_effect_id": "effect-create"})
        assert retry.origin_effect_id == "" and retry.operation_id == operation.operation_id
        assert store.get_origin_effect_binding("effect-create") == _expected("effect-create", work, operation, attempt)


def test_equal_text_different_opaque_effects_remain_distinct(tmp_path):
    with WorkLedgerStore(tmp_path / "work.sqlite3") as store:
        project = store.create_or_get_project(tmp_path)
        effects = ["Effect", "effect", " effect ", "effect';--"]
        triples = [_initial(store, project.project_id, effect) for effect in effects]
        assert len({triple[0].work_item_id for triple in triples}) == len(effects)
        for effect, triple in zip(effects, triples):
            assert store.get_origin_effect_binding(effect) == _expected(effect, *triple)


@pytest.mark.parametrize("origin", [None, 17, [], " ", "\n\t"])
def test_invalid_origin_is_rejected_without_normalization_or_writes(tmp_path, origin):
    with WorkLedgerStore(tmp_path / "work.sqlite3") as store:
        project = store.create_or_get_project(tmp_path)
        work = store.create_work_item(project.project_id, title="Existing")
        before = _rows(store)
        with pytest.raises(ValueError, match="origin_effect_id"):
            _initial(store, project.project_id, origin)
        with pytest.raises(ValueError, match="origin_effect_id"):
            _amend(store, work.work_item_id, origin)
        with pytest.raises(ValueError, match="origin_effect_id"):
            store.get_origin_effect_binding(origin)
        assert _rows(store) == before


def test_lookup_requires_identity_and_uses_one_read_only_indexed_snapshot(tmp_path):
    with WorkLedgerStore(tmp_path / "work.sqlite3") as store:
        project = store.create_or_get_project(tmp_path)
        triple = _initial(store, project.project_id)
        with pytest.raises(ValueError):
            store.get_origin_effect_binding("")
        statements = []
        store._connection.set_trace_callback(statements.append)
        assert store.get_origin_effect_binding("effect-create") == _expected("effect-create", *triple)
        store._connection.set_trace_callback(None)
        assert len(statements) == 1 and statements[0].lstrip().startswith("SELECT")
        plan = " ".join(str(row[3]) for row in store._connection.execute("EXPLAIN QUERY PLAN " + statements[0]))
        for table in TABLES:
            assert f"uq_{table}_origin_effect" in plan
        assert store.get_origin_effect_binding("absent") is None


@pytest.mark.parametrize("kind", ["initial", "amend", "mixed"])
def test_concurrent_connections_bind_one_effect_once(tmp_path, kind):
    database = tmp_path / "work.sqlite3"
    with WorkLedgerStore(database) as store:
        project_id = store.create_or_get_project(tmp_path).project_id
        manual = store.create_work_item(project_id, title="Existing")
    gate = Barrier(2)

    def create(index):
        with WorkLedgerStore(database) as store:
            gate.wait(timeout=5)
            try:
                if kind == "initial" or (kind == "mixed" and index == 0):
                    _initial(store, project_id, "concurrent-effect")
                else:
                    _amend(store, manual.work_item_id, "concurrent-effect")
                return True
            except WorkLedgerConflict as exc:
                assert "origin effect" in str(exc)
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create, index) for index in range(2)]
        assert sorted(future.result(timeout=10) for future in futures) == [False, True]
    with WorkLedgerStore(database) as store:
        binding = store.get_origin_effect_binding("concurrent-effect")
        assert binding is not None
        rows = _rows(store)
        assert len(rows["work_operations"]) == len(rows["run_attempts"]) == 1
        assert len(rows["work_items"]) == (1 if binding["work_item_id"] == manual.work_item_id else 2)


@pytest.mark.parametrize("table", TABLES)
def test_each_nonempty_origin_column_has_a_database_unique_constraint(tmp_path, table):
    with WorkLedgerStore(tmp_path / "work.sqlite3") as store:
        project = store.create_or_get_project(tmp_path)
        _initial(store, project.project_id, "first")
        _initial(store, project.project_id, "second")
        before = _rows(store)
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(f"UPDATE {table} SET origin_effect_id='first' WHERE origin_effect_id='second'")
        assert _rows(store) == before


@pytest.mark.parametrize("damage", ["work_only", "operation_only", "attempt_only", "wrong_work", "wrong_operation", "wrong_created_work"])
def test_incomplete_or_mismatched_origin_is_not_missing_or_reusable(tmp_path, damage):
    with WorkLedgerStore(tmp_path / "work.sqlite3") as store:
        project = store.create_or_get_project(tmp_path)
        work, operation, attempt = _initial(store, project.project_id, "")
        other, other_op, _ = _initial(store, project.project_id, "")
        if damage.endswith("_only"):
            table, key, identity = {
                "work_only": ("work_items", "work_item_id", work.work_item_id),
                "operation_only": ("work_operations", "operation_id", operation.operation_id),
                "attempt_only": ("run_attempts", "attempt_id", attempt.attempt_id),
            }[damage]
            store._connection.execute(f"UPDATE {table} SET origin_effect_id='damaged' WHERE {key}=?", (identity,))
        else:
            store._connection.execute("UPDATE work_operations SET origin_effect_id='damaged' WHERE operation_id=?", (operation.operation_id,))
            store._connection.execute("UPDATE run_attempts SET origin_effect_id='damaged' WHERE attempt_id=?", (attempt.attempt_id,))
            if damage == "wrong_work":
                store._connection.execute("UPDATE work_operations SET work_item_id=?,operation_number=2 WHERE operation_id=?", (other.work_item_id, operation.operation_id))
            elif damage == "wrong_operation":
                store._connection.execute("UPDATE run_attempts SET operation_id=? WHERE attempt_id=?", (other_op.operation_id, attempt.attempt_id))
            else:
                store._connection.execute("UPDATE work_items SET origin_effect_id='damaged' WHERE work_item_id=?", (other.work_item_id,))
        before = _rows(store)
        with pytest.raises(WorkLedgerConflict, match="incomplete or mismatched"):
            store.get_origin_effect_binding("damaged")
        with pytest.raises(WorkLedgerConflict, match="origin effect"):
            _initial(store, project.project_id, "damaged")
        with pytest.raises(WorkLedgerConflict, match="origin effect"):
            _amend(store, work.work_item_id, "damaged")
        assert _rows(store) == before


def test_provider_run_join_is_persistent_unique_and_not_native_acceptance(tmp_path):
    database = tmp_path / "work.sqlite3"
    with WorkLedgerStore(database) as store:
        project = store.create_or_get_project(tmp_path)
        triple = _initial(store, project.project_id)
        other = _initial(store, project.project_id, "other-effect")
        store.bind_provider_run(triple[2].attempt_id, "host-run")
        with pytest.raises(WorkLedgerConflict):
            store.bind_provider_run(other[2].attempt_id, "host-run")
        assert store.get_attempt(triple[2].attempt_id).execution_status == "queued"
    with WorkLedgerStore(database) as store:
        assert store.get_origin_effect_binding("effect-create") == _expected("effect-create", *triple, run="host-run")
        assert store.get_origin_effect_binding("other-effect")["provider_run_id"] == ""


def _version_seven(database, workspace):
    connection = create_historical_schema(database, 6)
    seed_historical_work(connection, workspace, metadata_json='{ "origin_effect_id": "not-authority", "intent": "amend" }')
    connection.executescript(HISTORICAL_MIGRATIONS[6])
    for table in ("work_operations", "run_attempts"):
        connection.execute(f"UPDATE {table} SET metadata_json=?", ('{"origin_effect_id":"not-authority","turn_id":"legacy-turn"}',))
    connection.row_factory = sqlite3.Row
    before = {table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")] for table in TABLES}
    connection.close()
    return before


def _assert_version_seven_unchanged(database, before):
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        for table in TABLES:
            assert "origin_effect_id" not in {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            assert [dict(row) for row in connection.execute(f"SELECT * FROM {table}")] == before[table]


def test_v7_upgrade_preserves_legacy_rows_without_guessing_origins_and_control_schema(tmp_path):
    database = tmp_path / "work.sqlite3"
    before = _version_seven(database, tmp_path / "project")
    control = ControlLedgerStore(database)
    schema = list(control._db.execute("SELECT sql FROM sqlite_master WHERE name LIKE 'control_%' ORDER BY name"))
    try:
        for _ in range(2):
            with WorkLedgerStore(database) as store:
                assert store.schema_version == SCHEMA_VERSION
                assert _rows(store) == {table: [dict(row, origin_effect_id="") for row in rows] for table, rows in before.items()}
                assert store.get_origin_effect_binding("not-authority") is None
                assert tuple(control._db.execute(
                    "SELECT version,accepting FROM control_ledger_meta"
                ).fetchone()) == (CONTROL_SCHEMA_VERSION, 1)
                assert list(control._db.execute("SELECT sql FROM sqlite_master WHERE name LIKE 'control_%' ORDER BY name")) == schema
                assert control._db.execute("SELECT COUNT(*) FROM control_effect_outbox").fetchone()[0] == 0
    finally:
        control.close()


def test_failed_migration_rolls_back_all_columns_and_version(tmp_path):
    database = tmp_path / "work.sqlite3"
    before = _version_seven(database, tmp_path / "project")
    original_connect = sqlite3.connect
    connections = []

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_authorizer(lambda action, first, second, *_:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ALTER_TABLE and second == "work_operations" else sqlite3.SQLITE_OK)
        connections.append(connection)
        return connection

    try:
        with patch("agent_host.work_ledger_store.sqlite3.connect", connect):
            with pytest.raises(sqlite3.DatabaseError):
                WorkLedgerStore(database)
    finally:
        for connection in connections:
            connection.close()
    _assert_version_seven_unchanged(database, before)
    with WorkLedgerStore(database) as store:
        assert store.schema_version == SCHEMA_VERSION


def test_two_connections_that_observed_v7_can_both_finish_upgrade(tmp_path):
    database = tmp_path / "work.sqlite3"
    _version_seven(database, tmp_path / "project")
    # Actual v7 Work Store files already use WAL. Do not conflate concurrent
    # journal-mode conversion of a synthetic DELETE-mode file with DDL upgrade.
    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    connection.close()
    gate = Barrier(2)
    original_connect = sqlite3.connect
    connections = []

    class RacingConnection(sqlite3.Connection):
        observed = False

        def execute(self, sql, *args, **kwargs):
            cursor = super().execute(sql, *args, **kwargs)
            if sql == "PRAGMA user_version" and not self.observed:
                # Fully consume the real observation before pausing the
                # caller. Holding the live cursor here can block the other
                # constructor's journal setup instead of testing stale v7.
                rows = cursor.fetchall()
                cursor.close()
                assert len(rows) == 1 and rows[0][0] == 7
                self.observed = True
                gate.wait(timeout=5)
                return SimpleNamespace(fetchone=lambda: rows[0])
            return cursor

    def open_store():
        with WorkLedgerStore(database) as store:
            return store.schema_version

    def connect(*args, **kwargs):
        connection = original_connect(*args, factory=RacingConnection, **kwargs)
        connections.append(connection)
        return connection

    try:
        with patch("agent_host.work_ledger_store.sqlite3.connect", connect):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(open_store) for _ in range(2)]
                assert [future.result(timeout=10) for future in futures] == [SCHEMA_VERSION, SCHEMA_VERSION]
    finally:
        # Keep a deliberately failed constructor from masking the assertion
        # with a Windows temporary-directory cleanup error.
        for connection in connections:
            connection.close()


@pytest.mark.parametrize("table", ["work_operations", "run_attempts"])
def test_failed_insert_does_not_reserve_origin_or_leave_partial_binding(tmp_path, table):
    with WorkLedgerStore(tmp_path / "work.sqlite3") as store:
        project = store.create_or_get_project(tmp_path)
        manual = store.create_work_item(project.project_id, title="Manual")
        before = _rows(store)
        store._connection.execute(f"CREATE TEMP TRIGGER reject_write BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(WorkLedgerConflict):
            _initial(store, project.project_id)
        with pytest.raises(WorkLedgerConflict):
            _amend(store, manual.work_item_id)
        assert _rows(store) == before
        assert store.get_origin_effect_binding("effect-create") is None
        assert store.get_origin_effect_binding("effect-amend") is None
        store._connection.execute("DROP TRIGGER reject_write")
        assert _initial(store, project.project_id)[2].origin_effect_id == "effect-create"
        assert _amend(store, manual.work_item_id)[1].origin_effect_id == "effect-amend"


_WRITE_CRASH_CHILD = r'''
import os, sys
from agent_host.work_ledger_store import WorkLedgerStore
database, project_id, stage = sys.argv[1:]
with WorkLedgerStore(database) as store:
    def trace(sql):
        text = " ".join(sql.split()).upper()
        if ((stage == "before_operation" and text.startswith("INSERT INTO WORK_OPERATIONS"))
            or (stage == "before_attempt" and text.startswith("INSERT INTO RUN_ATTEMPTS"))
            or (stage == "before_commit" and text == "COMMIT")):
            os._exit(77)
    store._connection.set_trace_callback(trace)
    store.create_work_item_with_attempt(project_id, title="Game", intent="execute", instruction="Build",
        provider="fake", task="Build", origin_effect_id="crash-effect")
    if stage == "after_commit":
        os._exit(77)
raise RuntimeError("crash point not reached")
'''


def _child(script, database, *arguments):
    result = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-c", script, str(database), *arguments],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf-8", timeout=30,
        env=dict(os.environ, PYTHONUTF8="1", AMADEUS_SESSION_DIR=str(database.parent / "child-session")),
    )
    assert result.returncode == 77, result.stdout + result.stderr


@pytest.mark.parametrize("stage", ["before_operation", "before_attempt", "before_commit", "after_commit"])
def test_real_process_exit_leaves_all_or_none_of_origin_binding(tmp_path, stage):
    database = tmp_path / "work.sqlite3"
    with WorkLedgerStore(database) as store:
        project_id = store.create_or_get_project(tmp_path).project_id
    _child(_WRITE_CRASH_CHILD, database, project_id, stage)
    with WorkLedgerStore(database) as store:
        committed = stage == "after_commit"
        assert all(len(rows) == int(committed) for rows in _rows(store).values())
        assert (store.get_origin_effect_binding("crash-effect") is not None) == committed
        if committed:
            with pytest.raises(WorkLedgerConflict, match="origin effect"):
                _initial(store, project_id, "crash-effect")
        else:
            triple = _initial(store, project_id, "crash-effect")
            assert store.get_origin_effect_binding("crash-effect") == _expected("crash-effect", *triple)


_MIGRATION_CRASH_CHILD = r'''
import os, sqlite3, sys
from unittest.mock import patch
from agent_host.work_ledger_store import WorkLedgerStore
database, stage = sys.argv[1:]
original_connect = sqlite3.connect
def connect(*args, **kwargs):
    connection = original_connect(*args, **kwargs)
    def trace(sql):
        text = " ".join(sql.split()).upper()
        if ((stage == "second_column" and text.startswith("ALTER TABLE WORK_OPERATIONS"))
            or (stage == "second_index" and text.startswith("CREATE UNIQUE INDEX UQ_WORK_OPERATIONS_ORIGIN_EFFECT"))
            or (stage == "version" and text == "PRAGMA USER_VERSION = 8;")
            or (stage == "commit" and text.rstrip(";") == "COMMIT")):
            os._exit(77)
    connection.set_trace_callback(trace)
    return connection
with patch("agent_host.work_ledger_store.sqlite3.connect", connect):
    with WorkLedgerStore(database):
        if stage == "after_commit":
            os._exit(77)
raise RuntimeError("crash point not reached")
'''


@pytest.mark.parametrize("stage", ["second_column", "second_index", "version", "commit", "after_commit"])
def test_real_process_exit_during_migration_can_reopen_without_partial_schema(tmp_path, stage):
    database = tmp_path / "work.sqlite3"
    before = _version_seven(database, tmp_path / "project")
    _child(_MIGRATION_CRASH_CHILD, database, stage)
    if stage != "after_commit":
        _assert_version_seven_unchanged(database, before)
    for _ in range(2):
        with WorkLedgerStore(database) as store:
            assert store.schema_version == SCHEMA_VERSION
            assert _rows(store) == {table: [dict(row, origin_effect_id="") for row in rows] for table, rows in before.items()}
