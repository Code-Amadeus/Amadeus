"""Cross-surface Project/WorkItem conversation binding tests."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.work_ledger_store import WorkLedgerStore
from core import session_manager as sm
from server.event_bus import bus
from server.handlers.session_handler import SessionHandler
from server.protocol import Method
from server.work_ledger_coordinator import WorkLedgerCoordinator


async def _project_and_work_item_contexts_are_atomic() -> None:
    original_session_dir = sm._SESSION_DIR
    original_session_id = sm.get_current_session_id()
    original_dialog = list(sm.conversation_history.dialog)
    with tempfile.TemporaryDirectory(prefix="session_project_context_") as temp:
        root = Path(temp)
        sm._SESSION_DIR = str(root / "sessions")
        sm.set_current_session_id(None)
        sm.conversation_history.reset()
        store = WorkLedgerStore(root / "ledger.sqlite3")
        project_root = root / "project"
        project_root.mkdir()
        project = store.create_or_get_project(project_root, name="Amadeus")
        item = store.create_work_item(
            project.project_id,
            title="Repair routing",
            goal="Keep project context coherent.",
        )
        attempt = store.create_attempt(
            item.work_item_id,
            provider="locus",
            task="Repair the route",
            metadata={"session_id": "origin-chat"},
        )
        store.update_attempt(attempt.attempt_id, execution_status="running")
        coordinator = WorkLedgerCoordinator(store)
        busy = False
        handler = SessionHandler()
        handler.configure(
            work_coordinator=coordinator,
            is_chat_busy=lambda: busy,
        )
        changed: list[dict] = []

        async def observe(_method: str, payload: dict) -> None:
            changed.append(payload)

        bus.on(Method.SESSION_CHANGED, observe)
        try:
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                opened = await handler.handle(
                    Method.SESSION_OPEN_CONTEXT,
                    {
                        "project_id": project.project_id,
                        "work_item_id": item.work_item_id,
                    },
                )
                assert opened is not None and opened["ok"] is True
                sid = str(opened["current_session_id"])
                assert opened["session"]["context"]["bindingKind"] == "work_item"
                assert opened["session"]["context"]["workItemId"] == item.work_item_id
                assert coordinator.session_project(sid) == project.project_id
                snapshot = coordinator.snapshot()
                assert snapshot["destinationProjectId"] == project.project_id
                assert snapshot["projects"][0]["projectId"] == project.project_id
                assert coordinator._task_dock(snapshot)["destinationProjectId"] == project.project_id

                bound_row = coordinator.bound_work_item_status_row(
                    sid, item.work_item_id
                )
                assert bound_row is not None
                assert bound_row["work_item_id"] == item.work_item_id
                persisted = store.get_conversation_binding(sid)
                assert persisted is not None
                assert persisted.anchor_work_item_id == item.work_item_id
                assert changed and changed[-1]["current_session_id"] == sid
                roster = coordinator.conversation_work_items(sid, limit=8)
                assert [row["work_item_id"] for row in roster] == [item.work_item_id]

                reopened = await handler.route_context_action(
                    {
                        "action": "open_work_item",
                        "project_id": project.project_id,
                        "work_item_id": item.work_item_id,
                    }
                )
                assert reopened["current_session_id"] == sid
                assert len(sm.list_sessions()) == 1

                busy = True
                blocked = await handler.route_context_action(
                    {
                        "action": "open_project",
                        "project_id": project.project_id,
                    }
                )
                assert blocked["ok"] is False
                assert blocked["error"] == "chat_busy"
                assert sm.get_current_session_id() == sid
                assert len(sm.list_sessions()) == 1
                busy = False

                general = await handler.handle(
                    Method.SESSION_CREATE,
                    {"session_id": "general-chat", "title": "General"},
                )
                assert general is not None
                assert general["session"]["context"] is None
                assert coordinator.session_project("general-chat") == ""
                sm.conversation_history.add_user("Keep this conversation history.")
                sm.save_session("general-chat", enable_conversation=True)
                corrected = await handler.handle(
                    Method.SESSION_CORRECT_PROJECT,
                    {
                        "session_id": "general-chat",
                        "project_id": project.project_id,
                    },
                )
                assert corrected is not None and corrected["ok"] is True
                assert corrected["current_session_id"] == "general-chat"
                assert corrected["session"]["context"]["projectId"] == project.project_id
                assert corrected["messages"][0]["content"] == "Keep this conversation history."

                created_root = root / "created-project"
                created_root.mkdir()
                created = await handler.handle(
                    Method.PROJECT_CREATE,
                    {"workspace_path": str(created_root)},
                )
                assert created is not None and created["ok"] is True
                assert created["projectCreated"] is True
                created_project_id = created["project"]["projectId"]
                created_session_id = created["current_session_id"]
                assert created["session"]["context"]["projectId"] == created_project_id
                assert created["messages"] == []

                second_project_chat = await handler.handle(
                    Method.SESSION_CREATE,
                    {
                        "project_id": created_project_id,
                        "title": "Fresh Project chat",
                    },
                )
                assert second_project_chat is not None and second_project_chat["ok"] is True
                assert second_project_chat["current_session_id"] != created_session_id
                assert second_project_chat["session"]["context"]["projectId"] == created_project_id
                assert second_project_chat["messages"] == []

                active_move = await handler.handle(
                    Method.SESSION_CORRECT_PROJECT,
                    {
                        "session_id": sid,
                        "project_id": created_project_id,
                    },
                )
                assert active_move is not None and active_move["ok"] is False
                assert active_move["error"] == "session_has_active_work"

                store.update_attempt(attempt.attempt_id, execution_status="succeeded")
                moved = await handler.handle(
                    Method.SESSION_CORRECT_PROJECT,
                    {
                        "session_id": sid,
                        "project_id": created_project_id,
                    },
                )
                assert moved is not None and moved["ok"] is True
                assert moved["session"]["context"]["projectId"] == created_project_id
                assert store.get_work_item(item.work_item_id).project_id == project.project_id  # type: ignore[union-attr]

                project_chat = await handler.handle(
                    Method.SESSION_OPEN_CONTEXT,
                    {"project_id": project.project_id},
                )
                assert project_chat is not None and project_chat["ok"] is True
                project_snapshot = coordinator.project_status_snapshot(
                    project.project_id
                )
                assert project_snapshot is not None
                assert project_snapshot["projectName"] == "Amadeus"

                restored = await handler.handle(
                    Method.SESSION_LOAD,
                    {"session_id": sid},
                )
                assert restored is not None and restored["ok"] is True
                assert restored["session"]["context"]["projectId"] == created_project_id
                assert coordinator.session_project(sid) == created_project_id
        finally:
            bus.off(Method.SESSION_CHANGED, observe)
            coordinator.close()
            sm._SESSION_DIR = original_session_dir
            sm.set_current_session_id(original_session_id)
            sm.conversation_history.dialog = original_dialog


def test_project_and_work_item_contexts_are_atomic() -> None:
    asyncio.run(_project_and_work_item_contexts_are_atomic())
    print("ok: Project and WorkItem chat contexts persist and switch atomically")


if __name__ == "__main__":
    test_project_and_work_item_contexts_are_atomic()
