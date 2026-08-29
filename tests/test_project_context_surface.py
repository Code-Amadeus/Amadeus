"""Static UI boundary checks for Project/WorkItem Chat contexts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
SLICE = ROOT / "render" / "web" / "crt_canvas_surface.js"
CHAT = ROOT / "electron" / "src" / "renderer" / "components" / "ChatPage.tsx"
CHAT_ACTIVITY = ROOT / "electron" / "src" / "renderer" / "components" / "chatWorkActivity.ts"
APP = ROOT / "electron" / "src" / "renderer" / "App.tsx"


def test_slice_context_switch_is_explicit_and_separate_from_view_selection() -> None:
    source = SLICE.read_text(encoding="utf-8")
    assert '["projects", ui("projects")]' in source
    assert 'data-conversation-open=\\"project\\"' in source
    assert 'data-conversation-open=\\"work-item\\"' in source
    assert 'postCanvasAction("conversation", action' in source
    assert 'postCanvasAction("work_item", "select"' in source
    assert 'data-work-item-id=\\"' in source


def test_electron_owns_history_and_receives_cross_surface_session_changes() -> None:
    source = CHAT.read_text(encoding="utf-8")
    app_source = APP.read_text(encoding="utf-8")
    assert "send('session.create'" in source
    assert "project_id: projectId" in source
    assert "subscribe('session.changed'" in source
    assert "subscribe('session.changed'" in app_source
    assert "setPage('chat')" in app_source
    assert "focusMainWindow()" in app_source
    assert 'aria-label="Current chat project"' in source
    assert "handleProjectContextChange" not in source
    assert "Promote this Draft to a Project" in source
    assert "session.correct_project" in source
    assert "Move this chat and preserve its history" in source
    assert "setMessages(toMessages(res.messages))" in source


def test_electron_projects_observer_entries_without_inventing_chat_content() -> None:
    source = CHAT.read_text(encoding="utf-8")
    assert "subscribe('chat.observer_decision'" in source
    assert "p.append_to_main_chat !== true" in source
    assert "p.main_chat_entry" in source
    assert "sessionId !== activeSessionRef.current" in source
    assert "messageId || attemptId || runId || workItemId" in source
    assert "work-observer:${identity}:${action}:${noteCount}" in source


def test_provider_activity_is_attached_to_the_originating_chat_turn_only() -> None:
    source = CHAT.read_text(encoding="utf-8")
    projection = CHAT_ACTIVITY.read_text(encoding="utf-8")
    assert "subscribe('provider.event'" in source
    assert "subscribe('provider.result'" in source
    assert "send('provider.activity.list'" in source
    assert "activitiesByTurn.get(msg.turnId)" in source
    assert "<ChatWorkActivityCard" in source
    assert "turnId: String(m.turn_id ?? m.turnId ?? '')" in source
    assert "setMessages(prev => applyProviderEvent" not in source
    assert "if (!eventOrigin.sessionId || !eventOrigin.turnId) return runs" in projection
    assert "assistant.delta" not in projection


def test_permission_terminal_events_and_stale_actions_cannot_leave_attention_cards() -> None:
    surface = SLICE.read_text(encoding="utf-8")
    projection = CHAT_ACTIVITY.read_text(encoding="utf-8")
    for event_type in ("permission.resolved", "permission.expired"):
        assert event_type in projection
    assert 'state.permissionRequest.id === request.id' in surface
    assert '"permission_request_not_pending"' in surface
    assert 'state.permissionVisible = false;' in surface


def test_host_readonly_answer_has_session_and_renderer_message_identity() -> None:
    async def scenario() -> None:
        import server.app as server_app
        from server.event_bus import bus

        emitted: list[tuple[str, dict]] = []

        async def capture(method: str, params: dict) -> None:
            emitted.append((method, dict(params)))

        with (
            patch.object(server_app, "_wait_for_output_idle", new=AsyncMock(return_value=False)),
            patch("core.session_manager.get_current_session_id", return_value="session-a"),
            patch("core.session_manager.conversation_history.add_assistant"),
            patch.object(bus, "emit", new=capture),
        ):
            assert await server_app._speak_task_lookup_answer(
                "まだ作業中よ。",
                source="work_ledger_status",
            )

        assert len(emitted) == 1
        payload = emitted[0][1]
        assert payload["session_id"] == "session-a"
        assert payload["message_id"].startswith(
            "host-answer:work_ledger_status:"
        )
        assert payload["append_to_main_chat"] is True

    asyncio.run(scenario())


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all Project context surface tests passed")


if __name__ == "__main__":
    _main()
