"""Session persistence adapter for the Electron frontend.

This reuses the established session store in core.session_manager so clients
see the same conversation history files.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from agent_host.work_ledger_store import WorkLedgerConflict
from core import session_manager as sm
from server.event_bus import bus
from server.protocol import Method
from server.ws_handler import RequestHandler

# Stored history keeps the DELEGATE tags the model emitted, because the next
# turn's prompt needs to see that the assistant delegates rather than merely
# promising. The tags are machine syntax, so they are stripped here, at the
# display boundary, rather than at the point of record.
_ACTION_TAG_RE = re.compile(
    r"\[(?:DELEGATE|EMO|EXPR|PARAM|HOTKEY)\b[^\]]*\]",
    flags=re.IGNORECASE,
)
_WORK_OBSERVER_PREFIX_RE = re.compile(r"^\s*\[WORK_OBSERVER\]\s*")


def _display_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    visible = _WORK_OBSERVER_PREFIX_RE.sub("", value)
    return re.sub(r"[ \t]{2,}", " ", _ACTION_TAG_RE.sub("", visible)).strip()


def _display_dialog(dialog: Any) -> list[dict[str, Any]]:
    if not isinstance(dialog, list):
        return []
    rendered: list[dict[str, Any]] = []
    for entry in dialog:
        if not isinstance(entry, dict):
            continue
        rendered.append({**entry, "content": _display_text(entry.get("content"))})
    return rendered


class SessionHandler(RequestHandler):
    methods = [
        Method.SESSION_LIST,
        Method.SESSION_CREATE,
        Method.SESSION_LOAD,
        Method.SESSION_DELETE,
        Method.SESSION_RENAME,
        Method.SESSION_OPEN_CONTEXT,
        Method.SESSION_CORRECT_PROJECT,
        Method.PROJECT_CREATE,
    ]

    def __init__(self) -> None:
        self._work_coordinator: Any = None
        self._is_chat_busy: Callable[[], bool] = lambda: False

    def configure(
        self,
        *,
        work_coordinator: Any,
        is_chat_busy: Callable[[], bool],
    ) -> None:
        self._work_coordinator = work_coordinator
        self._is_chat_busy = is_chat_busy

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.SESSION_LIST:
            return self._list()
        if method == Method.SESSION_CREATE:
            return self._create(params)
        if method == Method.SESSION_LOAD:
            return self._load(params)
        if method == Method.SESSION_DELETE:
            return self._delete(params)
        if method == Method.SESSION_RENAME:
            return self._rename(params)
        if method == Method.SESSION_OPEN_CONTEXT:
            return await self._open_context(params, source="electron")
        if method == Method.SESSION_CORRECT_PROJECT:
            return self._correct_project(params)
        if method == Method.PROJECT_CREATE:
            return await self._create_project(params)
        return None

    def _list(self) -> dict[str, Any]:
        sessions = []
        for sid in sm.list_sessions():
            data = self._read_data(sid)
            sessions.append({
                "id": sid,
                "title": data.get("title") or sm.get_session_title(sid),
                "timestamp": data.get("timestamp", 0),
                "message_count": len(data.get("dialog", []) or []),
                "context": self._context(sid),
            })
        sessions.sort(
            key=lambda item: (item.get("timestamp") or 0, item["id"]),
            reverse=True,
        )
        return {
            "sessions": sessions,
            "current_session_id": sm.get_current_session_id(),
            "projects": self._projects(),
        }

    async def ensure_current_session(self, *, source: str) -> dict[str, Any]:
        """Return the current chat session, creating one only when absent.

        Non-Electron input surfaces use this owner-level entry point before
        opening a chat turn.  They therefore share the session authority and
        projection that Electron uses, rather than inventing session IDs.
        """
        current_session_id = str(sm.get_current_session_id() or "").strip()
        if current_session_id and current_session_id in sm.list_sessions():
            return self._session_payload(current_session_id)

        if current_session_id:
            sm.set_current_session_id(None)
        result = self._create({})
        result["source"] = source
        await bus.emit(Method.SESSION_CHANGED, result)
        return result

    def _create(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = str(
            params.get("session_id")
            or f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        )
        title = str(params.get("title") or "")
        project_id = str(params.get("project_id") or params.get("projectId") or "").strip()
        if project_id and self._work_coordinator is None:
            return {"ok": False, "error": "work_context_unavailable"}
        previous_sid = str(sm.get_current_session_id() or "").strip()
        if previous_sid:
            sm.save_session(previous_sid, enable_conversation=True)
        sm.create_session(sid)
        if project_id:
            try:
                context = self._work_coordinator.bind_session_context(
                    sid,
                    project_id,
                    source="electron.create",
                )
            except Exception as exc:
                sm.delete_session(sid)
                if previous_sid:
                    sm.load_session(previous_sid)
                return {"ok": False, "error": str(exc)}
            if not title:
                title = str(context.get("projectName") or "Project")
        self._enable_conversation()
        sm.save_session(sid, enable_conversation=True)
        if title:
            sm.set_session_title(sid, title)
        result = self._session_payload(sid)
        self._publish_context_projection_now("session.created")
        return result

    def _load(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("session_id") or "")
        ok, _enable = sm.load_session(sid)
        if not ok:
            return {"ok": False, "error": "session not found"}
        self._enable_conversation()
        sm.save_session(sid, enable_conversation=True)
        result = self._session_payload(sid)
        self._publish_context_projection_now("session.loaded")
        return result

    def _delete(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("session_id") or "")
        ok = sm.delete_session(sid)
        if ok and self._work_coordinator is not None:
            self._work_coordinator.clear_session_project(sid)
        if sm.get_current_session_id() == sid:
            sm.set_current_session_id(None)
        result = {"ok": ok, **self._list()}
        self._publish_context_projection_now("session.deleted")
        return result

    def _rename(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("session_id") or "")
        title = str(params.get("title") or "").strip()
        ok = bool(sid and title and sm.set_session_title(sid, title))
        return {"ok": ok, **self._list()}

    def _session_payload(self, sid: str) -> dict[str, Any]:
        data = self._read_data(sid)
        return {
            "ok": True,
            "session": {
                "id": sid,
                "title": data.get("title") or sm.get_session_title(sid),
                "timestamp": data.get("timestamp", 0),
                "message_count": len(data.get("dialog", []) or []),
                "context": self._context(sid),
            },
            "messages": _display_dialog(data.get("dialog", []) or []),
            "current_session_id": sm.get_current_session_id(),
            **self._list(),
        }

    async def route_context_action(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        """Bounded entry point used by Slice's explicit Open in Chat action."""

        data = payload or {}
        action = str(data.get("action") or "").strip().lower().replace("-", "_")
        if action not in {"open_project", "open_work_item"}:
            return {"ok": False, "error": "unsupported_action"}
        return await self._open_context(data, source="slice")

    async def _create_project(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._work_coordinator is None:
            return {"ok": False, "error": "work_context_unavailable"}
        if self._is_chat_busy():
            return {
                "ok": False,
                "error": "chat_busy",
                "message": "Wait for the current Chat turn to finish before creating a Project.",
            }
        try:
            project = self._work_coordinator.create_project(
                str(params.get("workspace_path") or params.get("workspacePath") or ""),
                name=str(params.get("name") or ""),
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except WorkLedgerConflict as exc:
            code = str(exc)
            return {
                "ok": False,
                "error": code,
                "message": (
                    "Choose a directory inside WORK_PROJECT_ALLOWLIST."
                    if code == "workspace_outside_project_registry"
                    else code
                ),
            }

        opened = self._create(
            {
                "project_id": project["projectId"],
                "title": project["projectName"],
            }
        )
        opened["project"] = project
        opened["projectCreated"] = bool(project.get("created"))
        return opened

    def _correct_project(self, params: dict[str, Any]) -> dict[str, Any]:
        """Explicitly move a chat without rewriting its historical WorkItems.

        The new Project becomes the default for future unnamed work. Existing
        WorkItems keep their original Project identity. Moving is paused only
        while this Session still owns an active writer, because changing its
        default destination mid-run would make the visible context ambiguous.
        """

        if self._work_coordinator is None:
            return {"ok": False, "error": "work_context_unavailable"}
        if self._is_chat_busy():
            return {
                "ok": False,
                "error": "chat_busy",
                "message": "Wait for the current Chat turn to finish before moving it.",
            }
        session_id = str(
            params.get("session_id")
            or params.get("sessionId")
            or sm.get_current_session_id()
            or ""
        ).strip()
        if not session_id or session_id not in sm.list_sessions():
            return {"ok": False, "error": "session_not_found"}
        work_roster = self._work_coordinator.conversation_work_items_for_resolution(
            session_id,
            limit=200,
        )
        running_work = next(
            (
                row
                for row in work_roster.get("items") or []
                if str(row.get("execution") or "").lower() in {"queued", "running"}
            ),
            None,
        )
        if running_work is not None:
            return {
                "ok": False,
                "error": "session_has_active_work",
                "message": "Wait for this chat's running Work to finish before moving it.",
            }

        project_id = str(params.get("project_id") or params.get("projectId") or "").strip()
        try:
            if project_id:
                context = self._work_coordinator.bind_session_context(
                    session_id,
                    project_id,
                    source="electron.correct",
                )
                sm.set_session_title(session_id, str(context.get("projectName") or "Project"))
            else:
                self._work_coordinator.clear_session_project(session_id)
                sm.set_session_title(session_id, "Draft")
        except (ValueError, WorkLedgerConflict) as exc:
            return {"ok": False, "error": str(exc)}
        sm.save_session(session_id, enable_conversation=True)
        result = self._session_payload(session_id)
        result["correctedProject"] = True
        self._publish_context_projection_now("session.project_corrected")
        return result

    async def _open_context(
        self,
        params: dict[str, Any],
        *,
        source: str,
    ) -> dict[str, Any]:
        """Atomically expose a Project/WorkItem-bound chat to every surface."""

        if self._work_coordinator is None:
            return {"ok": False, "error": "work_context_unavailable"}
        if self._is_chat_busy():
            return {
                "ok": False,
                "error": "chat_busy",
                "message": "Wait for the current Chat turn to finish before changing context.",
            }
        project_id = str(params.get("project_id") or params.get("projectId") or "").strip()
        work_item_id = str(
            params.get("work_item_id") or params.get("workItemId") or ""
        ).strip()
        if work_item_id:
            item = self._work_coordinator.store.get_work_item(work_item_id)
            if item is None:
                return {"ok": False, "error": "unknown_work_item"}
            if project_id and project_id != item.project_id:
                return {"ok": False, "error": "work_item_project_mismatch"}
            project_id = item.project_id
        if not project_id:
            return {"ok": False, "error": "missing_project_id"}

        # Prefer the already-owned Chat for this exact context.  Slice never
        # imports or duplicates Electron history; it only asks Electron's
        # session authority to reveal the matching conversation.
        existing_sid = self._matching_session(project_id, work_item_id)
        if existing_sid:
            result = self._load({"session_id": existing_sid})
            result["openedContext"] = True
            result["source"] = source
            await bus.emit(Method.SESSION_CHANGED, result)
            await self._work_coordinator.publish_snapshot(reason="session.context_opened")
            return result

        previous_sid = str(sm.get_current_session_id() or "").strip()
        if previous_sid:
            sm.save_session(previous_sid, enable_conversation=True)
        sid = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        sm.create_session(sid)
        try:
            context = self._work_coordinator.bind_session_context(
                sid,
                project_id,
                work_item_id=work_item_id,
                source=source,
            )
        except Exception as exc:
            sm.delete_session(sid)
            if previous_sid:
                sm.load_session(previous_sid)
            return {"ok": False, "error": str(exc)}
        title = str(context.get("projectName") or "Project")
        if work_item_id:
            item = self._work_coordinator.store.get_work_item(work_item_id)
            if item is not None:
                title = f"{title} · {item.title}"
        sm.set_session_title(sid, title)
        result = self._session_payload(sid)
        result["openedContext"] = True
        result["source"] = source
        await bus.emit(Method.SESSION_CHANGED, result)
        await self._work_coordinator.publish_snapshot(reason="session.context_opened")
        return result

    def _matching_session(self, project_id: str, work_item_id: str) -> str:
        matches: list[tuple[float, str]] = []
        for sid in sm.list_sessions():
            binding = self._work_coordinator.conversation_binding(sid)
            if not binding:
                continue
            if work_item_id:
                if str(binding.get("workItemId") or "") != work_item_id:
                    continue
            else:
                if binding.get("projectId") != project_id:
                    continue
                if str(binding.get("workItemId") or ""):
                    continue
            data = self._read_data(sid)
            matches.append((float(data.get("timestamp") or 0.0), sid))
        return max(matches)[1] if matches else ""

    def _context(self, sid: str) -> dict[str, Any] | None:
        if self._work_coordinator is None:
            return None
        return self._work_coordinator.conversation_binding(sid)

    def _projects(self) -> list[dict[str, Any]]:
        if self._work_coordinator is None:
            return []
        return self._work_coordinator.project_catalog()

    def _publish_context_projection_now(self, reason: str) -> None:
        if self._work_coordinator is None:
            return
        snapshot = self._work_coordinator.snapshot()
        bus.emit_now(Method.WORK_UPDATED, {"work": snapshot, "reason": reason})

    def _read_data(self, sid: str) -> dict[str, Any]:
        path = Path(sm._session_path(sid))  # legacy helper; keeps filename sanitization identical.
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _enable_conversation(self) -> None:
        try:
            from core.chat_runtime import get_chat_runtime
            get_chat_runtime().enable_conversation = True
        except Exception:
            pass
