from __future__ import annotations

import asyncio
from typing import Any

from server.canvas_action_router import CanvasActionRouter


def test_work_preview_forwards_only_durable_identity() -> None:
    calls: list[dict[str, Any]] = []

    async def work_action(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {"ok": True}

    async def run() -> None:
        result = await CanvasActionRouter(work_action=work_action).route(
            {
                "target": "work_item",
                "action": "open_preview",
                "work_item_id": "work-preview",
                "attempt_id": "attempt-preview",
                "revision": "ledger-preview",
                "project_id": "renderer-project",
                "cwd": r"C:\renderer-controlled",
                "url": "http://renderer.invalid:5173",
                "port": 5173,
                "command": "npm run dev",
                "provider": "renderer-provider",
                "metadata": {"authority": "unbounded"},
            }
        )
        assert result == {"ok": True}

    asyncio.run(run())
    assert calls == [
        {
            "target": "work_item",
            "action": "open_preview",
            "work_item_id": "work-preview",
            "attempt_id": "attempt-preview",
            "revision": "ledger-preview",
        }
    ]


def test_work_preview_requires_complete_durable_identity() -> None:
    calls: list[dict[str, Any]] = []

    async def work_action(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {"ok": True}

    async def run() -> None:
        router = CanvasActionRouter(work_action=work_action)
        base = {
            "target": "work_item",
            "action": "open_preview",
            "work_item_id": "work-preview",
            "attempt_id": "attempt-preview",
            "revision": "ledger-preview",
        }
        expected = {
            "work_item_id": "missing_work_item_id",
            "attempt_id": "missing_attempt_id",
            "revision": "missing_revision",
        }
        for field, error in expected.items():
            payload = dict(base)
            payload.pop(field)
            result = await router.route(payload)
            assert result == {"ok": False, "error": error}

    asyncio.run(run())
    assert calls == []
