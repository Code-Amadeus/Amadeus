from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()

    with path.open("rb") as fh:
        for chunk in iter(
            lambda: fh.read(1024 * 1024),
            b"",
        ):
            h.update(chunk)

    return h.hexdigest()


def open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )


def scalar(
    conn: sqlite3.Connection,
    sql: str,
):
    row = conn.execute(sql).fetchone()
    return None if row is None else row[0]


def tables(
    conn: sqlite3.Connection,
) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table'
            """
        ).fetchall()
    }


def inspect_db(path: Path) -> dict:
    conn = open_ro(path)

    try:
        table_set = tables(conn)

        result = {
            "schema_version": int(
                scalar(
                    conn,
                    "PRAGMA user_version",
                )
                or 0
            ),
            "integrity": str(
                scalar(
                    conn,
                    "PRAGMA integrity_check",
                )
            ),
            "journal_mode": str(
                scalar(
                    conn,
                    "PRAGMA journal_mode",
                )
            ),
            "tables": sorted(table_set),
        }

        if "memory_items" in table_set:
            result["memory_items"] = int(
                scalar(
                    conn,
                    "SELECT COUNT(*) "
                    "FROM memory_items",
                )
                or 0
            )

        if "memory_tombstones" in table_set:
            result["memory_tombstones"] = int(
                scalar(
                    conn,
                    "SELECT COUNT(*) "
                    "FROM memory_tombstones",
                )
                or 0
            )

        if "memory_embeddings" in table_set:
            result["memory_embeddings"] = int(
                scalar(
                    conn,
                    "SELECT COUNT(*) "
                    "FROM memory_embeddings",
                )
                or 0
            )

        return result

    finally:
        conn.close()


def fail(
    message: str,
    code: int = 3,
) -> None:
    print(
        f"ROLLBACK_VERIFIER_FAIL: {message}"
    )
    raise SystemExit(code)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify and restore a genuine "
            "pre-C4 Continuity SQLite backup "
            "into an isolated recovery directory."
        )
    )

    parser.add_argument(
        "--backup",
        required=True,
    )

    parser.add_argument(
        "--recovery-dir",
        required=True,
    )

    parser.add_argument(
        "--evidence-dir",
        required=True,
    )

    parser.add_argument(
        "--expected-schema",
        type=int,
        default=3,
    )

    args = parser.parse_args()

    backup = Path(
        args.backup
    ).resolve()

    recovery_dir = Path(
        args.recovery_dir
    ).resolve()

    evidence_dir = Path(
        args.evidence_dir
    ).resolve()

    expected_schema = int(
        args.expected_schema
    )

    # ------------------------------------------------
    # Backup preflight
    # ------------------------------------------------

    if not backup.exists():
        fail(
            f"backup not found: {backup}"
        )

    if not backup.is_file():
        fail(
            f"backup is not a file: {backup}"
        )

    if backup.stat().st_size <= 0:
        fail(
            f"backup is zero bytes: {backup}"
        )

    backup_sha_before = sha256_file(
        backup
    )

    try:
        backup_info = inspect_db(
            backup
        )
    except Exception as exc:
        fail(
            "cannot inspect backup: "
            f"{type(exc).__name__}: {exc}"
        )

    if (
        backup_info["integrity"].lower()
        != "ok"
    ):
        fail(
            "backup integrity_check != ok: "
            f"{backup_info['integrity']}"
        )

    actual_schema = int(
        backup_info["schema_version"]
    )

    if actual_schema != expected_schema:
        fail(
            "backup schema is not the "
            "expected pre-C4 schema: "
            f"expected={expected_schema}, "
            f"actual={actual_schema}"
        )

    # Explicitly prevent current C4 DBs from
    # masquerading as rollback artifacts.
    if actual_schema >= 4:
        fail(
            "schema 4+ database cannot be used "
            "as a pre-C4 rollback artifact"
        )

    required_tables = {
        "memory_items",
        "memory_tombstones",
    }

    found_tables = set(
        backup_info["tables"]
    )

    missing_tables = (
        required_tables
        - found_tables
    )

    if missing_tables:
        fail(
            "backup missing required C3 tables: "
            + ", ".join(
                sorted(missing_tables)
            )
        )

    # ------------------------------------------------
    # Recovery destination must be isolated.
    # Refuse an existing non-empty runtime directory.
    # ------------------------------------------------

    runtime_dir = (
        recovery_dir
        / "runtime"
    )

    recovery_db = (
        runtime_dir
        / "continuity.sqlite3"
    )

    if recovery_db.exists():
        fail(
            "recovery target already exists; "
            "refusing to overwrite: "
            f"{recovery_db}"
        )

    runtime_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    evidence_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ------------------------------------------------
    # Restore only by whole-database copy.
    # Never migrate/downgrade the backup.
    # ------------------------------------------------

    shutil.copy2(
        backup,
        recovery_db,
    )

    if (
        not recovery_db.exists()
        or recovery_db.stat().st_size <= 0
    ):
        fail(
            "recovery copy is missing or zero bytes"
        )

    recovery_sha = sha256_file(
        recovery_db
    )

    if recovery_sha != backup_sha_before:
        fail(
            "recovery SHA-256 differs from backup"
        )

    try:
        recovery_info = inspect_db(
            recovery_db
        )
    except Exception as exc:
        fail(
            "cannot inspect recovery DB: "
            f"{type(exc).__name__}: {exc}"
        )

    if (
        recovery_info["integrity"].lower()
        != "ok"
    ):
        fail(
            "recovery integrity_check != ok"
        )

    if (
        recovery_info["schema_version"]
        != expected_schema
    ):
        fail(
            "recovery schema changed during restore"
        )

    # Counts of authority records must be identical.
    for key in (
        "memory_items",
        "memory_tombstones",
        "memory_embeddings",
    ):
        if (
            key in backup_info
            and recovery_info.get(key)
            != backup_info[key]
        ):
            fail(
                f"recovery count mismatch: {key}"
            )

    # Ensure source artifact itself was not changed.
    backup_sha_after = sha256_file(
        backup
    )

    if (
        backup_sha_after
        != backup_sha_before
    ):
        fail(
            "source backup changed during verification"
        )

    result = {
        "status": "PASS",
        "test":
            "rollback_recovery_artifact_verify",
        "timestamp_utc":
            datetime.now(
                timezone.utc
            ).isoformat(),
        "expected_schema":
            expected_schema,
        "backup": {
            "path": str(backup),
            "size_bytes":
                backup.stat().st_size,
            "sha256":
                backup_sha_before,
            **backup_info,
        },
        "recovery": {
            "path":
                str(recovery_db),
            "size_bytes":
                recovery_db.stat().st_size,
            "sha256":
                recovery_sha,
            **recovery_info,
        },
        "rules": {
            "whole_db_restore": True,
            "schema_downgrade_used": False,
            "source_backup_modified": False,
            "schema4_substitution_allowed":
                False,
        },
    }

    json_path = (
        evidence_dir
        / "ROLLBACK_RECOVERY_RESULT.json"
    )

    txt_path = (
        evidence_dir
        / "ROLLBACK_RECOVERY_RESULT.txt"
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
            "Amadeus Rollback / Recovery Verification",
            "",
            "Result: PASS",
            (
                "Expected pre-C4 schema: "
                f"{expected_schema}"
            ),
            (
                "Backup integrity: "
                f"{backup_info['integrity']}"
            ),
            (
                "Recovery integrity: "
                f"{recovery_info['integrity']}"
            ),
            (
                "Backup SHA-256: "
                f"{backup_sha_before}"
            ),
            (
                "Recovery SHA-256: "
                f"{recovery_sha}"
            ),
            "",
            "Restore method: whole SQLite file copy",
            "Schema downgrade used: NO",
            (
                "Schema-4 substitution permitted: "
                "NO"
            ),
        ]),
        encoding="utf-8",
    )

    print(
        "BACKUP_SCHEMA:",
        actual_schema,
    )

    print(
        "BACKUP_INTEGRITY:",
        backup_info["integrity"],
    )

    print(
        "BACKUP_SHA256:",
        backup_sha_before,
    )

    print(
        "RECOVERY_DB:",
        recovery_db,
    )

    print(
        "RECOVERY_SCHEMA:",
        recovery_info["schema_version"],
    )

    print(
        "RECOVERY_INTEGRITY:",
        recovery_info["integrity"],
    )

    print(
        "RECOVERY_SHA256_MATCH:",
        recovery_sha == backup_sha_before,
    )

    print(
        "ROLLBACK_RECOVERY_VERIFIER_PASS"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())