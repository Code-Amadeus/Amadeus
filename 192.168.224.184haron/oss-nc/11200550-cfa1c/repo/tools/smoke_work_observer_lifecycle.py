from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.event_bus import bus
from server.protocol import Method
from server.work_context import add_work_note, run_work_notes
from server.work_observer import WorkObserverCoordinator


async def main() -> None:
    appended: list[dict] = []
    narrated: list[dict] = []
    releases: list[dict] = []

    async def capture_release(_method: str, params: dict) -> None:
        releases.append(dict(params))

    async def observer_llm(*, note: dict, notes: list[dict], recent_chat: list[dict]) -> dict:
        return {
            "action": "final_report",
            "terminal": True,
            "append_to_main_chat": True,
            "speak": True,
            "display_text": "我这边确认好了，结果已经放到卡片里。",
            "main_chat_entry": "我这边确认好了，结果已经放到卡片里。",
        }

    coordinator = WorkObserverCoordinator()

    async def fast_wait() -> None:
        return None

    coordinator._wait_for_output_idle = fast_wait  # type: ignore[method-assign]
    coordinator.configure(
        is_chat_busy=lambda: False,
        is_tts_busy=lambda: False,
        append_to_main_chat=lambda decision: appended.append(dict(decision)),
        narrate=lambda payload: narrated.append(dict(payload)),
        get_recent_chat=lambda session_id: [{"role": "user", "content": "检查 provider 结果。"}],
        observer_llm=observer_llm,
    )
    bus.on(Method.RENDER_SPRITEFORGE_RELEASE, capture_release)

    run_id = "observer_lifecycle_smoke"
    note = {
        "source": "provider",
        "provider": "openclaw",
        "run_id": run_id,
        "session_id": "session_smoke",
        "phase": "Result",
        "title": "OpenClaw result report",
        "summary": "OpenClaw completed the delegated task.",
        "signals": [{"label": "status", "text": "Provider returned done.", "detail": "openclaw"}],
        "importance": "important",
    }

    add_work_note(note)
    await bus.emit(Method.CHAT_WORK_NOTE, note)
    assert coordinator._queue is not None
    await coordinator._queue.join()

    # A duplicate terminal note for the same run should be ignored after the
    # observer closes and records the run as handled.
    await bus.emit(Method.CHAT_WORK_NOTE, note)
    await coordinator._queue.join()

    assert len(appended) == 1, appended
    assert len(narrated) == 1, narrated
    assert len(releases) == 1, releases
    assert coordinator.get_session(run_id) is None
    assert not run_work_notes(run_id), "terminal observer should clear run notes"

    if coordinator._worker is not None:
        coordinator._worker.cancel()
        try:
            await coordinator._worker
        except asyncio.CancelledError:
            pass

    print("work observer lifecycle smoke ok")


if __name__ == "__main__":
    asyncio.run(main())
