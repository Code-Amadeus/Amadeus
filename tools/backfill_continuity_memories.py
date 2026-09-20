"""One-off operator tool: backfill durable memories from retained Session transcripts.

The 2026-09 memory-scheme change widened passive capture (substantive
utterances and plans) and moved retention thresholds into the reviewable policy.
Turns accepted *before* that change were consolidated by the old, narrower
extractor, so they hold no durable row and therefore no C8 archive anchor.
This tool replays the deterministic extractor over the retained transcripts.

Properties:

- it never rewrites the consolidation journal (``complete_turn=False``);
- it is safe to re-run: a candidate whose active row already carries the same
  value is skipped instead of double-counting mentions;
- it never applies historical remember/forget/mute control commands - only
  passive extraction results are written.  A control command that was already
  honored was honored when it arrived.

``--drop-memory-id`` is a repair hatch for rows produced by *old mis-parsing*
(the historical "我的意思是说..." user-fact slot is the recorded example).  It
deletes the row and its derived state without installing a forget tombstone,
because this is data repair, not a user forget request; user-requested
forgetting must keep going through ``continuity.memory.forget``.

Usage:

    python tools/backfill_continuity_memories.py --dry-run
    python tools/backfill_continuity_memories.py --backup
    python tools/backfill_continuity_memories.py --drop-memory-id <memory-id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.continuity.memory_extractor import DeterministicMemoryExtractor
from core.continuity.memory_resolver import MemoryResolver, normalize_memory_text
from core.continuity.store import ContinuityStore
from core.continuity.turn_ingest import load_turn_evidence

_SUMMARY_PREVIEW_CHARS = 60


def _preview(value: str) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= _SUMMARY_PREVIEW_CHARS:
        return text
    return text[: _SUMMARY_PREVIEW_CHARS - 1] + "…"


def _session_turns(session_path: Path) -> tuple[str, list[str]]:
    try:
        payload = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", []
    if not isinstance(payload, dict):
        return "", []
    session_id = str(payload.get("session_id") or session_path.stem).strip()
    turn_ids: list[str] = []
    for message in payload.get("dialog") or ():
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip() != "user":
            continue
        turn_id = str(message.get("turn_id") or "").strip()
        if turn_id and turn_id not in turn_ids:
            turn_ids.append(turn_id)
    return session_id, turn_ids


def _write_backup(db_path: str) -> Path:
    backup = Path(f"{db_path}.backup-{time.strftime('%Y%m%d-%H%M%S')}")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("VACUUM INTO ?", (str(backup),))
    finally:
        connection.close()
    return backup


async def _backfill(args: argparse.Namespace) -> dict[str, int]:
    session_dir = Path(args.session_dir)
    store = ContinuityStore(args.db)
    extractor = DeterministicMemoryExtractor()
    resolver = MemoryResolver()
    totals = {
        "sessions": 0,
        "turns": 0,
        "candidates": 0,
        "created": 0,
        "updated": 0,
        "skipped_same_value": 0,
        "suppressed": 0,
        "errors": 0,
    }
    try:
        for path in sorted(session_dir.glob("*.json")):
            session_id, turn_ids = _session_turns(path)
            if not session_id or session_id == "__unsessioned__":
                continue
            totals["sessions"] += 1
            for turn_id in turn_ids:
                evidence = load_turn_evidence(session_dir, session_id=session_id, turn_id=turn_id)
                if evidence is None:
                    continue
                totals["turns"] += 1
                try:
                    candidates = tuple(await extractor.extract(evidence))
                except Exception as exc:  # noqa: BLE001 - operator tool reports and continues
                    totals["errors"] += 1
                    print(f"error {session_id}/{turn_id}: {type(exc).__name__}: {exc}")
                    continue
                for candidate in candidates:
                    totals["candidates"] += 1
                    scope = str(candidate.scope or "global")
                    existing = store.get_active_memory(candidate.memory_key, scope=scope)
                    same_value = existing is not None and normalize_memory_text(
                        existing.object_text or existing.summary
                    ) == normalize_memory_text(candidate.object_text or candidate.summary)
                    if same_value:
                        totals["skipped_same_value"] += 1
                        continue
                    if args.dry_run:
                        print(
                            f"[dry-run] {session_id}/{turn_id} "
                            f"{candidate.kind.value} {candidate.memory_key} :: "
                            f"{_preview(candidate.summary)}"
                        )
                        continue
                    records = store.apply_memory_candidates(
                        evidence,
                        (candidate,),
                        resolver=resolver,
                        complete_turn=False,
                    )
                    if not records:
                        totals["suppressed"] += 1
                    elif existing is None:
                        totals["created"] += len(records)
                    else:
                        totals["updated"] += len(records)
                    print(
                        f"{'created' if existing is None and records else 'updated' if records else 'suppressed'} "
                        f"{session_id}/{turn_id} {candidate.kind.value} {candidate.memory_key} :: "
                        f"{_preview(candidate.summary)}"
                    )
    finally:
        store.close()
    return totals


def _list_rows(args: argparse.Namespace) -> int:
    store = ContinuityStore(args.db)
    try:
        records = store.list_active_memories()
        records.sort(key=lambda record: (float(record.created_at), record.memory_key))
        print(f"active memories: {len(records)}")
        for record in records:
            created = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(record.created_at)))
            print(
                f"- {record.id} [{created}] {record.retention_tier.value} "
                f"{record.kind.value} {record.memory_key} :: {_preview(record.summary)}"
            )
        print("diagnostics: " + json.dumps(store.continuity_diagnostics(), ensure_ascii=False))
    finally:
        store.close()
    return 0


def _drop_rows(args: argparse.Namespace) -> int:
    store = ContinuityStore(args.db)
    dropped = 0
    try:
        for memory_id in args.drop_memory_id or ():
            records = store.get_memories_by_ids((str(memory_id),))
            record = records[0] if records else None
            if record is None:
                print(f"drop {memory_id}: no active row")
                continue
            label = f"{record.memory_key} :: {_preview(record.summary)}"
            if args.dry_run:
                print(f"[dry-run] would drop {label}")
                continue
            with store._lock:
                store._ensure_open()
                store._connection.execute("DELETE FROM memory_items WHERE id = ?", (str(memory_id),))
            remaining = store._connection.execute(
                "SELECT COUNT(*) AS n FROM memory_fts WHERE memory_id = ?",
                (str(memory_id),),
            ).fetchone()["n"]
            dropped += 1
            print(f"dropped {label} (fts rows left: {remaining})")
    finally:
        store.close()
    return dropped


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="runtime/continuity.sqlite3", help="Continuity SQLite path")
    parser.add_argument("--session-dir", default="sessions", help="Session transcript directory")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--backup", action="store_true", help="VACUUM INTO a timestamped backup first")
    parser.add_argument(
        "--drop-memory-id",
        action="append",
        default=[],
        help="repair hatch: delete one mis-parsed active row without a tombstone (repeatable)",
    )
    parser.add_argument("--list", action="store_true", help="list active memories and stop")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.list:
        return _list_rows(args)
    if args.drop_memory_id:
        _drop_rows(args)
        return 0
    if args.backup and not args.dry_run:
        backup = _write_backup(args.db)
        print(f"backup written: {backup}")
    totals = asyncio.run(_backfill(args))
    print(
        "summary: "
        + ", ".join(f"{key}={value}" for key, value in totals.items())
        + (" (dry-run)" if args.dry_run else "")
    )
    return 0 if totals["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
