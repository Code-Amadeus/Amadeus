from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def scalar(conn: sqlite3.Connection, sql: str):
    row = conn.execute(sql).fetchone()
    return None if row is None else row[0]


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table'
            """
        )
    }


def snapshot(conn: sqlite3.Connection) -> dict:
    tables = table_names(conn)

    result = {
        "schema_version": int(
            scalar(conn, "PRAGMA user_version") or 0
        ),
        "integrity": str(
            scalar(conn, "PRAGMA integrity_check")
        ),
        "journal_mode": str(
            scalar(conn, "PRAGMA journal_mode")
        ),
        "tables": sorted(tables),
    }

    if "memory_items" in tables:
        result["memory_items"] = int(
            scalar(
                conn,
                "SELECT COUNT(*) FROM memory_items",
            )
            or 0
        )

    if "memory_tombstones" in tables:
        result["memory_tombstones"] = int(
            scalar(
                conn,
                "SELECT COUNT(*) FROM memory_tombstones",
            )
            or 0
        )

    if "memory_embeddings" in tables:
        result["memory_embeddings"] = int(
            scalar(
                conn,
                "SELECT COUNT(*) FROM memory_embeddings",
            )
            or 0
        )

    return result


def open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )


def fail(message: str, code: int = 2) -> None:
    print(f"BACKUP_GUARD_FAIL: {message}")
    raise SystemExit(code)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create a verified pre-migration SQLite backup. "
            "Refuses missing, zero-byte, corrupt, or unreadable DBs."
        )
    )

    parser.add_argument(
        "--db",
        required=True,
        help="Source SQLite database",
    )

    parser.add_argument(
        "--backup-dir",
        required=True,
        help="Directory for verified backup",
    )

    parser.add_argument(
        "--evidence-dir",
        required=True,
        help="Directory for JSON/TXT evidence",
    )

    parser.add_argument(
        "--label",
        default="pre_c4",
        help="Backup label",
    )

    args = parser.parse_args()

    source = Path(args.db).resolve()
    backup_dir = Path(args.backup_dir).resolve()
    evidence_dir = Path(args.evidence_dir).resolve()

    if not source.exists():
        fail(f"source DB does not exist: {source}")

    if not source.is_file():
        fail(f"source DB is not a file: {source}")

    if source.stat().st_size <= 0:
        fail(f"source DB is zero bytes: {source}")

    backup_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    evidence_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Read-only inspection first.
    try:
        source_conn = open_ro(source)
        source_info = snapshot(source_conn)
    except Exception as exc:
        fail(
            "cannot inspect source DB: "
            f"{type(exc).__name__}: {exc}"
        )

    if source_info["integrity"].lower() != "ok":
        source_conn.close()
        fail(
            "source integrity_check != ok: "
            f"{source_info['integrity']}"
        )

    stamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    final_backup = (
        backup_dir
        / f"continuity_{args.label}_{stamp}.sqlite3"
    )

    partial_backup = Path(
        str(final_backup) + ".partial"
    )

    if partial_backup.exists():
        partial_backup.unlink()

    # sqlite3 backup API captures a transactionally
    # consistent committed SQLite state, including
    # databases operating in WAL mode.
    try:
        destination = sqlite3.connect(
            partial_backup
        )

        source_conn.backup(destination)

        destination.close()
        source_conn.close()

    except Exception:
        try:
            source_conn.close()
        except Exception:
            pass

        try:
            destination.close()
        except Exception:
            pass

        partial_backup.unlink(
            missing_ok=True
        )
        raise

    if not partial_backup.exists():
        fail(
            "backup API returned without creating output"
        )

    if partial_backup.stat().st_size <= 0:
        partial_backup.unlink(
            missing_ok=True
        )
        fail(
            "backup output is zero bytes"
        )

    try:
        backup_conn = open_ro(
            partial_backup
        )

        backup_info = snapshot(
            backup_conn
        )

        backup_conn.close()

    except Exception as exc:
        partial_backup.unlink(
            missing_ok=True
        )

        fail(
            "cannot verify backup: "
            f"{type(exc).__name__}: {exc}"
        )

    if backup_info["integrity"].lower() != "ok":
        partial_backup.unlink(
            missing_ok=True
        )

        fail(
            "backup integrity_check != ok: "
            f"{backup_info['integrity']}"
        )

    if (
        backup_info["schema_version"]
        != source_info["schema_version"]
    ):
        partial_backup.unlink(
            missing_ok=True
        )

        fail(
            "schema version changed during backup"
        )

    # For known authority tables, counts must match
    # the transactionally captured source snapshot.
    for key in (
        "memory_items",
        "memory_tombstones",
        "memory_embeddings",
    ):
        if (
            key in source_info
            and backup_info.get(key)
            != source_info[key]
        ):
            partial_backup.unlink(
                missing_ok=True
            )

            fail(
                f"{key} count mismatch"
            )

    os.replace(
        partial_backup,
        final_backup,
    )

    backup_sha = sha256_file(
        final_backup
    )

    result = {
        "status": "PASS",
        "source": {
            "path": str(source),
            "size_bytes":
                source.stat().st_size,
            **source_info,
        },
        "backup": {
            "path": str(final_backup),
            "size_bytes":
                final_backup.stat().st_size,
            "sha256": backup_sha,
            **backup_info,
        },
        "method":
            "sqlite3.Connection.backup",
        "source_open_mode":
            "read-only",
    }

    json_path = (
        evidence_dir
        / "PRE_MIGRATION_BACKUP_RESULT.json"
    )

    txt_path = (
        evidence_dir
        / "PRE_MIGRATION_BACKUP_RESULT.txt"
    )

    json_path.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    txt_path.write_text(
        "\n".join([
            "Amadeus SQLite Pre-Migration Backup",
            "",
            "Result: PASS",
            f"Source: {source}",
            (
                "Source schema: "
                f"{source_info['schema_version']}"
            ),
            (
                "Source integrity: "
                f"{source_info['integrity']}"
            ),
            f"Backup: {final_backup}",
            (
                "Backup integrity: "
                f"{backup_info['integrity']}"
            ),
            f"Backup SHA-256: {backup_sha}",
            "",
            (
                "Backup method: "
                "SQLite online backup API"
            ),
        ]),
        encoding="utf-8",
    )

    print(
        "SOURCE_SCHEMA:",
        source_info["schema_version"],
    )

    print(
        "SOURCE_INTEGRITY:",
        source_info["integrity"],
    )

    print(
        "BACKUP:",
        final_backup,
    )

    print(
        "BACKUP_SHA256:",
        backup_sha,
    )

    print(
        "BACKUP_INTEGRITY:",
        backup_info["integrity"],
    )

    print(
        "PRE_MIGRATION_BACKUP_GUARD_PASS"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())