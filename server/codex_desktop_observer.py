"""Read-only Codex root-task observation across saved projects.

This is not a Desktop subscription API. The installed build's append-only
rollout provides question calls, structured answers and turn completion. It
does not provide a reliable read receipt or every approval lifecycle. Never
infer those facts from window focus, file age, or a tool's mere return.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID


ACTIVE_WINDOW_MS = 8 * 60 * 60 * 1000


def _timestamp_ms(record: dict) -> int:
    value = record.get("timestamp")
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000) if value else 0


class CodexRolloutTask:
    def __init__(self, thread_id: str, title: str, project_name: str = "", connected_at_ms: int = 0) -> None:
        self.thread_id = str(UUID(thread_id))
        self.title = title
        self.project_name = project_name
        self.turn_id = ""
        self.phase = "running"
        self.detail = "正在处理当前任务"
        self.pending: dict[str, dict] = {}
        self.last_activity_at = 0
        self.connected_at_ms = connected_at_ms
        self.announce_completion = False
        self.activities: list[dict] = []
        self.activity_sequence = 0

    def _append_activity(self, kind: str, text: str, record: dict, *, call_id: str = "") -> None:
        self.activity_sequence += 1
        self.activities.append({"id": f"{self.turn_id}:{self.activity_sequence}", "kind": kind,
                                "text": text[:6000], "at": _timestamp_ms(record),
                                **({"callId": call_id, "status": "running"} if call_id else {})})
        self.activities = self.activities[-80:]

    def consume(self, record: dict) -> None:
        payload = record.get("payload", {})
        kind = payload.get("type")
        # Source activity, not polling, title changes, token accounting or
        # private reasoning. Tool calls/results count even without new prose.
        activity = (
            record.get("type") == "response_item" and (
                kind in {"function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"}
                or (kind == "message" and payload.get("role") in {"user", "assistant"}
                    and payload.get("phase") != "analysis")
            )
        )
        if record.get("type") == "event_msg":
            if kind == "task_started":
                activity = True
                self.turn_id = str(payload.get("turn_id") or "")
                self.pending.clear()
                self.phase, self.detail = "running", "正在处理当前任务"
                self.activities.clear()
                self.activity_sequence = 0
            elif kind == "task_complete" and payload.get("turn_id") == self.turn_id:
                activity = True
                self.announce_completion = _timestamp_ms(record) >= self.connected_at_ms
                self.pending.clear()
                self.phase = "ready"
                self.detail = str(payload.get("last_agent_message") or "本轮结束，可以查看产出。")
                self._append_activity("result", self.detail, record)
            elif kind == "turn_aborted" and payload.get("turn_id", self.turn_id) == self.turn_id:
                activity = True
                self.phase, self.detail = "blocked", "本轮已中断，请回到 Codex 查看。"
            if activity:
                self.last_activity_at = max(self.last_activity_at, _timestamp_ms(record))
            return
        if record.get("type") != "response_item":
            return
        if activity:
            self.last_activity_at = max(self.last_activity_at, _timestamp_ms(record))
        if kind in {"function_call", "custom_tool_call"}:
            self._append_activity("tool", str(payload.get("name") or "工具"), record,
                                  call_id=str(payload.get("call_id") or ""))
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            for item in reversed(self.activities):
                if item.get("callId") == payload.get("call_id"):
                    item["status"] = "returned"
                    break
        if kind == "function_call" and payload.get("name") in {"request_user_input", "request_user_input_async"}:
            try:
                args = json.loads(payload["arguments"])
                first = args["questions"][0]
                text = first.get("question") or first.get("title")
                call_id = payload["call_id"]
                if not isinstance(text, str) or not text.strip():
                    return
                self.pending[call_id] = {"text": text, "turn": self.turn_id,
                                         "async": payload["name"].endswith("_async")}
            except (KeyError, TypeError, ValueError, IndexError):
                return
        elif kind == "function_call_output":
            call_id = payload.get("call_id")
            request = self.pending.get(call_id)
            # Async request creation returns immediately; that is not an answer.
            if request and not request["async"]:
                self.pending.pop(call_id)
        elif kind == "message":
            if payload.get("role") == "user":
                for content in payload.get("content", []):
                    self._consume_answer(content.get("text", ""))
            elif payload.get("role") == "assistant" and payload.get("phase") == "commentary":
                text = "\n".join(c.get("text", "") for c in payload.get("content", []))
                if text.strip() and self.phase == "running":
                    self.detail = text
                    self._append_activity("progress", text, record)

    def _consume_answer(self, text: str) -> None:
        prefix, suffix = "<send_user_message_question_reply>", "</send_user_message_question_reply>"
        text = text.strip()
        if not text.startswith(prefix) or not text.endswith(suffix):
            return
        try:
            for answer in json.loads(text[len(prefix):-len(suffix)]):
                identity = json.loads(answer["questionItemId"])
                if len(identity) == 3 and identity[0] == "request_user_input_async" and identity[2] == 0:
                    self.pending.pop(identity[1], None)
        except (KeyError, TypeError, ValueError):
            return

    def snapshot(self) -> dict[str, Any]:
        call_id, request = next(iter(self.pending.items()), ("", None))
        phase = "attention" if request else self.phase
        return {"id": self.thread_id, "provider": "Codex", "title": self.title,
                "lastActivityAt": self.last_activity_at,
                "activities": [dict(item) for item in self.activities],
                "projectName": self.project_name,
                "codexThreadId": self.thread_id, "sourceLabel": "本机 Codex",
                "detail": request["text"] if request else self.detail, "phase": phase,
                "key": f"{self.thread_id}:{request['turn'] if request else self.turn_id}:{call_id or phase}",
                # One later reminder is a local notification policy. A local
                # acknowledgement never becomes a Codex answer/read receipt.
                "repeatable": phase in {"attention", "ready"},
                "announce": phase == "attention" or (phase == "ready" and self.announce_completion)}


def _records_backwards(path: Path):
    """Yield complete JSONL records and their byte offsets from newest to oldest."""
    with path.open("rb") as stream:
        position = stream.seek(0, 2)
        suffix = b""
        discard_partial = True
        while position:
            size = min(position, 65536)
            position -= size
            stream.seek(position)
            data = stream.read(size) + suffix
            pieces = data.split(b"\n")
            suffix = pieces[0]
            cursor = position + len(data)
            for line in reversed(pieces[1:]):
                cursor -= len(line)
                if not discard_partial and line:
                    yield cursor, json.loads(line)
                discard_partial = False
                cursor -= 1
        if suffix and not discard_partial:
            yield 0, json.loads(suffix)


class CodexDesktopObserver:
    def __init__(self, codex_home: Path, thread_ids: list[str] | None = None) -> None:
        self.home = codex_home
        self.thread_ids = [str(UUID(value)) for value in thread_ids] if thread_ids is not None else None
        self.readers: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.connected_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.connected_at_ms = _timestamp_ms({"timestamp": self.connected_at})
        self.catalog: dict[str, tuple] = {}
        self.discover_at = 0.0
        self.project_state: dict = {}
        self.parents: dict[str, str] = {}

    def _project_name(self, thread_id: str) -> str:
        # Use Desktop's explicit saved-project assignment. A cwd or task title
        # is not evidence of project membership, especially for projectless work.
        try:
            assignment = self.project_state.get("thread-project-assignments", {}).get(thread_id, {})
            if assignment.get("projectKind") != "local":
                return ""
            project = self.project_state.get("local-projects", {}).get(assignment.get("projectId"), {})
            return str(project.get("name") or "")
        except (OSError, ValueError, AttributeError):
            return ""

    def _discover(self) -> None:
        if time.monotonic() < self.discover_at:
            return
        with sqlite3.connect((self.home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            if self.thread_ids is None:
                candidates = db.execute("SELECT id, rollout_path, name, source FROM threads WHERE archived=0").fetchall()
                rows = []
                self.parents = {}
                for row in candidates:
                    if row[3] in {"vscode", "cli"}:
                        rows.append(row[:3])
                        continue
                    try:
                        branch = json.loads(row[3]).get("subagent", {}).get("thread_spawn", {})
                        parent = branch.get("parent_thread_id")
                        if parent:
                            self.parents[row[0]] = str(UUID(parent))
                            rows.append((row[0], row[1], row[2] or branch.get("agent_nickname") or "子代理任务"))
                    except (ValueError, TypeError, AttributeError):
                        continue
            else:
                rows = []
                for thread_id in self.thread_ids:
                    row = db.execute("SELECT id, rollout_path, name FROM threads WHERE id=?", (thread_id,)).fetchone()
                    if row is None:
                        raise ValueError("指定的 Codex 任务不存在")
                    rows.append(row)
        # Desktop's saved title index also covers legacy tasks whose database
        # name is empty. threads.title is often the first prompt, not the name.
        titles = {}
        index = self.home / "session_index.jsonl"
        if index.exists():
            with index.open("rb") as stream:
                for line in stream:
                    if not line.endswith(b"\n"):
                        break  # An in-flight append is not a renamed task yet.
                    entry = json.loads(line)
                    if name := str(entry.get("thread_name") or "").strip():
                        titles[entry["id"]] = name
        self.catalog = {row[0]: (Path(row[1]), titles.get(row[0]) or row[2] or "Codex 任务") for row in rows}
        self.readers = {key: reader for key, reader in self.readers.items() if key in self.catalog}
        try:
            self.project_state = json.loads((self.home / ".codex-global-state.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.project_state = {}
        self.discover_at = time.monotonic() + 1

    def _open_task(self, thread_id: str, path: Path, title: str) -> dict:
        # Raw lifecycle events, not a stale history-database status, determine
        # the current turn. New tasks may finish between discovery polls: keep
        # their post-connect completion. Restore recent completed cards for the
        # workday view, but their pre-connect completion is never announced.
        offset = path.stat().st_size
        for at, record in _records_backwards(path):
            payload = record.get("payload", {})
            kind = payload.get("type")
            if record.get("type") != "event_msg":
                continue
            if kind in {"task_complete", "turn_aborted"} and _timestamp_ms(record) < self.connected_at_ms - ACTIVE_WINDOW_MS:
                break
            if kind == "task_started":
                offset = at
                break
        return {"path": path, "offset": offset,
                "task": CodexRolloutTask(thread_id, title, self._project_name(thread_id), self.connected_at_ms),
                "started": False, "catching_up": True}

    def poll(self) -> dict:
        with self.lock:
            return self._poll()

    def _poll(self) -> dict:
        tasks, errors = [], []
        try:
            self._discover()
        except (OSError, sqlite3.Error, ValueError) as exc:
            errors.append(str(exc))
        for thread_id, (path, title) in self.catalog.items():
            try:
                if thread_id not in self.readers:
                    self.readers[thread_id] = self._open_task(thread_id, path, title)
                reader = self.readers[thread_id]
                reader["task"].title = title
                reader["task"].project_name = self._project_name(thread_id)
                size = path.stat().st_size
                if size < reader["offset"]:
                    raise ValueError("Codex 记录已改变，需重新建立观察基线")
                if size > reader["offset"]:
                    with path.open("rb") as stream:
                        stream.seek(reader["offset"])
                        for _ in range(500):
                            line = stream.readline()
                            if not line.endswith(b"\n"):
                                reader["catching_up"] = False
                                break
                            record = json.loads(line)
                            reader["task"].consume(record)
                            reader["started"] |= record.get("type") == "event_msg" and record.get("payload", {}).get("type") == "task_started"
                            reader["offset"] = stream.tell()
                else:
                    reader["catching_up"] = False
                if reader["started"] and not reader["catching_up"]:
                    task = reader["task"].snapshot()
                    owner = thread_id
                    seen = {owner}
                    while owner in self.parents and self.parents[owner] not in seen:
                        owner = self.parents[owner]
                        seen.add(owner)
                    assignment = self.project_state.get("thread-project-assignments", {}).get(owner, {})
                    if assignment.get("projectKind") == "local":
                        task["projectId"] = assignment.get("projectId", "")
                        task["projectName"] = self._project_name(owner)
                    if thread_id in self.parents:
                        task["parentTaskId"] = self.parents[thread_id]
                        task["sourceKind"] = "subagent"
                        # Results feed the parent, while a proven question to
                        # the user still needs attention even on a child task.
                        task["announce"] = task["repeatable"] = task["phase"] == "attention"
                    else:
                        task["sourceKind"] = "task"
                    tasks.append(task)
            except (OSError, sqlite3.Error, ValueError) as exc:
                errors.append(f"{thread_id}: {exc}")
        return {"tasks": tasks, "status": "error" if errors else "experimental",
                "note": "；".join(errors) if errors else (
                    "本机 Codex 各项目任务" if self.thread_ids is None else "指定 Codex 任务"
                ) + " · 完整审批尚未接入；知道了仅确认 Amadeus 提醒。"}
