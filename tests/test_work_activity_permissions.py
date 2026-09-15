from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as settings
from server.event_bus import bus
from server.handlers.work_activity_handler import WorkActivityCoordinator
from server.protocol import Method


WORK_BINDING = {
    "work_item_id": "work_permission_canvas",
    "attempt_id": "attempt_permission_canvas",
    "attempt_number": 3,
}


async def _capture_permission_flow(
    events: list[dict[str, Any]],
    *,
    include_result: bool = False,
) -> tuple[WorkActivityCoordinator, list[dict[str, Any]]]:
    canvases: list[dict[str, Any]] = []

    async def capture_canvas(_method: str, params: dict[str, Any]) -> None:
        canvases.append(params)

    coordinator = WorkActivityCoordinator()
    bus.on(Method.WALLPAPER_CANVAS, capture_canvas)
    try:
        for event in events:
            await coordinator._on_provider_event(Method.PROVIDER_EVENT, event)
        if include_result:
            await coordinator._on_provider_result(
                Method.PROVIDER_RESULT,
                {
                    "provider": "locus",
                    "run_id": "locus-permission-run",
                    "status": "done",
                    "result": "SHOULD_NOT_REACH_PERMISSION_CANVAS secret_result_body",
                    "metadata": {"locus_job_id": "job-42"},
                },
            )
    finally:
        bus.off(Method.WALLPAPER_CANVAS, capture_canvas)
    return coordinator, canvases


def _request_event() -> dict[str, Any]:
    return {
        "provider": "locus",
        "run_id": "locus-permission-run",
        "task": "Write the chess game to Desktop.",
        "cwd": r"F:\Computer_Science\Amadeus\amadeus",
        "type": "permission.requested",
        "metadata": {"work": dict(WORK_BINDING), "session_id": "session-permission"},
        "payload": {
            "request_id": "permission-write-desktop",
            "capability": "filesystem.external_write",
            "action": "write",
            "scope": [r"C:\Users\user-example\Desktop\chess_game.py"],
            "reason": "Desktop is outside the workspace; " + "api" + "_key=TOP_SECRET_VALUE",
            "reversible": True,
            "diagnosticOnly": True,
            "options": [
                {"id": "allow_once", "secret": "OPTION_SECRET"},
                {"label": "deny", "content": "OPTION_CONTENT_SECRET"},
            ],
            "content": "RAW_FILE_CONTENT_MUST_NOT_LEAK",
            "secret": "RAW_SECRET_MUST_NOT_LEAK",
            "environment": {"DEEPSEEK_API_KEY": "ENV_SECRET_MUST_NOT_LEAK"},
        },
    }


async def test_cooperative_provider_execution_keeps_scene_and_slice_without_work_narration(monkeypatch) -> None:
    from server.character_presentation import CharacterPresentationCoordinator

    coordinator = WorkActivityCoordinator()
    canvases, notes, scene_events = [], [], []
    monkeypatch.setattr(settings, "PROVIDER_WORK_HEARTBEAT_S", 0)

    async def capture_scene(method, payload):
        scene_events.append((method, payload))

    monkeypatch.setattr("server.handlers.work_activity_handler.character_presentation",
        CharacterPresentationCoordinator(capture_scene))

    async def capture_canvas(_method, params):
        canvases.append(params)

    async def capture_note(_method, params):
        notes.append(params)

    bus.on(Method.WALLPAPER_CANVAS, capture_canvas)
    bus.on(Method.CHAT_WORK_NOTE, capture_note)
    metadata = {"cooperative_context_id":"context-chat", "session_id":"session-chat",
        "turn_id":"turn-chat"}
    try:
        await coordinator._on_provider_event(Method.PROVIDER_EVENT, {
            "provider":"codex", "run_id":"run-chat", "type":"run.created",
            "payload":{"task":"Read the directory"}, "metadata":metadata})
        assert "run-chat" in coordinator._active_runs
        assert canvases[-1]["phase"] == "Intake"
        await coordinator._on_provider_event(Method.PROVIDER_EVENT, {
            "provider":"codex", "run_id":"run-chat", "type":"semantic.progress",
            "payload":{"milestone":"validation", "summary":"The game passed its structural check."},
            "metadata":metadata})
        await coordinator._on_provider_result(Method.PROVIDER_RESULT, {
            "provider":"codex", "run_id":"run-chat", "status":"done",
            "result":"No files", "metadata":metadata})
    finally:
        bus.off(Method.WALLPAPER_CANVAS, capture_canvas)
        bus.off(Method.CHAT_WORK_NOTE, capture_note)
    assert canvases[-1]["phase"] == "Result"
    assert notes  # Native progress uses the original broadcaster again.
    assert any(note.get("metadata", {}).get("narration_keypoint") == "semantic_progress"
        for note in notes)
    assert all(note.get("phase", "").lower() != "result" for note in notes)
    assert all(not canvas.get("metadata", {}).get("work") for canvas in canvases)
    assert "run-chat" not in coordinator._active_runs
    assert [payload["activity"] for method, payload in scene_events
        if method == Method.WALLPAPER_ACTIVITY] == ["work"]
    assert coordinator._runs["run-chat"]["release_owned_by_observer"] is True
    await coordinator.release_work_presentation("run-chat", reason="native_role_finished")
    assert [payload["activity"] for method, payload in scene_events
        if method == Method.WALLPAPER_ACTIVITY] == ["work", ""]


