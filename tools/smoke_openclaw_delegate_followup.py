from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.provider_runtime import runtime
from agent_host.provider_types import (
    EmitProviderEvent,
    ProviderRunRequest,
    ProviderRunResult,
)


class FakeOpenClawAdapter:
    provider_id = "openclaw"

    async def run(
        self,
        request: ProviderRunRequest,
        run_id: str,
        emit: EmitProviderEvent,
    ) -> ProviderRunResult:
        return ProviderRunResult(
            status="done",
            result="OpenClaw finished the delegated task and found no blocker.",
            metadata={"result_type": "ok", "tool_names": []},
        )

    async def cancel(self, run_id: str) -> None:
        return None


async def main() -> None:
    streamed: list[str] = []
    followups: list[dict[str, Any]] = []

    fake_main = types.SimpleNamespace(
        _current_gui_callback=lambda text: streamed.append(str(text)),
    )
    sys.modules["main"] = fake_main

    os.chdir(tempfile.gettempdir())
    import server.app as server_app

    async def fake_stream_llm_query_adapter(
        text: str,
        gui_callback=None,
        provider: str | None = None,
        preserve_emotion: bool = False,
    ) -> str:
        followups.append(
            {
                "text": text,
                "provider": provider,
                "preserve_emotion": preserve_emotion,
                "has_callback": callable(gui_callback),
            }
        )
        if callable(gui_callback):
            gui_callback("Kurisu summary: OpenClaw completed the task.")
        return "Kurisu summary: OpenClaw completed the task."

    server_app._stream_llm_query_adapter = fake_stream_llm_query_adapter
    runtime.register(FakeOpenClawAdapter())  # type: ignore[arg-type]

    result = await server_app._handle_delegate("Check provider result callback wiring.")

    assert "found no blocker" in str(result)
    assert not followups, "ProviderRuntime delegate path should not run legacy main-chat follow-up"
    assert not streamed, "ProviderRuntime delegate path should leave narration to WorkObserver"
    print("delegate provider-runtime boundary ok")


if __name__ == "__main__":
    asyncio.run(main())
