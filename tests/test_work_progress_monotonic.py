"""Workflow progress monotonic clamp regression tests.

Live provider events carry coarse per-type progress hints (tool=52,
delta=68, artifact=74, ...). Replaying them verbatim made the wallpaper
progress bar saw-tooth (2026-07-13 snapshot section 6). The bar must
never move backwards within one run; a retry is a new run_id and may
start low again.

Runnable directly by tools/run_tests.py and compatible with pytest.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as settings
from server.event_bus import bus
from server.handlers.work_activity_handler import WorkActivityCoordinator
from server.protocol import Method


def _event(run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "provider": "openclaw",
        "run_id": run_id,
        "task": "Render a monotonic progress bar.",
        "type": event_type,
        "payload": payload or {},
    }


async def _drive(events: list[dict[str, Any]]) -> tuple[WorkActivityCoordinator, list[dict[str, Any]]]:
    canvases: list[dict[str, Any]] = []

    async def capture_canvas(_method: str, params: dict[str, Any]) -> None:
        canvases.append(params)

    coordinator = WorkActivityCoordinator()
    bus.on(Method.WALLPAPER_CANVAS, capture_canvas)
    old_interval = getattr(settings, "PROVIDER_WORK_HEARTBEAT_S", 45)
    settings.PROVIDER_WORK_HEARTBEAT_S = 0
    try:
        for event in events:
            await coordinator._on_provider_event(Method.PROVIDER_EVENT, event)
    finally:
        settings.PROVIDER_WORK_HEARTBEAT_S = old_interval
        bus.off(Method.WALLPAPER_CANVAS, capture_canvas)
    return coordinator, canvases


def _progress_values(canvases: list[dict[str, Any]]) -> list[int]:
    return [int(c.get("progress")) for c in canvases if c.get("progress") is not None]


async def test_live_event_progress_never_moves_backwards() -> None:
    run_id = "run-monotonic"
    _coordinator, canvases = await _drive(
        [
            _event(run_id, "run.created"),
            _event(run_id, "tool.call", {"tool": "Write"}),
            _event(run_id, "semantic.progress", {"summary": "Half of the board renders."}),
            _event(run_id, "tool.call", {"tool": "Bash"}),
            _event(run_id, "artifact.created", {"artifact_type": "file", "path": "chess.html"}),
            _event(run_id, "run.status", {"status": "running"}),
            _event(run_id, "tool.call", {"tool": "Read"}),
            _event(run_id, "run.status", {"status": "done"}),
        ]
    )
    values = _progress_values(canvases)
    assert values, "expected workflow canvases with progress"
    assert values == sorted(values), f"progress moved backwards: {values}"
    assert values[0] == 10, f"intake should open the run at 10, got {values[0]}"
    assert 70 in values, f"semantic progress should reach 70: {values}"
    raw_after_semantic = values[values.index(70) + 1 :]
    assert all(v >= 70 for v in raw_after_semantic), (
        f"tool=52 / running=24 hints must clamp to the high-water mark: {values}"
    )
    assert values[-1] == 92, f"terminal review should land at 92, got {values[-1]}"
    print("ok: live event progress hints clamp to the run's high-water mark")


async def test_new_run_id_resets_the_clamp() -> None:
    first = "run-clamp-first"
    second = "run-clamp-second"
    _coordinator, canvases = await _drive(
        [
            _event(first, "run.created"),
            _event(first, "run.status", {"status": "done"}),
            _event(second, "run.created"),
        ]
    )
    values = _progress_values(canvases)
    assert values[-1] == 10, f"a fresh run_id must start low again, got {values}"
    print("ok: the monotonic clamp is per run and resets on a new run_id")


async def test_run_intake_stays_on_the_work_surface_without_narration() -> None:
    coordinator = WorkActivityCoordinator()
    notes: list[dict[str, Any]] = []

    async def capture_note(_method: str, params: dict[str, Any]) -> None:
        notes.append(params)

    bus.on(Method.CHAT_WORK_NOTE, capture_note)
    try:
        await coordinator._on_provider_event(
            Method.PROVIDER_EVENT,
            _event("run-intake-direction", "run.created"),
        )
    finally:
        bus.off(Method.CHAT_WORK_NOTE, capture_note)

    assert len(notes) == 1
    note = notes[0]
    assert note["summary"] == "Render a monotonic progress bar."
    assert note["phase"] == "Intake"
    assert "narration_keypoint" not in note["metadata"]
    assert "result" not in note["metadata"]
    print("ok: bounded Host intake stays visible without becoming speech")


async def test_first_provider_direction_after_intake_remains_narratable() -> None:
    coordinator = WorkActivityCoordinator()
    notes: list[dict[str, Any]] = []

    async def capture_note(_method: str, params: dict[str, Any]) -> None:
        notes.append(params)

    bus.on(Method.CHAT_WORK_NOTE, capture_note)
    try:
        await coordinator._on_provider_event(
            Method.PROVIDER_EVENT,
            _event("run-first-provider-direction", "run.created"),
        )
        await coordinator._on_provider_event(
            Method.PROVIDER_EVENT,
            _event(
                "run-first-provider-direction",
                "assistant.update",
                {
                    "text": "Mapping the two-player controls before implementation.",
                    "source": "provider_tool_title",
                },
            ),
        )
    finally:
        bus.off(Method.CHAT_WORK_NOTE, capture_note)

    directional = [
        note
        for note in notes
        if note.get("metadata", {}).get("narration_keypoint")
        == "directional_progress"
    ]
    assert len(directional) == 1
    assert directional[0]["summary"] == (
        "Mapping the two-player controls before implementation."
    )
    assert directional[0]["metadata"]["semantic_candidate"] is True
    print("ok: the first Provider-authored direction remains narratable")


async def test_artifact_canvases_share_the_clamp() -> None:
    coordinator = WorkActivityCoordinator()
    canvases: list[dict[str, Any]] = []

    async def capture_canvas(_method: str, params: dict[str, Any]) -> None:
        canvases.append(params)

    bus.on(Method.WALLPAPER_CANVAS, capture_canvas)
    try:
        state = coordinator._run_state({"run_id": "run-artifact-clamp", "provider": "browser"})
        state["last_progress"] = 92
        await coordinator._emit_browser_canvas(
            state,
            {"title": "Docs", "url": "https://example.com", "excerpt": "snapshot"},
            phase="Preview",
            progress=82,
        )
    finally:
        bus.off(Method.WALLPAPER_CANVAS, capture_canvas)
    assert canvases, "expected a browser canvas"
    assert int(canvases[-1].get("progress")) == 92, (
        f"browser preview must not drop below the high-water mark: {canvases[-1].get('progress')}"
    )
    assert state["last_progress"] == 92
    print("ok: browser and diff canvases share the same monotonic clamp")


async def test_semantic_lead_survives_status_and_heartbeat_refreshes() -> None:
    run_id = "run-semantic-lead"
    coordinator = WorkActivityCoordinator()
    canvases: list[dict[str, Any]] = []

    async def capture_canvas(_method: str, params: dict[str, Any]) -> None:
        canvases.append(params)

    bus.on(Method.WALLPAPER_CANVAS, capture_canvas)
    try:
        await coordinator._on_provider_event(Method.PROVIDER_EVENT, _event(run_id, "run.created"))
        await coordinator._on_provider_event(
            Method.PROVIDER_EVENT,
            _event(
                run_id,
                "semantic.progress",
                {
                    "milestone": "design",
                    "summary": "The render path now reuses one native Slice surface.",
                },
            ),
        )
        await coordinator._on_provider_event(
            Method.PROVIDER_EVENT,
            _event(run_id, "run.status", {"status": "running"}),
        )
        state = coordinator._runs[run_id]
        await coordinator._emit_heartbeat_canvas(state, now=time.monotonic() + 1.0)
    finally:
        bus.off(Method.WALLPAPER_CANVAS, capture_canvas)

    semantic_lead = "The render path now reuses one native Slice surface."
    assert [canvas.get("lead") for canvas in canvases[-3:]] == [semantic_lead] * 3
    for canvas in canvases[-3:]:
        presentation = canvas.get("metadata", {}).get("presentation", {})
        assert "lead" not in presentation, "a static projection must not overwrite semantic text"
    print("ok: periodic canvas refreshes preserve the latest semantic lead")


async def test_heartbeat_names_the_run_provider_instead_of_a_legacy_default() -> None:
    coordinator = WorkActivityCoordinator()
    canvases: list[dict[str, Any]] = []

    async def capture_canvas(_method: str, params: dict[str, Any]) -> None:
        canvases.append(params)

    state = coordinator._run_state(
        {
            "run_id": "run-provider-label",
            "provider": "codex",
            "task": "Verify the active provider label.",
        }
    )
    state["liveness"] = "stalled"
    state["liveness_payload"] = {
        "silence_s": 5,
        "probe_status": "running",
    }
    bus.on(Method.WALLPAPER_CANVAS, capture_canvas)
    try:
        await coordinator._emit_heartbeat_canvas(state, now=time.monotonic() + 1.0)
    finally:
        bus.off(Method.WALLPAPER_CANVAS, capture_canvas)

    detail = canvases[-1]["signals"][0]["detail"]
    assert "reports running" in detail
    assert "Codex" in detail
    assert "Locus" not in detail


async def main() -> None:
    await test_live_event_progress_never_moves_backwards()
    await test_new_run_id_resets_the_clamp()
    await test_run_intake_stays_on_the_work_surface_without_narration()
    await test_first_provider_direction_after_intake_remains_narratable()
    await test_artifact_canvases_share_the_clamp()
    await test_semantic_lead_survives_status_and_heartbeat_refreshes()
    await test_heartbeat_names_the_run_provider_instead_of_a_legacy_default()


if __name__ == "__main__":
    asyncio.run(main())
