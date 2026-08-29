from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.canvas_action_router import CanvasActionRouter
from server.event_bus import bus
from server.handlers.work_activity_handler import WorkActivityCoordinator
from server.protocol import Method
from server.work_context import clear_work_run, run_work_notes
from server.work_observer import WorkObserverCoordinator


async def main() -> None:
    captured: dict[str, list[dict[str, Any]]] = {
        "canvas": [],
        "activity": [],
        "work_note": [],
        "observer": [],
        "release": [],
        "chat_token": [],
        "chat_complete": [],
        "subtitle": [],
    }

    async def capture(method: str, params: dict[str, Any]) -> None:
        mapping = {
            Method.WALLPAPER_CANVAS: "canvas",
            Method.WALLPAPER_ACTIVITY: "activity",
            Method.CHAT_WORK_NOTE: "work_note",
            Method.CHAT_OBSERVER_DECISION: "observer",
            Method.RENDER_SPRITEFORGE_RELEASE: "release",
            Method.CHAT_TOKEN: "chat_token",
            Method.CHAT_COMPLETE: "chat_complete",
            Method.RENDER_SUBTITLE: "subtitle",
        }
        key = mapping.get(Method(method) if not isinstance(method, Method) else method)
        if key:
            captured[key].append(dict(params or {}))

    for method in (
        Method.WALLPAPER_CANVAS,
        Method.WALLPAPER_ACTIVITY,
        Method.CHAT_WORK_NOTE,
        Method.CHAT_OBSERVER_DECISION,
        Method.RENDER_SPRITEFORGE_RELEASE,
        Method.CHAT_TOKEN,
        Method.CHAT_COMPLETE,
        Method.RENDER_SUBTITLE,
    ):
        bus.on(method, capture)

    work_activity = WorkActivityCoordinator()
    work_activity.configure()

    observer_llm_calls: list[dict[str, Any]] = []
    appended: list[dict[str, Any]] = []
    narrated: list[dict[str, Any]] = []
    narration_flushed = asyncio.Event()
    output_busy = {"value": True, "checks": 0}

    def capture_narration(payload: dict[str, Any]) -> None:
        narrated.append(dict(payload))
        narration_flushed.set()

    async def observer_llm(*, note: dict, notes: list[dict], recent_chat: list[dict]) -> dict:
        observer_llm_calls.append({"note": dict(note), "notes": list(notes), "recent_chat": list(recent_chat)})
        phase = str(note.get("phase") or "").lower()
        if phase == "result":
            return {
                "action": "final_report",
                "terminal": True,
                "append_to_main_chat": True,
                "speak": True,
                "display_text": "我这边确认好了，结果已经放到卡片里。",
                "main_chat_entry": "我这边确认好了，结果已经放到卡片里。",
            }
        return {
            "action": "progress_note",
            "terminal": False,
            "append_to_main_chat": False,
            "speak": True,
            "display_text": "我已经拿到有用进展，先继续压缩成卡片。",
        }

    observer = WorkObserverCoordinator()
    observer.configure(
        is_chat_busy=lambda: False,
        is_tts_busy=lambda: output_busy["value"],
        append_to_main_chat=lambda decision: appended.append(dict(decision)),
        narrate=capture_narration,
        get_recent_chat=lambda session_id: [{"role": "user", "content": "帮我观察这个 provider 任务。"}],
        observer_llm=observer_llm,
    )

    async def bounded_wait_for_idle() -> None:
        output_busy["checks"] += 1
        while output_busy["value"]:
            output_busy["checks"] += 1
            await asyncio.sleep(0.01)

    observer._wait_for_output_idle = bounded_wait_for_idle  # type: ignore[method-assign]

    run_id = "ai_os_contract_openclaw"
    base = {
        "provider": "openclaw",
        "run_id": run_id,
        "task": "Find a useful source and report it compactly.",
        "metadata": {"session_id": "session_contract"},
    }

    # Mechanical provider work should light up the work surface, but it should
    # not become main-chat tokens, direct subtitles, or character narration.
    await bus.emit(
        Method.PROVIDER_EVENT,
        {
            **base,
            "type": "run.created",
            "payload": {"task": base["task"], "cwd": str(ROOT), "mode": "delegate"},
        },
    )
    await bus.emit(
        Method.PROVIDER_EVENT,
        {
            **base,
            "type": "tool.call",
            "payload": {"tool": "web_fetch", "url": "https://example.com"},
        },
    )
    assert observer._queue is not None
    await observer._queue.join()

    assert captured["canvas"], "provider work should create compact canvas state"
    assert captured["activity"] and captured["activity"][0]["activity"] == "work"
    assert captured["work_note"], "work notes should be produced for observer/main side-channel"
    assert all(note.get("speak") is False for note in captured["work_note"]), captured["work_note"]
    assert not captured["chat_token"], "provider facts must not stream as main chat tokens"
    assert not captured["chat_complete"], "provider facts must not complete a main chat turn"
    assert not captured["subtitle"], "provider facts must not directly subtitle as character speech"
    assert not narrated, "mechanical provider events must stay silent"

    # Semantic progress may become a low-priority Kurisu narration, but only
    # after output is idle. This is the observer lane, not provider direct speech.
    progress_emit = asyncio.create_task(
        bus.emit(
            Method.PROVIDER_EVENT,
            {
                **base,
                "type": "semantic.progress",
                "payload": {
                    "summary": "Found a readable source and is checking whether it matches the task.",
                    "source": "openclaw_tool_result:web_fetch",
                    "explicit": True,
                },
            },
        )
    )
    await asyncio.sleep(0.05)
    assert not narrated, "observer narration should wait behind busy output"
    output_busy["value"] = False
    await progress_emit
    await observer._queue.join()
    await asyncio.wait_for(narration_flushed.wait(), timeout=5.0)
    assert narrated and narrated[-1]["source"] == "work_observer", narrated
    assert observer_llm_calls, "semantic progress should reach the observer LLM"
    assert observer_llm_calls[-1]["recent_chat"], "observer should see recent main-chat context"
    assert not appended, "non-terminal progress should not pollute main chat history"

    # Browser artifacts are canvas artifacts, and terminal/result events must
    # not collapse them back to workflow mode.
    browser_run = "ai_os_contract_browser"
    browser_artifact = {
        "artifact_type": "browser.snapshot",
        "browser_session_id": "browser_contract",
        "url": "https://example.com/",
        "title": "Example",
        "excerpt": "Example source excerpt.",
        "links": [{"title": "Docs", "url": "https://example.com/docs"}],
        "screenshot": "data:image/png;base64,abc",
        "engine": "playwright",
        "status_code": 200,
    }
    await bus.emit(
        Method.PROVIDER_EVENT,
        {
            "provider": "browser",
            "run_id": browser_run,
            "task": "Open source",
            "type": "artifact.created",
            "payload": browser_artifact,
            "metadata": {"session_id": "session_contract"},
        },
    )
    await bus.emit(
        Method.PROVIDER_EVENT,
        {
            "provider": "browser",
            "run_id": browser_run,
            "task": "Open source",
            "type": "run.finished",
            "payload": {"status": "done", "result": "Browser run done."},
            "metadata": {"session_id": "session_contract"},
        },
    )
    await bus.emit(
        Method.PROVIDER_RESULT,
        {
            "provider": "browser",
            "run_id": browser_run,
            "task": "Open source",
            "status": "done",
            "result": "Browser run done.",
            "metadata": {},
        },
    )
    browser_modes = [payload.get("mode") for payload in captured["canvas"][-3:]]
    assert browser_modes == ["browser", "browser", "browser"], browser_modes

    # Canvas actions must route through the system action router; browser
    # continuity belongs to ProviderRuntime session ids, not iframe embedding.
    provider_calls: list[dict[str, Any]] = []

    async def fake_provider_run(params: dict[str, Any]) -> dict[str, Any]:
        provider_calls.append(dict(params))
        return {"run": {"run_id": "browser_action_contract"}}

    action_router = CanvasActionRouter(provider_run=fake_provider_run)
    action_result = await action_router.route(
        {
            "target": "browser",
            "action": "open",
            "url": "https://example.com/docs",
            "browserSessionId": "browser_contract",
        }
    )
    assert action_result["ok"] is True, action_result
    assert provider_calls[-1]["provider"] == "browser", provider_calls
    assert provider_calls[-1]["metadata"]["browser_session_id"] == "browser_contract", provider_calls

    # Terminal results append/speak exactly once, release character runtime, and
    # clear ephemeral work context.
    final_note = {
        "source": "provider",
        "provider": "openclaw",
        "run_id": run_id,
        "session_id": "session_contract",
        "phase": "Result",
        "title": "OpenClaw result report",
        "summary": "OpenClaw completed the delegated task.",
        "signals": [{"label": "status", "text": "Provider returned done.", "detail": "openclaw"}],
        "importance": "important",
        "speak": False,
    }
    narration_flushed.clear()
    await bus.emit(Method.CHAT_WORK_NOTE, final_note)
    await observer._queue.join()
    await bus.emit(Method.CHAT_WORK_NOTE, final_note)
    await observer._queue.join()
    await asyncio.wait_for(narration_flushed.wait(), timeout=5.0)
    assert len(appended) == 1, appended
    line_ids = [str(item.get("line_id") or "") for item in narrated]
    assert len(line_ids) == len(set(line_ids)), narrated
    final_narrations = [item for item in narrated if item.get("action") == "final_report" and item.get("terminal")]
    assert len(final_narrations) == 1, narrated
    assert len([item for item in captured["release"] if item.get("run_id") == run_id]) == 1, captured["release"]
    assert not run_work_notes(run_id), "terminal observer should clear work notes"

    if observer._worker is not None:
        observer._worker.cancel()
        try:
            await observer._worker
        except asyncio.CancelledError:
            pass
    clear_work_run(browser_run)
    print("ai os interface contract smoke ok")


if __name__ == "__main__":
    asyncio.run(main())
