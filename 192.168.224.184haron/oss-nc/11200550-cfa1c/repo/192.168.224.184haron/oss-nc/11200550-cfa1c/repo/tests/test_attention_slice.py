"""Attention selections use the current Electron Slice, not legacy WorkPage."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.attention_request import (
    AttentionOption,
    AttentionRequestCoordinator,
)
from server.canvas_action_router import CanvasActionRouter
from server.handlers.attention_handler import AttentionRequestHandler
from server.handlers.wallpaper_handler import WallpaperHandler


async def test_slice_receipt_and_selection_use_host_session() -> None:
    coordinator = AttentionRequestCoordinator()
    selected: list[str] = []

    async def continuation(option_id: str):
        selected.append(option_id)
        return {"continued": True}

    request = await coordinator.create_selection(
        session_id="session-current",
        title="Choose",
        prompt="Pick one",
        options=[
            AttentionOption(option_id="opaque-a", label="A"),
            AttentionOption(option_id="opaque-b", label="B"),
        ],
        continuation=continuation,
    )
    handler = AttentionRequestHandler(
        coordinator,
        current_session_id=lambda: "session-current",
    )
    router = CanvasActionRouter(attention_action=handler.route_canvas_action)

    receipt = await router.route(
        {
            "target": "attention",
            "action": "presented",
            "request_id": request["id"],
            "session_id": "attacker-controlled",
            "provider": "attacker-controlled",
        }
    )
    assert receipt["ok"] is True
    assert receipt["surface"] == "electron_slice"
    assert len(coordinator.list_pending("session-current")) == 1

    resolved = await router.route(
        {
            "target": "attention",
            "action": "resolve",
            "request_id": request["id"],
            "option_id": "opaque-b",
            "session_id": "attacker-controlled",
            "canonical_id": "must-not-forward",
        }
    )
    assert resolved["ok"] is True
    assert selected == ["opaque-b"]
    assert coordinator.list_pending("session-current") == []
    coordinator.reset_for_tests()


async def test_slice_attention_route_rejects_incomplete_or_forged_actions() -> None:
    calls: list[dict] = []

    async def attention_action(payload: dict) -> dict:
        calls.append(payload)
        return {"ok": True}

    router = CanvasActionRouter(attention_action=attention_action)
    assert await router.route(
        {"target": "attention", "action": "resolve", "request_id": "request-1"}
    ) == {"ok": False, "error": "missing_attention_option_id"}
    assert await router.route(
        {"target": "attention", "action": "delete", "request_id": "request-1"}
    ) == {"ok": False, "error": "unsupported_action"}
    await router.route(
        {
            "target": "attention",
            "action": "resolve",
            "request_id": "request-1",
            "option_id": "option-1",
            "session_id": "forged",
            "task": "forged",
        }
    )
    assert calls == [
        {
            "target": "attention",
            "action": "resolve",
            "request_id": "request-1",
            "option_id": "option-1",
        }
    ]


def test_wallpaper_handler_projects_current_attention_to_electron_slice() -> None:
    calls: list[dict] = []

    class Host:
        slice_host = "electron"

        @staticmethod
        def set_attention(payload: dict) -> None:
            calls.append(payload)

    requests = [
        {
            "schemaId": "amadeus.attention.v1",
            "id": "attention-1",
            "status": "pending",
            "title": "Choose",
            "prompt": "Pick one",
            "options": [{"id": "option-1", "label": "One"}],
        }
    ]
    handler = WallpaperHandler()
    handler._wallpaper_host = Host()
    handler._attention_snapshot = lambda: requests

    assert handler._apply_attention_snapshot() is True
    assert calls[-1]["schemaId"] == "amadeus.attention.slice.v1"
    assert calls[-1]["requests"] == requests

    handler._attention_snapshot = lambda: []
    assert handler._apply_attention_snapshot() is True
    assert calls[-1]["requests"] == []


def test_attention_transport_is_independent_from_legacy_work_overlay() -> None:
    app = (ROOT / "electron" / "src" / "renderer" / "App.tsx").read_text(
        encoding="utf-8"
    )
    host = (ROOT / "render" / "web" / "electron_slice_host.js").read_text(
        encoding="utf-8"
    )
    surface = (ROOT / "render" / "web" / "crt_canvas_surface.js").read_text(
        encoding="utf-8"
    )

    assert "attention.updated" not in app
    assert 'call.method === "setAttention"' in host
    assert "surface.setAttention" in host
    assert "crt-canvas-attention" in surface
    assert 'postCanvasAction("attention", "resolve"' in surface


async def main() -> None:
    await test_slice_receipt_and_selection_use_host_session()
    await test_slice_attention_route_rejects_incomplete_or_forged_actions()
    test_wallpaper_handler_projects_current_attention_to_electron_slice()
    test_attention_transport_is_independent_from_legacy_work_overlay()
    print("all Electron Slice attention tests passed")


if __name__ == "__main__":
    asyncio.run(main())
