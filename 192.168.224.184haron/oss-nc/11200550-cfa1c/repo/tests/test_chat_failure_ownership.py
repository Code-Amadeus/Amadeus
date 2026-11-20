"""A failed or still-empty reply cannot acquire an earlier reply's text."""

import asyncio
from unittest.mock import AsyncMock

from test_chat_control_ingress import context as context, request


def test_aborting_before_first_reply_does_not_borrow_previous_text(context):
    async def run():
        entered = asyncio.Event()

        async def runner(*_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

        handler, _, _ = context.make(runner=runner)
        handler._last_assistant_turn_id = "previous"
        handler._last_assistant_text = "A different completed answer."
        await handler._handle_send(request("empty"))
        await entered.wait()
        result = await handler._handle_abort({"turn_id":"turn-empty"})
        assert result["turn_id"] == "turn-empty"
        assert result["accumulated_text"] == ""
        assert handler._last_assistant_turn_id == "previous"
        assert handler._last_assistant_text == "A different completed answer."

    asyncio.run(run())


def test_failed_generation_releases_active_reply_identity(context):
    async def run():
        handler, _, _ = context.make(runner=AsyncMock(side_effect=RuntimeError("query failed")))
        handler._last_assistant_turn_id = "previous"
        handler._last_assistant_text = "A different completed answer."
        await handler._handle_send(request("failed"))
        await handler._stream_task
        assert handler._active_turn_id == ""
        assert handler._active_accumulated_text == ""
        assert handler._last_assistant_turn_id == "previous"
        assert handler._last_assistant_text == "A different completed answer."

    asyncio.run(run())
