"""JSONL-backed long-term memory with a rebuildable SQLite FTS5 index."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Callable, Iterable, Iterator, Mapping, Sequence

from .models import MemoryRecord, MemoryStatus, normalize_iso, normalize_text, utc_now_iso


logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    namespace TEXT NOT NULL,
    tags TEXT NOT NULL,
    importance INTEGER NOT NULL,
    confidence REAL NOT NULL,
    source TEXT NOT NULL,
    source_ids TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    status TEXT NOT NULL,
    supersedes TEXT NOT NULL,
    superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS memory_scope_namespace_idx
    ON memory(scope, namespace, status, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS memory_hash_idx
    ON memory(content_hash, scope, namespace, kind, status);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    id UNINDEXED,
    text,
    tags,
    namespace,
    tokenize='trigram'
);
"""

_FTS_QUERY_SPLIT = re.compile(r"[^\w\u3400-\u9fff]+", re.UNICODE)
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")


def _fts_terms(query: str) -> list[str]:
    return [
        token
        for token in _FTS_QUERY_SPLIT.split(query)
        if token and token.isascii() and len(token) >= 3
    ]


def _like_terms(query: str) -> list[str]:
    terms: list[str] = []
    for token in _FTS_QUERY_SPLIT.split(query):
        if not token:
            continue
        if token.isascii():
            if len(token) >= 2:
                terms.append(token)
            continue
        for run in _CJK_RUN.findall(token):
            if len(run) <= 2:
                terms.append(run)
                continue
            terms.extend(run[index : index + 2] for index in range(len(run) - 1))
    return list(dict.fromkeys(terms))[:24]


