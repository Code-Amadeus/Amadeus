from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.canvas_action_router import CanvasActionRouter
from wallpaper.wallpaper_engine_bridge import _BridgeState, _route_canvas_action


async def main() -> None:
    provider_calls: list[dict] = []

    async def fake_provider_run(params: dict) -> dict:
        provider_calls.append(dict(params))
        return {"run": {"run_id": "browser_fake", "provider": params.get("provider")}}

    router = CanvasActionRouter(provider_run=fake_provider_run)

    source = await router.route(
        {
            "target": "url",
            "action": "source",
            "url": "https://example.com/docs",
        }
    )
    assert source["ok"] is True, source
    assert Path(source["sourcePath"]).is_file(), source

    command = await router.route(
        {
            "target": "command",
            "action": "make_bat",
            "command": "echo hello",
        }
    )
    assert command["ok"] is True, command
    assert Path(command["batPath"]).is_file(), command

    browser = await router.route(
        {
            "target": "browser",
            "action": "open",
            "url": "https://example.com/docs",
            "browserSessionId": "browser_123",
        }
    )
    assert browser["ok"] is True, browser
    assert provider_calls, "expected browser provider call"
    call = provider_calls[-1]
    assert call["provider"] == "browser", call
    assert call["mode"] == "open", call
    assert call["metadata"]["browser_session_id"] == "browser_123", call
    assert call["metadata"]["url"] == "https://example.com/docs", call

    state = _BridgeState()
    forwarded: list[dict] = []

    def capture(payload: dict) -> dict:
        forwarded.append(dict(payload))
        return {"ok": True, "forwarded": True}

    state.canvas_action_handler = capture
    routed = _route_canvas_action(
        state,
        "browser",
        {"action": "observe", "browserSessionId": "browser_123"},
    )
    assert routed["ok"] is True, routed
    assert forwarded and forwarded[-1]["target"] == "browser", forwarded
    assert forwarded[-1]["action"] == "observe", forwarded

    print("canvas action router smoke ok")


if __name__ == "__main__":
    asyncio.run(main())
