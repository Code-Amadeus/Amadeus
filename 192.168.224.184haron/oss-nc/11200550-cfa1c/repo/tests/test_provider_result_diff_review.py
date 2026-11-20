from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.event_bus import bus
from server.handlers.work_activity_handler import WorkActivityCoordinator
from server.protocol import Method


async def _result_canvas(*, workspace_mode: str, tracked: bool = True) -> dict[str, Any]:
    canvases: list[dict[str, Any]] = []

    async def capture_canvas(_method: str, payload: dict[str, Any]) -> None:
        canvases.append(payload)

    work = {
        "workspace_mode": workspace_mode,
        "workspace_path": r"C:\workspace\game",
    }
    if tracked:
        work.update(
            {
                "work_item_id": "work-provider-neutral-diff",
                "attempt_id": "attempt-provider-neutral-diff",
            }
        )
    coordinator = WorkActivityCoordinator()
    state = coordinator._run_state(
        {
            "provider": "codex-app-server",
            "run_id": "codex-provider-neutral-diff",
            "task": "Update the game.",
            "cwd": r"C:\workspace\game",
        }
    )
    state["metadata"] = {"work": work}
    state["status"] = "done"
    state["result"] = "Updated and validated the game."

    bus.on(Method.WALLPAPER_CANVAS, capture_canvas)
    try:
        await coordinator._emit_result_canvas(state)
    finally:
        bus.off(Method.WALLPAPER_CANVAS, capture_canvas)

    assert len(canvases) == 1
    return canvases[0]


async def test_workspace_attempt_keeps_report_and_offers_diff_review() -> None:
    canvas = await _result_canvas(workspace_mode="local")

    assert canvas["mode"] == "markdown"
    assert canvas["title"] == "Codex result report"
    assert canvas["markdown"].startswith("### Codex result\n")
    assert "Updated and validated the game." in canvas["markdown"]
    diff_actions = [
        action
        for action in canvas.get("actions") or []
        if action.get("defaultAction") == "view_diff"
    ]
    assert len(diff_actions) == 1
    assert diff_actions[0]["metadata"]["attempt_id"] == "attempt-provider-neutral-diff"
    assert diff_actions[0]["metadata"]["work_item_id"] == "work-provider-neutral-diff"


async def test_workspace_less_attempt_does_not_offer_diff_review() -> None:
    canvas = await _result_canvas(workspace_mode="none")

    assert canvas["mode"] == "markdown"
    assert all(action.get("defaultAction") != "view_diff" for action in canvas.get("actions") or [])


async def test_untracked_provider_result_does_not_invent_diff_review() -> None:
    canvas = await _result_canvas(workspace_mode="local", tracked=False)

    assert canvas["mode"] == "markdown"
    assert all(action.get("defaultAction") != "view_diff" for action in canvas.get("actions") or [])


async def main() -> None:
    await test_workspace_attempt_keeps_report_and_offers_diff_review()
    await test_workspace_less_attempt_does_not_offer_diff_review()
    await test_untracked_provider_result_does_not_invent_diff_review()
    print("ok: provider-neutral result diff review follows the Work Ledger boundary")


if __name__ == "__main__":
    asyncio.run(main())
