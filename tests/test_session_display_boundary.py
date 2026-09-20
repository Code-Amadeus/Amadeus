"""Display-only cleanup for persisted machine annotations."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from server.handlers.session_handler import SessionHandler, _display_dialog, _display_text


def test_work_observer_marker_is_hidden_only_at_the_display_boundary() -> None:
    stored = "[WORK_OBSERVER]\nThe delegated search completed."
    assert _display_text(stored) == "The delegated search completed."
    assert _display_dialog([{"role": "assistant", "content": stored}]) == [
        {"role": "assistant", "content": "The delegated search completed."}
    ]


def test_work_observer_words_inside_normal_chat_are_not_removed() -> None:
    text = "The marker [WORK_OBSERVER] is part of this explanation."
    assert _display_text(text) == text


def test_session_index_is_newest_first() -> None:
    handler = SessionHandler()
    records = {
        "older": {"title": "Older", "timestamp": 10},
        "newer": {"title": "Newer", "timestamp": 20},
        "newest": {"title": "Newest", "timestamp": 30},
    }
    with (
        patch("server.handlers.session_handler.sm.list_sessions", return_value=list(records)),
        patch("server.handlers.session_handler.sm.get_current_session_id", return_value="newest"),
        patch.object(handler, "_read_data", side_effect=lambda sid: records[sid]),
    ):
        result = handler._list()
    assert [session["id"] for session in result["sessions"]] == [
        "newest",
        "newer",
        "older",
    ]


def test_chat_history_uses_an_internal_project_and_draft_rail() -> None:
    root = Path(__file__).resolve().parents[1]
    chat = (root / "electron" / "src" / "renderer" / "components" / "ChatPage.tsx").read_text(
        encoding="utf-8"
    )
    rail = (
        root / "electron" / "src" / "renderer" / "components" / "ChatSessionRail.tsx"
    ).read_text(encoding="utf-8")
    apps = (
        root / "electron" / "src" / "renderer" / "components" / "ProjectAppsPanel.tsx"
    ).read_text(encoding="utf-8")
    assert "<ChatSessionRail" in chat
    assert "maxHeight: 124" not in chat
    assert "PROJECTS" in rail
    assert "label: 'Drafts'" in rail
    assert "projectGroups.map(renderProjectHeader)" in rail
    assert 'title="New Project"' in rail
    assert "onNewProjectSession(group.projectId" in rail
    assert "onOpenProject(group.projectId" in rail
    assert "onMouseEnter={openRail}" in rail
    assert 'aria-label="Chat history"\n      onMouseEnter' not in rail
    assert "aria-expanded={railOpen}\n        tabIndex={0}\n        onMouseEnter={openRail}" in rail
    assert "RAIL_CLOSE_DELAY_MS = 180" in rail
    assert "setPreviewGroupId(group.id)" in rail
    assert "onMouseEnter={() => setPreviewGroupId(group.id)}" not in rail
    assert "onClick={() => setPreviewGroupId(group.id)}" in rail
    assert "if (!selected) event.currentTarget.style.background = 'var(--hover)'" in rail
    assert "SESSION_PAGE_SIZE = 30" in rail
    assert "previewGroup.sessions.slice(0, visibleCount)" in rail
    assert 'aria-label="Search chats"' in rail
    assert 'type RailMode = \'chats\' | \'artifacts\'' in rail
    assert 'aria-label="Artifact collections"' in rail
    assert "Draft artifacts" in rail
    assert "Recent 5" in rail
    assert "onClick={onOpenDraftApps}" in rail
    assert "Open ${group.label} Apps" not in rail
    assert 'title="Open recent Draft Apps"' not in rail
    assert "revealedGroup" not in rail
    assert "setCollapsed" not in rail
    assert "toggleGroup" not in rail
    assert 'aria-label="Current chat project"' in chat
    assert chat.count('title="New chat"') >= 1
    assert "send('project.apps.list'" in chat
    assert "send('session.open_context'" in chat
    assert "mode: 'observe'" in chat
    assert "Interact with Amadeus" in apps
    assert "Promote to Project" in apps
    assert "The five most recent verified Draft artifacts" in apps
    assert "onInteract(app)" in apps
    assert "project.apps.list" not in apps
    assert "Promoted artifacts appear here without copying their files" in apps
    assert "send('draft.apps.list', { limit: 5 })" in chat
    assert "DRAFT_APPS_VIEW_ID" in chat
    assert "sourceSessionId" in chat


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all session display boundary tests passed")


if __name__ == "__main__":
    _main()
