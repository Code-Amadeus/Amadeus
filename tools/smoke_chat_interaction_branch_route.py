from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("AMADEUS_HEADLESS", "1")

from server.event_bus import bus  # noqa: E402
from server.handlers.chat_handler import ChatHandler  # noqa: E402
from server.protocol import Method  # noqa: E402


async def main() -> None:
    tokens: list[dict[str, Any]] = []
    complete: list[dict[str, Any]] = []
    turn_finished: list[dict[str, Any]] = []
    spoken: list[dict[str, Any]] = []
    stream_called = False

    async def capture(method: str, params: dict[str, Any]) -> None:
        if method == Method.CHAT_TOKEN:
            tokens.append(dict(params or {}))
        if method == Method.CHAT_COMPLETE:
            complete.append(dict(params or {}))

    bus.on(Method.CHAT_TOKEN, capture)
    bus.on(Method.CHAT_COMPLETE, capture)

    async def fake_stream(*args: Any, **kwargs: Any) -> str:
        nonlocal stream_called
        stream_called = True
        raise AssertionError("main LLM should not run when interaction branch handles the turn")

    async def fake_router(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["text"] == "Search Amadeus on this page.", kwargs
        return {"handled": True, "display_text": "Branch handled browser continuation.", "branch_id": "branch_test"}

    async def fake_voice_sink(payload: dict[str, Any]) -> None:
        spoken.append(dict(payload))

    handler = ChatHandler()
    handler.configure(
        stream_llm_query=fake_stream,
        pending_sentence_items=None,
        interaction_branch_router=fake_router,
        assistant_voice_sink=fake_voice_sink,
        on_turn_finished=lambda payload: turn_finished.append(dict(payload)),
    )
    await handler.send_text(
        "Search Amadeus on this page.",
        session_id="",
        turn_id="turn_branch_route",
        source="wake",
    )
    assert handler._stream_task is not None
    await handler._stream_task

    assert not stream_called
    assert tokens and tokens[-1]["token"] == "Branch handled browser continuation.", tokens
    assert complete and complete[-1]["full_text"] == "Branch handled browser continuation.", complete
    assert spoken and spoken[-1]["source"] == "browser_conversation_fork", spoken
    assert spoken[-1]["display_text"] == "Branch handled browser continuation.", spoken
    assert turn_finished and turn_finished[-1]["status"] == "complete", turn_finished
    print("chat interaction branch route smoke ok")


if __name__ == "__main__":
    asyncio.run(main())
