from __future__ import annotations

import sqlite3

import pytest

from core.continuity.migrations import SCHEMA_VERSION
from core.continuity.store import ContinuityStore, ContinuityStoreError


def test_store_creates_parent_and_expected_sqlite_pragmas(tmp_path) -> None:
    db_path = tmp_path / "nested" / "runtime" / "continuity.sqlite3"
    store = ContinuityStore(db_path)
    try:
        assert db_path.exists()
        assert store.schema_version == SCHEMA_VERSION == 8
        assert int(store.pragma("foreign_keys")) == 1
        assert int(store.pragma("secure_delete")) == 1
        assert str(store.pragma("journal_mode")).lower() == "wal"
        assert int(store.pragma("busy_timeout")) >= 100
        # SQLite reports NORMAL as 1.
        assert int(store.pragma("synchronous")) == 1
    finally:
        store.close()


def test_clock_singleton_survives_restart(tmp_path) -> None:
    db_path = tmp_path / "continuity.sqlite3"
    first = ContinuityStore(db_path)
    first.update_clock_state(
        last_user_turn_at=10.0,
        last_session_id="session-a",
        updated_at=10.0,
    )
    first.close()

    second = ContinuityStore(db_path)
    try:
        state = second.get_clock_state()
        assert state.last_user_turn_at == 10.0
        assert state.last_session_id == "session-a"
        assert second.schema_version == SCHEMA_VERSION
    finally:
        second.close()


def test_clock_update_rejects_unknown_fields(continuity_store) -> None:
    with pytest.raises(ValueError, match="unsupported clock fields"):
        continuity_store.update_clock_state(not_a_clock_field=True)


def test_store_rejects_database_from_newer_schema(tmp_path) -> None:
    db_path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA user_version = 99")
    connection.close()

    with pytest.raises(ContinuityStoreError, match="newer than supported"):
        ContinuityStore(db_path)