class MemoryStore:
    """Owns memory persistence and indexing; callers own semantic extraction."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.jsonl_path = self.root / "memory.jsonl"
        self.sqlite_path = self.root / "memory.sqlite3"
        self.markdown_path = self.root / "companion.md"
        self.views_dir = self.root / "views"
        self._write_lock = threading.RLock()

    @classmethod
    def open(cls, root: str | Path) -> "MemoryStore":
        store = cls(root)
        store.initialize()
        return store

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.views_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path.touch(exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
        if self._index_count() == 0 and self.jsonl_path.stat().st_size:
            self.rebuild_index()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.sqlite_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    def _index_count(self) -> int:
        if not self.sqlite_path.exists():
            return 0
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM memory").fetchone()
        return int(row["count"])

    def _record_from_row(self, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            text=row["text"],
            kind=row["kind"],
            scope=row["scope"],
            namespace=row["namespace"],
            tags=tuple(json.loads(row["tags"])),
            importance=int(row["importance"]),
            confidence=float(row["confidence"]),
            source=row["source"],
            source_ids=tuple(json.loads(row["source_ids"])),
            content_hash=row["content_hash"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            status=row["status"],
            supersedes=tuple(json.loads(row["supersedes"])),
            superseded_by=row["superseded_by"],
        )

    def _upsert(self, connection: sqlite3.Connection, record: MemoryRecord) -> None:
        payload = record.to_dict()
        connection.execute(
            """
            INSERT INTO memory (
                id, text, kind, scope, namespace, tags, importance, confidence,
                source, source_ids, content_hash, created_at, updated_at,
                valid_from, valid_to, status, supersedes, superseded_by
            ) VALUES (
                :id, :text, :kind, :scope, :namespace, :tags, :importance, :confidence,
                :source, :source_ids, :content_hash, :created_at, :updated_at,
                :valid_from, :valid_to, :status, :supersedes, :superseded_by
            )
            ON CONFLICT(id) DO UPDATE SET
                text=excluded.text,
                kind=excluded.kind,
                scope=excluded.scope,
                namespace=excluded.namespace,
                tags=excluded.tags,
                importance=excluded.importance,
                confidence=excluded.confidence,
                source=excluded.source,
                source_ids=excluded.source_ids,
                content_hash=excluded.content_hash,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at,
                valid_from=excluded.valid_from,
                valid_to=excluded.valid_to,
                status=excluded.status,
                supersedes=excluded.supersedes,
                superseded_by=excluded.superseded_by
            """,
            {
                **payload,
                "tags": json.dumps(payload["tags"], ensure_ascii=False),
                "source_ids": json.dumps(payload["source_ids"], ensure_ascii=False),
                "supersedes": json.dumps(payload["supersedes"], ensure_ascii=False),
            },
        )
        connection.execute("DELETE FROM memory_fts WHERE id = ?", (record.id,))
        connection.execute(
            "INSERT INTO memory_fts(id, text, tags, namespace) VALUES (?, ?, ?, ?)",
            (record.id, record.text, " ".join(record.tags), record.namespace),
        )

    def _append_snapshots(self, records: Sequence[MemoryRecord]) -> None:
        if not records:
            return
        with self.jsonl_path.open("a", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _read_snapshots(self) -> dict[str, MemoryRecord]:
        latest: dict[str, MemoryRecord] = {}
        with self.jsonl_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    record = MemoryRecord.from_mapping(payload)
                except Exception as error:
                    raise ValueError(f"invalid memory snapshot at line {line_number}: {error}") from error
                latest[record.id] = record
        return latest

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM memory WHERE id = ?", (memory_id,)).fetchone()
        return self._record_from_row(row) if row else None

    def remember(
        self,
        candidates: Iterable[MemoryRecord | Mapping[str, object]],
        *,
        now: str | None = None,
    ) -> list[MemoryRecord]:
        timestamp = normalize_iso(now or utc_now_iso())
        normalized: list[MemoryRecord] = []
        for candidate in candidates:
            record = candidate if isinstance(candidate, MemoryRecord) else MemoryRecord.from_mapping(candidate)
            normalized.append(record.evolve(updated_at=timestamp))
        accepted: list[MemoryRecord] = []
        with self._write_lock, self._connect() as connection:
            for record in normalized:
                duplicate = connection.execute(
                    """
                    SELECT id FROM memory
                    WHERE content_hash=? AND scope=? AND namespace=? AND kind=? AND status='active'
                    LIMIT 1
                    """,
                    (record.content_hash, record.scope, record.namespace, record.kind),
                ).fetchone()
                if duplicate:
                    continue
                accepted.append(record)
            if accepted:
                self._append_snapshots(accepted)
                connection.execute("BEGIN")
                try:
                    for record in accepted:
                        self._upsert(connection, record)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        return accepted

    def recall(
        self,
        query: str,
        *,
        scopes: Sequence[str] | None = None,
        namespaces: Sequence[str] | None = None,
        limit: int = 5,
        as_of: str | None = None,
    ) -> list[MemoryRecord]:
        clean_query = normalize_text(query)
        clean_limit = max(1, min(int(limit), 50))
        effective_time = normalize_iso(as_of or utc_now_iso())
        common_where = [
            "m.valid_from <= ?",
            "(m.valid_to IS NULL OR m.valid_to > ?)",
        ]
        common_params: list[object] = [effective_time, effective_time]
        if scopes:
            placeholders = ",".join("?" for _ in scopes)
            common_where.append(f"m.scope IN ({placeholders})")
            common_params.extend(scopes)
        if namespaces:
            placeholders = ",".join("?" for _ in namespaces)
            common_where.append(f"m.namespace IN ({placeholders})")
            common_params.extend(namespaces)

        rows: list[sqlite3.Row] = []
        fts_terms = _fts_terms(clean_query)
        if fts_terms:
            fts_query = " OR ".join(
                f'"{term.replace(chr(34), chr(34) * 2)}"' for term in fts_terms
            )
            sql = (
                "SELECT m.*, bm25(memory_fts) AS score "
                "FROM memory_fts f JOIN memory m ON m.id = f.id "
                f"WHERE memory_fts MATCH ? AND {' AND '.join(common_where)} "
                "ORDER BY score ASC, m.importance DESC, m.updated_at DESC LIMIT ?"
            )
            try:
                with self._connect() as connection:
                    rows.extend(
                        connection.execute(
                            sql, [fts_query, *common_params, clean_limit * 3]
                        ).fetchall()
                    )
            except sqlite3.OperationalError:
                logger.debug("FTS memory recall unavailable; using LIKE fallback", exc_info=True)

        like_terms = _like_terms(clean_query)
        if like_terms:
            clauses: list[str] = []
            like_params: list[object] = [*common_params]
            for term in like_terms:
                clauses.append("(m.text LIKE ? OR m.tags LIKE ? OR m.namespace LIKE ?)")
                pattern = f"%{term}%"
                like_params.extend((pattern, pattern, pattern))
            sql = (
                "SELECT m.* FROM memory m "
                f"WHERE {' AND '.join(common_where)} AND ({' OR '.join(clauses)}) "
                "ORDER BY m.importance DESC, m.updated_at DESC LIMIT ?"
            )
            with self._connect() as connection:
                rows.extend(
                    connection.execute(sql, [*like_params, clean_limit * 3]).fetchall()
                )

        if not clean_query:
            sql = (
                "SELECT m.* FROM memory m "
                f"WHERE {' AND '.join(common_where)} "
                "ORDER BY m.importance DESC, m.updated_at DESC LIMIT ?"
            )
            with self._connect() as connection:
                rows.extend(connection.execute(sql, [*common_params, clean_limit]).fetchall())

        deduped: list[MemoryRecord] = []
        seen: set[str] = set()
        for row in rows:
            record = self._record_from_row(row)
            if record.id in seen:
                continue
            seen.add(record.id)
            deduped.append(record)
            if len(deduped) >= clean_limit:
                break
        return deduped

    def _fallback_like(
        self,
        query: str,
        *,
        scopes: Sequence[str] | None,
        namespaces: Sequence[str] | None,
        limit: int,
        as_of: str,
    ) -> list[sqlite3.Row]:
        where = [
            "status='active'",
            "valid_from <= ?",
            "(valid_to IS NULL OR valid_to > ?)",
            "(text LIKE ? OR tags LIKE ?)",
        ]
        params: list[object] = [as_of, as_of, f"%{query}%", f"%{query}%"]
        if scopes:
            where.append(f"scope IN ({','.join('?' for _ in scopes)})")
            params.extend(scopes)
        if namespaces:
            where.append(f"namespace IN ({','.join('?' for _ in namespaces)})")
            params.extend(namespaces)
        params.append(limit)
        with self._connect() as connection:
            return connection.execute(
                f"SELECT * FROM memory WHERE {' AND '.join(where)} "
                "ORDER BY importance DESC, updated_at DESC LIMIT ?",
                params,
            ).fetchall()

    def supersede(
        self,
        old_id: str,
        replacement: MemoryRecord | Mapping[str, object],
        *,
        now: str | None = None,
    ) -> MemoryRecord:
        timestamp = normalize_iso(now or utc_now_iso())
        with self._write_lock:
            old = self.get(old_id)
            if old is None:
                raise KeyError(f"unknown memory id: {old_id}")
            candidate = replacement if isinstance(replacement, MemoryRecord) else MemoryRecord.from_mapping(replacement)
            new_record = candidate.evolve(
                supersedes=tuple(dict.fromkeys((*candidate.supersedes, old.id))),
                valid_from=timestamp,
                updated_at=timestamp,
            )
            old_record = old.evolve(
                valid_to=timestamp,
                status=MemoryStatus.SUPERSEDED.value,
                superseded_by=new_record.id,
                updated_at=timestamp,
            )
            snapshots = (old_record, new_record)
            self._append_snapshots(snapshots)
            with self._connect() as connection:
                connection.execute("BEGIN")
                try:
                    self._upsert(connection, old_record)
                    self._upsert(connection, new_record)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            return new_record

    def revoke(self, memory_id: str, *, now: str | None = None) -> MemoryRecord:
        timestamp = normalize_iso(now or utc_now_iso())
        with self._write_lock:
            record = self.get(memory_id)
            if record is None:
                raise KeyError(f"unknown memory id: {memory_id}")
            revoked = record.evolve(
                status=MemoryStatus.REVOKED.value,
                valid_to=timestamp,
                updated_at=timestamp,
            )
            self._append_snapshots((revoked,))
            with self._connect() as connection:
                self._upsert(connection, revoked)
                connection.commit()
            return revoked

    def compact_namespace(
        self,
        namespace: str,
        *,
        scope: str | None = None,
        min_records: int = 10,
        summarizer: Callable[[Sequence[MemoryRecord]], str] | None = None,
        now: str | None = None,
    ) -> MemoryRecord | None:
        timestamp = normalize_iso(now or utc_now_iso())
        candidates = self.list_records(scopes=[scope] if scope else None, namespaces=[namespace], active_only=True)
        candidates = [record for record in candidates if record.kind != "summary"]
        if len(candidates) < max(2, int(min_records)):
            return None
        chosen_scope = scope or candidates[0].scope
        if summarizer:
            summary_text = normalize_text(summarizer(candidates))
        else:
            unique = list(dict.fromkeys(record.text for record in candidates))
            summary_text = normalize_text("；".join(unique[:30]))
        if not summary_text:
            return None
        tags = tuple(dict.fromkeys(tag for record in candidates for tag in record.tags))
        source_ids = tuple(record.id for record in candidates)
        summary = MemoryRecord.create(
            text=summary_text,
            kind="summary",
            scope=chosen_scope,
            namespace=namespace,
            tags=tags,
            importance=max(record.importance for record in candidates),
            confidence=min(record.confidence for record in candidates),
            source="compaction",
            source_ids=source_ids,
            created_at=timestamp,
        ).evolve(supersedes=source_ids)
        compacted = tuple(
            record.evolve(
                status=MemoryStatus.COMPACTED.value,
                valid_to=timestamp,
                superseded_by=summary.id,
                updated_at=timestamp,
            )
            for record in candidates
        )
        snapshots = (*compacted, summary)
        with self._write_lock:
            self._append_snapshots(snapshots)
            with self._connect() as connection:
                connection.execute("BEGIN")
                try:
                    for record in snapshots:
                        self._upsert(connection, record)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        return summary

    def list_records(
        self,
        *,
        scopes: Sequence[str] | None = None,
        namespaces: Sequence[str] | None = None,
        active_only: bool = False,
        limit: int | None = None,
    ) -> list[MemoryRecord]:
        where: list[str] = []
        params: list[object] = []
        if active_only:
            where.append("status='active'")
        if scopes:
            where.append(f"scope IN ({','.join('?' for _ in scopes)})")
            params.extend(scopes)
        if namespaces:
            where.append(f"namespace IN ({','.join('?' for _ in namespaces)})")
            params.extend(namespaces)
        sql = "SELECT * FROM memory"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._record_from_row(row) for row in rows]

    def rebuild_index(self) -> int:
        records = self._read_snapshots()
        with self._write_lock, self._connect() as connection:
            connection.execute("DELETE FROM memory_fts")
            connection.execute("DELETE FROM memory")
            for record in records.values():
                self._upsert(connection, record)
            connection.commit()
        return len(records)

    def write_projections(self) -> None:
        records = self.list_records(active_only=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.views_dir.mkdir(parents=True, exist_ok=True)
        by_namespace: dict[str, list[MemoryRecord]] = {}
        for record in records:
            by_namespace.setdefault(record.namespace, []).append(record)
        self.markdown_path.write_text(self._render_markdown(records), encoding="utf-8")
        for namespace, namespace_records in by_namespace.items():
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", namespace).strip("-") or "general"
            (self.views_dir / f"{safe_name}.md").write_text(
                self._render_markdown(namespace_records, title=f"Memory: {namespace}"),
                encoding="utf-8",
            )

    @staticmethod
    def _render_markdown(records: Sequence[MemoryRecord], *, title: str = "Companion Memory") -> str:
        lines = [f"# {title}", "", "<!-- Generated from memory.jsonl; manual edits are not authoritative. -->", ""]
        grouped: dict[str, list[MemoryRecord]] = {}
        for record in records:
            grouped.setdefault(record.kind, []).append(record)
        for kind in sorted(grouped):
            lines.extend((f"## {kind}", ""))
            for record in sorted(grouped[kind], key=lambda item: (-item.importance, item.updated_at)):
                tags = f" `{' '.join(record.tags)}`" if record.tags else ""
                lines.append(f"- {record.text}{tags}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"