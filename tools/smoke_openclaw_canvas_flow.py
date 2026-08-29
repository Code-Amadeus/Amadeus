from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.event_bus import bus
from server.handlers.work_activity_handler import WorkActivityCoordinator
from server.protocol import Method
from server.work_observer import WorkObserverCoordinator


async def main() -> None:
    captured: list[tuple[str, dict[str, Any]]] = []
    appended: list[dict[str, Any]] = []
    narrated: list[dict[str, Any]] = []

    async def capture(method: str, params: dict[str, Any]) -> None:
        captured.append((method, dict(params)))

    bus.on(Method.WALLPAPER_CANVAS, capture)
    bus.on(Method.WALLPAPER_ACTIVITY, capture)
    bus.on(Method.RENDER_SPRITEFORGE_INTENT, capture)
    bus.on(Method.RENDER_SPRITEFORGE_RELEASE, capture)
    bus.on(Method.CHAT_WORK_NOTE, capture)
    bus.on(Method.CHAT_OBSERVER_DECISION, capture)

    coordinator = WorkActivityCoordinator()
    coordinator.configure()
    observer = WorkObserverCoordinator()

    async def observer_llm(*, note: dict, notes: list[dict], recent_chat: list[dict]) -> dict:
        phase = str(note.get("phase") or "").lower()
        if phase == "result":
            return {
                "action": "final_report",
                "terminal": True,
                "append_to_main_chat": True,
                "speak": True,
                "display_text": "OpenClaw completed the delegated check.",
                "main_chat_entry": "OpenClaw completed the delegated check.",
            }
        return {
            "action": "silent",
            "terminal": False,
            "append_to_main_chat": False,
            "speak": False,
            "display_text": "",
        }

    observer.configure(
        is_chat_busy=lambda: False,
        is_tts_busy=lambda: False,
        append_to_main_chat=lambda decision: appended.append(dict(decision)),
        narrate=lambda payload: narrated.append(dict(payload)),
        get_recent_chat=lambda session_id: [{"role": "user", "content": "Check this OpenClaw task."}],
        observer_llm=observer_llm,
    )

    run_id = "openclaw_smoke_demo"
    events: list[tuple[str, dict[str, Any]]] = [
        (
            Method.PROVIDER_EVENT,
            {
                "provider": "openclaw",
                "run_id": run_id,
                "type": "run.created",
                "payload": {
                    "task": "Check the current project status and report blockers.",
                    "cwd": "F:/Computer_Science/Amadeus/amadeus",
                    "mode": "delegate",
                },
                "metadata": {"source": "smoke"},
            },
        ),
        (
            Method.PROVIDER_EVENT,
            {
                "provider": "openclaw",
                "run_id": run_id,
                "type": "run.status",
                "payload": {"status": "running"},
            },
        ),
        (
            Method.PROVIDER_EVENT,
            {
                "provider": "openclaw",
                "run_id": run_id,
                "type": "assistant.delta",
                "payload": {"text": "I am checking the available context and tool state."},
            },
        ),
        (
            Method.PROVIDER_EVENT,
            {
                "provider": "openclaw",
                "run_id": run_id,
                "type": "tool.call",
                "payload": {"tool": "browser.open", "raw": {"type": "browser.open"}},
            },
        ),
        (
            Method.PROVIDER_EVENT,
            {
                "provider": "openclaw",
                "run_id": run_id,
                "type": "assistant.delta",
                "payload": {"text": " The delegated check is ready to summarize."},
            },
        ),
        (
            Method.PROVIDER_EVENT,
            {
                "provider": "openclaw",
                "run_id": run_id,
                "type": "run.finished",
                "payload": {"status": "done", "result": "OpenClaw completed the delegated check."},
            },
        ),
        (
            Method.PROVIDER_RESULT,
            {
                "provider": "openclaw",
                "run_id": run_id,
                "task": "Check the current project status and report blockers.",
                "cwd": "F:/Computer_Science/Amadeus/amadeus",
                "status": "done",
                "result": "OpenClaw completed the delegated check and found no blocker.",
                "metadata": {"result_type": "ok", "tool_names": ["browser.open"]},
            },
        ),
    ]

    for method, params in events:
        await bus.emit(method, params)
        await asyncio.sleep(0.01)

    assert observer._queue is not None
    await observer._queue.join()

    canvas_payloads = [params for method, params in captured if method == Method.WALLPAPER_CANVAS]
    activity_payloads = [params for method, params in captured if method == Method.WALLPAPER_ACTIVITY]
    behavior_payloads = [params for method, params in captured if method == Method.RENDER_SPRITEFORGE_INTENT]
    release_payloads = [params for method, params in captured if method == Method.RENDER_SPRITEFORGE_RELEASE]
    work_notes = [params for method, params in captured if method == Method.CHAT_WORK_NOTE]
    observer_decisions = [params for method, params in captured if method == Method.CHAT_OBSERVER_DECISION]

    assert canvas_payloads, "expected wallpaper canvas payloads"
    assert activity_payloads, "expected wallpaper activity payloads"
    assert behavior_payloads, "expected SpriteForge work intent"
    assert canvas_payloads[0]["mode"] == "workflow"
    assert canvas_payloads[0]["phase"] == "Intake"
    assert any(payload.get("phase") == "Work" for payload in canvas_payloads)
    assert any(payload.get("phase") == "Review" for payload in canvas_payloads)
    assert canvas_payloads[-1]["mode"] == "markdown"
    assert "OpenClaw completed" in canvas_payloads[-1]["markdown"]
    assert activity_payloads[0]["activity"] == "work"
    # Result work notes are terminal observer-owned now: provider result does
    # not release the scene directly; the observer emits the final report and
    # then releases wallpaper/SpriteForge runtime.
    assert activity_payloads[-1]["activity"] == ""
    assert work_notes and work_notes[-1]["phase"] == "Result"
    assert observer_decisions and observer_decisions[-1]["terminal"] is True
    assert appended and appended[-1]["action"] == "final_report"
    assert narrated and narrated[-1]["source"] == "work_observer"
    assert release_payloads and release_payloads[-1]["source"] == "work_observer_runtime"

    if observer._worker is not None:
        observer._worker.cancel()
        try:
            await observer._worker
        except asyncio.CancelledError:
            pass

    summary = {
        "activity_events": len(activity_payloads),
        "behavior_events": len(behavior_payloads),
        "canvas_events": len(canvas_payloads),
        "observer_events": len(observer_decisions),
        "canvas_sequence": [
            {
                "mode": payload.get("mode"),
                "phase": payload.get("phase"),
                "title": payload.get("title"),
                "progress": payload.get("progress"),
                "lead": payload.get("lead"),
            }
            for payload in canvas_payloads
        ],
        "final_markdown": canvas_payloads[-1].get("markdown"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
