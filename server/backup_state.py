"""Fixed-slot backup of local durable chat state.

Every server start refreshes one backup set under ``runtime/backup/``:

- ``continuity.sqlite3`` — durable memories, relationship and life state;
- ``work_ledger.sqlite3`` — Work Ledger facts;
- ``sessions/`` — a mirror of the Session transcript directory.

The slot is overwritten in place; timestamped copies never stack.  The backup
exists to *recover* deleted or damaged state, so it never deletes:

- a Session transcript removed from the live directory stays recoverable from
  the mirror (same-name files are overwritten, missing files are kept);
- a database is only refreshed while the source passes ``PRAGMA quick_check``;
  a missing or unreadable source keeps the previous good backup untouched.

A backup must never block startup: failures are logged and skipped.  Because
the fixed slot belongs to the default local state layout, an explicitly
overridden layout (``AMADEUS_SESSION_DIR`` / ``AMADEUS_CONTINUITY_DB_PATH`` /
``AMADEUS_WORK_LEDGER_PATH`` — CI, e2e and tooling isolation) opts out instead
of clobbering the real slot.  All paths are resolved relative to the project
root handed in by the caller, never from the folder's absolute name.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_OVERRIDE_ENV_KEYS = (
    "AMADEUS_SESSION_DIR",
    "AMADEUS_CONTINUITY_DB_PATH",
    "AMADEUS_WORK_LEDGER_PATH",
)


def _database_is_healthy(path: Path) -> bool:
    """Refuse to replace a good backup with an unreadable/damaged source."""

    try:
        connection = sqlite3.connect(str(path))
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return bool(row) and str(row[0]).strip().lower() == "ok"


def backup_database(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> bool:
    """Snapshot one healthy SQLite database to a fixed target path in place.

    The sqlite backup API reads through any WAL sidecar, so the snapshot is a
    consistent point-in-time copy.  A missing or damaged source keeps the
    previous backup: recovery value must never be destroyed by a bad refresh.
    """

    source_path = Path(source)
    target_path = Path(target)
    if not source_path.is_file():
        return False
    if not _database_is_healthy(source_path):
        logger.warning(
            "state backup skipped damaged database; keeping previous backup: %s",
            source_path,
        )
        return False
    staging = target_path.with_name(target_path.name + ".staging")
    source_conn: sqlite3.Connection | None = None
    target_conn: sqlite3.Connection | None = None
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        staging.unlink(missing_ok=True)
        source_conn = sqlite3.connect(str(source_path))
        target_conn = sqlite3.connect(str(staging))
        source_conn.backup(target_conn)
        target_conn.close()
        target_conn = None
        source_conn.close()
        source_conn = None
        os.replace(staging, target_path)
    except (sqlite3.Error, OSError):
        logger.warning("state backup skipped unreadable database: %s", source_path, exc_info=True)
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    finally:
        for connection in (target_conn, source_conn):
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
    return True


def mirror_sessions(source_dir: str | os.PathLike[str], target_dir: str | os.PathLike[str]) -> int:
    """Mirror ``*.json`` Session transcripts into the backup folder.

    Merge semantics: same-name files are overwritten with the current copy and
    files that no longer exist in the live directory are *kept*, so a deleted
    transcript stays recoverable.  Returns the copied count, or -1 when the
    source directory is missing.
    """

    source = Path(source_dir)
    target = Path(target_dir)
    if not source.is_dir():
        return -1
    try:
        target.mkdir(parents=True, exist_ok=True)
        copied = 0
        for path in sorted(source.glob("*.json")):
            shutil.copy2(path, target / path.name)
            copied += 1
    except OSError:
        logger.warning("state backup skipped unreadable sessions: %s", source, exc_info=True)
        return -1
    return copied


def backup_state_set(
    *,
    continuity_db: str | os.PathLike[str],
    ledger_db: str | os.PathLike[str],
    sessions_dir: str | os.PathLike[str],
    backup_dir: str | os.PathLike[str],
) -> dict[str, str]:
    """Refresh the fixed backup set from explicit state paths."""

    backup_root = Path(backup_dir)
    results: dict[str, str] = {}
    if Path(continuity_db).is_file():
        ok = backup_database(continuity_db, backup_root / "continuity.sqlite3")
        results["continuity.sqlite3"] = "backed_up" if ok else "failed"
    else:
        results["continuity.sqlite3"] = "missing"
    if Path(ledger_db).is_file():
        ok = backup_database(ledger_db, backup_root / "work_ledger.sqlite3")
        results["work_ledger.sqlite3"] = "backed_up" if ok else "failed"
    else:
        results["work_ledger.sqlite3"] = "missing"
    mirrored = mirror_sessions(sessions_dir, backup_root / "sessions")
    results["sessions"] = f"{mirrored}_files" if mirrored >= 0 else "missing"
    if any(value != "missing" for value in results.values()):
        logger.info("startup backup set refreshed: %s in %s", results, backup_root)
    return results


def run_startup_backup(project_root: str | os.PathLike[str]) -> dict[str, str]:
    """Refresh the fixed backup slot for the default local state layout."""

    root = Path(project_root)
    if any(str(os.environ.get(key) or "").strip() for key in _OVERRIDE_ENV_KEYS):
        logger.debug("startup backup skipped: state paths are overridden by environment")
        return {}
    return backup_state_set(
        continuity_db=root / "runtime" / "continuity.sqlite3",
        ledger_db=root / "runtime" / "work_ledger.sqlite3",
        sessions_dir=root / "sessions",
        backup_dir=root / "runtime" / "backup",
    )


__all__ = [
    "backup_database",
    "backup_state_set",
    "mirror_sessions",
    "run_startup_backup",
]
