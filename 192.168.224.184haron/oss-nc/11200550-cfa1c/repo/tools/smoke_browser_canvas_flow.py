from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.event_bus import bus
from server.handlers.work_activity_handler import WorkActivityCoordinator
from server.protocol import Method


async def main() -> None:
    captured: list[dict[str, Any]] = []

    async def capture(method: str, params: dict[str, Any]) -> None:
        if method == Method.WALLPAPER_CANVAS:
            captured.append(dict(params))

    bus.on(Method.WALLPAPER_CANVAS, capture)

    coordinator = WorkActivityCoordinator()
    coordinator.configure()
    run_id = "browser_canvas_smoke"
    base = {
        "provider": "browser",
        "run_id": run_id,
        "task": "Open a source and show it inside the CRT canvas.",
        "metadata": {"session_id": "session_browser_smoke"},
    }
    artifact = {
        "artifact_type": "browser.snapshot",
        "browser_session_id": "browser_abc123",
        "url": "https://example.com/",
        "title": "Example",
        "excerpt": "A readable browser page excerpt.",
        "links": [{"title": "Docs", "url": "https://example.com/docs"}],
        "screenshot": "data:image/png;base64,abc",
        "engine": "playwright",
        "status_code": 200,
    }

    await bus.emit(
        Method.PROVIDER_EVENT,
        {
            **base,
            "type": "artifact.created",
            "payload": artifact,
        },
    )
    await bus.emit(
        Method.PROVIDER_EVENT,
        {
            **base,
            "type": "run.finished",
            "payload": {"status": "done", "result": "Browser run completed."},
        },
    )
    await bus.emit(
        Method.PROVIDER_RESULT,
        {
            **base,
            "status": "done",
            "result": "Browser run completed.",
            # Deliberately no metadata.browser: artifact semantics should be
            # enough to keep the canvas in browser mode.
            "metadata": {},
        },
    )

    assert len(captured) >= 3, captured
    assert [payload.get("mode") for payload in captured[-3:]] == ["browser", "browser", "browser"], captured
    assert captured[-1]["phase"] == "Result", captured[-1]
    assert captured[-1]["browserSessionId"] == "browser_abc123", captured[-1]
    assert captured[-1]["url"] == "https://example.com/", captured[-1]
    print("browser canvas flow smoke ok")


if __name__ == "__main__":
    asyncio.run(main())
