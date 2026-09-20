"""SQLite persistence for provider-neutral Amadeus work history.

This module is intentionally a storage boundary.  It does not subscribe to the
event bus, start providers, choose worktrees, inspect Provider storage, or make
completion decisions.  A coordinator can build those policies on top of these
transactional records.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from agent_host.work_ledger_types import (
    ARTIFACT_LOCATIONS,
    ARTIFACT_STATUSES,
    ATTENTION_STATES,
    COMPLETENESS_STATES,
    CONVERSATION_BINDING_KINDS,
    EXECUTION_STATUSES,
    FOCUS_MODES,
    PERMISSION_REQUEST_STATUSES,
    PROJECT_STATES,
    WORK_OPERATION_INTENTS,
    WORK_ITEM_STATES,
    WORKSPACE_LEASE_STATUSES,
    ArtifactLocation,
    ArtifactRecord,
    ArtifactStatus,
    CompletionAssessmentRecord,
    CompletionDecision,
    ConversationBindingRecord,
    ExecutionStatus,
    FocusMode,
    FocusRecord,
    PermissionRequestRecord,
    PermissionRequestStatus,
    ProjectRecord,
    RunAttemptRecord,
    SessionWorkContextRecord,
    WorkOperationRecord,
    WorkItemRecord,
    WorkItemState,
    WorkspaceLeaseRecord,
    canonicalize_path,
    new_ledger_id,
    path_is_within,
    project_name_from_path,
    utc_timestamp,
)


SCHEMA_VERSION = 7
_UNSET = object()
_TERMINAL_EXECUTION = frozenset({"succeeded", "failed", "cancelled"})


class WorkLedgerError(RuntimeError):
    pass


class WorkLedgerNotFound(WorkLedgerError):
    pass


class WorkLedgerConflict(WorkLedgerError):
    pass


_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    display_path TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    path_identity TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS work_items (
    work_item_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK (state IN ('open', 'review_ready', 'accepted', 'archived')),
    workspace_mode TEXT NOT NULL DEFAULT 'local',
    workspace_path TEXT NOT NULL,
    workspace_identity TEXT NOT NULL,
    branch TEXT NOT NULL DEFAULT '',
    base_revision TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_activity_at REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_work_items_project_activity
    ON work_items(project_id, last_activity_at DESC);
CREATE INDEX IF NOT EXISTS idx_work_items_state_activity
    ON work_items(state, last_activity_at DESC);

CREATE TABLE IF NOT EXISTS run_attempts (
    attempt_id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    provider TEXT NOT NULL,
    provider_run_id TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'agent',
    execution_status TEXT NOT NULL CHECK (
        execution_status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'orphaned')
    ),
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(work_item_id, attempt_number)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_run_attempts_provider_run
    ON run_attempts(provider_run_id) WHERE provider_run_id <> '';
CREATE INDEX IF NOT EXISTS idx_run_attempts_item_number
    ON run_attempts(work_item_id, attempt_number DESC);
CREATE INDEX IF NOT EXISTS idx_run_attempts_status_updated
    ON run_attempts(execution_status, updated_at DESC);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id) ON DELETE CASCADE,
    attempt_id TEXT REFERENCES run_attempts(attempt_id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    uri TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    path_identity TEXT NOT NULL DEFAULT '',
    identity_key TEXT NOT NULL,
    location TEXT NOT NULL CHECK (location IN ('workspace', 'project', 'external', 'virtual')),
    status TEXT NOT NULL CHECK (status IN ('registered', 'pending', 'missing', 'approved', 'rejected')),
    sha256 TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER,
    modified_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_artifacts_attempt_identity
    ON artifacts(attempt_id, identity_key) WHERE attempt_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_artifacts_item_identity
    ON artifacts(work_item_id, identity_key) WHERE attempt_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_artifacts_item_updated
    ON artifacts(work_item_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS completion_assessments (
    assessment_id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id) ON DELETE CASCADE,
    attempt_id TEXT REFERENCES run_attempts(attempt_id) ON DELETE SET NULL,
    source TEXT NOT NULL,
    execution_status TEXT NOT NULL CHECK (
        execution_status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'orphaned')
    ),
    completeness TEXT NOT NULL CHECK (completeness IN ('unknown', 'incomplete', 'partial', 'complete')),
    attention TEXT NOT NULL CHECK (attention IN ('none', 'review', 'input', 'permission', 'conflict', 'error')),
    work_item_state TEXT NOT NULL CHECK (work_item_state IN ('open', 'review_ready', 'accepted', 'archived')),
    rationale TEXT NOT NULL,
    terminal INTEGER NOT NULL CHECK (terminal IN (0, 1)),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_completion_item_created
    ON completion_assessments(work_item_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_completion_attempt_created
    ON completion_assessments(attempt_id, created_at DESC);

CREATE TABLE IF NOT EXISTS focus_slots (
    surface TEXT PRIMARY KEY,
    work_item_id TEXT REFERENCES work_items(work_item_id) ON DELETE SET NULL,
    mode TEXT NOT NULL CHECK (mode IN ('auto', 'pinned')),
    updated_at REAL NOT NULL
);

PRAGMA user_version = 1;
"""


_MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS workspace_leases (
    lease_id TEXT PRIMARY KEY,
    workspace_path TEXT NOT NULL,
    workspace_identity TEXT NOT NULL,
    work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES run_attempts(attempt_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('active', 'released', 'stale')),
    acquired_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL,
    released_at REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_active_writer
    ON workspace_leases(workspace_identity) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_workspace_leases_item
    ON workspace_leases(work_item_id, acquired_at DESC);
CREATE INDEX IF NOT EXISTS idx_workspace_leases_status_heartbeat
    ON workspace_leases(status, heartbeat_at);

PRAGMA user_version = 2;
"""


_MIGRATION_3 = """
-- A WorkItem has one current Attempt.  Older development builds could race
-- between their in-process preflight check and INSERT, leaving two queued or
-- running attempts for the same item.  Preserve the newest one and make the
-- invariant atomic for every writer/connection from here on.
UPDATE run_attempts
SET execution_status = 'orphaned'
WHERE execution_status IN ('queued', 'running')
  AND EXISTS (
      SELECT 1
      FROM run_attempts AS newer
      WHERE newer.work_item_id = run_attempts.work_item_id
        AND newer.execution_status IN ('queued', 'running')
        AND newer.attempt_number > run_attempts.attempt_number
  );

UPDATE workspace_leases
SET status = 'stale', released_at = COALESCE(released_at, heartbeat_at)
WHERE status = 'active'
  AND attempt_id IN (
      SELECT attempt_id FROM run_attempts WHERE execution_status = 'orphaned'
  );

CREATE UNIQUE INDEX IF NOT EXISTS uq_work_item_active_attempt
    ON run_attempts(work_item_id)
    WHERE execution_status IN ('queued', 'running');

PRAGMA user_version = 3;
"""


_MIGRATION_4 = """
CREATE TABLE IF NOT EXISTS permission_requests (
    request_id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL REFERENCES run_attempts(attempt_id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL DEFAULT '',
    capability TEXT NOT NULL,
    action TEXT NOT NULL,
    scope_paths_json TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT '',
    reversibility TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL CHECK (status IN ('pending', 'allowed', 'denied', 'expired')),
    options_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    resolved_at REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_permission_request_attempt_key
    ON permission_requests(attempt_id, idempotency_key)
    WHERE idempotency_key <> '';
CREATE INDEX IF NOT EXISTS idx_permission_requests_item_status
    ON permission_requests(work_item_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_permission_requests_attempt_status
    ON permission_requests(attempt_id, status, updated_at DESC);

PRAGMA user_version = 4;
"""


# ALTER TABLE ADD COLUMN has no IF NOT EXISTS, so unlike every other migration
# here this one cannot be replayed against a database that already has the
# column -- which happens whenever a version is rewound. The caller checks
# first; this script only runs when the column is genuinely absent.
_MIGRATION_5_ADD_COLUMN = """
ALTER TABLE projects ADD COLUMN state TEXT NOT NULL DEFAULT 'active';
"""

_MIGRATION_5 = """
CREATE INDEX IF NOT EXISTS idx_projects_state
    ON projects(state, updated_at DESC);

PRAGMA user_version = 5;
"""


_MIGRATION_6 = """
CREATE TABLE IF NOT EXISTS conversation_bindings (
    session_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    anchor_work_item_id TEXT REFERENCES work_items(work_item_id) ON DELETE SET NULL,
    binding_kind TEXT NOT NULL CHECK (binding_kind IN ('project', 'work_item')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_conversation_bindings_project_updated
    ON conversation_bindings(project_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_bindings_anchor
    ON conversation_bindings(anchor_work_item_id)
    WHERE anchor_work_item_id IS NOT NULL;

PRAGMA user_version = 6;
"""


_MIGRATION_7_OPERATIONS = """
CREATE TABLE IF NOT EXISTS work_operations (
    operation_id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id) ON DELETE CASCADE,
    operation_number INTEGER NOT NULL CHECK (operation_number > 0),
    intent TEXT NOT NULL CHECK (intent IN ('execute', 'amend')),
    instruction TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(work_item_id, operation_number)
);
"""

_MIGRATION_7_ADD_ATTEMPT_OPERATION = """
ALTER TABLE run_attempts
ADD COLUMN operation_id TEXT REFERENCES work_operations(operation_id) ON DELETE RESTRICT;
"""

_MIGRATION_7 = """
-- Schema v6 treated one WorkItem as one semantic instruction, so every
-- historical Attempt under that WorkItem is a retry/resume lineage for a
-- single migrated Operation. WorkItem and Attempt ids stay untouched.
INSERT OR IGNORE INTO work_operations (
    operation_id, work_item_id, operation_number, intent, instruction,
    created_at, updated_at, metadata_json
)
SELECT
    'operation_legacy_' || work_item_id,
    work_item_id,
    1,
    CASE
        WHEN json_valid(metadata_json)
             AND lower(COALESCE(json_extract(metadata_json, '$.intent'), '')) = 'amend'
            THEN 'amend'
        ELSE 'execute'
    END,
    COALESCE(NULLIF(goal, ''), title),
    created_at,
    last_activity_at,
    '{"migration":"schema_v7"}'
FROM work_items;

UPDATE run_attempts
SET operation_id = (
    SELECT operation_id
    FROM work_operations
    WHERE work_operations.work_item_id = run_attempts.work_item_id
    ORDER BY operation_number
    LIMIT 1
)
WHERE operation_id IS NULL OR operation_id = '';

CREATE INDEX IF NOT EXISTS idx_work_operations_item_number
    ON work_operations(work_item_id, operation_number DESC);
CREATE INDEX IF NOT EXISTS idx_run_attempts_operation_number
    ON run_attempts(operation_id, attempt_number DESC);

CREATE TABLE IF NOT EXISTS session_work_contexts (
    session_id TEXT PRIMARY KEY,
    active_work_item_id TEXT REFERENCES work_items(work_item_id) ON DELETE SET NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

INSERT OR IGNORE INTO session_work_contexts (
    session_id, active_work_item_id, created_at, updated_at, metadata_json
)
SELECT
    session_id,
    anchor_work_item_id,
    created_at,
    updated_at,
    '{"migration":"conversation_binding_anchor"}'
FROM conversation_bindings
WHERE anchor_work_item_id IS NOT NULL AND anchor_work_item_id <> '';

CREATE INDEX IF NOT EXISTS idx_session_work_contexts_active
    ON session_work_contexts(active_work_item_id, updated_at DESC)
    WHERE active_work_item_id IS NOT NULL;

PRAGMA user_version = 7;
"""


def _escape_like(value: str) -> str:
    """Neutralise LIKE wildcards so a filename is matched literally."""

    return (
        str(value)
        .replace("!", "!!")
        .replace("%", "!%")
        .replace("_", "!_")
    )


def _dump_json(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _load_json(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _merged_json(existing: Any, update: dict[str, Any] | None) -> str:
    merged = _load_json(existing)
    if update:
        merged.update(update)
    return _dump_json(merged)


def _dump_string_list(value: Sequence[Any] | None) -> str:
    return json.dumps(
        _clean_string_list(value),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _load_string_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return _clean_string_list(parsed)


def _clean_string_list(value: Sequence[Any] | None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    entries: Sequence[Any] = (value,) if isinstance(value, str) else (value or ())
    for entry in entries:
        text = str(entry or "").strip()
        if text and text not in seen:
            cleaned.append(text)
            seen.add(text)
    return cleaned


class WorkLedgerStore:
    """Thread-safe SQLite repository for Projects and long-lived WorkItems."""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        clock: Callable[[], float] = utc_timestamp,
        timeout_seconds: float = 5.0,
    ) -> None:
        path_text = os.fspath(db_path)
        if not path_text:
            raise ValueError("db_path is required")
        if path_text != ":memory:":
            path_text = os.path.abspath(os.path.expandvars(os.path.expanduser(path_text)))
        self.db_path = path_text
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False
        if path_text != ":memory:":
            Path(path_text).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            path_text,
            timeout=max(0.1, float(timeout_seconds)),
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {max(100, int(timeout_seconds * 1000))}")
        if path_text != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
        self._migrate()

    def __enter__(self) -> "WorkLedgerStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    @property
    def schema_version(self) -> int:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row else 0

    def _ensure_open(self) -> None:
        if self._closed:
            raise WorkLedgerError("work ledger store is closed")

    def _migrate(self) -> None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute("PRAGMA user_version").fetchone()
            current = int(row[0]) if row else 0
            if current > SCHEMA_VERSION:
                raise WorkLedgerError(
                    f"work ledger schema {current} is newer than supported version {SCHEMA_VERSION}"
                )
            if current < 1:
                try:
                    self._connection.executescript("BEGIN IMMEDIATE;\n" + _MIGRATION_1 + "\nCOMMIT;")
                    current = 1
                except Exception:
                    try:
                        self._connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            if current < 2:
                try:
                    self._connection.executescript("BEGIN IMMEDIATE;\n" + _MIGRATION_2 + "\nCOMMIT;")
                    current = 2
                except Exception:
                    try:
                        self._connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            if current < 3:
                try:
                    self._connection.executescript("BEGIN IMMEDIATE;\n" + _MIGRATION_3 + "\nCOMMIT;")
                    current = 3
                except Exception:
                    try:
                        self._connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            if current < 4:
                try:
                    self._connection.executescript("BEGIN IMMEDIATE;\n" + _MIGRATION_4 + "\nCOMMIT;")
                    current = 4
                except Exception:
                    try:
                        self._connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            if current < 5:
                project_columns = {
                    str(row["name"])
                    for row in self._connection.execute("PRAGMA table_info(projects)")
                }
                script = (
                    _MIGRATION_5
                    if "state" in project_columns
                    else _MIGRATION_5_ADD_COLUMN + _MIGRATION_5
                )
                try:
                    self._connection.executescript("BEGIN IMMEDIATE;\n" + script + "\nCOMMIT;")
                    current = 5
                except Exception:
                    try:
                        self._connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            if current < 6:
                try:
                    self._connection.executescript("BEGIN IMMEDIATE;\n" + _MIGRATION_6 + "\nCOMMIT;")
                    current = 6
                except Exception:
                    try:
                        self._connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            if current < 7:
                attempt_columns = {
                    str(row["name"])
                    for row in self._connection.execute(
                        "PRAGMA table_info(run_attempts)"
                    )
                }
                script = _MIGRATION_7_OPERATIONS
                if "operation_id" not in attempt_columns:
                    script += _MIGRATION_7_ADD_ATTEMPT_OPERATION
                script += _MIGRATION_7
                try:
                    self._connection.executescript(
                        "BEGIN IMMEDIATE;\n" + script + "\nCOMMIT;"
                    )
                    current = 7
                except Exception:
                    try:
                        self._connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            # Repair the short-lived pre-v2 development schema where
            # completion_assessments did not yet persist the terminal bit.
            completion_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(completion_assessments)"
                ).fetchall()
            }
            if completion_columns and "terminal" not in completion_columns:
                self._connection.execute(
                    "ALTER TABLE completion_assessments "
                    "ADD COLUMN terminal INTEGER NOT NULL DEFAULT 1 CHECK (terminal IN (0, 1))"
                )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            self._ensure_open()
            cursor = self._connection.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                yield cursor
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
            finally:
                cursor.close()

    def _fetchone(self, sql: str, params: Sequence[Any]) -> sqlite3.Row | None:
        with self._lock:
            self._ensure_open()
            return self._connection.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: Sequence[Any]) -> list[sqlite3.Row]:
        with self._lock:
            self._ensure_open()
            return list(self._connection.execute(sql, params).fetchall())

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            project_id=str(row["project_id"]),
            name=str(row["name"]),
            display_path=str(row["display_path"]),
            canonical_path=str(row["canonical_path"]),
            path_identity=str(row["path_identity"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            state=str(row["state"] if "state" in row.keys() else "active") or "active",
            metadata=_load_json(row["metadata_json"]),
        )

    @staticmethod
    def _conversation_binding_from_row(row: sqlite3.Row) -> ConversationBindingRecord:
        return ConversationBindingRecord(
            session_id=str(row["session_id"]),
            project_id=str(row["project_id"]),
            anchor_work_item_id=str(row["anchor_work_item_id"] or ""),
            binding_kind=str(row["binding_kind"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            metadata=_load_json(row["metadata_json"]),
        )

    @staticmethod
    def _session_work_context_from_row(row: sqlite3.Row) -> SessionWorkContextRecord:
        return SessionWorkContextRecord(
            session_id=str(row["session_id"]),
            active_work_item_id=str(row["active_work_item_id"] or ""),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            metadata=_load_json(row["metadata_json"]),
        )

    @staticmethod
    def _work_item_from_row(row: sqlite3.Row) -> WorkItemRecord:
        return WorkItemRecord(
            work_item_id=str(row["work_item_id"]),
            project_id=str(row["project_id"]),
            title=str(row["title"]),
            goal=str(row["goal"]),
            state=str(row["state"]),  # type: ignore[arg-type]
            workspace_mode=str(row["workspace_mode"]),
            workspace_path=str(row["workspace_path"]),
            workspace_identity=str(row["workspace_identity"]),
            branch=str(row["branch"]),
            base_revision=str(row["base_revision"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            last_activity_at=float(row["last_activity_at"]),
            metadata=_load_json(row["metadata_json"]),
        )

    @staticmethod
    def _operation_from_row(row: sqlite3.Row) -> WorkOperationRecord:
        return WorkOperationRecord(
            operation_id=str(row["operation_id"]),
            work_item_id=str(row["work_item_id"]),
            operation_number=int(row["operation_number"]),
            intent=str(row["intent"]),
            instruction=str(row["instruction"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            metadata=_load_json(row["metadata_json"]),
        )

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> RunAttemptRecord:
        return RunAttemptRecord(
            attempt_id=str(row["attempt_id"]),
            work_item_id=str(row["work_item_id"]),
            operation_id=(
                str(row["operation_id"] or "")
                if "operation_id" in row.keys()
                else ""
            ),
            attempt_number=int(row["attempt_number"]),
            provider=str(row["provider"]),
            provider_run_id=str(row["provider_run_id"] or ""),
            task=str(row["task"]),
            mode=str(row["mode"]),
            execution_status=str(row["execution_status"]),  # type: ignore[arg-type]
            result=str(row["result"]),
            error=str(row["error"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            started_at=float(row["started_at"]) if row["started_at"] is not None else None,
            finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
            metadata=_load_json(row["metadata_json"]),
        )

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=str(row["artifact_id"]),
            work_item_id=str(row["work_item_id"]),
            attempt_id=str(row["attempt_id"] or ""),
            kind=str(row["kind"]),
            title=str(row["title"]),
            uri=str(row["uri"]),
            path=str(row["path"]),
            path_identity=str(row["path_identity"]),
            location=str(row["location"]),  # type: ignore[arg-type]
            status=str(row["status"]),  # type: ignore[arg-type]
            sha256=str(row["sha256"]),
            size_bytes=int(row["size_bytes"]) if row["size_bytes"] is not None else None,
            modified_at=float(row["modified_at"]) if row["modified_at"] is not None else None,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            metadata=_load_json(row["metadata_json"]),
        )

    @staticmethod
    def _permission_request_from_row(row: sqlite3.Row) -> PermissionRequestRecord:
        return PermissionRequestRecord(
            request_id=str(row["request_id"]),
            work_item_id=str(row["work_item_id"]),
            attempt_id=str(row["attempt_id"]),
            idempotency_key=str(row["idempotency_key"] or ""),
            capability=str(row["capability"]),
            action=str(row["action"]),
            scope_paths=_load_string_list(row["scope_paths_json"]),
            reason=str(row["reason"]),
            reversibility=str(row["reversibility"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            options=_load_string_list(row["options_json"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            resolved_at=float(row["resolved_at"]) if row["resolved_at"] is not None else None,
            metadata=_load_json(row["metadata_json"]),
        )

    @staticmethod
    def _assessment_from_row(row: sqlite3.Row) -> CompletionAssessmentRecord:
        return CompletionAssessmentRecord(
            assessment_id=str(row["assessment_id"]),
            work_item_id=str(row["work_item_id"]),
            attempt_id=str(row["attempt_id"] or ""),
            source=str(row["source"]),
            execution_status=str(row["execution_status"]),  # type: ignore[arg-type]
            completeness=str(row["completeness"]),  # type: ignore[arg-type]
            attention=str(row["attention"]),  # type: ignore[arg-type]
            work_item_state=str(row["work_item_state"]),  # type: ignore[arg-type]
            rationale=str(row["rationale"]),
            terminal=bool(row["terminal"]),
            evidence=_load_json(row["evidence_json"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _focus_from_row(row: sqlite3.Row) -> FocusRecord:
        return FocusRecord(
            surface=str(row["surface"]),
            work_item_id=str(row["work_item_id"] or ""),
            mode=str(row["mode"]),  # type: ignore[arg-type]
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _workspace_lease_from_row(row: sqlite3.Row) -> WorkspaceLeaseRecord:
        return WorkspaceLeaseRecord(
            lease_id=str(row["lease_id"]),
            workspace_path=str(row["workspace_path"]),
            workspace_identity=str(row["workspace_identity"]),
            work_item_id=str(row["work_item_id"]),
            attempt_id=str(row["attempt_id"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            acquired_at=float(row["acquired_at"]),
            heartbeat_at=float(row["heartbeat_at"]),
            released_at=float(row["released_at"]) if row["released_at"] is not None else None,
            metadata=_load_json(row["metadata_json"]),
        )

    # -- Project ---------------------------------------------------------

    def create_or_get_project(
        self,
        path: str | os.PathLike[str],
        *,
        name: str = "",
        metadata: dict[str, Any] | None = None,
        project_id: str = "",
    ) -> ProjectRecord:
        canonical = canonicalize_path(path)
        now = float(self._clock())
        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM projects WHERE path_identity = ?",
                (canonical.identity_key,),
            ).fetchone()
            if row is not None:
                next_metadata = _merged_json(row["metadata_json"], metadata)
                next_name = str(name).strip() or str(row["name"])
                cursor.execute(
                    "UPDATE projects SET name = ?, updated_at = ?, metadata_json = ? WHERE project_id = ?",
                    (next_name, now, next_metadata, row["project_id"]),
                )
                row = cursor.execute(
                    "SELECT * FROM projects WHERE project_id = ?", (row["project_id"],)
                ).fetchone()
                assert row is not None
                return self._project_from_row(row)

            next_id = str(project_id or new_ledger_id("project"))
            next_name = str(name).strip() or project_name_from_path(canonical.canonical_path)
            try:
                cursor.execute(
                    """
                    INSERT INTO projects (
                        project_id, name, display_path, canonical_path, path_identity,
                        created_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        next_id,
                        next_name,
                        canonical.display_path,
                        canonical.canonical_path,
                        canonical.identity_key,
                        now,
                        now,
                        _dump_json(metadata),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkLedgerConflict(f"project id or path already exists: {next_id}") from exc
            row = cursor.execute("SELECT * FROM projects WHERE project_id = ?", (next_id,)).fetchone()
            assert row is not None
            return self._project_from_row(row)

    def get_project(self, project_id: str) -> ProjectRecord | None:
        row = self._fetchone("SELECT * FROM projects WHERE project_id = ?", (str(project_id),))
        return self._project_from_row(row) if row is not None else None

    def get_project_by_path(self, path: str | os.PathLike[str]) -> ProjectRecord | None:
        identity = canonicalize_path(path).identity_key
        row = self._fetchone("SELECT * FROM projects WHERE path_identity = ?", (identity,))
        return self._project_from_row(row) if row is not None else None

    def list_projects(self, *, include_retired: bool = False) -> list[ProjectRecord]:
        """Places work can be sent. Retired ones are excluded by default.

        Every caller that builds a menu wants the active set, and the one that
        wants everything says so -- the safe default here is the smaller list,
        because the failure being designed out is a menu that only grows.
        """

        sql = "SELECT * FROM projects{where} ORDER BY updated_at DESC, project_id"
        rows = self._fetchall(
            sql.format(where="" if include_retired else " WHERE state = 'active'"),
            (),
        )
        return [self._project_from_row(row) for row in rows]

    def set_project_state(self, project_id: str, state: str) -> ProjectRecord:
        if state not in PROJECT_STATES:
            raise ValueError(f"unsupported project state: {state!r}")
        now = float(self._clock())
        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM projects WHERE project_id = ?", (str(project_id),)
            ).fetchone()
            if row is None:
                raise WorkLedgerNotFound(f"unknown project: {project_id}")
            cursor.execute(
                "UPDATE projects SET state = ?, updated_at = ? WHERE project_id = ?",
                (state, now, str(project_id)),
            )
            row = cursor.execute(
                "SELECT * FROM projects WHERE project_id = ?", (str(project_id),)
            ).fetchone()
            assert row is not None
            return self._project_from_row(row)

    # -- Conversation context ------------------------------------------

    def bind_conversation(
        self,
        session_id: str,
        project_id: str,
        *,
        anchor_work_item_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ConversationBindingRecord:
        """Create or replace one chat's explicit Project/WorkItem context."""

        clean_session = str(session_id or "").strip()
        clean_project = str(project_id or "").strip()
        clean_anchor = str(anchor_work_item_id or "").strip()
        if not clean_session:
            raise ValueError("session_id is required")
        if not clean_project:
            raise ValueError("project_id is required")
        kind = "work_item" if clean_anchor else "project"
        if kind not in CONVERSATION_BINDING_KINDS:
            raise ValueError(f"unsupported conversation binding kind: {kind!r}")
        now = float(self._clock())
        with self._transaction() as cursor:
            project = cursor.execute(
                "SELECT 1 FROM projects WHERE project_id = ?",
                (clean_project,),
            ).fetchone()
            if project is None:
                raise WorkLedgerNotFound(f"unknown project: {clean_project}")
            if clean_anchor:
                item = cursor.execute(
                    "SELECT project_id FROM work_items WHERE work_item_id = ?",
                    (clean_anchor,),
                ).fetchone()
                if item is None:
                    raise WorkLedgerNotFound(f"unknown work item: {clean_anchor}")
                if str(item["project_id"]) != clean_project:
                    raise WorkLedgerConflict(
                        "conversation anchor belongs to a different project"
                    )
            existing = cursor.execute(
                "SELECT * FROM conversation_bindings WHERE session_id = ?",
                (clean_session,),
            ).fetchone()
            if existing is None:
                cursor.execute(
                    """
                    INSERT INTO conversation_bindings (
                        session_id, project_id, anchor_work_item_id, binding_kind,
                        created_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_session,
                        clean_project,
                        clean_anchor or None,
                        kind,
                        now,
                        now,
                        _dump_json(metadata),
                    ),
                )
            else:
                cursor.execute(
                    """
                    UPDATE conversation_bindings
                    SET project_id = ?, anchor_work_item_id = ?, binding_kind = ?,
                        updated_at = ?, metadata_json = ?
                    WHERE session_id = ?
                    """,
                    (
                        clean_project,
                        clean_anchor or None,
                        kind,
                        now,
                        _merged_json(existing["metadata_json"], metadata),
                        clean_session,
                    ),
                )
            row = cursor.execute(
                "SELECT * FROM conversation_bindings WHERE session_id = ?",
                (clean_session,),
            ).fetchone()
            assert row is not None
            return self._conversation_binding_from_row(row)

    def get_conversation_binding(self, session_id: str) -> ConversationBindingRecord | None:
        row = self._fetchone(
            "SELECT * FROM conversation_bindings WHERE session_id = ?",
            (str(session_id or "").strip(),),
        )
        return self._conversation_binding_from_row(row) if row is not None else None

    def clear_conversation_binding(self, session_id: str) -> bool:
        clean_session = str(session_id or "").strip()
        if not clean_session:
            return False
        with self._transaction() as cursor:
            cursor.execute(
                "DELETE FROM conversation_bindings WHERE session_id = ?",
                (clean_session,),
            )
            return cursor.rowcount > 0

    def set_session_active_work_item(
        self,
        session_id: str,
        work_item_id: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> SessionWorkContextRecord:
        """Set the Session's narrow WorkItem referent without changing Project.

        Project destination and active work are intentionally stored in
        separate rows. This permits a Session bound to Project A to foreground
        a one-off Draft, then return to Project A for the next unplaced goal.
        """

        clean_session = str(session_id or "").strip()
        clean_work_item = str(work_item_id or "").strip()
        if not clean_session:
            raise ValueError("session_id is required")
        if not clean_work_item:
            raise ValueError("work_item_id is required")
        now = float(self._clock())
        with self._transaction() as cursor:
            item = cursor.execute(
                "SELECT 1 FROM work_items WHERE work_item_id = ?",
                (clean_work_item,),
            ).fetchone()
            if item is None:
                raise WorkLedgerNotFound(f"unknown work item: {clean_work_item}")
            existing = cursor.execute(
                "SELECT * FROM session_work_contexts WHERE session_id = ?",
                (clean_session,),
            ).fetchone()
            if existing is None:
                cursor.execute(
                    """
                    INSERT INTO session_work_contexts (
                        session_id, active_work_item_id, created_at, updated_at,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        clean_session,
                        clean_work_item,
                        now,
                        now,
                        _dump_json(metadata),
                    ),
                )
            else:
                cursor.execute(
                    """
                    UPDATE session_work_contexts
                    SET active_work_item_id = ?, updated_at = ?, metadata_json = ?
                    WHERE session_id = ?
                    """,
                    (
                        clean_work_item,
                        now,
                        _merged_json(existing["metadata_json"], metadata),
                        clean_session,
                    ),
                )
            row = cursor.execute(
                "SELECT * FROM session_work_contexts WHERE session_id = ?",
                (clean_session,),
            ).fetchone()
            assert row is not None
            return self._session_work_context_from_row(row)

    def get_session_work_context(
        self,
        session_id: str,
    ) -> SessionWorkContextRecord | None:
        row = self._fetchone(
            "SELECT * FROM session_work_contexts WHERE session_id = ?",
            (str(session_id or "").strip(),),
        )
        return self._session_work_context_from_row(row) if row is not None else None

    def clear_session_active_work_item(self, session_id: str) -> bool:
        clean_session = str(session_id or "").strip()
        if not clean_session:
            return False
        with self._transaction() as cursor:
            cursor.execute(
                "DELETE FROM session_work_contexts WHERE session_id = ?",
                (clean_session,),
            )
            return cursor.rowcount > 0

    # -- WorkItem --------------------------------------------------------

    def create_work_item(
        self,
        project_id: str,
        *,
        title: str,
        goal: str = "",
        workspace_mode: str = "local",
        workspace_path: str | os.PathLike[str] | None = None,
        branch: str = "",
        base_revision: str = "",
        metadata: dict[str, Any] | None = None,
        work_item_id: str = "",
    ) -> WorkItemRecord:
        clean_title = str(title or "").strip()
        if not clean_title:
            raise ValueError("work item title is required")
        clean_workspace_mode = str(workspace_mode or "local").strip().lower()
        if not clean_workspace_mode:
            raise ValueError("workspace_mode is required")
        next_id = str(work_item_id or new_ledger_id("work"))
        if clean_workspace_mode == "none":
            if str(workspace_path or "").strip():
                raise ValueError("workspace_mode 'none' cannot carry a workspace path")
            workspace_value = ""
            # There is deliberately no filesystem identity here.  A unique
            # ledger identity prevents future code from treating all
            # workspace-less tasks as concurrent writers of one fake path.
            workspace_identity = f"none:{next_id}"
        else:
            workspace_value = ""
            workspace_identity = ""
        now = float(self._clock())
        with self._transaction() as cursor:
            project = cursor.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
            if project is None:
                raise WorkLedgerNotFound(f"unknown project: {project_id}")
            if clean_workspace_mode != "none":
                workspace = canonicalize_path(
                    workspace_path or str(project["canonical_path"])
                )
                workspace_value = workspace.canonical_path
                workspace_identity = workspace.identity_key
            try:
                cursor.execute(
                    """
                    INSERT INTO work_items (
                        work_item_id, project_id, title, goal, state,
                        workspace_mode, workspace_path, workspace_identity, branch, base_revision,
                        created_at, updated_at, last_activity_at, metadata_json
                    ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        next_id,
                        str(project_id),
                        clean_title,
                        str(goal or "").strip(),
                        clean_workspace_mode,
                        workspace_value,
                        workspace_identity,
                        str(branch or "").strip(),
                        str(base_revision or "").strip(),
                        now,
                        now,
                        now,
                        _dump_json(metadata),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkLedgerConflict(f"work item already exists: {next_id}") from exc
            row = cursor.execute("SELECT * FROM work_items WHERE work_item_id = ?", (next_id,)).fetchone()
            assert row is not None
            return self._work_item_from_row(row)

    def get_work_item(self, work_item_id: str) -> WorkItemRecord | None:
        row = self._fetchone("SELECT * FROM work_items WHERE work_item_id = ?", (str(work_item_id),))
        return self._work_item_from_row(row) if row is not None else None

    def list_work_items(
        self,
        *,
        project_id: str = "",
        states: Sequence[str] | None = None,
        limit: int = 200,
    ) -> list[WorkItemRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(str(project_id))
        if states:
            clean_states = [str(state) for state in states]
            invalid = [state for state in clean_states if state not in WORK_ITEM_STATES]
            if invalid:
                raise ValueError(f"unsupported work item state: {invalid[0]!r}")
            clauses.append("state IN (" + ",".join("?" for _ in clean_states) + ")")
            params.extend(clean_states)
        sql = "SELECT * FROM work_items"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY last_activity_at DESC, work_item_id LIMIT ?"
        params.append(max(1, min(int(limit), 2000)))
        return [self._work_item_from_row(row) for row in self._fetchall(sql, params)]

    def find_work_item_ids_by_title_match(self, text: str, *, limit: int = 64) -> list[str]:
        """Work items whose title contains ``text``, newest activity first.

        Recall-oriented: the caller re-verifies with its own exact reference
        matching, so a substring superset here is correct and a miss is not.
        ASCII case folds the way SQLite LIKE folds it; other scripts compare
        literally, which is also what the Python-side ``.lower()`` does.
        """

        clean = str(text or "").strip()
        if not clean:
            return []
        rows = self._fetchall(
            "SELECT work_item_id FROM work_items "
            "WHERE title LIKE '%' || ? || '%' ESCAPE '!' "
            "ORDER BY last_activity_at DESC, work_item_id LIMIT ?",
            (_escape_like(clean), max(1, min(int(limit), 200))),
        )
        return [str(row["work_item_id"]) for row in rows]

    def set_work_item_state(
        self,
        work_item_id: str,
        state: WorkItemState,
        *,
        expected_state: WorkItemState | None = None,
    ) -> WorkItemRecord:
        if state not in WORK_ITEM_STATES:
            raise ValueError(f"unsupported work item state: {state!r}")
        now = float(self._clock())
        with self._transaction() as cursor:
            row = cursor.execute("SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)).fetchone()
            if row is None:
                raise WorkLedgerNotFound(f"unknown work item: {work_item_id}")
            if expected_state is not None and str(row["state"]) != expected_state:
                raise WorkLedgerConflict(
                    f"work item {work_item_id} is {row['state']}, expected {expected_state}"
                )
            cursor.execute(
                "UPDATE work_items SET state = ?, updated_at = ?, last_activity_at = ? WHERE work_item_id = ?",
                (state, now, now, work_item_id),
            )
            row = cursor.execute("SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)).fetchone()
            assert row is not None
            return self._work_item_from_row(row)

    def reassign_workspace_to_project(
        self,
        workspace_path: str | os.PathLike[str],
        project_id: str,
    ) -> int:
        """File every task that ran in one directory under a project.

        A project is a place, and which project a task belongs to follows from
        where it ran -- so when a directory becomes a project, everything that
        ever happened there belongs to it. Re-filing only the task the user
        happened to click would leave its siblings, and often the very task that
        created the place, outside the project they are plainly part of.

        Activity timestamps are left alone: this records where work lives, and
        nothing about it was worked on just now.
        """

        identity = canonicalize_path(workspace_path).identity_key
        now = float(self._clock())
        with self._transaction() as cursor:
            if cursor.execute(
                "SELECT 1 FROM projects WHERE project_id = ?", (str(project_id),)
            ).fetchone() is None:
                raise WorkLedgerNotFound(f"unknown project: {project_id}")
            rows = cursor.execute(
                "SELECT work_item_id, workspace_path FROM work_items WHERE project_id != ?",
                (str(project_id),),
            ).fetchall()
            targets = [
                str(row["work_item_id"])
                for row in rows
                if str(row["workspace_path"] or "").strip()
                and canonicalize_path(row["workspace_path"]).identity_key == identity
            ]
            for work_item_id in targets:
                cursor.execute(
                    "UPDATE work_items SET project_id = ?, updated_at = ? WHERE work_item_id = ?",
                    (str(project_id), now, work_item_id),
                )
            return len(targets)

    def update_work_item_metadata(
        self,
        work_item_id: str,
        metadata: dict[str, Any],
        *,
        touch_activity: bool = False,
    ) -> WorkItemRecord:
        """Merge metadata without treating presentation churn as user work.

        Canvas/presentation snapshots may be stored under a metadata key such
        as ``presentation``.  By default they advance ``updated_at`` but keep
        ``last_activity_at`` stable, so a renderer refresh does not reorder the
        user's task history.  Semantic mutations can opt into activity touch.
        """

        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        now = float(self._clock())
        with self._transaction() as cursor:
            row = cursor.execute("SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)).fetchone()
            if row is None:
                raise WorkLedgerNotFound(f"unknown work item: {work_item_id}")
            last_activity_at = now if touch_activity else float(row["last_activity_at"])
            cursor.execute(
                """
                UPDATE work_items
                SET metadata_json = ?, updated_at = ?, last_activity_at = ?
                WHERE work_item_id = ?
                """,
                (
                    _merged_json(row["metadata_json"], metadata),
                    now,
                    last_activity_at,
                    work_item_id,
                ),
            )
            row = cursor.execute("SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)).fetchone()
            assert row is not None
            return self._work_item_from_row(row)

    # -- WorkOperation / RunAttempt -------------------------------------

    def create_operation(
        self,
        work_item_id: str,
        *,
        intent: str,
        instruction: str,
        metadata: dict[str, Any] | None = None,
        operation_id: str = "",
    ) -> WorkOperationRecord:
        """Append one semantic instruction without starting a Provider.

        Runtime intake normally uses :meth:`create_operation_attempt` so the
        instruction and its first execution are atomic. This narrower method
        is retained for import/repair tooling and deterministic store tests.
        """

        clean_intent = str(intent or "").strip().lower()
        clean_instruction = str(instruction or "").strip()
        if clean_intent not in WORK_OPERATION_INTENTS:
            raise ValueError(f"unsupported work operation intent: {intent!r}")
        if not clean_instruction:
            raise ValueError("operation instruction is required")
        now = float(self._clock())
        with self._transaction() as cursor:
            item = cursor.execute(
                "SELECT * FROM work_items WHERE work_item_id = ?",
                (str(work_item_id),),
            ).fetchone()
            if item is None:
                raise WorkLedgerNotFound(f"unknown work item: {work_item_id}")
            if str(item["state"]) == "archived":
                raise WorkLedgerConflict(
                    f"work item {work_item_id} must be reopened before adding an operation"
                )
            number_row = cursor.execute(
                "SELECT COALESCE(MAX(operation_number), 0) + 1 AS next_number "
                "FROM work_operations WHERE work_item_id = ?",
                (str(work_item_id),),
            ).fetchone()
            next_number = int(number_row["next_number"] if number_row else 1)
            next_id = str(operation_id or new_ledger_id("operation"))
            try:
                cursor.execute(
                    """
                    INSERT INTO work_operations (
                        operation_id, work_item_id, operation_number, intent,
                        instruction, created_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        next_id,
                        str(work_item_id),
                        next_number,
                        clean_intent,
                        clean_instruction,
                        now,
                        now,
                        _dump_json(metadata),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkLedgerConflict(
                    f"work operation already exists: {next_id}"
                ) from exc
            cursor.execute(
                """
                UPDATE work_items
                SET state = 'open', updated_at = ?, last_activity_at = ?
                WHERE work_item_id = ?
                """,
                (now, now, str(work_item_id)),
            )
            row = cursor.execute(
                "SELECT * FROM work_operations WHERE operation_id = ?",
                (next_id,),
            ).fetchone()
            assert row is not None
            return self._operation_from_row(row)

    def get_operation(self, operation_id: str) -> WorkOperationRecord | None:
        row = self._fetchone(
            "SELECT * FROM work_operations WHERE operation_id = ?",
            (str(operation_id),),
        )
        return self._operation_from_row(row) if row is not None else None

    def list_operations(self, work_item_id: str) -> list[WorkOperationRecord]:
        rows = self._fetchall(
            "SELECT * FROM work_operations WHERE work_item_id = ? "
            "ORDER BY operation_number",
            (str(work_item_id),),
        )
        return [self._operation_from_row(row) for row in rows]

    def create_operation_attempt(
        self,
        work_item_id: str,
        *,
        intent: str,
        instruction: str,
        provider: str,
        task: str,
        mode: str = "agent",
        provider_run_id: str = "",
        operation_metadata: dict[str, Any] | None = None,
        attempt_metadata: dict[str, Any] | None = None,
        operation_id: str = "",
        attempt_id: str = "",
    ) -> tuple[WorkOperationRecord, RunAttemptRecord]:
        """Atomically append an Operation and its first queued Attempt."""

        clean_intent = str(intent or "").strip().lower()
        clean_instruction = str(instruction or "").strip()
        clean_provider = str(provider or "").strip().lower()
        clean_task = str(task or "").strip()
        if clean_intent not in WORK_OPERATION_INTENTS:
            raise ValueError(f"unsupported work operation intent: {intent!r}")
        if not clean_instruction:
            raise ValueError("operation instruction is required")
        if not clean_provider:
            raise ValueError("provider is required")
        if not clean_task:
            raise ValueError("task is required")
        now = float(self._clock())
        with self._transaction() as cursor:
            item = cursor.execute(
                "SELECT * FROM work_items WHERE work_item_id = ?",
                (str(work_item_id),),
            ).fetchone()
            if item is None:
                raise WorkLedgerNotFound(f"unknown work item: {work_item_id}")
            if str(item["state"]) == "archived":
                raise WorkLedgerConflict(
                    f"work item {work_item_id} must be reopened before adding an operation"
                )
            operation_number_row = cursor.execute(
                "SELECT COALESCE(MAX(operation_number), 0) + 1 AS next_number "
                "FROM work_operations WHERE work_item_id = ?",
                (str(work_item_id),),
            ).fetchone()
            attempt_number_row = cursor.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS next_number "
                "FROM run_attempts WHERE work_item_id = ?",
                (str(work_item_id),),
            ).fetchone()
            next_operation_number = int(
                operation_number_row["next_number"] if operation_number_row else 1
            )
            next_attempt_number = int(
                attempt_number_row["next_number"] if attempt_number_row else 1
            )
            next_operation_id = str(operation_id or new_ledger_id("operation"))
            next_attempt_id = str(attempt_id or new_ledger_id("attempt"))
            try:
                cursor.execute(
                    """
                    INSERT INTO work_operations (
                        operation_id, work_item_id, operation_number, intent,
                        instruction, created_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        next_operation_id,
                        str(work_item_id),
                        next_operation_number,
                        clean_intent,
                        clean_instruction,
                        now,
                        now,
                        _dump_json(operation_metadata),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO run_attempts (
                        attempt_id, work_item_id, operation_id, attempt_number,
                        provider, provider_run_id, task, mode, execution_status,
                        created_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                    """,
                    (
                        next_attempt_id,
                        str(work_item_id),
                        next_operation_id,
                        next_attempt_number,
                        clean_provider,
                        str(provider_run_id or "").strip(),
                        clean_task,
                        str(mode or "agent").strip() or "agent",
                        now,
                        now,
                        _dump_json(attempt_metadata),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkLedgerConflict(
                    "work item already has an active attempt, or the operation/attempt binding already exists"
                ) from exc
            cursor.execute(
                """
                UPDATE work_items
                SET state = 'open', updated_at = ?, last_activity_at = ?
                WHERE work_item_id = ?
                """,
                (now, now, str(work_item_id)),
            )
            operation_row = cursor.execute(
                "SELECT * FROM work_operations WHERE operation_id = ?",
                (next_operation_id,),
            ).fetchone()
            attempt_row = cursor.execute(
                "SELECT * FROM run_attempts WHERE attempt_id = ?",
                (next_attempt_id,),
            ).fetchone()
            assert operation_row is not None and attempt_row is not None
            return (
                self._operation_from_row(operation_row),
                self._attempt_from_row(attempt_row),
            )

    def create_attempt(
        self,
        work_item_id: str,
        *,
        provider: str,
        task: str,
        mode: str = "agent",
        provider_run_id: str = "",
        metadata: dict[str, Any] | None = None,
        attempt_id: str = "",
        operation_id: str = "",
    ) -> RunAttemptRecord:
        clean_provider = str(provider or "").strip().lower()
        clean_task = str(task or "").strip()
        if not clean_provider:
            raise ValueError("provider is required")
        if not clean_task:
            raise ValueError("task is required")
        now = float(self._clock())
        with self._transaction() as cursor:
            item = cursor.execute("SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)).fetchone()
            if item is None:
                raise WorkLedgerNotFound(f"unknown work item: {work_item_id}")
            if str(item["state"]) in {"accepted", "archived"}:
                raise WorkLedgerConflict(
                    f"work item {work_item_id} must be reopened before creating another attempt"
                )
            clean_operation_id = str(operation_id or "").strip()
            operation = None
            if clean_operation_id:
                operation = cursor.execute(
                    "SELECT * FROM work_operations WHERE operation_id = ?",
                    (clean_operation_id,),
                ).fetchone()
                if operation is None:
                    raise WorkLedgerNotFound(
                        f"unknown work operation: {clean_operation_id}"
                    )
                if str(operation["work_item_id"]) != str(work_item_id):
                    raise WorkLedgerConflict(
                        "attempt operation belongs to a different work item"
                    )
            else:
                operation = cursor.execute(
                    "SELECT * FROM work_operations WHERE work_item_id = ? "
                    "ORDER BY operation_number DESC LIMIT 1",
                    (str(work_item_id),),
                ).fetchone()
                if operation is None:
                    # Compatibility for store/import callers that predate the
                    # Operation boundary. Runtime intake always creates its
                    # Operation explicitly and atomically.
                    clean_operation_id = new_ledger_id("operation")
                    cursor.execute(
                        """
                        INSERT INTO work_operations (
                            operation_id, work_item_id, operation_number, intent,
                            instruction, created_at, updated_at, metadata_json
                        ) VALUES (?, ?, 1, 'execute', ?, ?, ?, ?)
                        """,
                        (
                            clean_operation_id,
                            str(work_item_id),
                            clean_task,
                            now,
                            now,
                            _dump_json({"source": "legacy_create_attempt"}),
                        ),
                    )
                else:
                    clean_operation_id = str(operation["operation_id"])
            row = cursor.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS next_number "
                "FROM run_attempts WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            next_number = int(row["next_number"] if row is not None else 1)
            next_id = str(attempt_id or new_ledger_id("attempt"))
            try:
                cursor.execute(
                    """
                    INSERT INTO run_attempts (
                        attempt_id, work_item_id, operation_id, attempt_number, provider, provider_run_id,
                        task, mode, execution_status, created_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                    """,
                    (
                        next_id,
                        work_item_id,
                        clean_operation_id,
                        next_number,
                        clean_provider,
                        str(provider_run_id or "").strip(),
                        clean_task,
                        str(mode or "agent").strip() or "agent",
                        now,
                        now,
                        _dump_json(metadata),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkLedgerConflict(
                    "work item already has an active attempt, or the attempt binding already exists"
                ) from exc
            cursor.execute(
                """
                UPDATE work_items
                SET state = 'open', updated_at = ?, last_activity_at = ?
                WHERE work_item_id = ?
                """,
                (now, now, work_item_id),
            )
            row = cursor.execute("SELECT * FROM run_attempts WHERE attempt_id = ?", (next_id,)).fetchone()
            assert row is not None
            return self._attempt_from_row(row)

    def get_attempt(self, attempt_id: str) -> RunAttemptRecord | None:
        row = self._fetchone("SELECT * FROM run_attempts WHERE attempt_id = ?", (str(attempt_id),))
        return self._attempt_from_row(row) if row is not None else None

    def get_attempt_by_provider_run(self, provider_run_id: str) -> RunAttemptRecord | None:
        clean = str(provider_run_id or "").strip()
        if not clean:
            return None
        row = self._fetchone("SELECT * FROM run_attempts WHERE provider_run_id = ?", (clean,))
        return self._attempt_from_row(row) if row is not None else None

    def list_attempts(self, work_item_id: str) -> list[RunAttemptRecord]:
        rows = self._fetchall(
            "SELECT * FROM run_attempts WHERE work_item_id = ? ORDER BY attempt_number",
            (str(work_item_id),),
        )
        return [self._attempt_from_row(row) for row in rows]

    def bind_provider_run(self, attempt_id: str, provider_run_id: str) -> RunAttemptRecord:
        clean_run_id = str(provider_run_id or "").strip()
        if not clean_run_id:
            raise ValueError("provider_run_id is required")
        now = float(self._clock())
        with self._transaction() as cursor:
            row = cursor.execute("SELECT * FROM run_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if row is None:
                raise WorkLedgerNotFound(f"unknown attempt: {attempt_id}")
            existing = str(row["provider_run_id"] or "")
            if existing and existing != clean_run_id:
                raise WorkLedgerConflict(f"attempt {attempt_id} is already bound to {existing}")
            try:
                cursor.execute(
                    "UPDATE run_attempts SET provider_run_id = ?, updated_at = ? WHERE attempt_id = ?",
                    (clean_run_id, now, attempt_id),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkLedgerConflict(f"provider run is already bound: {clean_run_id}") from exc
            row = cursor.execute("SELECT * FROM run_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            assert row is not None
            return self._attempt_from_row(row)

    def update_attempt(
        self,
        attempt_id: str,
        *,
        execution_status: ExecutionStatus | None = None,
        result: str | object = _UNSET,
        error: str | object = _UNSET,
        metadata: dict[str, Any] | None = None,
    ) -> RunAttemptRecord:
        if execution_status is not None and execution_status not in EXECUTION_STATUSES:
            raise ValueError(f"unsupported execution status: {execution_status!r}")
        now = float(self._clock())
        with self._transaction() as cursor:
            row = cursor.execute("SELECT * FROM run_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            if row is None:
                raise WorkLedgerNotFound(f"unknown attempt: {attempt_id}")
            previous = str(row["execution_status"])
            next_status = str(execution_status or previous)
            if previous in _TERMINAL_EXECUTION and next_status != previous:
                raise WorkLedgerConflict(
                    f"terminal attempt {attempt_id} cannot change from {previous} to {next_status}"
                )
            started_at = row["started_at"]
            if next_status == "running" and started_at is None:
                started_at = now
            finished_at = row["finished_at"]
            if next_status in _TERMINAL_EXECUTION and finished_at is None:
                finished_at = now
            next_result = str(row["result"]) if result is _UNSET else str(result or "")
            next_error = str(row["error"]) if error is _UNSET else str(error or "")
            try:
                cursor.execute(
                    """
                    UPDATE run_attempts
                    SET execution_status = ?, result = ?, error = ?, updated_at = ?,
                        started_at = ?, finished_at = ?, metadata_json = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        next_status,
                        next_result,
                        next_error,
                        now,
                        started_at,
                        finished_at,
                        _merged_json(row["metadata_json"], metadata),
                        attempt_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkLedgerConflict(
                    f"work item already has another active attempt; cannot mark {attempt_id} {next_status}"
                ) from exc
            cursor.execute(
                "UPDATE work_items SET updated_at = ?, last_activity_at = ? WHERE work_item_id = ?",
                (now, now, row["work_item_id"]),
            )
            row = cursor.execute("SELECT * FROM run_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            assert row is not None
            return self._attempt_from_row(row)

    def merge_attempt_control_metadata(
        self,
        attempt_id: str,
        metadata: dict[str, Any],
    ) -> RunAttemptRecord:
        """Persist hidden control-plane facts without fabricating activity.

        Delivery cursors and similar receipts must survive restart, but their
        persistence is not user work, Provider progress, or a lifecycle edge.
        Preserve Attempt and WorkItem timestamps so history ordering and
        visible activity clocks remain owned by material events.
        """

        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM run_attempts WHERE attempt_id = ?",
                (str(attempt_id),),
            ).fetchone()
            if row is None:
                raise WorkLedgerNotFound(f"unknown attempt: {attempt_id}")
            cursor.execute(
                "UPDATE run_attempts SET metadata_json = ? WHERE attempt_id = ?",
                (
                    _merged_json(row["metadata_json"], metadata),
                    str(attempt_id),
                ),
            )
            row = cursor.execute(
                "SELECT * FROM run_attempts WHERE attempt_id = ?",
                (str(attempt_id),),
            ).fetchone()
            assert row is not None
            return self._attempt_from_row(row)

    def compare_and_set_attempt_metadata(
        self,
        attempt_id: str,
        *,
        key: str,
        expected_present: bool,
        expected_value: Any = None,
        value: Any,
    ) -> tuple[RunAttemptRecord, bool]:
        """Atomically replace one top-level Attempt metadata fact.

        Recovery and delivery state machines must not use a read followed by a
        merge update: a repeated event can otherwise restore an older state
        between those operations.  This narrow compare-and-set keeps the
        durable Attempt record as the single winner without teaching the store
        the meaning of any particular metadata key.
        """

        clean_key = str(key or "").strip()
        if not clean_key or len(clean_key) > 160:
            raise ValueError("attempt metadata compare-and-set key is invalid")
        now = float(self._clock())
        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM run_attempts WHERE attempt_id = ?",
                (str(attempt_id),),
            ).fetchone()
            if row is None:
                raise WorkLedgerNotFound(f"unknown attempt: {attempt_id}")
            metadata = _load_json(row["metadata_json"])
            present = clean_key in metadata
            matches = present == bool(expected_present) and (
                not present or metadata.get(clean_key) == expected_value
            )
            if not matches:
                return self._attempt_from_row(row), False
            metadata[clean_key] = value
            cursor.execute(
                """
                UPDATE run_attempts
                SET updated_at = ?, metadata_json = ?
                WHERE attempt_id = ?
                """,
                (now, _dump_json(metadata), str(attempt_id)),
            )
            cursor.execute(
                "UPDATE work_items SET updated_at = ?, last_activity_at = ? WHERE work_item_id = ?",
                (now, now, row["work_item_id"]),
            )
            row = cursor.execute(
                "SELECT * FROM run_attempts WHERE attempt_id = ?",
                (str(attempt_id),),
            ).fetchone()
            assert row is not None
            return self._attempt_from_row(row), True

    # -- Artifact --------------------------------------------------------

    def register_artifact(
        self,
        work_item_id: str,
        *,
        kind: str,
        title: str = "",
        attempt_id: str = "",
        uri: str = "",
        path: str | os.PathLike[str] | None = None,
        identity: str = "",
        location: ArtifactLocation | None = None,
        status: ArtifactStatus | None = None,
        sha256: str = "",
        size_bytes: int | None = None,
        modified_at: float | None = None,
        metadata: dict[str, Any] | None = None,
        artifact_id: str = "",
    ) -> ArtifactRecord:
        clean_kind = str(kind or "").strip()
        if not clean_kind:
            raise ValueError("artifact kind is required")
        if location is not None and location not in ARTIFACT_LOCATIONS:
            raise ValueError(f"unsupported artifact location: {location!r}")
        if status is not None and status not in ARTIFACT_STATUSES:
            raise ValueError(f"unsupported artifact status: {status!r}")
        now = float(self._clock())
        with self._transaction() as cursor:
            item = cursor.execute(
                """
                SELECT wi.*, p.canonical_path AS project_path
                FROM work_items wi JOIN projects p ON p.project_id = wi.project_id
                WHERE wi.work_item_id = ?
                """,
                (work_item_id,),
            ).fetchone()
            if item is None:
                raise WorkLedgerNotFound(f"unknown work item: {work_item_id}")
            clean_attempt_id = str(attempt_id or "").strip()
            if clean_attempt_id:
                attempt = cursor.execute(
                    "SELECT work_item_id FROM run_attempts WHERE attempt_id = ?", (clean_attempt_id,)
                ).fetchone()
                if attempt is None:
                    raise WorkLedgerNotFound(f"unknown attempt: {clean_attempt_id}")
                if str(attempt["work_item_id"]) != work_item_id:
                    raise WorkLedgerConflict("artifact attempt belongs to a different work item")

            clean_path = ""
            path_identity = ""
            if path is not None and os.fspath(path).strip():
                canonical = canonicalize_path(path)
                clean_path = canonical.canonical_path
                path_identity = canonical.identity_key
            clean_uri = str(uri or "").strip()
            if identity:
                identity_key = "explicit:" + str(identity).strip()
            elif path_identity:
                identity_key = "path:" + path_identity
            elif clean_uri:
                identity_key = "uri:" + clean_uri
            else:
                # Virtual artifacts without a stable ref are intentionally not
                # deduplicated; callers can pass ``identity`` when they need it.
                identity_key = "virtual:" + new_ledger_id("ref")

            resolved_location: ArtifactLocation
            if location is not None:
                resolved_location = location
            elif (
                clean_path
                and str(item["workspace_mode"] or "") != "none"
                and path_is_within(clean_path, str(item["workspace_path"]))
            ):
                resolved_location = "workspace"
            elif clean_path and path_is_within(clean_path, str(item["project_path"])):
                resolved_location = "project"
            elif clean_path:
                resolved_location = "external"
            else:
                resolved_location = "virtual"
            resolved_status: ArtifactStatus = status or (
                "pending" if resolved_location == "external" else "registered"
            )

            if clean_attempt_id:
                existing = cursor.execute(
                    "SELECT * FROM artifacts WHERE attempt_id = ? AND identity_key = ?",
                    (clean_attempt_id, identity_key),
                ).fetchone()
            else:
                existing = cursor.execute(
                    "SELECT * FROM artifacts WHERE work_item_id = ? AND attempt_id IS NULL AND identity_key = ?",
                    (work_item_id, identity_key),
                ).fetchone()
            clean_title = str(title or "").strip() or Path(clean_path).name or clean_kind
            if existing is not None:
                cursor.execute(
                    """
                    UPDATE artifacts
                    SET kind = ?, title = ?, uri = ?, path = ?, path_identity = ?,
                        location = ?, status = ?, sha256 = ?, size_bytes = ?, modified_at = ?,
                        updated_at = ?, metadata_json = ?
                    WHERE artifact_id = ?
                    """,
                    (
                        clean_kind,
                        clean_title,
                        clean_uri or str(existing["uri"]),
                        clean_path or str(existing["path"]),
                        path_identity or str(existing["path_identity"]),
                        resolved_location,
                        resolved_status,
                        str(sha256 or existing["sha256"]),
                        size_bytes if size_bytes is not None else existing["size_bytes"],
                        modified_at if modified_at is not None else existing["modified_at"],
                        now,
                        _merged_json(existing["metadata_json"], metadata),
                        existing["artifact_id"],
                    ),
                )
                next_id = str(existing["artifact_id"])
            else:
                next_id = str(artifact_id or new_ledger_id("artifact"))
                try:
                    cursor.execute(
                        """
                        INSERT INTO artifacts (
                            artifact_id, work_item_id, attempt_id, kind, title, uri, path,
                            path_identity, identity_key, location, status, sha256, size_bytes,
                            modified_at, created_at, updated_at, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            next_id,
                            work_item_id,
                            clean_attempt_id or None,
                            clean_kind,
                            clean_title,
                            clean_uri,
                            clean_path,
                            path_identity,
                            identity_key,
                            resolved_location,
                            resolved_status,
                            str(sha256 or ""),
                            size_bytes,
                            modified_at,
                            now,
                            now,
                            _dump_json(metadata),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise WorkLedgerConflict(f"artifact already exists: {next_id}") from exc
            cursor.execute(
                "UPDATE work_items SET updated_at = ?, last_activity_at = ? WHERE work_item_id = ?",
                (now, now, work_item_id),
            )
            row = cursor.execute("SELECT * FROM artifacts WHERE artifact_id = ?", (next_id,)).fetchone()
            assert row is not None
            return self._artifact_from_row(row)

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        row = self._fetchone("SELECT * FROM artifacts WHERE artifact_id = ?", (str(artifact_id),))
        return self._artifact_from_row(row) if row is not None else None

    def list_artifacts(self, work_item_id: str, *, attempt_id: str = "") -> list[ArtifactRecord]:
        if attempt_id:
            rows = self._fetchall(
                "SELECT * FROM artifacts WHERE work_item_id = ? AND attempt_id = ? ORDER BY updated_at DESC",
                (str(work_item_id), str(attempt_id)),
            )
        else:
            rows = self._fetchall(
                "SELECT * FROM artifacts WHERE work_item_id = ? ORDER BY updated_at DESC",
                (str(work_item_id),),
            )
        return [self._artifact_from_row(row) for row in rows]

    def find_work_item_ids_by_artifact_name(
        self,
        name: str,
        *,
        kind: str = "business.file",
        limit: int = 64,
    ) -> list[str]:
        """Work items that registered an artifact with this base filename.

        This is the index the task-lookup ladder queries: a spoken filename is
        answered from the whole artifacts table rather than from a recency
        window, so a task cannot silently fall out of reach as the ledger
        grows. Matching mirrors how the coordinator derives base names (the
        text after the last ``/`` or ``\\``); precision beyond that is the
        caller's exact filter.
        """

        clean = str(name or "").strip()
        if not clean:
            return []
        escaped = _escape_like(clean)
        rows = self._fetchall(
            "SELECT work_item_id, MAX(updated_at) AS latest FROM artifacts "
            "WHERE kind = ? AND path <> '' AND ("
            "path LIKE ? ESCAPE '!' "
            "OR path LIKE '%/' || ? ESCAPE '!' "
            "OR path LIKE '%\\' || ? ESCAPE '!'"
            ") GROUP BY work_item_id ORDER BY latest DESC LIMIT ?",
            (str(kind), escaped, escaped, escaped, max(1, min(int(limit), 200))),
        )
        return [str(row["work_item_id"]) for row in rows]

    def find_work_item_ids_by_artifact_path(
        self,
        path: str | os.PathLike[str],
        *,
        kind: str = "",
        status: str = "",
        limit: int = 64,
    ) -> list[str]:
        """Work items owning an artifact at one exact canonical path."""

        identity = canonicalize_path(path).identity_key
        clauses = ["path_identity = ?"]
        params: list[Any] = [identity]
        if kind:
            clauses.append("kind = ?")
            params.append(str(kind))
        if status:
            if status not in ARTIFACT_STATUSES:
                raise ValueError(f"unsupported artifact status: {status!r}")
            clauses.append("status = ?")
            params.append(str(status))
        params.append(max(1, min(int(limit), 200)))
        rows = self._fetchall(
            "SELECT work_item_id, MAX(updated_at) AS latest FROM artifacts WHERE "
            + " AND ".join(clauses)
            + " GROUP BY work_item_id ORDER BY latest DESC LIMIT ?",
            params,
        )
        return [str(row["work_item_id"]) for row in rows]

    # -- Permission interventions --------------------------------------

    def create_permission_request(
        self,
        work_item_id: str,
        *,
        attempt_id: str,
        capability: str,
        action: str,
        scope_paths: Sequence[str] | None = None,
        reason: str = "",
        reversibility: str = "",
        options: Sequence[str] | None = None,
        metadata: dict[str, Any] | None = None,
        request_id: str = "",
        idempotency_key: str = "",
    ) -> PermissionRequestRecord:
        """Create or idempotently enrich a pending permission request.

        A provider should pass its stable tool-call/event identifier as
        ``idempotency_key`` (or as ``request_id``).  Exact re-delivery then
        returns the same row.  Every pending field, including metadata, is
        immutable because provider-specific metadata may contain hashes,
        previews, and source paths that define the effective authority.  A
        resolved decision is immutable and is returned unchanged.
        """

        clean_work_item_id = str(work_item_id or "").strip()
        clean_attempt_id = str(attempt_id or "").strip()
        clean_capability = str(capability or "").strip()
        clean_action = str(action or "").strip()
        clean_request_id = str(request_id or "").strip()
        clean_key = str(idempotency_key or clean_request_id).strip()
        if not clean_work_item_id:
            raise ValueError("work_item_id is required")
        if not clean_attempt_id:
            raise ValueError("attempt_id is required")
        if not clean_capability:
            raise ValueError("permission capability is required")
        if not clean_action:
            raise ValueError("permission action is required")
        clean_scope_paths = _clean_string_list(scope_paths)
        clean_options = _clean_string_list(options)
        if len(clean_scope_paths) > 32 or any(len(path) > 2048 for path in clean_scope_paths):
            raise ValueError("permission scope exceeds the displayable contract bound")
        if len(clean_options) > 8 or any(len(option) > 80 for option in clean_options):
            raise ValueError("permission options exceed the bounded contract")
        clean_reason = str(reason or "").strip()
        clean_reversibility = str(reversibility or "").strip()
        now = float(self._clock())

        with self._transaction() as cursor:
            item = cursor.execute(
                "SELECT 1 FROM work_items WHERE work_item_id = ?",
                (clean_work_item_id,),
            ).fetchone()
            if item is None:
                raise WorkLedgerNotFound(f"unknown work item: {clean_work_item_id}")
            attempt = cursor.execute(
                "SELECT work_item_id FROM run_attempts WHERE attempt_id = ?",
                (clean_attempt_id,),
            ).fetchone()
            if attempt is None:
                raise WorkLedgerNotFound(f"unknown attempt: {clean_attempt_id}")
            if str(attempt["work_item_id"]) != clean_work_item_id:
                raise WorkLedgerConflict(
                    "permission request attempt belongs to a different work item"
                )

            by_id = None
            if clean_request_id:
                by_id = cursor.execute(
                    "SELECT * FROM permission_requests WHERE request_id = ?",
                    (clean_request_id,),
                ).fetchone()
            by_key = None
            if clean_key:
                by_key = cursor.execute(
                    """
                    SELECT * FROM permission_requests
                    WHERE attempt_id = ? AND idempotency_key = ?
                    """,
                    (clean_attempt_id, clean_key),
                ).fetchone()
            if (
                by_id is not None
                and by_key is not None
                and str(by_id["request_id"]) != str(by_key["request_id"])
            ):
                raise WorkLedgerConflict(
                    "permission request id and idempotency key identify different records"
                )
            existing = by_id if by_id is not None else by_key
            if existing is not None:
                if (
                    str(existing["work_item_id"]) != clean_work_item_id
                    or str(existing["attempt_id"]) != clean_attempt_id
                ):
                    raise WorkLedgerConflict(
                        "permission request identity belongs to a different work attempt"
                    )
                if (
                    str(existing["capability"]) != clean_capability
                    or str(existing["action"]) != clean_action
                ):
                    raise WorkLedgerConflict(
                        "permission request identity cannot be reused for another action"
                    )
                if str(existing["status"]) != "pending":
                    return self._permission_request_from_row(existing)

                existing_scope_paths = _load_string_list(existing["scope_paths_json"])
                existing_options = _load_string_list(existing["options_json"])
                existing_reason = str(existing["reason"])
                existing_reversibility = str(existing["reversibility"])
                if scope_paths is not None and clean_scope_paths != existing_scope_paths:
                    raise WorkLedgerConflict(
                        "pending permission scope is immutable for an idempotent request"
                    )
                if options is not None and clean_options != existing_options:
                    raise WorkLedgerConflict(
                        "pending permission options are immutable for an idempotent request"
                    )
                if clean_reason and clean_reason != existing_reason:
                    raise WorkLedgerConflict(
                        "pending permission reason is immutable for an idempotent request"
                    )
                if clean_reversibility and clean_reversibility != existing_reversibility:
                    raise WorkLedgerConflict(
                        "pending permission reversibility is immutable for an idempotent request"
                    )
                if (
                    metadata is not None
                    and _dump_json(metadata) != str(existing["metadata_json"])
                ):
                    raise WorkLedgerConflict(
                        "pending permission metadata is immutable for an idempotent request"
                    )
                return self._permission_request_from_row(existing)
            else:
                next_id = clean_request_id or new_ledger_id("permission")
                # The generated request id is also a useful replay key when a
                # caller did not have a provider event id.
                next_key = clean_key or next_id
                try:
                    cursor.execute(
                        """
                        INSERT INTO permission_requests (
                            request_id, work_item_id, attempt_id, idempotency_key,
                            capability, action, scope_paths_json, reason,
                            reversibility, status, options_json, created_at,
                            updated_at, resolved_at, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL, ?)
                        """,
                        (
                            next_id,
                            clean_work_item_id,
                            clean_attempt_id,
                            next_key,
                            clean_capability,
                            clean_action,
                            _dump_string_list(clean_scope_paths),
                            clean_reason,
                            clean_reversibility or "unknown",
                            _dump_string_list(clean_options),
                            now,
                            now,
                            _dump_json(metadata),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise WorkLedgerConflict(
                        f"permission request already exists: {next_id}"
                    ) from exc

            cursor.execute(
                """
                UPDATE work_items
                SET updated_at = ?, last_activity_at = ?
                WHERE work_item_id = ?
                """,
                (now, now, clean_work_item_id),
            )
            row = cursor.execute(
                "SELECT * FROM permission_requests WHERE request_id = ?",
                (next_id,),
            ).fetchone()
            assert row is not None
            return self._permission_request_from_row(row)

    # Both names intentionally share the same idempotent semantics.  The
    # coordinator can use ``create`` for first delivery and ``upsert`` when an
    # observer may replay provider events.
    upsert_permission_request = create_permission_request

    def get_permission_request(self, request_id: str) -> PermissionRequestRecord | None:
        row = self._fetchone(
            "SELECT * FROM permission_requests WHERE request_id = ?",
            (str(request_id),),
        )
        return self._permission_request_from_row(row) if row is not None else None

    def list_permission_requests(
        self,
        work_item_id: str,
        *,
        attempt_id: str = "",
        status: str = "",
    ) -> list[PermissionRequestRecord]:
        clean_work_item_id = str(work_item_id or "").strip()
        if not clean_work_item_id:
            raise ValueError("work_item_id is required")
        clean_attempt_id = str(attempt_id or "").strip()
        clean_status = str(status or "").strip().lower()
        if clean_status and clean_status not in PERMISSION_REQUEST_STATUSES:
            raise ValueError(f"unsupported permission request status: {status!r}")
        conditions = ["work_item_id = ?"]
        params: list[Any] = [clean_work_item_id]
        if clean_attempt_id:
            conditions.append("attempt_id = ?")
            params.append(clean_attempt_id)
        if clean_status:
            conditions.append("status = ?")
            params.append(clean_status)
        rows = self._fetchall(
            "SELECT * FROM permission_requests WHERE "
            + " AND ".join(conditions)
            + " ORDER BY created_at, rowid",
            params,
        )
        return [self._permission_request_from_row(row) for row in rows]

    def resolve_permission_request(
        self,
        request_id: str,
        status: PermissionRequestStatus,
        *,
        metadata: dict[str, Any] | None = None,
        expected_status: PermissionRequestStatus = "pending",
    ) -> PermissionRequestRecord:
        """Atomically resolve one pending request.

        Decisions are immutable.  A stale card, duplicate click, or competing
        surface therefore raises :class:`WorkLedgerConflict` instead of
        rewriting the first user's decision.
        """

        clean_request_id = str(request_id or "").strip()
        clean_status = str(status or "").strip().lower()
        clean_expected = str(expected_status or "").strip().lower()
        if not clean_request_id:
            raise ValueError("request_id is required")
        if clean_status not in PERMISSION_REQUEST_STATUSES - {"pending"}:
            raise ValueError(f"unsupported permission resolution status: {status!r}")
        if clean_expected != "pending":
            raise ValueError("permission requests can only be resolved from pending")
        now = float(self._clock())
        with self._transaction() as cursor:
            existing = cursor.execute(
                "SELECT * FROM permission_requests WHERE request_id = ?",
                (clean_request_id,),
            ).fetchone()
            if existing is None:
                raise WorkLedgerNotFound(f"unknown permission request: {clean_request_id}")
            if str(existing["status"]) != clean_expected:
                raise WorkLedgerConflict(
                    f"permission request {clean_request_id} is already {existing['status']}"
                )
            cursor.execute(
                """
                UPDATE permission_requests
                SET status = ?, updated_at = ?, resolved_at = ?, metadata_json = ?
                WHERE request_id = ? AND status = ?
                """,
                (
                    clean_status,
                    now,
                    now,
                    _merged_json(existing["metadata_json"], metadata),
                    clean_request_id,
                    clean_expected,
                ),
            )
            if cursor.rowcount != 1:
                raise WorkLedgerConflict(
                    f"permission request {clean_request_id} was resolved concurrently"
                )
            cursor.execute(
                """
                UPDATE work_items
                SET updated_at = ?, last_activity_at = ?
                WHERE work_item_id = ?
                """,
                (now, now, existing["work_item_id"]),
            )
            row = cursor.execute(
                "SELECT * FROM permission_requests WHERE request_id = ?",
                (clean_request_id,),
            ).fetchone()
            assert row is not None
            return self._permission_request_from_row(row)

    # -- Completion assessment -----------------------------------------

    def record_completion(
        self,
        work_item_id: str,
        decision: CompletionDecision,
        *,
        attempt_id: str = "",
        source: str = "host",
        evidence: dict[str, Any] | None = None,
        assessment_id: str = "",
    ) -> CompletionAssessmentRecord:
        if decision.execution_status not in EXECUTION_STATUSES:
            raise ValueError(f"unsupported execution status: {decision.execution_status!r}")
        if decision.completeness not in COMPLETENESS_STATES:
            raise ValueError(f"unsupported completeness: {decision.completeness!r}")
        if decision.attention not in ATTENTION_STATES:
            raise ValueError(f"unsupported attention: {decision.attention!r}")
        if decision.work_item_state not in WORK_ITEM_STATES:
            raise ValueError(f"unsupported work item state: {decision.work_item_state!r}")
        clean_source = str(source or "host").strip().lower() or "host"
        now = float(self._clock())
        with self._transaction() as cursor:
            item = cursor.execute("SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)).fetchone()
            if item is None:
                raise WorkLedgerNotFound(f"unknown work item: {work_item_id}")
            clean_attempt_id = str(attempt_id or "").strip()
            if clean_attempt_id:
                attempt = cursor.execute(
                    "SELECT work_item_id FROM run_attempts WHERE attempt_id = ?", (clean_attempt_id,)
                ).fetchone()
                if attempt is None:
                    raise WorkLedgerNotFound(f"unknown attempt: {clean_attempt_id}")
                if str(attempt["work_item_id"]) != work_item_id:
                    raise WorkLedgerConflict("completion attempt belongs to a different work item")
            current_state = str(item["state"])
            if decision.work_item_state == "accepted" and current_state != "accepted":
                policy_accept = bool(
                    clean_source == "policy"
                    and isinstance(evidence, dict)
                    and evidence.get("policy") == "auto_accept_approved_export"
                    and evidence.get("permission_request_id")
                    and evidence.get("export_status") == "committed"
                    and evidence.get("permission_resolution") == "user_allowed"
                )
                if clean_source != "user" and not policy_accept:
                    raise WorkLedgerConflict(
                        "only an explicit user assessment or a fully evidenced approved-export policy can accept a work item"
                    )
            next_id = str(assessment_id or new_ledger_id("assessment"))
            cursor.execute(
                """
                INSERT INTO completion_assessments (
                    assessment_id, work_item_id, attempt_id, source, execution_status,
                    completeness, attention, work_item_state, rationale, terminal, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    next_id,
                    work_item_id,
                    clean_attempt_id or None,
                    clean_source,
                    decision.execution_status,
                    decision.completeness,
                    decision.attention,
                    decision.work_item_state,
                    str(decision.rationale or ""),
                    1 if decision.terminal else 0,
                    _dump_json(evidence),
                    now,
                ),
            )
            # Completion facts may advance an open item to review_ready, but
            # never undo explicit accepted/archived disposition.
            next_state = current_state
            if current_state not in {"accepted", "archived"}:
                next_state = decision.work_item_state
            cursor.execute(
                "UPDATE work_items SET state = ?, updated_at = ?, last_activity_at = ? WHERE work_item_id = ?",
                (next_state, now, now, work_item_id),
            )
            row = cursor.execute(
                "SELECT * FROM completion_assessments WHERE assessment_id = ?", (next_id,)
            ).fetchone()
            assert row is not None
            return self._assessment_from_row(row)

    def latest_completion(self, work_item_id: str) -> CompletionAssessmentRecord | None:
        row = self._fetchone(
            """
            SELECT * FROM completion_assessments
            WHERE work_item_id = ?
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (str(work_item_id),),
        )
        return self._assessment_from_row(row) if row is not None else None

    def list_completions(self, work_item_id: str) -> list[CompletionAssessmentRecord]:
        rows = self._fetchall(
            """
            SELECT * FROM completion_assessments
            WHERE work_item_id = ? ORDER BY created_at, rowid
            """,
            (str(work_item_id),),
        )
        return [self._assessment_from_row(row) for row in rows]

    # -- Workspace writer lease -----------------------------------------

    def acquire_writer_lease(
        self,
        work_item_id: str,
        attempt_id: str,
        *,
        workspace_path: str | os.PathLike[str],
        metadata: dict[str, Any] | None = None,
        lease_id: str = "",
    ) -> WorkspaceLeaseRecord:
        """Atomically acquire the single active writer slot for a path."""
        workspace = canonicalize_path(workspace_path)
        now = float(self._clock())
        with self._transaction() as cursor:
            attempt = cursor.execute(
                "SELECT work_item_id, execution_status FROM run_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise WorkLedgerNotFound(f"unknown attempt: {attempt_id}")
            if str(attempt["work_item_id"]) != str(work_item_id):
                raise WorkLedgerConflict("writer lease attempt belongs to a different work item")
            if str(attempt["execution_status"]) in _TERMINAL_EXECUTION:
                raise WorkLedgerConflict("terminal attempt cannot acquire a writer lease")
            existing_attempt = cursor.execute(
                "SELECT * FROM workspace_leases WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if existing_attempt is not None:
                if str(existing_attempt["status"]) == "active":
                    return self._workspace_lease_from_row(existing_attempt)
            active = cursor.execute(
                "SELECT * FROM workspace_leases WHERE workspace_identity = ? AND status = 'active'",
                (workspace.identity_key,),
            ).fetchone()
            if active is not None:
                active_attempt = cursor.execute(
                    "SELECT execution_status FROM run_attempts WHERE attempt_id = ?",
                    (active["attempt_id"],),
                ).fetchone()
                if (
                    active_attempt is None
                    or str(active_attempt["execution_status"]) in _TERMINAL_EXECUTION
                ):
                    cursor.execute(
                        """
                        UPDATE workspace_leases
                        SET status = 'released', heartbeat_at = ?, released_at = ?
                        WHERE lease_id = ?
                        """,
                        (now, now, active["lease_id"]),
                    )
                    active = None
            if active is not None:
                raise WorkLedgerConflict(
                    "workspace already has an active writer: "
                    f"{active['work_item_id']} ({active['attempt_id']})"
                )
            if existing_attempt is not None:
                cursor.execute(
                    """
                    UPDATE workspace_leases
                    SET workspace_path = ?, workspace_identity = ?, status = 'active',
                        acquired_at = ?, heartbeat_at = ?, released_at = NULL,
                        metadata_json = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        workspace.canonical_path,
                        workspace.identity_key,
                        now,
                        now,
                        _merged_json(existing_attempt["metadata_json"], metadata),
                        attempt_id,
                    ),
                )
                row = cursor.execute(
                    "SELECT * FROM workspace_leases WHERE attempt_id = ?", (attempt_id,)
                ).fetchone()
                assert row is not None
                return self._workspace_lease_from_row(row)
            next_id = str(lease_id or new_ledger_id("lease"))
            try:
                cursor.execute(
                    """
                    INSERT INTO workspace_leases (
                        lease_id, workspace_path, workspace_identity, work_item_id,
                        attempt_id, status, acquired_at, heartbeat_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        next_id,
                        workspace.canonical_path,
                        workspace.identity_key,
                        work_item_id,
                        attempt_id,
                        now,
                        now,
                        _dump_json(metadata),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkLedgerConflict("workspace writer lease was acquired concurrently") from exc
            row = cursor.execute(
                "SELECT * FROM workspace_leases WHERE lease_id = ?", (next_id,)
            ).fetchone()
            assert row is not None
            return self._workspace_lease_from_row(row)

    def heartbeat_writer_lease(self, attempt_id: str) -> WorkspaceLeaseRecord:
        now = float(self._clock())
        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM workspace_leases WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise WorkLedgerNotFound(f"attempt has no writer lease: {attempt_id}")
            if str(row["status"]) != "active":
                raise WorkLedgerConflict(f"writer lease for {attempt_id} is {row['status']}")
            cursor.execute(
                "UPDATE workspace_leases SET heartbeat_at = ? WHERE attempt_id = ?",
                (now, attempt_id),
            )
            row = cursor.execute(
                "SELECT * FROM workspace_leases WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            assert row is not None
            return self._workspace_lease_from_row(row)

    def release_writer_lease(
        self,
        attempt_id: str,
        *,
        status: str = "released",
        metadata: dict[str, Any] | None = None,
    ) -> WorkspaceLeaseRecord | None:
        if status not in WORKSPACE_LEASE_STATUSES - {"active"}:
            raise ValueError(f"unsupported writer lease release status: {status!r}")
        now = float(self._clock())
        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM workspace_leases WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                return None
            if str(row["status"]) == "active":
                cursor.execute(
                    """
                    UPDATE workspace_leases
                    SET status = ?, heartbeat_at = ?, released_at = ?, metadata_json = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        status,
                        now,
                        now,
                        _merged_json(row["metadata_json"], metadata),
                        attempt_id,
                    ),
                )
            row = cursor.execute(
                "SELECT * FROM workspace_leases WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            assert row is not None
            return self._workspace_lease_from_row(row)

    def get_writer_lease(self, attempt_id: str) -> WorkspaceLeaseRecord | None:
        row = self._fetchone(
            "SELECT * FROM workspace_leases WHERE attempt_id = ?", (str(attempt_id),)
        )
        return self._workspace_lease_from_row(row) if row is not None else None

    def list_writer_leases(self, *, active_only: bool = False) -> list[WorkspaceLeaseRecord]:
        sql = "SELECT * FROM workspace_leases"
        if active_only:
            sql += " WHERE status = 'active'"
        sql += " ORDER BY acquired_at, lease_id"
        return [self._workspace_lease_from_row(row) for row in self._fetchall(sql, ())]

    # -- Focus -----------------------------------------------------------

    def set_focus(
        self,
        surface: str,
        work_item_id: str | None,
        *,
        mode: FocusMode = "auto",
    ) -> FocusRecord:
        clean_surface = str(surface or "").strip()
        if not clean_surface:
            raise ValueError("surface is required")
        if mode not in FOCUS_MODES:
            raise ValueError(f"unsupported focus mode: {mode!r}")
        clean_work_item_id = str(work_item_id or "").strip()
        if not clean_work_item_id and mode == "pinned":
            raise ValueError("pinned focus requires a work item")
        now = float(self._clock())
        with self._transaction() as cursor:
            if clean_work_item_id:
                item = cursor.execute(
                    "SELECT 1 FROM work_items WHERE work_item_id = ?", (clean_work_item_id,)
                ).fetchone()
                if item is None:
                    raise WorkLedgerNotFound(f"unknown work item: {clean_work_item_id}")
            cursor.execute(
                """
                INSERT INTO focus_slots (surface, work_item_id, mode, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(surface) DO UPDATE SET
                    work_item_id = excluded.work_item_id,
                    mode = excluded.mode,
                    updated_at = excluded.updated_at
                """,
                (clean_surface, clean_work_item_id or None, mode, now),
            )
            row = cursor.execute("SELECT * FROM focus_slots WHERE surface = ?", (clean_surface,)).fetchone()
            assert row is not None
            return self._focus_from_row(row)

    def clear_focus(self, surface: str) -> FocusRecord:
        return self.set_focus(surface, None, mode="auto")

    def get_focus(self, surface: str) -> FocusRecord | None:
        row = self._fetchone("SELECT * FROM focus_slots WHERE surface = ?", (str(surface),))
        return self._focus_from_row(row) if row is not None else None

    def list_focus(self) -> list[FocusRecord]:
        rows = self._fetchall("SELECT * FROM focus_slots ORDER BY surface", ())
        return [self._focus_from_row(row) for row in rows]
