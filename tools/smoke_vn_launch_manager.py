from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def main() -> int:
    from server.vn_launch_manager import VNLaunchManager, _agent_switch

    assert _agent_switch("pname", "1234") == "--pname=1234"

    calls: list[tuple[str, dict[str, Any]]] = []
    runtime_state: dict[str, Any] = {"status": "stopped"}

    async def start(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(("start", dict(params)))
        runtime_state.update({"status": "active", "profile": dict(params)})
        return dict(runtime_state)

    async def stop(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(("stop", dict(params)))
        runtime_state.update({"status": "stopped"})
        return dict(runtime_state)

    async def status() -> dict[str, Any]:
        return dict(runtime_state)

    async def line(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(("line", dict(params)))
        return {"status": "accepted", "line": dict(params)}

    async def before_external_launch(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(("before_external_launch", dict(params)))
        return {"status": "stopped"}

    manager = VNLaunchManager(
        ROOT,
        runtime_start=start,
        runtime_stop=stop,
        runtime_status=status,
        runtime_line=line,
        before_external_launch=before_external_launch,
    )

    profiles = manager.profiles()["profiles"]
    assert profiles, "expected at least one VN profile"
    assert profiles[0]["id"] == "paranormasight"
    assert "scriptPath" in profiles[0]
    assert "gameExe" in profiles[0]
    assert "agentExe" in profiles[0]
    assert "overlayHelper" in profiles[0]
    assert "overlayUrl" in profiles[0]

    started = await manager.start({
        "profileId": "paranormasight",
        "sessionId": "test_session",
        "launchGame": False,
        "attachHook": False,
        "launchOverlay": False,
        "bridgeClipboard": False,
    })
    assert started["status"] == "active"
    assert started["profileId"] == "paranormasight"
    assert started["sessionId"] == "test_session"
    assert started["game"]["status"] == "not_started"
    assert started["hook"]["status"] == "manual_required"
    assert started["overlay"]["status"] == "not_started"
    assert started["bridge"]["status"] == "not_started"
    assert calls[0][0] == "start"
    assert calls[0][1]["session_id"] == "test_session"
    assert calls[0][1]["game_id"] == "paranormasight"
    assert calls[0][1]["output_language"] == "ja"
    assert "overlay_url" not in calls[0][1]

    stopped = await manager.stop({"reason": "smoke"})
    assert stopped["status"] == "idle"
    assert calls[-1][0] == "stop"
    assert calls[-1][1]["reason"] == "smoke"

    await manager.start({
        "profileId": "paranormasight",
        "sessionId": "test_runtime_only_again",
        "launchGame": False,
        "attachHook": False,
        "launchOverlay": False,
        "bridgeClipboard": False,
    })
    assert not any(name == "before_external_launch" for name, _ in calls)
    await manager.stop({"reason": "smoke_runtime_only"})
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
