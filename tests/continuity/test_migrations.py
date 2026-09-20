from __future__ import annotations

import sqlite3

from core.continuity import ContinuityStore
from core.continuity.migrations import MIGRATION_1, MIGRATION_2, MIGRATION_3, MIGRATION_4, MIGRATION_5, SCHEMA_VERSION


def test_c8_migration_is_idempotent_and_adds_archive_forget_guard(tmp_path) -> None:
    db_path = tmp_path / "continuity.sqlite3"
    first = ContinuityStore(db_path)
    first.close()
    second = ContinuityStore(db_path)
    second.close()

    connection = sqlite3.connect(db_path)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(memory_items)"
            ).fetchall()
        }
    finally:
        connection.close()

    assert version == SCHEMA_VERSION == 7
    assert {
        "continuity_meta",
        "conversation_clock",
        "memory_items",
        "memory_links",
        "memory_mentions",
        "memory_tombstones",
        "consolidation_turns",
        "memory_fts",
        "memory_embeddings",
        "continuity_maintenance_runs",
        "continuity_work_events",
        "relationship_events",
        "relationship_state",
        "short_term_affect",
        "daily_schedules",
        "schedule_items",
        "life_threads",
        "life_events",
        "memory_forget_sources",
    }.issubset(tables)

    assert {
        "retention_tier",
        "retention_score",
        "last_retention_at",
    }.issubset(columns)
    tombstone_connection = sqlite3.connect(db_path)
    try:
        tombstone_columns = {
            row[1]
            for row in tombstone_connection.execute(
                "PRAGMA table_info(memory_tombstones)"
            ).fetchall()
        }
    finally:
        tombstone_connection.close()
    assert "archive_guard_complete" in tombstone_columns
    relationship_connection = sqlite3.connect(db_path)
    try:
        relationship_columns = {
            row[1]
            for row in relationship_connection.execute(
                "PRAGMA table_info(relationship_events)"
            ).fetchall()
        }
    finally:
        relationship_connection.close()
    assert {
        "source_session_id",
        "source_turn_id",
        "source_memory_id",
        "source_memory_key",
        "source_hash",
        "source_fingerprint",
        "proposed_delta",
        "bounded_delta",
        "invalidated_at",
        "policy_version",
    }.issubset(relationship_columns)
    life_connection = sqlite3.connect(db_path)
    try:
        life_columns = {
            row[1]
            for row in life_connection.execute(
                "PRAGMA table_info(life_events)"
            ).fetchall()
        }
    finally:
        life_connection.close()
    assert {
        "source_class",
        "source_session_id",
        "source_turn_id",
        "source_memory_id",
        "source_memory_key",
        "source_hash",
        "source_fingerprint",
        "invalidated_at",
        "policy_version",
    }.issubset(life_columns)


def test_existing_c1_database_migrates_through_c6_without_losing_clock_state(tmp_path) -> None:
    db_path = tmp_path / "continuity-v1.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(MIGRATION_1)
        connection.execute(
            """
            UPDATE conversation_clock
               SET last_user_turn_at = 123.0,
                   last_session_id = 'session-c1',
                   updated_at = 123.0
             WHERE singleton_id = 1
            """
        )
        connection.commit()
    finally:
        connection.close()

    store = ContinuityStore(db_path)
    try:
        assert store.schema_version == 7
        state = store.get_clock_state()
        assert state.last_user_turn_at == 123.0
        assert state.last_session_id == "session-c1"
        assert store.list_active_memories() == []
    finally:
        store.close()


def test_existing_c2_memory_is_backfilled_into_c3_fts(tmp_path) -> None:
    db_path = tmp_path / "continuity-v2.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(MIGRATION_1)
        connection.executescript(MIGRATION_2)
        connection.commit()
    finally:
        connection.close()

    # Open with a temporary C2-compatible Store-like SQL insert before C3 migration.
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            INSERT INTO memory_items(
                id, memory_key, scope, kind, subject, predicate, object_text, summary,
                importance, confidence, future_value, relationship_value, priority_class,
                mention_count, recall_count, created_at, updated_at, last_mentioned_at,
                pinned, state, source_type
            ) VALUES (
                'm-c2', 'user.fact.birth_date', 'global', 'user_fact', 'user', 'birth_date',
                '1月2日', '我的生日是1月2日', 0.7, 1.0, 0.0, 0.0, 'P2', 1, 0,
                10.0, 10.0, 10.0, 0, 'active', 'user_asserted'
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    store = ContinuityStore(db_path)
    try:
        assert store.schema_version == 7
        hits = store.search_memory_fts('"我的生日"', now=20.0)
        assert [record.id for record, _ in hits] == ["m-c2"]
    finally:
        store.close()



def test_existing_c4_database_migrates_through_c6_without_changing_memory_truth(tmp_path) -> None:
    db_path = tmp_path / "continuity-v4.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(MIGRATION_1)
        connection.executescript(MIGRATION_2)
        connection.executescript(MIGRATION_3)
        connection.executescript(MIGRATION_4)
        connection.execute(
            """
            INSERT INTO memory_items(
                id, memory_key, scope, kind, subject, predicate, object_text, summary,
                importance, confidence, future_value, relationship_value, priority_class,
                mention_count, recall_count, created_at, updated_at, last_mentioned_at,
                pinned, state, source_type, retention_tier, retention_score
            ) VALUES (
                'm-c4', 'user.fact.name', 'global', 'user_fact', 'user', 'name',
                '真由理', '我的名字叫真由理', 0.8, 1.0, 0.0, 0.0, 'P1', 1, 0,
                10.0, 10.0, 10.0, 1, 'active', 'user_asserted', 'cold', 0.44
            )
            """
        )
        connection.commit()
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 4
    finally:
        connection.close()

    store = ContinuityStore(db_path)
    try:
        assert store.schema_version == 7
        memory = store.get_active_memory("user.fact.name")
        assert memory is not None
        assert memory.object_text == "真由理"
        assert memory.retention_tier.value == "cold"
        assert memory.retention_score == 0.44
        snapshot = store.get_relationship_snapshot(now=20.0)
        assert snapshot.relationship_value("trust") == 0.5
        assert snapshot.affect_value("irritation") == 0.0
        assert store.list_relationship_events() == []
    finally:
        store.close()


def test_existing_c5_database_migrates_to_c6_preserving_relationship_state(tmp_path) -> None:
    db_path = tmp_path / "continuity-v5.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(MIGRATION_1)
        connection.executescript(MIGRATION_2)
        connection.executescript(MIGRATION_3)
        connection.executescript(MIGRATION_4)
        connection.executescript(MIGRATION_5)
        connection.execute(
            "UPDATE relationship_state SET value = 0.62, event_count = 1, updated_at = 100.0 WHERE dimension = 'trust'"
        )
        connection.commit()
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 5
    finally:
        connection.close()

    store = ContinuityStore(db_path)
    try:
        assert store.schema_version == 7
        snapshot = store.get_relationship_snapshot(now=100.0)
        assert snapshot.relationship_value("trust") == 0.62
        assert store.get_life_schedule("kurisu", "2026-09-18") is None
        assert store.list_life_events("kurisu") == []
    finally:
        store.close()