async def test_native_permission_owner_is_not_overwritten_by_work_activity(monkeypatch):
    from unittest.mock import AsyncMock
    coordinator = WorkActivityCoordinator()
    emit = AsyncMock()
    monkeypatch.setattr(bus, "emit", emit)
    state = {"provider":"codex", "run_id":"native-run", "last_progress":24,
        "metadata":{"cooperative_context_id":"context-a", "session_id":"session-a"}}
    permission = {"id":"native-request", "reason":"Copy the game to Desktop",
        "options":["allow_once", "deny"], "action":"execute_command"}
    await coordinator._emit_permission_canvas(state, permission)
    assert not any(call.args[0] == Method.WALLPAPER_CANVAS for call in emit.await_args_list)
    assert any(call.args[0] == Method.CHAT_WORK_NOTE for call in emit.await_args_list)
    await coordinator._emit_permission_canvas({**state, "metadata":{}}, permission)
    assert any(call.args[0] == Method.WALLPAPER_CANVAS for call in emit.await_args_list)


async def test_provider_permission_request_is_bounded_nonblocking_diagnostic() -> None:
    old_interval = getattr(settings, "PROVIDER_WORK_HEARTBEAT_S", 45)
    settings.PROVIDER_WORK_HEARTBEAT_S = 0
    try:
        coordinator, canvases = await _capture_permission_flow([_request_event()])
    finally:
        settings.PROVIDER_WORK_HEARTBEAT_S = old_interval

    assert len(canvases) == 1
    diagnostic = canvases[0]
    assert diagnostic["mode"] == "workflow"
    assert diagnostic["phase"] == "Checkpoint"
    assert diagnostic["title"] == "Locus action blocked"
    assert diagnostic["blocking"] is False
    assert diagnostic["permissionVisible"] is False
    assert "permissionRequest" not in diagnostic
    request = coordinator._bounded_permission_request(
        coordinator._runs["locus-permission-run"],
        _request_event()["payload"],
    )
    assert set(request) == {
        "id",
        "capability",
        "action",
        "tool",
        "scope",
        "reason",
        "reversibility",
        "options",
        "diagnosticOnly",
        "retryRequired",
    }
    assert request["id"] == "permission-write-desktop"
    assert request["scope"] == [r"C:\Users\user-example\Desktop\chess_game.py"]
    assert request["options"] == ["deny"]
    assert request["diagnosticOnly"] is True
    assert request["retryRequired"] is False
    assert request["reversibility"] == "reversible"
    serialized = json.dumps(request, ensure_ascii=False)
    for forbidden in (
        "TOP_SECRET_VALUE",
        "RAW_FILE_CONTENT_MUST_NOT_LEAK",
        "RAW_SECRET_MUST_NOT_LEAK",
        "ENV_SECRET_MUST_NOT_LEAK",
        "OPTION_SECRET",
        "OPTION_CONTENT_SECRET",
    ):
        assert forbidden not in serialized
    stored = coordinator._runs["locus-permission-run"]["pending_permissions"]
    assert stored == {}
    print("ok: provider denial is visible without becoming an actionable approval")


async def test_provider_diagnostic_does_not_hide_sparse_terminal_result() -> None:
    old_interval = getattr(settings, "PROVIDER_WORK_HEARTBEAT_S", 45)
    settings.PROVIDER_WORK_HEARTBEAT_S = 0
    try:
        _coordinator, canvases = await _capture_permission_flow([_request_event()], include_result=True)
    finally:
        settings.PROVIDER_WORK_HEARTBEAT_S = old_interval

    canvas = canvases[-1]
    assert canvas["phase"] == "Result"
    assert canvas["mode"] != "permission"
    assert canvas.get("permissionVisible") is not True
    assert canvas["metadata"]["work"] == WORK_BINDING
    serialized = json.dumps(canvas, ensure_ascii=False)
    assert "SHOULD_NOT_REACH_PERMISSION_CANVAS" in serialized
    assert "secret_result_body" in serialized
    print("ok: a retrospective denial cannot hide the terminal provider result")


async def test_permission_required_accepts_nested_provider_payload() -> None:
    old_interval = getattr(settings, "PROVIDER_WORK_HEARTBEAT_S", 45)
    settings.PROVIDER_WORK_HEARTBEAT_S = 0
    event = {
        "provider": "openclaw",
        "run_id": "openclaw-permission-required",
        "type": "permission.required",
        "metadata": {"work": dict(WORK_BINDING)},
        "payload": {
            "permissionRequest": {
                "requestId": "permission-nested",
                "capability": "filesystem.write",
                "operation": "export",
                "paths": [r"C:\Users\user-example\Desktop\chess_game.py"],
                "message": "Approve the explicit export target.",
                "reversibility": "replaceable",
                "choices": ["allow_once", "deny"],
            },
            "content": "NESTED_WRAPPER_CONTENT_SECRET",
        },
    }
    try:
        _coordinator, canvases = await _capture_permission_flow([event])
    finally:
        settings.PROVIDER_WORK_HEARTBEAT_S = old_interval

    assert canvases
    state = _coordinator._runs["openclaw-permission-required"]
    request = canvases[-1]["permissionRequest"]
    assert request["id"] == "permission-nested"
    assert request["action"] == "export"
    assert request["scope"] == [r"C:\Users\user-example\Desktop\chess_game.py"]
    assert "NESTED_WRAPPER_CONTENT_SECRET" not in json.dumps(request, ensure_ascii=False)
    assert state["pending_permissions"] == {request["id"]: request}
    print("ok: a non-Locus provider retains its existing explicit checkpoint contract")


