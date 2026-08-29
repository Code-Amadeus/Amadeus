from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.adapters.openclaw import OpenClawAdapter
from agent_host.provider_runtime import runtime
from agent_host.provider_types import ProviderRunRequest
from server.event_bus import bus
from server.handlers.work_activity_handler import WorkActivityCoordinator
from server.protocol import Method


async def main() -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    async def capture(method: str, params: dict[str, Any]) -> None:
        captured.append((method, dict(params)))

    bus.on(Method.PROVIDER_EVENT, capture)
    bus.on(Method.PROVIDER_RESULT, capture)
    bus.on(Method.WALLPAPER_CANVAS, capture)
    bus.on(Method.WALLPAPER_ACTIVITY, capture)
    bus.on(Method.RENDER_SPRITEFORGE_INTENT, capture)

    runtime.register(OpenClawAdapter())
    coordinator = WorkActivityCoordinator()
    coordinator.configure()

    task = (
        "Live smoke test for Amadeus provider canvas. "
        "Reply in one concise sentence: OpenClaw canvas live smoke ok. "
        "Do not use tools, browse, edit files, or ask follow-up questions."
    )
    started = time.monotonic()
    record = await runtime.start(
        ProviderRunRequest(
            provider="openclaw",
            task=task,
            mode="delegate",
            metadata={"source": "live_smoke", "timeout": 25.0},
        )
    )
    if record.task_handle is not None:
        await record.task_handle
    elapsed_ms = int((time.monotonic() - started) * 1000)

    provider_events = [params for method, params in captured if method == Method.PROVIDER_EVENT]
    provider_results = [params for method, params in captured if method == Method.PROVIDER_RESULT]
    canvas_payloads = [params for method, params in captured if method == Method.WALLPAPER_CANVAS]
    activity_payloads = [params for method, params in captured if method == Method.WALLPAPER_ACTIVITY]
    behavior_payloads = [params for method, params in captured if method == Method.RENDER_SPRITEFORGE_INTENT]

    assert provider_events, "expected provider events"
    assert provider_results, "expected provider result"
    assert canvas_payloads, "expected wallpaper canvas payloads"
    assert activity_payloads and activity_payloads[0].get("activity") == "work", "expected work activity"
    assert behavior_payloads, "expected SpriteForge work intent"
    assert canvas_payloads[-1].get("mode") == "markdown", "expected final markdown canvas report"

    summary = {
        "elapsed_ms": elapsed_ms,
        "run": record.to_dict(),
        "provider_events": [
            {
                "type": payload.get("type"),
                "time_ms": payload.get("time_ms"),
                "payload": payload.get("payload"),
            }
            for payload in provider_events[-12:]
        ],
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
        "final_canvas_markdown": canvas_payloads[-1].get("markdown"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
