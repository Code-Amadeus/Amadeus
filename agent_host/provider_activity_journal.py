"""Bounded local persistence for the Provider activity display surface.

This journal stores canonical Host-bound execution evidence only.  It is not
part of role history, does not retain hidden reasoning, and never decides Work
or Provider semantics.  The append-only file can be rebuilt into the same
run-shaped projection consumed by Electron after a backend restart.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

_DISPLAY_EVENT_TYPES = frozenset(
    {
        "run.created",
        "run.started",
        "run.status",
        "run.finished",
        "run.failed",
        "run.cancelled",
        "tool.call",
        "tool.result",
        "semantic.progress",
        "permission.requested",
        "permission.required",
        "permission.resolved",
        "permission.allowed",
        "permission.approved",
        "permission.granted",
        "permission.denied",
        "permission.rejected",
        "permission.expired",
        "artifact.created",
        "diff.updated",
    }
)
_TERMINAL_STATUSES = frozenset(
    {"done", "succeeded", "success", "failed", "error", "cancelled", "canceled"}
)
_SECRET_PATTERNS = (
    re.compile(r"(bearer\s+)[a-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(
        r"((?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+",
        re.IGNORECASE,
    ),
)


def provider_activity_journal_path(path: Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    configured = str(os.environ.get("AMADEUS_PROVIDER_ACTIVITY_PATH") or "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else Path("runtime") / "provider_activity.jsonl"
    )


def _redact(value: str, limit: int = 4000) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1•••", text)
    return text[:limit]


def _bounded(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact(value)
    if depth >= 5:
        return _redact(str(value), 500)
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth=depth + 1) for item in value[:12]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:32]:
            result[_redact(str(key), 120)] = _bounded(item, depth=depth + 1)
        return result
    return _redact(str(value), 500)


def _origin_metadata(value: dict[str, Any]) -> dict[str, Any]:
    metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
    work = metadata.get("work") if isinstance(metadata.get("work"), dict) else {}
    session_id = str(metadata.get("session_id") or metadata.get("sessionId") or "").strip()
    turn_id = str(metadata.get("turn_id") or metadata.get("turnId") or "").strip()
    return {
        "session_id": session_id[:240],
        "turn_id": turn_id[:240],
        "work": {
            "work_item_id": str(
                value.get("task_id")
                or work.get("work_item_id")
                or work.get("workItemId")
                or ""
            )[:240],
            "attempt_id": str(
                value.get("attempt_id")
                or work.get("attempt_id")
                or work.get("attemptId")
                or ""
            )[:240],
        },
    }


class ProviderActivityJournal:
    """Append canonical activity facts and expose bounded run projections."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        max_runs: int = 128,
        max_events_per_run: int = 80,
        max_file_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self.path = provider_activity_journal_path(path)
        self.max_runs = max(1, int(max_runs))
        self.max_events_per_run = max(1, int(max_events_per_run))
        self.max_file_bytes = max(1024, int(max_file_bytes))
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._writes_since_compact = 0
        self._load()

    def record_event(self, raw: dict[str, Any]) -> bool:
        event = self._normalize_event(raw)
        if event is None:
            return False
        with self._lock:
            self._ingest_event(event)
            self._append_locked({"kind": "event", "data": event})
        return True

    def record_result(self, raw: dict[str, Any]) -> bool:
        result = self._normalize_result(raw)
        if result is None:
            return False
        with self._lock:
            self._ingest_result(result)
            self._append_locked({"kind": "result", "data": result})
        return True

    def list_runs(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        clean_session = str(session_id or "").strip()
        if not clean_session:
            return []
        with self._lock:
            rows = [
                copy.deepcopy(run)
                for run in self._runs.values()
                if str((run.get("metadata") or {}).get("session_id") or "")
                == clean_session
            ]
        rows.sort(key=lambda item: float(item.get("created_at") or 0.0))
        return rows[-max(1, min(int(limit or 100), self.max_runs)) :]

    def close(self) -> None:
        with self._lock:
            if self._writes_since_compact:
                self._compact_locked()

    def _normalize_event(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(raw, dict) or raw.get("replay") is True:
            return None
        event_type = str(raw.get("type") or "").strip().lower()
        run_id = str(raw.get("run_id") or raw.get("runId") or "").strip()
        provider = str(raw.get("provider") or "").strip()
        metadata = _origin_metadata(raw)
        if (
            event_type not in _DISPLAY_EVENT_TYPES
            or not run_id
            or not provider
            or not metadata["session_id"]
            or not metadata["turn_id"]
        ):
            return None
        source_metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        if source_metadata.get("replay") is True:
            return None
        observed_at = float(raw.get("observed_at") or time.time())
        return {
            "provider": provider[:120],
            "run_id": run_id[:240],
            "type": event_type,
            "payload": _bounded(raw.get("payload") if isinstance(raw.get("payload"), dict) else {}),
            "metadata": metadata,
            "task_id": metadata["work"]["work_item_id"],
            "attempt_id": metadata["work"]["attempt_id"],
            "sequence": max(0, int(raw.get("sequence") or 0)),
            "observed_at": observed_at,
            "replay": False,
            "ownership": str(raw.get("ownership") or "managed")[:40],
        }

    def _normalize_result(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        run_id = str(raw.get("run_id") or raw.get("runId") or "").strip()
        provider = str(raw.get("provider") or "").strip()
        metadata = _origin_metadata(raw)
        if (
            not run_id
            or not provider
            or not metadata["session_id"]
            or not metadata["turn_id"]
        ):
            return None
        now = time.time()
        return {
            "run_id": run_id[:240],
            "provider": provider[:120],
            "task": _redact(str(raw.get("task") or ""), 4000),
            "cwd": _redact(str(raw.get("cwd") or ""), 1200),
            "status": str(raw.get("status") or "").strip().lower()[:40],
            "created_at": float(raw.get("created_at") or now),
            "updated_at": float(raw.get("updated_at") or now),
            "result": _redact(str(raw.get("result") or ""), 6000),
            "error": _redact(str(raw.get("error") or ""), 3000),
            "metadata": metadata,
            "task_id": metadata["work"]["work_item_id"],
            "attempt_id": metadata["work"]["attempt_id"],
            "attempt_epoch": max(0, int(raw.get("attempt_epoch") or 0)),
            "ownership": str(raw.get("ownership") or "managed")[:40],
        }

    def _new_run(self, value: dict[str, Any], at: float) -> dict[str, Any]:
        return {
            "run_id": str(value.get("run_id") or ""),
            "provider": str(value.get("provider") or ""),
            "task": str(value.get("task") or ""),
            "cwd": str(value.get("cwd") or ""),
            "status": "running",
            "created_at": at,
            "updated_at": at,
            "result": "",
            "error": "",
            "metadata": copy.deepcopy(value.get("metadata") or {}),
            "events": [],
            "task_id": str(value.get("task_id") or ""),
            "attempt_id": str(value.get("attempt_id") or ""),
            "attempt_epoch": max(0, int(value.get("attempt_epoch") or 0)),
            "ownership": str(value.get("ownership") or "managed"),
            "event_sequence": 0,
        }

    def _ingest_event(self, event: dict[str, Any]) -> None:
        run_id = str(event["run_id"])
        at = float(event.get("observed_at") or time.time())
        run = self._runs.get(run_id) or self._new_run(event, at)
        sequence = max(0, int(event.get("sequence") or 0))
        events = list(run.get("events") or [])
        if sequence and any(int(item.get("sequence") or 0) == sequence for item in events):
            return
        events.append(copy.deepcopy(event))
        run["events"] = events[-self.max_events_per_run :]
        run["event_sequence"] = max(int(run.get("event_sequence") or 0), sequence)
        run["updated_at"] = max(float(run.get("updated_at") or 0.0), at)
        run["metadata"] = copy.deepcopy(event["metadata"])
        run["task_id"] = str(event.get("task_id") or run.get("task_id") or "")
        run["attempt_id"] = str(event.get("attempt_id") or run.get("attempt_id") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if payload.get("task") and not run.get("task"):
            run["task"] = str(payload["task"])
        event_type = str(event.get("type") or "")
        if event_type == "run.failed":
            run["status"] = "error"
        elif event_type == "run.cancelled":
            run["status"] = "cancelled"
        elif event_type == "run.finished":
            run["status"] = str(payload.get("status") or "done").lower()
        elif event_type == "run.status" and str(payload.get("status") or "").lower() in _TERMINAL_STATUSES:
            run["status"] = str(payload.get("status") or "").lower()
        self._runs[run_id] = run
        self._trim_runs_locked()

    def _ingest_result(self, result: dict[str, Any]) -> None:
        run_id = str(result["run_id"])
        at = float(result.get("updated_at") or time.time())
        run = self._runs.get(run_id) or self._new_run(result, float(result.get("created_at") or at))
        events = list(run.get("events") or [])[-self.max_events_per_run :]
        run.update(copy.deepcopy(result))
        run["events"] = events
        run["event_sequence"] = max(
            int(run.get("event_sequence") or 0),
            max((int(item.get("sequence") or 0) for item in events), default=0),
        )
        self._runs[run_id] = run
        self._trim_runs_locked()

    def _trim_runs_locked(self) -> None:
        if len(self._runs) <= self.max_runs:
            return
        ordered = sorted(
            self._runs,
            key=lambda key: float(self._runs[key].get("updated_at") or 0.0),
        )
        for run_id in ordered[: len(self._runs) - self.max_runs]:
            self._runs.pop(run_id, None)

    def _append_locked(self, envelope: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._writes_since_compact += 1
            size = self.path.stat().st_size
            if self._writes_since_compact >= 1000 or (
                size > self.max_file_bytes and self._writes_since_compact >= 50
            ):
                self._compact_locked()
        except Exception:
            logger.warning("failed to persist Provider activity", exc_info=True)

    def _compact_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                for run in sorted(
                    self._runs.values(),
                    key=lambda item: float(item.get("updated_at") or 0.0),
                ):
                    handle.write(
                        json.dumps(
                            {"kind": "snapshot", "data": run},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
            os.replace(temp_path, self.path)
            self._writes_since_compact = 0
        except Exception:
            logger.warning("failed to compact Provider activity", exc_info=True)

    def _load(self) -> None:
        if not self.path.exists():
            return
        corrupt = False
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        envelope = json.loads(text)
                    except json.JSONDecodeError:
                        corrupt = True
                        continue
                    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), dict):
                        corrupt = True
                        continue
                    kind = str(envelope.get("kind") or "")
                    data = envelope["data"]
                    if kind == "event":
                        event = self._normalize_event(data)
                        if event is not None:
                            self._ingest_event(event)
                    elif kind == "result":
                        result = self._normalize_result(data)
                        if result is not None:
                            self._ingest_result(result)
                    elif kind == "snapshot":
                        run_id = str(data.get("run_id") or "").strip()
                        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
                        if run_id and metadata.get("session_id") and metadata.get("turn_id"):
                            snapshot = copy.deepcopy(data)
                            snapshot["events"] = list(snapshot.get("events") or [])[-self.max_events_per_run :]
                            self._runs[run_id] = snapshot
                    else:
                        corrupt = True
            self._trim_runs_locked()
            if corrupt or self.path.stat().st_size > self.max_file_bytes:
                self._compact_locked()
        except Exception:
            logger.warning("failed to load Provider activity", exc_info=True)


__all__ = ["ProviderActivityJournal", "provider_activity_journal_path"]