async def test_locus_tool_use_id_and_path_scope_match_the_durable_contract() -> None:
    event = {
        "provider": "locus",
        "run_id": "locus-structured-permission",
        "type": "permission.requested",
        "metadata": {"work": dict(WORK_BINDING)},
        "payload": {
            "toolName": "Write",
            "toolUseId": "write-desktop-structured",
            "capability": "filesystem.write",
            "action": "write_file",
            "scope": {
                "kind": "path",
                "path": r"C:\Users\user-example\Desktop\structured.py",
            },
            "reason": "The target is outside the approved workspace.",
            "diagnosticOnly": True,
            "options": [
                {"id": "approve_once", "kind": "allow_once"},
                {"id": "reject", "kind": "reject_once"},
            ],
        },
    }
    coordinator, canvases = await _capture_permission_flow([event])
    assert len(canvases) == 1
    assert canvases[0]["permissionVisible"] is False
    state = coordinator._runs["locus-structured-permission"]
    request = coordinator._bounded_permission_request(state, event["payload"])
    assert request["id"] == "write-desktop-structured"
    assert request["scope"] == [r"C:\Users\user-example\Desktop\structured.py"]
    assert request["options"] == ["deny"]
    assert state["pending_permissions"] == {}
    print("ok: transient Locus denial identity and scope remain available for durable audit")


async def test_provider_resolution_events_do_not_resurrect_diagnostic_cards() -> None:
    old_interval = getattr(settings, "PROVIDER_WORK_HEARTBEAT_S", 45)
    settings.PROVIDER_WORK_HEARTBEAT_S = 0
    denied = {
        "provider": "locus",
        "run_id": "locus-permission-run",
        "type": "permission.denied",
        "metadata": {"locus_job_id": "job-42"},
        "payload": {
            "request_id": "permission-write-desktop",
            "reason": "User denied this export; token=DENIAL_SECRET",
            "content": "DENIAL_CONTENT_SECRET",
        },
    }
    try:
        coordinator, canvases = await _capture_permission_flow([_request_event(), denied])
    finally:
        settings.PROVIDER_WORK_HEARTBEAT_S = old_interval

    assert len(canvases) == 1
    assert coordinator._runs["locus-permission-run"]["pending_permissions"] == {}

    allowed_event = _request_event()
    allowed_event["run_id"] = "locus-permission-allowed"
    allowed_event["payload"] = {**allowed_event["payload"], "request_id": "permission-allowed"}
    resolved = {
        "provider": "locus",
        "run_id": "locus-permission-allowed",
        "type": "permission.resolved",
        "payload": {"request_id": "permission-allowed", "status": "allowed"},
    }
    settings.PROVIDER_WORK_HEARTBEAT_S = 0
    try:
        allowed_coordinator, allowed_canvases = await _capture_permission_flow([allowed_event, resolved])
    finally:
        settings.PROVIDER_WORK_HEARTBEAT_S = old_interval
    assert len(allowed_canvases) == 1
    assert allowed_coordinator._runs["locus-permission-allowed"]["pending_permissions"] == {}
    print("ok: provider resolution events cannot resurrect an actionable permission card")


async def test_repeated_provider_denials_coalesce_to_one_visible_checkpoint() -> None:
    first = _request_event()
    first["payload"] = {**first["payload"], "toolName": "Bash"}
    repeated = _request_event()
    repeated["payload"] = {
        **repeated["payload"],
        "request_id": "permission-write-desktop-second-call",
        "toolName": "PowerShell",
    }
    coordinator, canvases = await _capture_permission_flow([first, repeated])
    assert len(canvases) == 1
    state = coordinator._runs["locus-permission-run"]
    assert state["permission_diagnostic_count"] == 2
    assert state["pending_permissions"] == {}
    print("ok: repeated retrospective denials stay visible without becoming speech spam")


async def main() -> None:
    await test_provider_permission_request_is_bounded_nonblocking_diagnostic()
    await test_provider_diagnostic_does_not_hide_sparse_terminal_result()
    await test_permission_required_accepts_nested_provider_payload()
    await test_locus_tool_use_id_and_path_scope_match_the_durable_contract()
    await test_provider_resolution_events_do_not_resurrect_diagnostic_cards()
    await test_repeated_provider_denials_coalesce_to_one_visible_checkpoint()


if __name__ == "__main__":
    asyncio.run(main())
