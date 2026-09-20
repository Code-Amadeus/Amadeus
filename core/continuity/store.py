"""Thread-safe SQLite authority for continuity state.

SQLite memory rows are the single source of truth. FTS/vector indexes are
derived artifacts and must remain rebuildable from records stored behind this boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
import math
from collections import defaultdict, deque
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from core.continuity.memory_resolver import (
    MemoryResolutionAction,
    MemoryResolver,
    normalize_memory_text,
)
from core.continuity.migrations import MIGRATIONS, SCHEMA_VERSION
from core.continuity.models import (
    ConsolidationStatus,
    ConsolidationTurnState,
    ContinuityFactSource,
    ConversationClockState,
    MemoryCandidate,
    MemoryKind,
    MemoryMute,
    MemoryPriorityClass,
    MemoryRecord,
    MemoryState,
    RetentionTier,
    MemoryTombstone,
    RelationshipProposal,
    RelationshipEvent,
    RelationshipStateClass,
    RelationshipDimensionState,
    AffectDimensionState,
    RelationshipSnapshot,
    LifeScheduleStatus,
    LifeThreadStatus,
    LifeScheduleItemPlan,
    LifeSchedule,
    LifeScheduleItem,
    LifeThread,
    LifeEvent,
    LifeSnapshot,
    TurnEvidence,
)
from core.continuity.topic_mute import MUTE_MAX_ACTIVE, normalize_text, normalize_topic
from core.continuity.turn_ingest import evidence_source_hash, storage_session_id
from core.continuity.relationship import relationship_evidence_hash
from core.continuity.relationship_policy import RelationshipPolicy
from core.continuity.life_policy import LifePolicy


class ContinuityStoreError(RuntimeError):
    """Base error for continuity persistence failures."""


_CLOCK_COLUMNS = (
    "last_user_turn_at",
    "last_assistant_completed_at",
    "last_successful_chat_at",
    "last_app_started_at",
    "last_graceful_shutdown_at",
    "last_observed_wall_at",
    "wall_clock_high_water_at",
    "last_observed_local_date",
    "last_observed_utc_offset_minutes",
    "clock_adjusted",
    "last_session_id",
    "updated_at",
)

_PRIORITY_RANK = {
    MemoryPriorityClass.P0: 0,
    MemoryPriorityClass.P1: 1,
    MemoryPriorityClass.P2: 2,
    MemoryPriorityClass.P3: 3,
    MemoryPriorityClass.P4: 4,
}


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _stronger_priority(a: MemoryPriorityClass, b: MemoryPriorityClass) -> MemoryPriorityClass:
    return a if _PRIORITY_RANK[a] <= _PRIORITY_RANK[b] else b


class ContinuityStore:
    """Host-owned repository for Session-scoped continuity state.

    Memory, tombstones, topic mutes and relationship/affect state belong to
    their owning chat Session (``scope``); Character Life and the RealityClock
    intentionally stay character-/host-global.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        path_text = os.fspath(db_path)
        if not path_text:
            raise ValueError("db_path is required")
        if path_text != ":memory:":
            path_text = os.path.abspath(os.path.expandvars(os.path.expanduser(path_text)))
            Path(path_text).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = path_text
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            path_text,
            timeout=max(0.1, float(timeout_seconds)),
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        # Explicit forget should not leave deleted Continuity values in reusable
        # SQLite pages. Session JSON remains a separate transcript authority.
        self._connection.execute("PRAGMA secure_delete = ON")
        self._connection.execute(
            f"PRAGMA busy_timeout = {max(100, int(float(timeout_seconds) * 1000))}"
        )
        if path_text != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
        self._migrate()
        self._backfill_mute_scopes()

    def _backfill_mute_scopes(self) -> None:
        """Schema-8 ownership backfill for topic mutes stored as JSON meta.

        Mutes move from the legacy 'global' placeholder to the source Session
        recorded when the user asked for them; idempotent and cheap to run on
        every open.
        """

        with self._lock:
            self._ensure_open()
            entries = self._mute_entries_locked()
            migrated = [
                replace(mute, scope=mute.source_session_id)
                if mute.scope == "global" and mute.source_session_id
                else mute
                for mute in entries
            ]
            if migrated != entries:
                self._write_mute_entries_locked(migrated)

    def __enter__(self) -> "ContinuityStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ContinuityStoreError("continuity store is closed")

    @property
    def schema_version(self) -> int:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row else 0

    def pragma(self, name: str) -> Any:
        """Expose bounded PRAGMA reads for diagnostics/tests."""

        if name not in {"foreign_keys", "journal_mode", "synchronous", "busy_timeout", "secure_delete"}:
            raise ValueError(f"unsupported pragma: {name}")
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(f"PRAGMA {name}").fetchone()
            return row[0] if row else None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def _migrate(self) -> None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute("PRAGMA user_version").fetchone()
            current = int(row[0]) if row else 0
            if current > SCHEMA_VERSION:
                raise ContinuityStoreError(
                    f"continuity schema {current} is newer than supported version {SCHEMA_VERSION}"
                )
            for version in range(current + 1, SCHEMA_VERSION + 1):
                script = MIGRATIONS.get(version)
                if not script:
                    raise ContinuityStoreError(f"missing continuity migration {version}")
                try:
                    self._connection.executescript(
                        "BEGIN IMMEDIATE;\n" + script + "\nCOMMIT;"
                    )
                except Exception:
                    try:
                        self._connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise

    # ------------------------------------------------------------------
    # Reality clock (C1)
    # ------------------------------------------------------------------
    def get_clock_state(self) -> ConversationClockState:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM conversation_clock WHERE singleton_id = 1"
            ).fetchone()
            if row is None:
                raise ContinuityStoreError("conversation_clock singleton is missing")
            return ConversationClockState(
                last_user_turn_at=row["last_user_turn_at"],
                last_assistant_completed_at=row["last_assistant_completed_at"],
                last_successful_chat_at=row["last_successful_chat_at"],
                last_app_started_at=row["last_app_started_at"],
                last_graceful_shutdown_at=row["last_graceful_shutdown_at"],
                last_observed_wall_at=row["last_observed_wall_at"],
                wall_clock_high_water_at=row["wall_clock_high_water_at"],
                last_observed_local_date=str(row["last_observed_local_date"] or ""),
                last_observed_utc_offset_minutes=row["last_observed_utc_offset_minutes"],
                clock_adjusted=bool(row["clock_adjusted"]),
                last_session_id=str(row["last_session_id"] or ""),
                updated_at=row["updated_at"],
            )

    def update_clock_state(self, **changes: Any) -> ConversationClockState:
        """Atomically update selected fields on the clock singleton."""

        unknown = set(changes) - set(_CLOCK_COLUMNS)
        if unknown:
            raise ValueError(f"unsupported clock fields: {sorted(unknown)}")
        if not changes:
            return self.get_clock_state()

        normalized = dict(changes)
        if "clock_adjusted" in normalized:
            normalized["clock_adjusted"] = 1 if bool(normalized["clock_adjusted"]) else 0

        assignments = ", ".join(f"{column} = ?" for column in normalized)
        values = [normalized[column] for column in normalized]
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    f"UPDATE conversation_clock SET {assignments} WHERE singleton_id = 1",
                    values,
                )
                self._connection.execute("COMMIT")
            except Exception:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            return self.get_clock_state()

    # ------------------------------------------------------------------
    # Row conversion helpers (C2)
    # ------------------------------------------------------------------
    @staticmethod
    def _memory_from_row(row: sqlite3.Row | None) -> MemoryRecord | None:
        if row is None:
            return None
        return MemoryRecord(
            id=str(row["id"]),
            memory_key=str(row["memory_key"]),
            scope=str(row["scope"]),
            kind=MemoryKind(str(row["kind"])),
            subject=str(row["subject"] or ""),
            predicate=str(row["predicate"] or ""),
            object_text=str(row["object_text"] or ""),
            summary=str(row["summary"] or ""),
            importance=float(row["importance"]),
            confidence=float(row["confidence"]),
            future_value=float(row["future_value"]),
            relationship_value=float(row["relationship_value"]),
            priority_class=MemoryPriorityClass(str(row["priority_class"])),
            mention_count=int(row["mention_count"]),
            recall_count=int(row["recall_count"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            last_mentioned_at=float(row["last_mentioned_at"]),
            last_recalled_at=row["last_recalled_at"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            expires_at=row["expires_at"],
            pinned=bool(row["pinned"]),
            state=MemoryState(str(row["state"])),
            source_type=ContinuityFactSource(str(row["source_type"])),
            source_session_id=str(row["source_session_id"] or ""),
            source_turn_id=str(row["source_turn_id"] or ""),
            source_hash=str(row["source_hash"] or ""),
            work_item_id=str(row["work_item_id"] or ""),
            retention_tier=RetentionTier(str(row["retention_tier"] or "hot")),
            retention_score=float(row["retention_score"] if row["retention_score"] is not None else 0.5),
            last_retention_at=row["last_retention_at"],
        )

    @staticmethod
    def _tombstone_from_row(row: sqlite3.Row | None) -> MemoryTombstone | None:
        if row is None:
            return None
        raw_kind = str(row["kind"] or "")
        return MemoryTombstone(
            id=str(row["id"]),
            scope=str(row["scope"]),
            memory_key=str(row["memory_key"]),
            kind=MemoryKind(raw_kind) if raw_kind else None,
            created_at=float(row["created_at"]),
            source_session_id=str(row["source_session_id"] or ""),
            source_turn_id=str(row["source_turn_id"] or ""),
            reason=str(row["reason"] or ""),
            cleared_at=row["cleared_at"],
        )

    @staticmethod
    def _consolidation_from_row(row: sqlite3.Row | None) -> ConsolidationTurnState | None:
        if row is None:
            return None
        return ConsolidationTurnState(
            session_id=str(row["session_id"]),
            turn_id=str(row["turn_id"]),
            status=ConsolidationStatus(str(row["status"])),
            source=str(row["source"] or ""),
            user_created_at=str(row["user_created_at"] or ""),
            assistant_created_at=str(row["assistant_created_at"] or ""),
            source_hash=str(row["source_hash"] or ""),
            attempts=int(row["attempts"]),
            last_error=str(row["last_error"] or ""),
            observed_at=row["observed_at"],
            completed_at=row["completed_at"],
            updated_at=float(row["updated_at"]),
        )

    # ------------------------------------------------------------------
    # Memory read/diagnostic APIs (C2 authority; C3 retrieval reads through
    # this boundary without creating a second source of truth).
    # ------------------------------------------------------------------
    def get_active_memory(self, memory_key: str, *, scope: str = "global") -> MemoryRecord | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM memory_items WHERE scope = ? AND memory_key = ? AND state = 'active'",
                (str(scope), str(memory_key)),
            ).fetchone()
            return self._memory_from_row(row)

    def list_active_memories(self, *, scope: str | None = None) -> list[MemoryRecord]:
        with self._lock:
            self._ensure_open()
            if scope is None:
                rows = self._connection.execute(
                    "SELECT * FROM memory_items WHERE state = 'active' ORDER BY created_at, id"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM memory_items WHERE state = 'active' AND scope = ? ORDER BY created_at, id",
                    (str(scope),),
                ).fetchall()
            return [record for row in rows if (record := self._memory_from_row(row))]

    def get_memory_history(self, memory_key: str, *, scope: str = "global") -> list[MemoryRecord]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM memory_items WHERE scope = ? AND memory_key = ? ORDER BY created_at, id",
                (str(scope), str(memory_key)),
            ).fetchall()
            return [record for row in rows if (record := self._memory_from_row(row))]

    def get_active_tombstone(self, memory_key: str, *, scope: str = "global") -> MemoryTombstone | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM memory_tombstones WHERE scope = ? AND memory_key = ? AND cleared_at IS NULL",
                (str(scope), str(memory_key)),
            ).fetchone()
            return self._tombstone_from_row(row)

    def list_tombstones(self, *, active_only: bool = True) -> list[MemoryTombstone]:
        with self._lock:
            self._ensure_open()
            where = "WHERE cleared_at IS NULL" if active_only else ""
            rows = self._connection.execute(
                f"SELECT * FROM memory_tombstones {where} ORDER BY created_at, id"
            ).fetchall()
            return [item for row in rows if (item := self._tombstone_from_row(row))]

    def archive_forget_guard_ready(self) -> bool:
        """Whether every historical tombstone has C8 source-closure metadata.

        Pre-C8 tombstones cannot be mapped back to the source Session turn
        without retaining forgotten content. Historical Session fallback must
        therefore fail closed while any unmapped legacy tombstone exists.
        """

        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT 1 FROM memory_tombstones "
                "WHERE archive_guard_complete = 0 LIMIT 1"
            ).fetchone()
            return row is None

    def is_archive_source_blocked(self, session_id: str, turn_id: str) -> bool:
        sid = str(session_id or "").strip()
        tid = str(turn_id or "").strip()
        if not sid or not tid:
            return True
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT 1
                  FROM memory_forget_sources
                 WHERE source_session_id = ?
                   AND source_turn_id = ?
                 LIMIT 1
                """,
                (sid, tid),
            ).fetchone()
            return row is not None

    def list_archive_source_anchors(
        self,
        *,
        scope: str = "global",
        now: float | None = None,
        exclude_turn_id: str = "",
        limit: int = 5000,
    ) -> list[MemoryRecord]:
        """Return active source-bearing rows across every retention tier."""

        bounded = max(1, min(10000, int(limit)))
        clauses = [
            "state = 'active'",
            "scope = ?",
            "source_session_id <> ''",
            "source_turn_id <> ''",
        ]
        params: list[Any] = [str(scope)]
        if now is not None:
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(float(now))
        if exclude_turn_id:
            clauses.append("source_turn_id <> ?")
            params.append(str(exclude_turn_id))
        params.append(bounded)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM memory_items WHERE " + " AND ".join(clauses)
                + " ORDER BY pinned DESC, importance DESC, created_at DESC, id LIMIT ?",
                params,
            ).fetchall()
            return [record for row in rows if (record := self._memory_from_row(row))]

    # ------------------------------------------------------------------
    # C3 derived retrieval/index APIs. memory_items remains authoritative.
    # ------------------------------------------------------------------
    def get_memories_by_ids(self, memory_ids: Iterable[str]) -> list[MemoryRecord]:
        ids = tuple(dict.fromkeys(str(value) for value in memory_ids if str(value)))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                f"SELECT * FROM memory_items WHERE id IN ({placeholders}) AND state = 'active'",
                ids,
            ).fetchall()
            by_id = {
                str(row["id"]): record
                for row in rows
                if (record := self._memory_from_row(row)) is not None
            }
            return [by_id[value] for value in ids if value in by_id]

    def set_memory_pinned(
        self,
        memory_id: str,
        pinned: bool,
        *,
        observed_at: float | None = None,
    ) -> MemoryRecord | None:
        """Set the explicit C7 pin control on one active memory.

        Pinning is a user control over retention only. It never changes the
        memory's fact authority or logical state. A newly pinned record is
        promoted to the hot tier so the existing C4 maintenance invariant can
        protect it; unpinning leaves tier selection to normal maintenance.
        """

        value = str(memory_id or "").strip()
        if not value:
            raise ValueError("memory_id is required")
        when = float(time.time() if observed_at is None else observed_at)
        with self._lock:
            self._ensure_open()
            self._connection.execute(
                """
                UPDATE memory_items
                   SET pinned = ?,
                       retention_tier = CASE WHEN ? THEN 'hot' ELSE retention_tier END,
                       updated_at = MAX(updated_at, ?)
                 WHERE id = ? AND state = 'active'
                """,
                (1 if pinned else 0, 1 if pinned else 0, when, value),
            )
            row = self._connection.execute(
                "SELECT * FROM memory_items WHERE id = ? AND state = 'active'",
                (value,),
            ).fetchone()
            return self._memory_from_row(row)

    def list_retrievable_memories(
        self,
        *,
        scope: str = "global",
        now: float | None = None,
        exclude_turn_id: str = "",
        kinds: Iterable[MemoryKind] | None = None,
        limit: int = 200,
        include_cold: bool = False,
    ) -> list[MemoryRecord]:
        bounded_limit = max(1, min(1000, int(limit)))
        clauses = ["state = 'active'", "scope = ?"]
        if include_cold:
            clauses.append("retention_tier IN ('hot', 'cold')")
        else:
            clauses.append("retention_tier = 'hot'")
        params: list[Any] = [str(scope)]
        if now is not None:
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(float(now))
        if exclude_turn_id:
            clauses.append("source_turn_id <> ?")
            params.append(str(exclude_turn_id))
        kind_values = tuple(str(item.value if isinstance(item, MemoryKind) else item) for item in (kinds or ()))
        if kind_values:
            clauses.append("kind IN (" + ",".join("?" for _ in kind_values) + ")")
            params.extend(kind_values)
        params.append(bounded_limit)
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM memory_items WHERE " + " AND ".join(clauses)
                + " ORDER BY pinned DESC, importance DESC, last_mentioned_at DESC, id LIMIT ?",
                params,
            ).fetchall()
            return [record for row in rows if (record := self._memory_from_row(row))]

    def search_memory_fts(
        self,
        match_query: str,
        *,
        scope: str = "global",
        now: float | None = None,
        exclude_turn_id: str = "",
        limit: int = 24,
        include_cold: bool = False,
    ) -> list[tuple[MemoryRecord, float]]:
        query = str(match_query or "").strip()
        if not query:
            return []
        bounded_limit = max(1, min(200, int(limit)))
        clauses = ["memory_fts MATCH ?", "m.state = 'active'", "m.scope = ?"]
        if include_cold:
            clauses.append("m.retention_tier IN ('hot', 'cold')")
        else:
            clauses.append("m.retention_tier = 'hot'")
        params: list[Any] = [query, str(scope)]
        if now is not None:
            clauses.append("(m.expires_at IS NULL OR m.expires_at > ?)")
            params.append(float(now))
        if exclude_turn_id:
            clauses.append("m.source_turn_id <> ?")
            params.append(str(exclude_turn_id))
        params.append(bounded_limit)
        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(
                    "SELECT m.*, bm25(memory_fts) AS fts_rank "
                    "FROM memory_fts JOIN memory_items AS m ON m.id = memory_fts.memory_id "
                    "WHERE " + " AND ".join(clauses)
                    + " ORDER BY fts_rank, m.importance DESC, m.last_mentioned_at DESC LIMIT ?",
                    params,
                ).fetchall()
            except sqlite3.OperationalError as exc:
                # User text can contain FTS operators. Retrieval must fail open
                # to structured recall rather than breaking the chat turn.
                if "fts5" in str(exc).lower() or "syntax" in str(exc).lower():
                    return []
                raise
            result: list[tuple[MemoryRecord, float]] = []
            for row in rows:
                record = self._memory_from_row(row)
                if record is None:
                    continue
                rank = float(row["fts_rank"] or 0.0)
                result.append((record, rank))
            return result

    def mark_memories_recalled(self, memory_ids: Iterable[str], *, recalled_at: float | None = None) -> None:
        ids = tuple(dict.fromkeys(str(value) for value in memory_ids if str(value)))
        if not ids:
            return
        when = float(time.time() if recalled_at is None else recalled_at)
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            self._ensure_open()
            self._connection.execute(
                f"UPDATE memory_items SET recall_count = recall_count + 1, "
                f"last_recalled_at = ?, updated_at = MAX(updated_at, ?) "
                f"WHERE id IN ({placeholders}) AND state = 'active'",
                (when, when, *ids),
            )

    @staticmethod
    def memory_embedding_content_hash(record: MemoryRecord) -> str:
        content = "\x1f".join(
            (
                record.memory_key,
                record.kind.value,
                record.subject,
                record.predicate,
                record.object_text,
                record.summary,
            )
        )
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def get_memory_embedding(self, memory_id: str, *, model_id: str) -> tuple[int, bytes, str] | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT dimension, vector_blob, content_hash FROM memory_embeddings "
                "WHERE memory_id = ? AND model_id = ?",
                (str(memory_id), str(model_id)),
            ).fetchone()
            if row is None:
                return None
            return int(row["dimension"]), bytes(row["vector_blob"]), str(row["content_hash"])

    def upsert_memory_embedding(
        self,
        memory_id: str,
        *,
        model_id: str,
        dimension: int,
        vector_blob: bytes,
        content_hash: str,
        updated_at: float | None = None,
    ) -> None:
        when = float(time.time() if updated_at is None else updated_at)
        with self._lock:
            self._ensure_open()
            self._connection.execute(
                """
                INSERT INTO memory_embeddings(
                    memory_id, model_id, dimension, vector_blob, content_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id, model_id) DO UPDATE SET
                    dimension = excluded.dimension,
                    vector_blob = excluded.vector_blob,
                    content_hash = excluded.content_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    str(memory_id), str(model_id), int(dimension), sqlite3.Binary(vector_blob),
                    str(content_hash), when,
                ),
            )

    def delete_memory_embeddings_for_model(self, model_id: str) -> int:
        with self._lock:
            self._ensure_open()
            cursor = self._connection.execute(
                "DELETE FROM memory_embeddings WHERE model_id = ?",
                (str(model_id),),
            )
            return int(cursor.rowcount or 0)

    def clear_memory_embeddings(self) -> int:
        """Drop all rebuildable semantic cache rows for C7 index repair."""

        with self._lock:
            self._ensure_open()
            cursor = self._connection.execute("DELETE FROM memory_embeddings")
            return int(cursor.rowcount or 0)

    def rebuild_fts(self) -> int:
        """Rebuild the derived lexical index entirely from active authority rows."""
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute("DELETE FROM memory_fts")
                self._connection.execute(
                    """
                    INSERT INTO memory_fts(
                        memory_id, scope, memory_key, kind, subject, predicate, object_text, summary
                    )
                    SELECT id, scope, memory_key, kind, subject, predicate, object_text, summary
                    FROM memory_items WHERE state = 'active' AND retention_tier IN ('hot', 'cold')
                    """
                )
                count = int(self._connection.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0])
                self._connection.execute("COMMIT")
                return count
            except Exception:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    # ------------------------------------------------------------------
    # User-governed topic mutes.  A mute keeps durable memory intact and only
    # suppresses proactive surfacing: it is Host-owned presentation state, not
    # a fact and not explicit forget.  Topics are bounded and user-authored.
    # ------------------------------------------------------------------
    _MUTES_META_KEY = "memory_mutes"

    @staticmethod
    def _mute_from_payload(payload: Any) -> MemoryMute | None:
        if not isinstance(payload, dict):
            return None
        mute_id = str(payload.get("id") or "").strip()
        topic = str(payload.get("topic") or "").strip()
        if not mute_id or not topic:
            return None
        try:
            created_at = float(payload.get("created_at"))
        except (TypeError, ValueError):
            return None
        raw_cleared = payload.get("cleared_at")
        try:
            cleared_at = None if raw_cleared is None else float(raw_cleared)
        except (TypeError, ValueError):
            cleared_at = None
        return MemoryMute(
            id=mute_id,
            scope=str(payload.get("scope") or "global"),
            topic=topic,
            created_at=created_at,
            cleared_at=cleared_at,
            source_session_id=str(payload.get("source_session_id") or ""),
            source_turn_id=str(payload.get("source_turn_id") or ""),
            reason=str(payload.get("reason") or "user_mute"),
        )

    def _mute_entries_locked(self) -> list[MemoryMute]:
        row = self._connection.execute(
            "SELECT value_json FROM continuity_meta WHERE key = ?",
            (self._MUTES_META_KEY,),
        ).fetchone()
        if row is None:
            return []
        try:
            payload = json.loads(str(row["value_json"]))
        except (TypeError, ValueError):
            return []
        if not isinstance(payload, list):
            return []
        return [mute for item in payload if (mute := self._mute_from_payload(item)) is not None]

    def _write_mute_entries_locked(self, mutes: list[MemoryMute]) -> None:
        now = time.time()
        payload = [
            {
                "id": mute.id,
                "scope": mute.scope,
                "topic": mute.topic,
                "created_at": mute.created_at,
                "cleared_at": mute.cleared_at,
                "source_session_id": mute.source_session_id,
                "source_turn_id": mute.source_turn_id,
                "reason": mute.reason,
            }
            for mute in mutes
        ]
        self._connection.execute(
            """
            INSERT INTO continuity_meta(key, value_json, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (self._MUTES_META_KEY, json.dumps(payload, ensure_ascii=False), now, now),
        )

    def add_topic_mute(
        self,
        topic: str,
        *,
        scope: str = "global",
        session_id: str = "",
        turn_id: str = "",
        reason: str = "user_mute",
        observed_at: float | None = None,
    ) -> MemoryMute:
        """Record one user-requested topic suppression, idempotently."""

        normalized = normalize_topic(topic)
        if not normalized:
            raise ValueError("mute topic is required")
        when = float(observed_at if observed_at is not None else time.time())
        target_scope = str(scope or "").strip() or "global"
        with self._lock:
            self._ensure_open()
            active = [mute for mute in self._mute_entries_locked() if mute.cleared_at is None]
            scope_entries = [mute for mute in active if mute.scope == target_scope]
            for mute in scope_entries:
                if normalize_text(mute.topic) == normalize_text(normalized):
                    return mute
            if len(scope_entries) >= MUTE_MAX_ACTIVE:
                raise ContinuityStoreError("too many active topic mutes for this session")
            mute = MemoryMute(
                id=uuid.uuid4().hex,
                scope=target_scope,
                topic=normalized,
                created_at=when,
                source_session_id=str(session_id or ""),
                source_turn_id=str(turn_id or ""),
                reason=str(reason or "user_mute"),
            )
            active.append(mute)
            self._write_mute_entries_locked(active)
            return mute

    def list_topic_mutes(
        self,
        *,
        active_only: bool = True,
        scope: str | None = None,
    ) -> list[MemoryMute]:
        with self._lock:
            self._ensure_open()
            mutes = self._mute_entries_locked()
        if active_only:
            mutes = [mute for mute in mutes if mute.cleared_at is None]
        if scope is not None:
            wanted = str(scope)
            mutes = [mute for mute in mutes if mute.scope == wanted]
        return sorted(mutes, key=lambda mute: (mute.created_at, mute.id))

    def clear_topic_mute(
        self,
        mute_id: str,
        *,
        observed_at: float | None = None,
    ) -> MemoryMute | None:
        target = str(mute_id or "").strip()
        if not target:
            return None
        when = float(observed_at if observed_at is not None else time.time())
        with self._lock:
            self._ensure_open()
            entries = self._mute_entries_locked()
            cleared: MemoryMute | None = None
            updated: list[MemoryMute] = []
            for mute in entries:
                if mute.id == target and mute.cleared_at is None:
                    cleared = MemoryMute(
                        id=mute.id,
                        scope=mute.scope,
                        topic=mute.topic,
                        created_at=mute.created_at,
                        cleared_at=when,
                        source_session_id=mute.source_session_id,
                        source_turn_id=mute.source_turn_id,
                        reason=mute.reason,
                    )
                    updated.append(cleared)
                    continue
                updated.append(mute)
            if cleared is not None:
                self._write_mute_entries_locked(updated)
            return cleared

    # ------------------------------------------------------------------
    # C4 retention / maintenance and Work lifecycle linkage
    # ------------------------------------------------------------------
    @staticmethod
    def _retention_score(record: MemoryRecord, *, now: float, half_life_days: float) -> float:
        age_days = max(0.0, (now - record.last_mentioned_at) / 86400.0)
        recency = math.exp(-math.log(2.0) * age_days / max(1.0, half_life_days))
        # Counts are intentionally capped/log-scaled: retrieval alone cannot
        # create an unbounded self-reinforcement loop.
        mention = min(1.0, math.log1p(max(0, record.mention_count)) / math.log(11.0))
        recall = min(1.0, math.log1p(max(0, record.recall_count)) / math.log(11.0))
        priority = {"P0": 1.0, "P1": 0.82, "P2": 0.58, "P3": 0.34, "P4": 0.16}.get(record.priority_class.value, 0.5)
        score = (
            0.28 * record.importance + 0.16 * record.confidence
            + 0.16 * record.future_value + 0.10 * record.relationship_value
            + 0.12 * priority + 0.10 * recency + 0.06 * mention + 0.02 * recall
        )
        return _bounded(score)

    def run_retention_maintenance(
        self,
        *,
        now: float | None = None,
        half_life_days: float = 30.0,
        cold_after_days: float = 45.0,
        archive_after_days: float = 180.0,
        max_hot_memories: int = 5000,
        hot_score_threshold: float = 0.62,
        cold_score_threshold: float = 0.55,
        archive_score_threshold: float = 0.35,
        reason: str = "scheduled",
    ) -> dict[str, int | float | str]:
        """Idempotently score active memories and move only retrieval tiers.

        Pinned/P0 facts remain hot. Explicit tombstones and superseded facts are
        never promoted or resurrected by maintenance. The unprotected hot
        working set is deterministically capped so retrieval latency remains
        bounded without deleting durable memory. Score thresholds live in the
        reviewable retrieval policy so visibility demotion can be tuned without
        touching the store.
        """
        when = float(time.time() if now is None else now)
        hot_limit = max(1, int(max_hot_memories))
        hot_threshold = _bounded(float(hot_score_threshold))
        cold_threshold = _bounded(float(cold_score_threshold))
        archive_threshold = _bounded(float(archive_score_threshold))
        started = time.perf_counter()
        started_wall = time.time()
        run_id = uuid.uuid4().hex
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM memory_items WHERE state = 'active'"
            ).fetchall()
            plans: list[dict[str, Any]] = []
            scanned = promoted = demoted = archived = 0
            for row in rows:
                record = self._memory_from_row(row)
                if record is None:
                    continue
                scanned += 1
                age_days = max(0.0, (when - record.last_mentioned_at) / 86400.0)
                score = self._retention_score(record, now=when, half_life_days=half_life_days)
                protected = bool(record.pinned or record.priority_class.value == "P0")
                if protected:
                    new_tier = RetentionTier.HOT
                elif age_days >= archive_after_days and score < archive_threshold:
                    new_tier = RetentionTier.ARCHIVE
                elif age_days >= cold_after_days and score < cold_threshold:
                    new_tier = RetentionTier.COLD
                elif score >= hot_threshold:
                    new_tier = RetentionTier.HOT
                else:
                    new_tier = record.retention_tier
                plans.append({
                    "record": record,
                    "score": score,
                    "tier": new_tier,
                    "protected": protected,
                })

            protected_hot = [p for p in plans if p["tier"] is RetentionTier.HOT and p["protected"]]
            normal_hot = [p for p in plans if p["tier"] is RetentionTier.HOT and not p["protected"]]
            normal_hot.sort(
                key=lambda p: (
                    -float(p["score"]),
                    -float(p["record"].last_mentioned_at),
                    str(p["record"].id),
                )
            )
            available_slots = max(0, hot_limit - len(protected_hot))
            for overflow in normal_hot[available_slots:]:
                overflow["tier"] = RetentionTier.COLD

            try:
                self._connection.execute("BEGIN IMMEDIATE")
                for plan in plans:
                    record = plan["record"]
                    score = float(plan["score"])
                    tier = record.retention_tier
                    new_tier = plan["tier"]
                    if tier is not new_tier:
                        if new_tier is RetentionTier.HOT:
                            promoted += 1
                        elif new_tier is RetentionTier.ARCHIVE:
                            archived += 1
                        else:
                            demoted += 1
                    self._connection.execute(
                        "UPDATE memory_items SET retention_tier = ?, retention_score = ?, last_retention_at = ?, updated_at = MAX(updated_at, ?) WHERE id = ? AND state = 'active'",
                        (new_tier.value, score, when, when, record.id),
                    )
                completed = time.time()
                self._connection.execute(
                    "INSERT INTO continuity_maintenance_runs(run_id, started_at, completed_at, now_at, scanned, promoted, demoted, archived, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, started_wall, completed, when, scanned, promoted, demoted, archived, str(reason or "scheduled")),
                )
                self._connection.execute("COMMIT")
            except Exception:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return {
            "run_id": run_id, "scanned": scanned, "promoted": promoted,
            "demoted": demoted, "archived": archived,
            "hot_limit": hot_limit,
            "protected_hot": len(protected_hot),
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }

    def apply_work_update(self, payload: dict[str, Any], *, observed_at: float | None = None) -> dict[str, int | str | bool]:
        """Apply one ordered, idempotent WORK_UPDATED projection to linked open loops."""
        data = payload.get("work") if isinstance(payload.get("work"), dict) else payload
        work_id = str(data.get("work_item_id") or data.get("workItemId") or data.get("id") or "").strip()
        status = str(data.get("status") or data.get("state") or data.get("completion") or "").lower().strip()
        event_id = str(payload.get("event_id") or payload.get("eventId") or "").strip()
        if not work_id or not status:
            return {"applied": False, "updated": 0, "work_item_id": work_id}
        if not event_id:
            material = f"{work_id}\0{status}\0{observed_at!r}".encode("utf-8")
            event_id = hashlib.sha256(material).hexdigest()
        when = float(time.time() if observed_at is None else observed_at)
        terminal = status in {
            "completed", "complete", "accepted", "done", "closed",
            "archived", "review_ready", "succeeded",
            "cancelled", "canceled", "failed", "orphaned",
        }
        payload_hash = hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                latest = self._connection.execute(
                    "SELECT observed_at FROM continuity_work_events WHERE work_item_id = ? ORDER BY observed_at DESC LIMIT 1",
                    (work_id,),
                ).fetchone()
                if latest is not None and when < float(latest["observed_at"]):
                    self._connection.execute("ROLLBACK")
                    return {"applied": False, "updated": 0, "work_item_id": work_id, "stale": True}
                cur = self._connection.execute(
                    "INSERT OR IGNORE INTO continuity_work_events(event_id, work_item_id, status, observed_at, payload_hash) VALUES (?, ?, ?, ?, ?)",
                    (event_id, work_id, status, when, payload_hash),
                )
                if not cur.rowcount:
                    self._connection.execute("ROLLBACK")
                    return {"applied": False, "updated": 0, "work_item_id": work_id}
                if terminal:
                    updated = self._connection.execute(
                        "UPDATE memory_items SET future_value = MIN(future_value, 0.2), retention_tier = CASE WHEN pinned = 1 OR priority_class = 'P0' THEN 'hot' ELSE 'cold' END, retention_score = MIN(retention_score, 0.45), updated_at = MAX(updated_at, ?) WHERE work_item_id = ? AND state = 'active' AND kind = 'open_loop'",
                        (when, work_id),
                    ).rowcount
                else:
                    updated = self._connection.execute(
                        "UPDATE memory_items SET future_value = MAX(future_value, 0.35), retention_tier = 'hot', updated_at = MAX(updated_at, ?) WHERE work_item_id = ? AND state = 'active' AND kind = 'open_loop'",
                        (when, work_id),
                    ).rowcount
                self._connection.execute("COMMIT")
                return {"applied": True, "updated": int(updated or 0), "work_item_id": work_id}
            except Exception:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def continuity_diagnostics(self) -> dict[str, Any]:
        """Return bounded content-free counts for Host diagnostics."""
        with self._lock:
            self._ensure_open()
            counts = {str(row["retention_tier"]): int(row["n"]) for row in self._connection.execute("SELECT retention_tier, COUNT(*) n FROM memory_items WHERE state = 'active' GROUP BY retention_tier")}
            last = self._connection.execute("SELECT * FROM continuity_maintenance_runs ORDER BY completed_at DESC LIMIT 1").fetchone()
            return {"hot_memory_count": counts.get("hot", 0), "cold_memory_count": counts.get("cold", 0), "archive_memory_count": counts.get("archive", 0), "last_maintenance_at": float(last["completed_at"]) if last else None, "last_maintenance_duration_ms": None if not last else round((float(last["completed_at"]) - float(last["started_at"])) * 1000.0, 3)}

    def list_memories_by_tier(self, tier: RetentionTier, *, scope: str | None = None) -> list[MemoryRecord]:
        """Return active records in one retention tier for diagnostics/archive jobs."""
        value = tier.value if isinstance(tier, RetentionTier) else str(tier)
        clauses = ["state = 'active'", "retention_tier = ?"]
        params: list[Any] = [value]
        if scope is not None:
            clauses.append("scope = ?")
            params.append(str(scope))
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM memory_items WHERE " + " AND ".join(clauses) + " ORDER BY retention_score DESC, last_mentioned_at DESC",
                params,
            ).fetchall()
            return [record for row in rows if (record := self._memory_from_row(row))]

    get_retention_diagnostics = continuity_diagnostics

    # ------------------------------------------------------------------
    def list_scopes(self) -> list[str]:
        """Return every scope that currently owns durable Continuity state."""

        with self._lock:
            self._ensure_open()
            scopes: set[str] = set()
            for table in ("memory_items", "memory_tombstones", "relationship_events"):
                rows = self._connection.execute(
                    f"SELECT DISTINCT scope FROM {table}"
                ).fetchall()
                scopes.update(str(row["scope"] or "") for row in rows)
            scopes.update(mute.scope for mute in self._mute_entries_locked())
        return sorted(scope for scope in scopes if scope)

    def purge_scope(self, scope: str) -> dict[str, int]:
        """Remove every durable row owned by one Session scope (recovery purge).

        Used by the startup reaper when a scope has neither a live transcript
        nor a backup transcript.  Derived rows (FTS/embeddings/mentions/links,
        relationship snapshots) are removed together with their sources; the
        returned counts are for logs and tests.
        """

        target = str(scope or "").strip()
        if not target:
            raise ValueError("scope is required")
        counts = {
            "memories": 0,
            "tombstones": 0,
            "relationship_events": 0,
            "journal_turns": 0,
            "mutes": 0,
        }
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                memory_rows = self._connection.execute(
                    "SELECT id FROM memory_items WHERE scope = ?", (target,)
                ).fetchall()
                memory_ids = [str(row["id"]) for row in memory_rows]
                if memory_ids:
                    placeholders = ",".join("?" for _ in memory_ids)
                    for table in ("memory_embeddings", "memory_mentions", "memory_links"):
                        column = "memory_id"
                        if table == "memory_links":
                            self._connection.execute(
                                f"DELETE FROM memory_links WHERE from_memory_id IN ({placeholders}) "
                                f"OR to_memory_id IN ({placeholders})",
                                (*memory_ids, *memory_ids),
                            )
                            continue
                        self._connection.execute(
                            f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
                            memory_ids,
                        )
                    self._connection.execute(
                        f"DELETE FROM memory_items WHERE id IN ({placeholders})", memory_ids
                    )
                counts["memories"] = len(memory_ids)

                tombstone_rows = self._connection.execute(
                    "SELECT id FROM memory_tombstones WHERE scope = ?", (target,)
                ).fetchall()
                tombstone_ids = [str(row["id"]) for row in tombstone_rows]
                if tombstone_ids:
                    placeholders = ",".join("?" for _ in tombstone_ids)
                    self._connection.execute(
                        f"DELETE FROM memory_forget_sources WHERE tombstone_id IN ({placeholders})",
                        tombstone_ids,
                    )
                self._connection.execute(
                    "DELETE FROM memory_forget_sources WHERE source_session_id = ?", (target,)
                )
                self._connection.execute(
                    "DELETE FROM memory_tombstones WHERE scope = ?", (target,)
                )
                counts["tombstones"] = len(tombstone_ids)

                cursor = self._connection.execute(
                    "DELETE FROM relationship_events WHERE scope = ?", (target,)
                )
                counts["relationship_events"] = int(cursor.rowcount or 0)
                self._connection.execute(
                    "DELETE FROM relationship_state WHERE scope = ?", (target,)
                )
                self._connection.execute(
                    "DELETE FROM short_term_affect WHERE scope = ?", (target,)
                )

                cursor = self._connection.execute(
                    "DELETE FROM consolidation_turns WHERE session_id = ?", (target,)
                )
                counts["journal_turns"] = int(cursor.rowcount or 0)

                entries = self._mute_entries_locked()
                kept = [mute for mute in entries if mute.scope != target]
                counts["mutes"] = len(entries) - len(kept)
                if counts["mutes"]:
                    self._write_mute_entries_locked(kept)
                self._connection.execute("COMMIT")
            except Exception:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return counts

    # C5 relationship derived state
    # ------------------------------------------------------------------
    @staticmethod
    def _relationship_event_from_row(row: sqlite3.Row | None) -> RelationshipEvent | None:
        if row is None:
            return None
        return RelationshipEvent(
            event_id=str(row["event_id"]),
            source_session_id=str(row["source_session_id"] or ""),
            source_turn_id=str(row["source_turn_id"] or ""),
            source_memory_id=str(row["source_memory_id"] or ""),
            source_memory_key=str(row["source_memory_key"] or ""),
            source_hash=str(row["source_hash"] or ""),
            source_fingerprint=str(row["source_fingerprint"] or ""),
            event_type=str(row["event_type"] or ""),
            state_class=RelationshipStateClass(str(row["state_class"])),
            dimension=str(row["dimension"]),
            proposed_delta=float(row["proposed_delta"]),
            bounded_delta=float(row["bounded_delta"]),
            confidence=float(row["confidence"]),
            occurred_at=float(row["occurred_at"]),
            created_at=float(row["created_at"]),
            invalidated_at=(None if row["invalidated_at"] is None else float(row["invalidated_at"])),
            invalidation_reason=str(row["invalidation_reason"] or ""),
            policy_version=str(row["policy_version"] or ""),
        )

    @staticmethod
    def _relationship_fingerprint(
        *,
        source_session_id: str,
        source_turn_id: str,
        source_hash: str,
        event_type: str,
        state_class: str,
        dimension: str,
    ) -> str:
        material = "\x1f".join((
            source_session_id,
            source_turn_id,
            source_hash,
            event_type,
            state_class,
            dimension,
        ))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _relationship_scopes_locked(self) -> list[str]:
        rows = self._connection.execute(
            "SELECT DISTINCT scope FROM relationship_events"
        ).fetchall()
        return sorted(str(row["scope"]) for row in rows)

    def _rebuild_relationship_locked(self, *, as_of: float, policy: RelationshipPolicy) -> None:
        """Recompute every scope that has relationship events (broad invalidation)."""

        for scope in self._relationship_scopes_locked():
            self._rebuild_relationship_scope_locked(scope=scope, as_of=as_of, policy=policy)

    def _rebuild_relationship_scope_locked(
        self, *, scope: str, as_of: float, policy: RelationshipPolicy
    ) -> None:
        """Deterministically recompute one Session's bounded deltas and snapshots."""

        policy.validate()
        target_scope = str(scope)
        rows = self._connection.execute(
            """
            SELECT * FROM relationship_events
             WHERE invalidated_at IS NULL AND scope = ?
             ORDER BY occurred_at ASC, event_id ASC
            """,
            (target_scope,),
        ).fetchall()
        window_seconds = float(policy.window_hours) * 3600.0
        windows: dict[tuple[str, str, int], deque[tuple[float, float]]] = defaultdict(deque)
        relationship_sums = {name: 0.0 for name in policy.relationship_dimensions}
        relationship_counts = {name: 0 for name in policy.relationship_dimensions}
        affect_events: dict[str, list[tuple[float, float]]] = {
            name: [] for name in policy.affect_dimensions
        }

        for row in rows:
            state_class = str(row["state_class"])
            dimension = str(row["dimension"])
            proposed = float(row["proposed_delta"])
            occurred = float(row["occurred_at"])
            sign = 1 if proposed >= 0.0 else -1
            event_cap = policy.event_cap(dimension)
            proposed_capped = sign * min(abs(proposed), event_cap)
            key = (state_class, dimension, sign)
            window = windows[key]
            cutoff = occurred - window_seconds
            while window and window[0][0] < cutoff:
                window.popleft()
            used = sum(amount for _when, amount in window)
            room = max(0.0, policy.window_cap(dimension) - used)
            bounded = sign * min(abs(proposed_capped), room)
            if abs(bounded) < 1e-12:
                bounded = 0.0
            if float(row["bounded_delta"]) != bounded or str(row["policy_version"]) != policy.policy_version:
                self._connection.execute(
                    "UPDATE relationship_events SET bounded_delta = ?, policy_version = ? WHERE event_id = ?",
                    (bounded, policy.policy_version, str(row["event_id"])),
                )
            if bounded != 0.0:
                window.append((occurred, abs(bounded)))
                if state_class == RelationshipStateClass.RELATIONSHIP.value:
                    relationship_sums[dimension] = relationship_sums.get(dimension, 0.0) + bounded
                    relationship_counts[dimension] = relationship_counts.get(dimension, 0) + 1
                else:
                    affect_events.setdefault(dimension, []).append((occurred, bounded))

        for dimension in policy.relationship_dimensions:
            value = _bounded(policy.long_term_baseline + relationship_sums.get(dimension, 0.0))
            self._connection.execute(
                """
                INSERT INTO relationship_state(scope, dimension, value, event_count, updated_at, policy_version)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, dimension) DO UPDATE SET
                    value = excluded.value,
                    event_count = excluded.event_count,
                    updated_at = excluded.updated_at,
                    policy_version = excluded.policy_version
                """,
                (
                    target_scope,
                    dimension,
                    value,
                    relationship_counts.get(dimension, 0),
                    as_of,
                    policy.policy_version,
                ),
            )

        for dimension in policy.affect_dimensions:
            half_life_seconds = policy.affect_half_life(dimension) * 3600.0
            value = float(policy.affect_baseline)
            count = 0
            for occurred, bounded in affect_events.get(dimension, ()):
                age = max(0.0, as_of - occurred)
                decay = math.exp(-math.log(2.0) * age / half_life_seconds)
                contribution = bounded * decay
                if abs(contribution) >= 1e-12:
                    value += contribution
                    count += 1
            value = _bounded(value)
            self._connection.execute(
                """
                INSERT INTO short_term_affect(scope, dimension, value, event_count, as_of, policy_version)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, dimension) DO UPDATE SET
                    value = excluded.value,
                    event_count = excluded.event_count,
                    as_of = excluded.as_of,
                    policy_version = excluded.policy_version
                """,
                (target_scope, dimension, value, count, as_of, policy.policy_version),
            )

    def apply_relationship_proposals(
        self,
        evidence: TurnEvidence,
        proposals: Iterable[RelationshipProposal],
        *,
        policy: RelationshipPolicy | None = None,
        as_of: float | None = None,
    ) -> tuple[RelationshipEvent, ...]:
        """Validate/commit proposals and deterministically rebuild C5 state."""

        policy = policy or RelationshipPolicy()
        policy.validate()
        source_session_id = storage_session_id(evidence.session_id)
        scope = source_session_id
        source_turn_id = str(evidence.turn_id or "").strip()
        if not source_turn_id:
            raise ValueError("relationship proposal requires turn_id")
        source_hash = relationship_evidence_hash(evidence)
        now = float(time.time() if as_of is None else as_of)
        created_at = time.time()
        accepted: list[str] = []
        relationship_dims = set(policy.relationship_dimensions)
        affect_dims = set(policy.affect_dimensions)

        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                for proposal in proposals:
                    confidence = _bounded(proposal.confidence)
                    if confidence < policy.minimum_confidence:
                        continue
                    state_class = (
                        proposal.state_class.value
                        if isinstance(proposal.state_class, RelationshipStateClass)
                        else str(proposal.state_class)
                    )
                    dimension = str(proposal.dimension or "").strip()
                    if state_class == RelationshipStateClass.RELATIONSHIP.value:
                        if dimension not in relationship_dims:
                            raise ValueError(f"invalid relationship dimension: {dimension}")
                    elif state_class == RelationshipStateClass.AFFECT.value:
                        if dimension not in affect_dims:
                            raise ValueError(f"invalid affect dimension: {dimension}")
                    else:
                        raise ValueError(f"invalid relationship state class: {state_class}")
                    event_type = str(proposal.event_type or "").strip()
                    if not event_type:
                        raise ValueError("relationship event_type is required")
                    proposed_delta = float(proposal.delta)
                    if not math.isfinite(proposed_delta) or proposed_delta == 0.0:
                        continue
                    occurred_at = float(
                        proposal.occurred_at
                        if proposal.occurred_at is not None
                        else (evidence.observed_at if evidence.observed_at is not None else now)
                    )
                    source_memory_key = str(proposal.source_memory_key or "").strip()
                    source_memory_id = str(proposal.source_memory_id or "").strip()
                    if not source_memory_id and source_memory_key:
                        linked = self._connection.execute(
                            "SELECT id FROM memory_items WHERE scope = ? AND memory_key = ? AND state = 'active'",
                            (scope, source_memory_key),
                        ).fetchone()
                        if linked is not None:
                            source_memory_id = str(linked["id"])
                    fingerprint = self._relationship_fingerprint(
                        source_session_id=source_session_id,
                        source_turn_id=source_turn_id,
                        source_hash=source_hash,
                        event_type=event_type,
                        state_class=state_class,
                        dimension=dimension,
                    )
                    event_id = hashlib.sha256(("c5.relationship\x1f" + fingerprint).encode("utf-8")).hexdigest()
                    cursor = self._connection.execute(
                        """
                        INSERT OR IGNORE INTO relationship_events(
                            event_id, scope, source_session_id, source_turn_id,
                            source_memory_id, source_memory_key, source_hash,
                            source_fingerprint, event_type, state_class, dimension,
                            proposed_delta, bounded_delta, confidence, occurred_at,
                            created_at, invalidated_at, invalidation_reason, policy_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?, ?, NULL, '', ?)
                        """,
                        (
                            event_id,
                            scope,
                            source_session_id,
                            source_turn_id,
                            source_memory_id or None,
                            source_memory_key,
                            source_hash,
                            fingerprint,
                            event_type,
                            state_class,
                            dimension,
                            proposed_delta,
                            confidence,
                            occurred_at,
                            created_at,
                            policy.policy_version,
                        ),
                    )
                    if int(cursor.rowcount or 0) > 0:
                        accepted.append(event_id)
                self._rebuild_relationship_scope_locked(scope=scope, as_of=now, policy=policy)
                self._connection.execute("COMMIT")
            except Exception:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            if not accepted:
                return ()
            placeholders = ",".join("?" for _ in accepted)
            rows = self._connection.execute(
                f"SELECT * FROM relationship_events WHERE event_id IN ({placeholders}) ORDER BY occurred_at, event_id",
                accepted,
            ).fetchall()
            return tuple(
                event for row in rows if (event := self._relationship_event_from_row(row)) is not None
            )

    def rebuild_relationship_state(
        self,
        *,
        scope: str = "",
        now: float | None = None,
        policy: RelationshipPolicy | None = None,
    ) -> RelationshipSnapshot:
        policy = policy or RelationshipPolicy()
        when = float(time.time() if now is None else now)
        target_scope = str(scope or "").strip()
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                if target_scope:
                    self._rebuild_relationship_scope_locked(
                        scope=target_scope, as_of=when, policy=policy
                    )
                else:
                    self._rebuild_relationship_locked(as_of=when, policy=policy)
                self._connection.execute("COMMIT")
            except Exception:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return self.get_relationship_snapshot(
            scope=target_scope or "global", now=when, policy=policy
        )

    def _read_relationship_snapshot_rows_locked(self, scope: str) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        relationship_rows = self._connection.execute(
            "SELECT * FROM relationship_state WHERE scope = ? ORDER BY dimension",
            (scope,),
        ).fetchall()
        affect_rows = self._connection.execute(
            "SELECT * FROM short_term_affect WHERE scope = ? ORDER BY dimension",
            (scope,),
        ).fetchall()
        return relationship_rows, affect_rows

    def get_relationship_snapshot(
        self,
        *,
        scope: str = "global",
        now: float | None = None,
        policy: RelationshipPolicy | None = None,
    ) -> RelationshipSnapshot:
        """Read one Session's bounded snapshot without scanning event history.

        Affect rows are analytically decayed from their stored as-of timestamp,
        so the Main Chat fast path reads only the small snapshot tables.  A
        scope with events but no cached rows heals itself on read; a scope with
        no events returns the neutral baseline (a fresh dialogue starts at
        zero relationship history).
        """

        target_scope = str(scope or "").strip() or "global"
        policy = policy or RelationshipPolicy()
        when = float(time.time() if now is None else now)
        with self._lock:
            self._ensure_open()
            relationship_rows, affect_rows = self._read_relationship_snapshot_rows_locked(target_scope)
            if not relationship_rows and not affect_rows:
                has_events = self._connection.execute(
                    "SELECT 1 FROM relationship_events WHERE invalidated_at IS NULL AND scope = ? LIMIT 1",
                    (target_scope,),
                ).fetchone() is not None
                if has_events:
                    try:
                        self._connection.execute("BEGIN IMMEDIATE")
                        self._rebuild_relationship_scope_locked(
                            scope=target_scope, as_of=when, policy=policy
                        )
                        self._connection.execute("COMMIT")
                    except Exception:
                        try:
                            self._connection.execute("ROLLBACK")
                        except sqlite3.Error:
                            pass
                        raise
                    relationship_rows, affect_rows = self._read_relationship_snapshot_rows_locked(
                        target_scope
                    )
        if relationship_rows:
            relationship = tuple(
                RelationshipDimensionState(
                    dimension=str(row["dimension"]),
                    value=float(row["value"]),
                    event_count=int(row["event_count"]),
                    updated_at=float(row["updated_at"]),
                    policy_version=str(row["policy_version"]),
                )
                for row in relationship_rows
            )
        else:
            relationship = tuple(
                RelationshipDimensionState(
                    dimension=dimension,
                    value=_bounded(policy.long_term_baseline),
                    event_count=0,
                    updated_at=when,
                    policy_version=policy.policy_version,
                )
                for dimension in policy.relationship_dimensions
            )
        affects: list[AffectDimensionState] = []
        if affect_rows:
            for row in affect_rows:
                dimension = str(row["dimension"])
                value = float(row["value"])
                as_of = float(row["as_of"])
                if when > as_of and value != policy.affect_baseline:
                    half_life_seconds = policy.affect_half_life(dimension) * 3600.0
                    decay = math.exp(-math.log(2.0) * (when - as_of) / half_life_seconds)
                    value = _bounded(policy.affect_baseline + (value - policy.affect_baseline) * decay)
                affects.append(
                    AffectDimensionState(
                        dimension=dimension,
                        value=value,
                        event_count=int(row["event_count"]),
                        as_of=when,
                        policy_version=str(row["policy_version"]),
                    )
                )
        else:
            affects = [
                AffectDimensionState(
                    dimension=dimension,
                    value=float(policy.affect_baseline),
                    event_count=0,
                    as_of=when,
                    policy_version=policy.policy_version,
                )
                for dimension in policy.affect_dimensions
            ]
        return RelationshipSnapshot(relationship=relationship, affect=tuple(affects), as_of=when)

    def list_relationship_events(self, *, include_invalidated: bool = False) -> list[RelationshipEvent]:
        with self._lock:
            self._ensure_open()
            sql = "SELECT * FROM relationship_events"
            if not include_invalidated:
                sql += " WHERE invalidated_at IS NULL"
            sql += " ORDER BY occurred_at, event_id"
            rows = self._connection.execute(sql).fetchall()
            return [event for row in rows if (event := self._relationship_event_from_row(row)) is not None]

    def _invalidate_relationship_events_locked(
        self,
        *,
        when: float,
        reason: str,
        memory_ids: Iterable[str] = (),
        source_memory_key: str = "",
        source_session_id: str = "",
        source_turn_id: str = "",
        source_hash: str = "",
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        ids = [str(value) for value in memory_ids if str(value)]
        if ids:
            clauses.append("source_memory_id IN (" + ",".join("?" for _ in ids) + ")")
            params.extend(ids)
        if source_memory_key:
            clauses.append("source_memory_key = ?")
            params.append(str(source_memory_key))
        source_parts: list[str] = []
        if source_session_id:
            source_parts.append("source_session_id = ?")
            params.append(storage_session_id(source_session_id))
        if source_turn_id:
            source_parts.append("source_turn_id = ?")
            params.append(str(source_turn_id))
        if source_hash:
            source_parts.append("source_hash = ?")
            params.append(str(source_hash))
        if source_parts:
            clauses.append("(" + " AND ".join(source_parts) + ")")
        if not clauses:
            return 0
        sql = (
            "UPDATE relationship_events SET invalidated_at = ?, invalidation_reason = ? "
            "WHERE invalidated_at IS NULL AND (" + " OR ".join(clauses) + ")"
        )
        cursor = self._connection.execute(sql, (when, str(reason or "source_invalidated"), *params))
        return int(cursor.rowcount or 0)

    def invalidate_relationship_events_for_source(
        self,
        *,
        source_memory_key: str = "",
        source_session_id: str = "",
        source_turn_id: str = "",
        source_hash: str = "",
        reason: str = "source_invalidated",
        observed_at: float | None = None,
        policy: RelationshipPolicy | None = None,
    ) -> int:
        policy = policy or RelationshipPolicy()
        when = float(time.time() if observed_at is None else observed_at)
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                count = self._invalidate_relationship_events_locked(
                    when=when,
                    reason=reason,
                    source_memory_key=source_memory_key,
                    source_session_id=source_session_id,
                    source_turn_id=source_turn_id,
                    source_hash=source_hash,
                )
                self._rebuild_relationship_locked(as_of=when, policy=policy)
                self._connection.execute("COMMIT")
                return count
            except Exception:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def relationship_diagnostics(
        self,
        *,
        scope: str = "global",
        now: float | None = None,
        policy: RelationshipPolicy | None = None,
    ) -> dict[str, Any]:
        target_scope = str(scope or "").strip() or "global"
        policy = policy or RelationshipPolicy()
        snapshot = self.get_relationship_snapshot(scope=target_scope, now=now, policy=policy)
        with self._lock:
            self._ensure_open()
            active = int(self._connection.execute(
                "SELECT COUNT(*) FROM relationship_events WHERE invalidated_at IS NULL AND scope = ?",
                (target_scope,),
            ).fetchone()[0])
            invalidated = int(self._connection.execute(
                "SELECT COUNT(*) FROM relationship_events WHERE invalidated_at IS NOT NULL AND scope = ?",
                (target_scope,),
            ).fetchone()[0])
        return {
            "scope": target_scope,
            "policy_version": policy.policy_version,
            "active_event_count": active,
            "invalidated_event_count": invalidated,
            "relationship": {item.dimension: round(item.value, 6) for item in snapshot.relationship},
            "affect": {item.dimension: round(item.value, 6) for item in snapshot.affect},
        }

    # ------------------------------------------------------------------
    # C6 Character Life derived state
    # ------------------------------------------------------------------
    @staticmethod
    def _life_schedule_from_row(row: sqlite3.Row | None) -> LifeSchedule | None:
        if row is None:
            return None
        from core.continuity.models import ContinuityFactAuthority
        return LifeSchedule(
            schedule_id=str(row["schedule_id"]),
            character_id=str(row["character_id"]),
            local_date=str(row["local_date"]),
            profile_version=str(row["profile_version"]),
            seed_hash=str(row["seed_hash"]),
            generated_at=float(row["generated_at"]),
            source_class=ContinuityFactAuthority(str(row["source_class"])),
        )

    @staticmethod
    def _life_schedule_item_from_row(row: sqlite3.Row | None) -> LifeScheduleItem | None:
        if row is None:
            return None
        return LifeScheduleItem(
            item_id=str(row["item_id"]),
            schedule_id=str(row["schedule_id"]),
            ordinal=int(row["ordinal"]),
            category=str(row["category"]),
            title=str(row["title"]),
            starts_at=float(row["starts_at"]),
            ends_at=float(row["ends_at"]),
            status=LifeScheduleStatus(str(row["status"])),
            thread_key=str(row["thread_key"] or ""),
            interrupted_by_session_id=str(row["interrupted_by_session_id"] or ""),
            interrupted_by_turn_id=str(row["interrupted_by_turn_id"] or ""),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _life_thread_from_row(row: sqlite3.Row | None) -> LifeThread | None:
        if row is None:
            return None
        from core.continuity.models import ContinuityFactAuthority
        return LifeThread(
            thread_id=str(row["thread_id"]),
            character_id=str(row["character_id"]),
            thread_key=str(row["thread_key"]),
            title=str(row["title"]),
            status=LifeThreadStatus(str(row["status"])),
            progress=float(row["progress"]),
            source_class=ContinuityFactAuthority(str(row["source_class"])),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            last_event_at=(None if row["last_event_at"] is None else float(row["last_event_at"])),
        )

    @staticmethod
    def _life_event_from_row(row: sqlite3.Row | None) -> LifeEvent | None:
        if row is None:
            return None
        from core.continuity.models import ContinuityFactAuthority
        return LifeEvent(
            event_id=str(row["event_id"]),
            character_id=str(row["character_id"]),
            local_date=str(row["local_date"]),
            schedule_item_id=str(row["schedule_item_id"] or ""),
            thread_id=str(row["thread_id"] or ""),
            event_type=str(row["event_type"]),
            summary=str(row["summary"] or ""),
            feeling=str(row["feeling"] or ""),
            follow_up=str(row["follow_up"] or ""),
            source_class=ContinuityFactAuthority(str(row["source_class"])),
            source_session_id=str(row["source_session_id"] or ""),
            source_turn_id=str(row["source_turn_id"] or ""),
            source_memory_id=str(row["source_memory_id"] or ""),
            source_memory_key=str(row["source_memory_key"] or ""),
            source_hash=str(row["source_hash"] or ""),
            source_fingerprint=str(row["source_fingerprint"]),
            occurred_at=float(row["occurred_at"]),
            created_at=float(row["created_at"]),
            invalidated_at=(None if row["invalidated_at"] is None else float(row["invalidated_at"])),
            invalidation_reason=str(row["invalidation_reason"] or ""),
            policy_version=str(row["policy_version"] or ""),
        )

    @staticmethod
    def _life_stable_id(*parts: object) -> str:
        material = "\x1f".join(str(part) for part in parts)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def ensure_life_schedule(
        self,
        *,
        character_id: str,
        local_date: str,
        profile_version: str,
        seed_hash: str,
        generated_at: float,
        items: Iterable[LifeScheduleItemPlan],
    ) -> LifeSchedule:
        """Insert one immutable deterministic plan per character/local date."""
        character = str(character_id or "").strip()
        day = str(local_date or "").strip()
        if not character or not day:
            raise ValueError("character_id and local_date are required")
        schedule_id = self._life_stable_id("life-schedule", character, day)
        plans = tuple(items)
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    "SELECT * FROM daily_schedules WHERE character_id = ? AND local_date = ?",
                    (character, day),
                ).fetchone()
                if existing is None:
                    self._connection.execute(
                        """
                        INSERT INTO daily_schedules(
                            schedule_id, character_id, local_date, profile_version,
                            seed_hash, generated_at, source_class
                        ) VALUES (?, ?, ?, ?, ?, ?, 'simulated_life')
                        """,
                        (schedule_id, character, day, str(profile_version), str(seed_hash), float(generated_at)),
                    )
                    for plan in sorted(plans, key=lambda value: value.ordinal):
                        item_id = self._life_stable_id("life-item", schedule_id, int(plan.ordinal))
                        self._connection.execute(
                            """
                            INSERT INTO schedule_items(
                                item_id, schedule_id, ordinal, category, title,
                                starts_at, ends_at, status, thread_key,
                                interrupted_by_session_id, interrupted_by_turn_id, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', ?, '', '', ?)
                            """,
                            (
                                item_id, schedule_id, int(plan.ordinal), str(plan.category), str(plan.title),
                                float(plan.starts_at), float(plan.ends_at), str(plan.thread_key or ""), float(generated_at),
                            ),
                        )
                        if plan.thread_key:
                            thread_id = self._life_stable_id("life-thread", character, plan.thread_key)
                            self._connection.execute(
                                """
                                INSERT OR IGNORE INTO life_threads(
                                    thread_id, character_id, thread_key, title, status, progress,
                                    source_class, created_at, updated_at, last_event_at
                                ) VALUES (?, ?, ?, ?, 'active', 0.0, 'simulated_life', ?, ?, NULL)
                                """,
                                (
                                    thread_id, character, str(plan.thread_key),
                                    str(plan.thread_title or plan.thread_key), float(generated_at), float(generated_at),
                                ),
                            )
                    existing = self._connection.execute(
                        "SELECT * FROM daily_schedules WHERE schedule_id = ?", (schedule_id,)
                    ).fetchone()
                self._connection.execute("COMMIT")
                schedule = self._life_schedule_from_row(existing)
                if schedule is None:
                    raise ContinuityStoreError("failed to create daily life schedule")
                return schedule
            except Exception:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def get_life_schedule(self, character_id: str, local_date: str) -> LifeSchedule | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM daily_schedules WHERE character_id = ? AND local_date = ?",
                (str(character_id), str(local_date)),
            ).fetchone()
            return self._life_schedule_from_row(row)

    def get_latest_life_schedule(self, character_id: str, *, before_local_date: str | None = None) -> LifeSchedule | None:
        with self._lock:
            self._ensure_open()
            if before_local_date:
                row = self._connection.execute(
                    """
                    SELECT * FROM daily_schedules
                     WHERE character_id = ? AND local_date < ?
                     ORDER BY local_date DESC LIMIT 1
                    """,
                    (str(character_id), str(before_local_date)),
                ).fetchone()
            else:
                row = self._connection.execute(
                    "SELECT * FROM daily_schedules WHERE character_id = ? ORDER BY local_date DESC LIMIT 1",
                    (str(character_id),),
                ).fetchone()
            return self._life_schedule_from_row(row)

    def list_life_schedule_items(self, schedule_id: str) -> list[LifeScheduleItem]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT * FROM schedule_items WHERE schedule_id = ? ORDER BY ordinal",
                (str(schedule_id),),
            ).fetchall()
            return [item for row in rows if (item := self._life_schedule_item_from_row(row))]

    def list_life_threads(self, character_id: str, *, status: str | None = None) -> list[LifeThread]:
        with self._lock:
            self._ensure_open()
            if status:
                rows = self._connection.execute(
                    "SELECT * FROM life_threads WHERE character_id = ? AND status = ? ORDER BY created_at, thread_key",
                    (str(character_id), str(status)),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM life_threads WHERE character_id = ? ORDER BY created_at, thread_key",
                    (str(character_id),),
                ).fetchall()
            return [item for row in rows if (item := self._life_thread_from_row(row))]

    def set_life_schedule_item_status(
        self,
        item_id: str,
        status: LifeScheduleStatus,
        *,
        observed_at: float | None = None,
        clear_interrupt: bool = False,
    ) -> LifeScheduleItem | None:
        when = float(time.time() if observed_at is None else observed_at)
        with self._lock:
            self._ensure_open()
            if clear_interrupt:
                self._connection.execute(
                    """
                    UPDATE schedule_items
                       SET status = ?, interrupted_by_session_id = '', interrupted_by_turn_id = '', updated_at = ?
                     WHERE item_id = ?
                    """,
                    (status.value, when, str(item_id)),
                )
            else:
                self._connection.execute(
                    "UPDATE schedule_items SET status = ?, updated_at = ? WHERE item_id = ?",
                    (status.value, when, str(item_id)),
                )
            row = self._connection.execute("SELECT * FROM schedule_items WHERE item_id = ?", (str(item_id),)).fetchone()
            return self._life_schedule_item_from_row(row)

    def _record_life_event_locked(
        self,
        *,
        character_id: str,
        local_date: str,
        event_type: str,
        summary: str,
        feeling: str,
        follow_up: str,
        occurred_at: float,
        policy: LifePolicy,
        fingerprint_material: str,
        schedule_item_id: str = "",
        thread_id: str = "",
        source_session_id: str = "",
        source_turn_id: str = "",
        source_memory_id: str = "",
        source_memory_key: str = "",
        source_hash: str = "",
    ) -> tuple[LifeEvent, bool]:
        fingerprint = hashlib.sha256(str(fingerprint_material).encode("utf-8")).hexdigest()
        event_id = self._life_stable_id("life-event", fingerprint)
        linked_memory_id = str(source_memory_id or "")
        if not linked_memory_id and source_memory_key:
            # Character Life is character-global while memories are Session-scoped,
            # so the provenance link resolves the newest matching active row
            # across scopes; forget closure additionally matches by key.
            row = self._connection.execute(
                "SELECT id FROM memory_items WHERE memory_key = ? AND state = 'active' "
                "ORDER BY updated_at DESC LIMIT 1",
                (str(source_memory_key),),
            ).fetchone()
            if row is not None:
                linked_memory_id = str(row["id"])
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO life_events(
                event_id, character_id, local_date, schedule_item_id, thread_id,
                event_type, summary, feeling, follow_up, source_class,
                source_session_id, source_turn_id, source_memory_id, source_memory_key,
                source_hash, source_fingerprint, occurred_at, created_at,
                invalidated_at, invalidation_reason, policy_version
            ) VALUES (?, ?, ?, NULLIF(?, ''), NULLIF(?, ''), ?, ?, ?, ?, 'simulated_life',
                      ?, ?, NULLIF(?, ''), ?, ?, ?, ?, ?, NULL, '', ?)
            """,
            (
                event_id, str(character_id), str(local_date), str(schedule_item_id), str(thread_id),
                str(event_type), str(summary), str(feeling), str(follow_up),
                storage_session_id(source_session_id) if source_session_id else "",
                str(source_turn_id or ""), linked_memory_id, str(source_memory_key or ""),
                str(source_hash or ""), fingerprint, float(occurred_at), float(occurred_at), policy.policy_version,
            ),
        )
        row = self._connection.execute(
            "SELECT * FROM life_events WHERE source_fingerprint = ?", (fingerprint,)
        ).fetchone()
        event = self._life_event_from_row(row)
        if event is None:
            raise ContinuityStoreError("failed to persist life event")
        return event, bool(cursor.rowcount)

    def record_life_event(
        self,
        *,
        character_id: str,
        local_date: str,
        event_type: str,
        summary: str,
        occurred_at: float,
        policy: LifePolicy | None = None,
        fingerprint_material: str,
        feeling: str = "",
        follow_up: str = "",
        schedule_item_id: str = "",
        thread_id: str = "",
        source_session_id: str = "",
        source_turn_id: str = "",
        source_memory_id: str = "",
        source_memory_key: str = "",
        source_hash: str = "",
    ) -> LifeEvent:
        policy = policy or LifePolicy(character_id=str(character_id))
        with self._lock:
            self._ensure_open()
            event, _ = self._record_life_event_locked(
                character_id=character_id, local_date=local_date, event_type=event_type,
                summary=summary, feeling=feeling, follow_up=follow_up, occurred_at=occurred_at,
                policy=policy, fingerprint_material=fingerprint_material,
                schedule_item_id=schedule_item_id, thread_id=thread_id,
                source_session_id=source_session_id, source_turn_id=source_turn_id,
                source_memory_id=source_memory_id, source_memory_key=source_memory_key, source_hash=source_hash,
            )
            return event

    def complete_life_schedule_item(
        self,
        item_id: str,
        *,
        occurred_at: float,
        summary: str,
        feeling: str,
        follow_up: str,
        policy: LifePolicy,
    ) -> LifeScheduleItem | None:
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    """
                    SELECT i.*, s.character_id, s.local_date
                      FROM schedule_items i
                      JOIN daily_schedules s ON s.schedule_id = i.schedule_id
                     WHERE i.item_id = ?
                    """,
                    (str(item_id),),
                ).fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    return None
                self._connection.execute(
                    """
                    UPDATE schedule_items
                       SET status = 'completed', interrupted_by_session_id = '', interrupted_by_turn_id = '', updated_at = ?
                     WHERE item_id = ?
                    """,
                    (float(occurred_at), str(item_id)),
                )
                thread_id = ""
                if str(row["thread_key"] or ""):
                    trow = self._connection.execute(
                        "SELECT * FROM life_threads WHERE character_id = ? AND thread_key = ?",
                        (str(row["character_id"]), str(row["thread_key"])),
                    ).fetchone()
                    if trow is not None:
                        thread_id = str(trow["thread_id"])
                _, inserted = self._record_life_event_locked(
                    character_id=str(row["character_id"]), local_date=str(row["local_date"]),
                    event_type="activity_outcome", summary=summary, feeling=feeling, follow_up=follow_up,
                    occurred_at=float(occurred_at), policy=policy,
                    fingerprint_material=f"activity-outcome:{item_id}", schedule_item_id=str(item_id), thread_id=thread_id,
                )
                if inserted and thread_id:
                    self._connection.execute(
                        """
                        UPDATE life_threads
                           SET progress = MIN(1.0, progress + ?), updated_at = ?, last_event_at = ?
                         WHERE thread_id = ?
                        """,
                        (float(policy.thread_progress_step), float(occurred_at), float(occurred_at), thread_id),
                    )
                self._connection.execute("COMMIT")
                item_row = self._connection.execute("SELECT * FROM schedule_items WHERE item_id = ?", (str(item_id),)).fetchone()
                return self._life_schedule_item_from_row(item_row)
            except Exception:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def pause_current_life_item_for_chat(
        self,
        character_id: str,
        *,
        local_date: str,
        observed_at: float,
        session_id: str,
        turn_id: str,
        source_hash: str,
        policy: LifePolicy,
    ) -> LifeScheduleItem | None:
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    """
                    SELECT i.* FROM schedule_items i
                      JOIN daily_schedules s ON s.schedule_id = i.schedule_id
                     WHERE s.character_id = ? AND s.local_date = ?
                       AND i.status = 'running' AND i.starts_at <= ? AND i.ends_at > ?
                     ORDER BY i.ordinal LIMIT 1
                    """,
                    (str(character_id), str(local_date), float(observed_at), float(observed_at)),
                ).fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    return None
                item_id = str(row["item_id"])
                self._connection.execute(
                    """
                    UPDATE schedule_items
                       SET status = 'paused', interrupted_by_session_id = ?, interrupted_by_turn_id = ?, updated_at = ?
                     WHERE item_id = ?
                    """,
                    (storage_session_id(session_id), str(turn_id), float(observed_at), item_id),
                )
                self._record_life_event_locked(
                    character_id=character_id, local_date=local_date, event_type="chat_interrupted",
                    summary="The current simulated activity was paused for a conversation.", feeling="", follow_up="Resume or close the bounded simulated activity after the conversation.",
                    occurred_at=observed_at, policy=policy,
                    fingerprint_material=f"chat-interrupt:{item_id}:{storage_session_id(session_id)}:{turn_id}",
                    schedule_item_id=item_id, source_session_id=session_id, source_turn_id=turn_id, source_hash=source_hash,
                )
                self._connection.execute("COMMIT")
                updated = self._connection.execute("SELECT * FROM schedule_items WHERE item_id = ?", (item_id,)).fetchone()
                return self._life_schedule_item_from_row(updated)
            except Exception:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def resume_life_item_after_chat(
        self,
        character_id: str,
        *,
        local_date: str,
        observed_at: float,
        session_id: str,
        turn_id: str,
        source_hash: str,
        policy: LifePolicy,
    ) -> LifeScheduleItem | None:
        sid = storage_session_id(session_id)
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    """
                    SELECT i.* FROM schedule_items i
                      JOIN daily_schedules s ON s.schedule_id = i.schedule_id
                     WHERE s.character_id = ? AND s.local_date = ? AND i.status = 'paused'
                       AND i.interrupted_by_session_id = ? AND i.interrupted_by_turn_id = ?
                     ORDER BY i.ordinal LIMIT 1
                    """,
                    (str(character_id), str(local_date), sid, str(turn_id)),
                ).fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    return None
                next_status = LifeScheduleStatus.RUNNING if float(observed_at) < float(row["ends_at"]) else LifeScheduleStatus.COMPLETED
                item_id = str(row["item_id"])
                self._connection.execute(
                    """
                    UPDATE schedule_items
                       SET status = ?, interrupted_by_session_id = '', interrupted_by_turn_id = '', updated_at = ?
                     WHERE item_id = ?
                    """,
                    (next_status.value, float(observed_at), item_id),
                )
                self._record_life_event_locked(
                    character_id=character_id, local_date=local_date, event_type="chat_resumed",
                    summary="The paused simulated activity was resumed after the conversation." if next_status == LifeScheduleStatus.RUNNING else "The paused simulated activity reached its bounded end after the conversation.",
                    feeling="", follow_up="", occurred_at=observed_at, policy=policy,
                    fingerprint_material=f"chat-resume:{item_id}:{sid}:{turn_id}", schedule_item_id=item_id,
                    source_session_id=session_id, source_turn_id=turn_id, source_hash=source_hash,
                )
                self._connection.execute("COMMIT")
                updated = self._connection.execute("SELECT * FROM schedule_items WHERE item_id = ?", (item_id,)).fetchone()
                return self._life_schedule_item_from_row(updated)
            except Exception:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def list_life_events(
        self,
        character_id: str,
        *,
        include_invalidated: bool = False,
        local_date: str | None = None,
        limit: int | None = None,
    ) -> list[LifeEvent]:
        clauses = ["character_id = ?"]
        params: list[Any] = [str(character_id)]
        if not include_invalidated:
            clauses.append("invalidated_at IS NULL")
        if local_date:
            clauses.append("local_date = ?")
            params.append(str(local_date))
        sql = "SELECT * FROM life_events WHERE " + " AND ".join(clauses) + " ORDER BY occurred_at DESC, event_id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(sql, params).fetchall()
            return [event for row in rows if (event := self._life_event_from_row(row))]

    def _invalidate_life_events_locked(
        self,
        *,
        when: float,
        reason: str,
        memory_ids: Iterable[str] = (),
        source_memory_key: str = "",
        source_session_id: str = "",
        source_turn_id: str = "",
        source_hash: str = "",
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        ids = [str(value) for value in memory_ids if str(value)]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"source_memory_id IN ({placeholders})")
            params.extend(ids)
        if source_memory_key:
            clauses.append("source_memory_key = ?")
            params.append(str(source_memory_key))
        source_parts: list[str] = []
        if source_session_id:
            source_parts.append("source_session_id = ?")
            params.append(storage_session_id(source_session_id))
        if source_turn_id:
            source_parts.append("source_turn_id = ?")
            params.append(str(source_turn_id))
        if source_hash:
            source_parts.append("source_hash = ?")
            params.append(str(source_hash))
        if source_parts:
            clauses.append("(" + " AND ".join(source_parts) + ")")
        if not clauses:
            return 0
        cursor = self._connection.execute(
            "UPDATE life_events SET invalidated_at = ?, invalidation_reason = ? WHERE invalidated_at IS NULL AND (" + " OR ".join(clauses) + ")",
            (float(when), str(reason or "source_invalidated"), *params),
        )
        return int(cursor.rowcount or 0)

    def invalidate_life_events_for_source(
        self,
        *,
        source_memory_key: str = "",
        source_session_id: str = "",
        source_turn_id: str = "",
        source_hash: str = "",
        reason: str = "source_invalidated",
        observed_at: float | None = None,
    ) -> int:
        when = float(time.time() if observed_at is None else observed_at)
        with self._lock:
            self._ensure_open()
            return self._invalidate_life_events_locked(
                when=when, reason=reason, source_memory_key=source_memory_key,
                source_session_id=source_session_id, source_turn_id=source_turn_id, source_hash=source_hash,
            )

    def get_life_snapshot(
        self,
        character_id: str,
        *,
        local_date: str,
        now: float,
    ) -> LifeSnapshot:
        with self._lock:
            self._ensure_open()
            schedule_row = self._connection.execute(
                "SELECT * FROM daily_schedules WHERE character_id = ? AND local_date = ?",
                (str(character_id), str(local_date)),
            ).fetchone()
            schedule = self._life_schedule_from_row(schedule_row)
            items: list[LifeScheduleItem] = []
            current: LifeScheduleItem | None = None
            next_item: LifeScheduleItem | None = None
            if schedule is not None:
                rows = self._connection.execute(
                    "SELECT * FROM schedule_items WHERE schedule_id = ? ORDER BY ordinal",
                    (schedule.schedule_id,),
                ).fetchall()
                items = [item for row in rows if (item := self._life_schedule_item_from_row(row))]
                current = next((item for item in items if item.status in {LifeScheduleStatus.RUNNING, LifeScheduleStatus.PAUSED}), None)
                next_item = next((item for item in items if item.status == LifeScheduleStatus.PLANNED and item.starts_at > float(now)), None)
            event_rows = self._connection.execute(
                """
                SELECT * FROM life_events
                 WHERE character_id = ? AND invalidated_at IS NULL AND occurred_at <= ?
                 ORDER BY occurred_at DESC, event_id DESC LIMIT 3
                """,
                (str(character_id), float(now)),
            ).fetchall()
            thread_rows = self._connection.execute(
                "SELECT * FROM life_threads WHERE character_id = ? AND status = 'active' ORDER BY updated_at DESC, thread_key LIMIT 3",
                (str(character_id),),
            ).fetchall()
            return LifeSnapshot(
                character_id=str(character_id), local_date=str(local_date), schedule=schedule,
                items=tuple(items), current_item=current, next_item=next_item,
                recent_events=tuple(event for row in event_rows if (event := self._life_event_from_row(row))),
                active_threads=tuple(thread for row in thread_rows if (thread := self._life_thread_from_row(row))),
                as_of=float(now),
            )

    def life_diagnostics(self, character_id: str, *, local_date: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            schedule_count = int(self._connection.execute(
                "SELECT COUNT(*) FROM daily_schedules WHERE character_id = ?", (str(character_id),)
            ).fetchone()[0])
            active_events = int(self._connection.execute(
                "SELECT COUNT(*) FROM life_events WHERE character_id = ? AND invalidated_at IS NULL", (str(character_id),)
            ).fetchone()[0])
            invalidated_events = int(self._connection.execute(
                "SELECT COUNT(*) FROM life_events WHERE character_id = ? AND invalidated_at IS NOT NULL", (str(character_id),)
            ).fetchone()[0])
            active_threads = int(self._connection.execute(
                "SELECT COUNT(*) FROM life_threads WHERE character_id = ? AND status = 'active'", (str(character_id),)
            ).fetchone()[0])
            result = {
                "character_id": str(character_id),
                "schedule_count": schedule_count,
                "active_event_count": active_events,
                "invalidated_event_count": invalidated_events,
                "active_thread_count": active_threads,
                "source_class": "simulated_life",
            }
            if local_date:
                schedule = self.get_life_schedule(character_id, local_date)
                result["local_date"] = str(local_date)
                result["schedule_present"] = schedule is not None
                result["item_count"] = len(self.list_life_schedule_items(schedule.schedule_id)) if schedule else 0
            return result

    # ------------------------------------------------------------------
    # Consolidation journal and crash recovery
    # ------------------------------------------------------------------
    def register_turn_observed(self, evidence: TurnEvidence) -> ConsolidationTurnState:
        sid = storage_session_id(evidence.session_id)
        turn_id = str(evidence.turn_id or "").strip()
        if not turn_id:
            raise ValueError("turn_id is required for memory consolidation")
        now = float(evidence.observed_at if evidence.observed_at is not None else time.time())
        with self._lock:
            self._ensure_open()
            self._connection.execute(
                """
                INSERT INTO consolidation_turns(
                    session_id, turn_id, status, source, user_created_at,
                    assistant_created_at, source_hash, attempts, last_error,
                    observed_at, completed_at, updated_at
                ) VALUES (?, ?, 'observed', ?, ?, '', '', 0, '', ?, NULL, ?)
                ON CONFLICT(session_id, turn_id) DO UPDATE SET
                    source = CASE WHEN excluded.source <> '' THEN excluded.source ELSE consolidation_turns.source END,
                    user_created_at = CASE WHEN excluded.user_created_at <> '' THEN excluded.user_created_at ELSE consolidation_turns.user_created_at END,
                    observed_at = COALESCE(consolidation_turns.observed_at, excluded.observed_at),
                    updated_at = MAX(consolidation_turns.updated_at, excluded.updated_at)
                """,
                (
                    sid,
                    turn_id,
                    str(evidence.source or ""),
                    str(evidence.user_created_at or ""),
                    now,
                    now,
                ),
            )
            state = self.get_consolidation_turn(sid, turn_id)
            if state is None:
                raise ContinuityStoreError("failed to register consolidation turn")
            return state

    def mark_turn_queued(self, evidence: TurnEvidence) -> ConsolidationTurnState:
        sid = storage_session_id(evidence.session_id)
        turn_id = str(evidence.turn_id or "").strip()
        if not turn_id:
            raise ValueError("turn_id is required for memory consolidation")
        now = time.time()
        source_hash = evidence_source_hash(evidence)
        with self._lock:
            self._ensure_open()
            self.register_turn_observed(evidence)
            self._connection.execute(
                """
                UPDATE consolidation_turns
                   SET status = CASE WHEN status = 'completed' THEN status ELSE 'queued' END,
                       source = CASE WHEN ? <> '' THEN ? ELSE source END,
                       user_created_at = CASE WHEN ? <> '' THEN ? ELSE user_created_at END,
                       assistant_created_at = CASE WHEN ? <> '' THEN ? ELSE assistant_created_at END,
                       source_hash = CASE WHEN status = 'completed' THEN source_hash ELSE ? END,
                       last_error = CASE WHEN status = 'completed' THEN last_error ELSE '' END,
                       updated_at = ?
                 WHERE session_id = ? AND turn_id = ?
                """,
                (
                    str(evidence.source or ""),
                    str(evidence.source or ""),
                    str(evidence.user_created_at or ""),
                    str(evidence.user_created_at or ""),
                    str(evidence.assistant_created_at or ""),
                    str(evidence.assistant_created_at or ""),
                    source_hash,
                    now,
                    sid,
                    turn_id,
                ),
            )
            state = self.get_consolidation_turn(sid, turn_id)
            if state is None:
                raise ContinuityStoreError("failed to queue consolidation turn")
            return state

    def get_consolidation_turn(self, session_id: str, turn_id: str) -> ConsolidationTurnState | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT * FROM consolidation_turns WHERE session_id = ? AND turn_id = ?",
                (storage_session_id(session_id), str(turn_id)),
            ).fetchone()
            return self._consolidation_from_row(row)

    def list_recoverable_turns(self) -> list[ConsolidationTurnState]:
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT * FROM consolidation_turns
                 WHERE status IN ('observed', 'queued', 'processing', 'failed')
                 ORDER BY updated_at, session_id, turn_id
                """
            ).fetchall()
            return [state for row in rows if (state := self._consolidation_from_row(row))]

    def mark_consolidation_failed(
        self,
        session_id: str,
        turn_id: str,
        *,
        error: str,
    ) -> None:
        with self._lock:
            self._ensure_open()
            self._connection.execute(
                """
                UPDATE consolidation_turns
                   SET status = 'failed', last_error = ?, updated_at = ?
                 WHERE session_id = ? AND turn_id = ?
                """,
                (
                    str(error or "")[:1000],
                    time.time(),
                    storage_session_id(session_id),
                    str(turn_id),
                ),
            )

    # ------------------------------------------------------------------
    # Explicit forget and deterministic candidate application
    # ------------------------------------------------------------------
    def forget_memory(
        self,
        memory_key: str,
        *,
        scope: str = "global",
        kind: MemoryKind | None = None,
        session_id: str = "",
        turn_id: str = "",
        reason: str = "explicit_forget",
        observed_at: float | None = None,
        relationship_policy: RelationshipPolicy | None = None,
    ) -> int:
        """Hard-delete all durable values for a key and install a tombstone.

        The tombstone contains the key/category only; no summary/object value is
        copied into it.  C3 derived indexes will be required to delete matching
        entries in the same explicit-forget operation when they exist.
        """

        key = str(memory_key or "").strip()
        if not key:
            raise ValueError("memory_key is required")
        when = float(observed_at if observed_at is not None else time.time())
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                source_rows = self._connection.execute(
                    "SELECT id, source_session_id, source_turn_id, source_hash "
                    "FROM memory_items WHERE scope = ? AND memory_key = ?",
                    (str(scope), key),
                ).fetchall()
                source_memory_ids = [str(row["id"]) for row in source_rows]
                deleted = len(source_memory_ids)
                invalidated_relationship_events = self._invalidate_relationship_events_locked(
                    when=when,
                    reason=str(reason or "explicit_forget"),
                    memory_ids=source_memory_ids,
                    source_memory_key=key,
                )
                self._invalidate_life_events_locked(
                    when=when,
                    reason=str(reason or "explicit_forget"),
                    memory_ids=source_memory_ids,
                    source_memory_key=key,
                )
                self._connection.execute(
                    "DELETE FROM memory_items WHERE scope = ? AND memory_key = ?",
                    (str(scope), key),
                )
                active = self._connection.execute(
                    "SELECT id, archive_guard_complete FROM memory_tombstones WHERE scope = ? AND memory_key = ? AND cleared_at IS NULL",
                    (str(scope), key),
                ).fetchone()
                tombstone_id = str(active["id"]) if active is not None else uuid.uuid4().hex
                if active is None:
                    self._connection.execute(
                        """
                        INSERT INTO memory_tombstones(
                            id, scope, memory_key, kind, created_at,
                            source_session_id, source_turn_id, reason, cleared_at, archive_guard_complete
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)
                        """,
                        (
                            tombstone_id,
                            str(scope),
                            key,
                            str(kind.value) if kind is not None else None,
                            when,
                            str(session_id or ""),
                            str(turn_id or ""),
                            str(reason or "explicit_forget"),
                        ),
                    )
                else:
                    self._connection.execute(
                        """
                        UPDATE memory_tombstones
                           SET kind = COALESCE(?, kind),
                               source_session_id = ?, source_turn_id = ?, reason = ?
                         WHERE id = ?
                        """,
                        (
                            str(kind.value) if kind is not None else None,
                            str(session_id or ""),
                            str(turn_id or ""),
                            str(reason or "explicit_forget"),
                            tombstone_id,
                        ),
                    )
                for row in source_rows:
                    source_sid = str(row["source_session_id"] or "").strip()
                    source_tid = str(row["source_turn_id"] or "").strip()
                    if not source_sid or not source_tid:
                        continue
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO memory_forget_sources(
                            tombstone_id, source_session_id, source_turn_id, source_hash, forgotten_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (tombstone_id, source_sid, source_tid, str(row["source_hash"] or ""), when),
                    )
                if active is None or bool(active["archive_guard_complete"]):
                    self._connection.execute(
                        "UPDATE memory_tombstones SET archive_guard_complete = 1 WHERE id = ?",
                        (tombstone_id,),
                    )
                if invalidated_relationship_events:
                    self._rebuild_relationship_scope_locked(
                        scope=scope,
                        as_of=when,
                        policy=relationship_policy or RelationshipPolicy(),
                    )
                self._connection.execute("COMMIT")
                if self.db_path != ":memory:":
                    try:
                        # Best-effort WAL retirement so the forgotten value is
                        # not needlessly retained in old WAL frames.
                        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except sqlite3.Error:
                        pass
                return deleted
            except Exception:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def _active_memory_locked(self, scope: str, memory_key: str) -> MemoryRecord | None:
        row = self._connection.execute(
            "SELECT * FROM memory_items WHERE scope = ? AND memory_key = ? AND state = 'active'",
            (scope, memory_key),
        ).fetchone()
        return self._memory_from_row(row)

    def _active_tombstone_locked(self, scope: str, memory_key: str) -> MemoryTombstone | None:
        row = self._connection.execute(
            "SELECT * FROM memory_tombstones WHERE scope = ? AND memory_key = ? AND cleared_at IS NULL",
            (scope, memory_key),
        ).fetchone()
        return self._tombstone_from_row(row)

    @staticmethod
    def _candidate_values(candidate: MemoryCandidate) -> tuple[Any, ...]:
        return (
            str(candidate.memory_key).strip(),
            str(candidate.scope or "global"),
            candidate.kind.value,
            str(candidate.subject or ""),
            str(candidate.predicate or ""),
            str(candidate.object_text or ""),
            str(candidate.summary or "").strip(),
            _bounded(candidate.importance),
            _bounded(candidate.confidence),
            _bounded(candidate.future_value),
            _bounded(candidate.relationship_value),
            candidate.priority_class.value,
            1 if candidate.pinned else 0,
            candidate.source_type.value,
            str(candidate.work_item_id or ""),
        )

    def _insert_memory_locked(
        self,
        candidate: MemoryCandidate,
        evidence: TurnEvidence,
        *,
        when: float,
        source_hash: str,
    ) -> MemoryRecord:
        (
            memory_key,
            scope,
            kind,
            subject,
            predicate,
            object_text,
            summary,
            importance,
            confidence,
            future_value,
            relationship_value,
            priority_class,
            pinned,
            source_type,
            work_item_id,
        ) = self._candidate_values(candidate)
        if not memory_key or not summary:
            raise ValueError("memory candidate requires memory_key and summary")
        memory_id = uuid.uuid4().hex
        valid_from = candidate.valid_from if candidate.valid_from is not None else when
        self._connection.execute(
            """
            INSERT INTO memory_items(
                id, memory_key, scope, kind, subject, predicate, object_text,
                summary, importance, confidence, future_value, relationship_value,
                priority_class, mention_count, recall_count, created_at, updated_at,
                last_mentioned_at, last_recalled_at, valid_from, valid_to,
                expires_at, pinned, state, source_type, source_session_id,
                source_turn_id, source_hash, work_item_id
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, NULL,
                ?, NULL, ?, ?, 'active', ?, ?, ?, ?, ?
            )
            """,
            (
                memory_id,
                memory_key,
                scope,
                kind,
                subject,
                predicate,
                object_text,
                summary,
                importance,
                confidence,
                future_value,
                relationship_value,
                priority_class,
                when,
                when,
                when,
                valid_from,
                candidate.expires_at,
                pinned,
                source_type,
                str(evidence.session_id or ""),
                str(evidence.turn_id or ""),
                source_hash,
                work_item_id,
            ),
        )
        self._connection.execute(
            """
            INSERT OR IGNORE INTO memory_mentions(
                memory_id, session_id, turn_id, source_type, source_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                str(evidence.session_id or ""),
                str(evidence.turn_id or ""),
                source_type,
                source_hash,
                when,
            ),
        )
        record = self._active_memory_locked(scope, memory_key)
        if record is None:
            raise ContinuityStoreError("created memory could not be reloaded")
        return record

    def _reinforce_memory_locked(
        self,
        active: MemoryRecord,
        candidate: MemoryCandidate,
        evidence: TurnEvidence,
        *,
        when: float,
        source_hash: str,
    ) -> MemoryRecord:
        source_type = candidate.source_type.value
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO memory_mentions(
                memory_id, session_id, turn_id, source_type, source_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                active.id,
                str(evidence.session_id or ""),
                str(evidence.turn_id or ""),
                source_type,
                source_hash,
                when,
            ),
        )
        new_mention = int(cursor.rowcount or 0) > 0
        priority = _stronger_priority(active.priority_class, candidate.priority_class)
        summary = active.summary
        if len(str(candidate.summary or "")) > len(summary):
            summary = str(candidate.summary)
        self._connection.execute(
            """
            UPDATE memory_items
               SET summary = ?,
                   importance = MAX(importance, ?),
                   confidence = MAX(confidence, ?),
                   future_value = MAX(future_value, ?),
                   relationship_value = MAX(relationship_value, ?),
                   priority_class = ?,
                   mention_count = mention_count + ?,
                   last_mentioned_at = CASE WHEN ? = 1 THEN MAX(last_mentioned_at, ?) ELSE last_mentioned_at END,
                   pinned = CASE WHEN pinned = 1 OR ? = 1 THEN 1 ELSE 0 END,
                   updated_at = MAX(updated_at, ?),
                   expires_at = CASE
                       WHEN ? IS NULL THEN expires_at
                       WHEN expires_at IS NULL THEN ?
                       ELSE MAX(expires_at, ?)
                   END,
                   work_item_id = CASE WHEN ? <> '' THEN ? ELSE work_item_id END
             WHERE id = ?
            """,
            (
                summary,
                _bounded(candidate.importance),
                _bounded(candidate.confidence),
                _bounded(candidate.future_value),
                _bounded(candidate.relationship_value),
                priority.value,
                1 if new_mention else 0,
                1 if new_mention else 0,
                when,
                1 if candidate.pinned else 0,
                when,
                candidate.expires_at,
                candidate.expires_at,
                candidate.expires_at,
                str(candidate.work_item_id or ""),
                str(candidate.work_item_id or ""),
                active.id,
            ),
        )
        record = self._active_memory_locked(active.scope, active.memory_key)
        if record is None:
            raise ContinuityStoreError("reinforced memory could not be reloaded")
        return record

    @staticmethod
    def _owning_scope(evidence: TurnEvidence) -> str:
        """Session isolation: durable state belongs to its accepted turn's Session."""

        return storage_session_id(evidence.session_id)

    @staticmethod
    def _resolve_scope(candidate_scope: str, owning_scope: str) -> str:
        """Legacy/default placeholder values resolve to the owning Session."""

        scope = str(candidate_scope or "").strip()
        if not scope or scope == "global":
            return owning_scope
        return scope

    def apply_memory_candidates(
        self,
        evidence: TurnEvidence,
        candidates: Iterable[MemoryCandidate],
        *,
        resolver: MemoryResolver,
        complete_turn: bool,
    ) -> list[MemoryRecord]:
        """Apply candidates atomically under Host policy.

        When `complete_turn` is true, memory writes and the durable
        consolidation-completed marker commit in the same SQLite transaction.
        This is the key C2 crash-safety invariant.
        """

        candidate_list = list(candidates)
        sid = storage_session_id(evidence.session_id)
        turn_id = str(evidence.turn_id or "").strip()
        if not turn_id:
            raise ValueError("turn_id is required")
        when = float(evidence.observed_at if evidence.observed_at is not None else time.time())
        source_hash = evidence_source_hash(evidence)
        applied: list[MemoryRecord] = []
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                if complete_turn:
                    self._connection.execute(
                        """
                        INSERT INTO consolidation_turns(
                            session_id, turn_id, status, source, user_created_at,
                            assistant_created_at, source_hash, attempts, last_error,
                            observed_at, completed_at, updated_at
                        ) VALUES (?, ?, 'processing', ?, ?, ?, ?, 1, '', ?, NULL, ?)
                        ON CONFLICT(session_id, turn_id) DO UPDATE SET
                            status = CASE WHEN consolidation_turns.status = 'completed' THEN 'completed' ELSE 'processing' END,
                            source = CASE WHEN excluded.source <> '' THEN excluded.source ELSE consolidation_turns.source END,
                            user_created_at = CASE WHEN excluded.user_created_at <> '' THEN excluded.user_created_at ELSE consolidation_turns.user_created_at END,
                            assistant_created_at = CASE WHEN excluded.assistant_created_at <> '' THEN excluded.assistant_created_at ELSE consolidation_turns.assistant_created_at END,
                            source_hash = CASE WHEN consolidation_turns.status = 'completed' THEN consolidation_turns.source_hash ELSE excluded.source_hash END,
                            attempts = CASE WHEN consolidation_turns.status = 'completed' THEN consolidation_turns.attempts ELSE consolidation_turns.attempts + 1 END,
                            last_error = CASE WHEN consolidation_turns.status = 'completed' THEN consolidation_turns.last_error ELSE '' END,
                            observed_at = COALESCE(consolidation_turns.observed_at, excluded.observed_at),
                            updated_at = excluded.updated_at
                        """,
                        (
                            sid,
                            turn_id,
                            str(evidence.source or ""),
                            str(evidence.user_created_at or ""),
                            str(evidence.assistant_created_at or ""),
                            source_hash,
                            when,
                            time.time(),
                        ),
                    )
                    current = self._connection.execute(
                        "SELECT status FROM consolidation_turns WHERE session_id = ? AND turn_id = ?",
                        (sid, turn_id),
                    ).fetchone()
                    if current is not None and str(current["status"]) == "completed":
                        self._connection.execute("COMMIT")
                        return []

                seen: set[tuple[str, str]] = set()
                owning_scope = self._owning_scope(evidence)
                for candidate in candidate_list:
                    key = str(candidate.memory_key or "").strip()
                    scope = self._resolve_scope(candidate.scope, owning_scope)
                    if not key or not str(candidate.summary or "").strip():
                        continue
                    candidate = replace(candidate, scope=scope)
                    dedupe_key = (scope, key)
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    active = self._active_memory_locked(scope, key)
                    tombstone = self._active_tombstone_locked(scope, key)
                    resolution = resolver.resolve(
                        candidate=candidate,
                        active=active,
                        tombstone=tombstone,
                        observed_at=when,
                    )
                    if resolution.action is MemoryResolutionAction.SUPPRESS:
                        continue
                    if resolution.action is MemoryResolutionAction.CLEAR_TOMBSTONE_AND_CREATE:
                        self._connection.execute(
                            """
                            UPDATE memory_tombstones
                               SET cleared_at = ?
                             WHERE scope = ? AND memory_key = ? AND cleared_at IS NULL
                            """,
                            (when, scope, key),
                        )
                        active = self._active_memory_locked(scope, key)
                        if active is None:
                            applied.append(
                                self._insert_memory_locked(
                                    candidate,
                                    evidence,
                                    when=when,
                                    source_hash=source_hash,
                                )
                            )
                        else:
                            applied.append(
                                self._reinforce_memory_locked(
                                    active,
                                    candidate,
                                    evidence,
                                    when=when,
                                    source_hash=source_hash,
                                )
                            )
                        continue
                    if resolution.action is MemoryResolutionAction.CREATE:
                        applied.append(
                            self._insert_memory_locked(
                                candidate,
                                evidence,
                                when=when,
                                source_hash=source_hash,
                            )
                        )
                        continue
                    if resolution.action is MemoryResolutionAction.REINFORCE:
                        if active is None:
                            raise ContinuityStoreError("resolver requested reinforce without active memory")
                        applied.append(
                            self._reinforce_memory_locked(
                                active,
                                candidate,
                                evidence,
                                when=when,
                                source_hash=source_hash,
                            )
                        )
                        continue
                    if resolution.action is MemoryResolutionAction.SUPERSEDE:
                        if active is None:
                            raise ContinuityStoreError("resolver requested supersede without active memory")
                        self._connection.execute(
                            """
                            UPDATE memory_items
                               SET state = 'superseded', valid_to = ?, updated_at = ?
                             WHERE id = ?
                            """,
                            (when, when, active.id),
                        )
                        replacement = self._insert_memory_locked(
                            candidate,
                            evidence,
                            when=when,
                            source_hash=source_hash,
                        )
                        self._connection.execute(
                            """
                            INSERT OR IGNORE INTO memory_links(
                                from_memory_id, to_memory_id, relation, created_at
                            ) VALUES (?, ?, 'supersedes', ?)
                            """,
                            (replacement.id, active.id, when),
                        )
                        applied.append(replacement)

                if complete_turn:
                    completed_at = time.time()
                    self._connection.execute(
                        """
                        UPDATE consolidation_turns
                           SET status = 'completed', source_hash = ?, last_error = '',
                               completed_at = ?, updated_at = ?
                         WHERE session_id = ? AND turn_id = ?
                        """,
                        (source_hash, completed_at, completed_at, sid, turn_id),
                    )
                self._connection.execute("COMMIT")
                return applied
            except Exception:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def find_active_memory_key_by_exact_summary(
        self,
        summary: str,
        *,
        scope: str = "global",
    ) -> str:
        target = normalize_memory_text(summary)
        if not target:
            return ""
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                "SELECT memory_key, summary FROM memory_items WHERE scope = ? AND state = 'active'",
                (str(scope),),
            ).fetchall()
            matches = [
                str(row["memory_key"])
                for row in rows
                if normalize_memory_text(str(row["summary"] or "")) == target
            ]
            return matches[0] if len(matches) == 1 else ""
