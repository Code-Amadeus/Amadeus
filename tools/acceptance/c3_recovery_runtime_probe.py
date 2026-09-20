from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def fail(message: str, code: int = 4) -> None:
    print(f"C3_RUNTIME_PROBE_FAIL: {message}")
    raise SystemExit(code)


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--c3-root",
        required=True,
    )

    parser.add_argument(
        "--db",
        required=True,
    )

    parser.add_argument(
        "--evidence",
        required=True,
    )

    args = parser.parse_args()

    c3_root = Path(args.c3_root).resolve()
    db = Path(args.db).resolve()
    evidence = Path(args.evidence).resolve()

    if not c3_root.exists():
        fail("C3 root missing")

    if not db.exists():
        fail("recovery database missing")

    if db.stat().st_size <= 0:
        fail("recovery database is zero bytes")

    # Make absolutely sure imports come from the supplied
    # C3 v1.1 source tree, not from C4 acceptance.
    sys.path.insert(
        0,
        str(c3_root),
    )

    from core.continuity import (
        ContinuityStore,
        MemoryRetriever,
    )

    conn = sqlite3.connect(
        f"file:{db.as_posix()}?mode=ro",
        uri=True,
    )

    raw_schema = conn.execute(
        "PRAGMA user_version"
    ).fetchone()[0]

    integrity = conn.execute(
        "PRAGMA integrity_check"
    ).fetchone()[0]

    tables = {
        row[0]
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table'
            """
        )
    }

    memory_count = (
        conn.execute(
            "SELECT COUNT(*) FROM memory_items"
        ).fetchone()[0]
        if "memory_items" in tables
        else None
    )

    tombstone_count = (
        conn.execute(
            "SELECT COUNT(*) "
            "FROM memory_tombstones"
        ).fetchone()[0]
        if "memory_tombstones" in tables
        else None
    )

    conn.close()

    if raw_schema != 3:
        fail(
            f"expected schema 3, got {raw_schema}"
        )

    if integrity != "ok":
        fail(
            f"integrity_check={integrity}"
        )

    store = ContinuityStore(db)

    try:
        value = store.schema_version

        store_schema = (
            value()
            if callable(value)
            else value
        )

        if int(store_schema) != 3:
            fail(
                "C3 ContinuityStore did not "
                "open database as schema 3"
            )

        retriever = MemoryRetriever(
            store
        )

        # This deliberately uses a unique harmless
        # no-match query. We only verify that the
        # real C3 retrieval path executes normally.
        # No user memory text is printed or persisted.
        hits = retriever.retrieve(
            "__amadeus_c3_rollback_probe_no_match__",
            now=1000.0,
        )

        try:
            hit_count = len(hits)
        except TypeError:
            fail(
                "C3 retrieve result is not sized"
            )

    finally:
        close = getattr(
            store,
            "close",
            None,
        )

        if callable(close):
            close()

    result = {
        "status": "PASS",
        "c3_root": str(c3_root),
        "database": str(db),
        "schema": int(raw_schema),
        "store_schema": int(store_schema),
        "integrity": integrity,
        "memory_count": memory_count,
        "tombstone_count": tombstone_count,
        "retrieval_call": "PASS",
        "probe_hit_count": int(hit_count),
        "memory_content_logged": False,
    }

    evidence.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    evidence.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        "C3_RUNTIME_SCHEMA:",
        raw_schema,
    )

    print(
        "C3_RUNTIME_INTEGRITY:",
        integrity,
    )

    print(
        "C3_RUNTIME_MEMORY_COUNT:",
        memory_count,
    )

    print(
        "C3_RUNTIME_TOMBSTONE_COUNT:",
        tombstone_count,
    )

    print(
        "C3_RUNTIME_RETRIEVAL_CALL: PASS"
    )

    print(
        "C3_RUNTIME_RECOVERY_PROBE_PASS"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())