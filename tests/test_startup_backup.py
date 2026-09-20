from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from server.backup_state import backup_database, backup_state_set, run_startup_backup

_OVERRIDE_KEYS = (
    "AMADEUS_SESSION_DIR",
    "AMADEUS_CONTINUITY_DB_PATH",
    "AMADEUS_WORK_LEDGER_PATH",
)


def _set_db_value(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS t(value TEXT)")
        connection.execute("DELETE FROM t")
        connection.execute("INSERT INTO t VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def _read_db_value(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT value FROM t").fetchone()
        return str(row[0])
    finally:
        connection.close()


def test_backup_state_set_refreshes_in_place_and_keeps_deleted_transcripts(tmp_path) -> None:
    root = tmp_path / "state"
    sessions = root / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s1.json").write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")
    continuity = root / "runtime" / "continuity.sqlite3"
    ledger = root / "runtime" / "work_ledger.sqlite3"
    _set_db_value(continuity, "v1")
    _set_db_value(ledger, "w1")
    backup_dir = root / "runtime" / "backup"

    first = backup_state_set(
        continuity_db=continuity,
        ledger_db=ledger,
        sessions_dir=sessions,
        backup_dir=backup_dir,
    )
    assert first["continuity.sqlite3"] == "backed_up"
    assert first["work_ledger.sqlite3"] == "backed_up"
    assert first["sessions"] == "1_files"
    assert _read_db_value(backup_dir / "continuity.sqlite3") == "v1"
    assert _read_db_value(backup_dir / "work_ledger.sqlite3") == "w1"
    assert (backup_dir / "sessions" / "s1.json").is_file()

    # Second run: same-name content is refreshed, and a transcript deleted
    # from the live directory is *kept* in the mirror so it stays recoverable.
    _set_db_value(continuity, "v2")
    (sessions / "s1.json").unlink()
    (sessions / "s2.json").write_text(json.dumps({"session_id": "s2"}), encoding="utf-8")
    second = backup_state_set(
        continuity_db=continuity,
        ledger_db=ledger,
        sessions_dir=sessions,
        backup_dir=backup_dir,
    )
    assert second["continuity.sqlite3"] == "backed_up"
    assert _read_db_value(backup_dir / "continuity.sqlite3") == "v2"
    assert (backup_dir / "sessions" / "s1.json").is_file()
    assert (backup_dir / "sessions" / "s2.json").is_file()
    # No timestamped stacking: the slot holds the fixed names only.
    assert sorted(path.name for path in backup_dir.iterdir()) == [
        "continuity.sqlite3",
        "sessions",
        "work_ledger.sqlite3",
    ]
    assert not list(backup_dir.glob("*.staging"))


def test_backup_state_set_skips_missing_sources_without_creating_anything(tmp_path) -> None:
    backup_dir = tmp_path / "backup"

    result = backup_state_set(
        continuity_db=tmp_path / "missing-continuity.sqlite3",
        ledger_db=tmp_path / "missing-ledger.sqlite3",
        sessions_dir=tmp_path / "missing-sessions",
        backup_dir=backup_dir,
    )

    assert result == {
        "continuity.sqlite3": "missing",
        "work_ledger.sqlite3": "missing",
        "sessions": "missing",
    }
    assert not backup_dir.exists()


def test_backup_database_keeps_previous_backup_when_source_is_damaged(tmp_path) -> None:
    source = tmp_path / "runtime" / "continuity.sqlite3"
    target = tmp_path / "runtime" / "backup" / "continuity.sqlite3"
    _set_db_value(source, "v1")
    assert backup_database(source, target) is True
    assert _read_db_value(target) == "v1"

    # Damage the live database: the refresh must be refused so the previous
    # good backup remains the recovery point.
    source.write_bytes(b"this is not a database anymore")
    assert backup_database(source, target) is False
    assert _read_db_value(target) == "v1"
    assert not (tmp_path / "runtime" / "backup" / "continuity.sqlite3.staging").exists()


def test_backup_database_skips_fresh_unreadable_source(tmp_path) -> None:
    broken = tmp_path / "broken.sqlite3"
    broken.write_text("this is not a database", encoding="utf-8")
    target = tmp_path / "backup" / "broken.sqlite3"

    assert backup_database(broken, target) is False
    assert not target.exists()


def test_run_startup_backup_refreshes_default_layout(tmp_path, monkeypatch) -> None:
    for key in _OVERRIDE_KEYS:
        monkeypatch.delenv(key, raising=False)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "s.json").write_text(json.dumps({"session_id": "s"}), encoding="utf-8")
    _set_db_value(tmp_path / "runtime" / "continuity.sqlite3", "v1")

    result = run_startup_backup(tmp_path)

    assert result["continuity.sqlite3"] == "backed_up"
    assert result["sessions"] == "1_files"
    assert (tmp_path / "runtime" / "backup" / "sessions" / "s.json").is_file()


def test_run_startup_backup_opts_out_when_state_paths_are_overridden(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AMADEUS_SESSION_DIR", str(tmp_path / "isolated-sessions"))

    assert run_startup_backup(tmp_path / "project") == {}
    assert not (tmp_path / "project").exists()
