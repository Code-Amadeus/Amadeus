from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

from config import settings
from server.event_bus import EventBus


def test_slow_callback_is_attributed_without_changing_delivery() -> None:
    async def scenario() -> None:
        event_bus = EventBus()
        delivered: list[tuple[str, dict]] = []

        async def subscriber(method: str, params: dict) -> None:
            await asyncio.sleep(0.01)
            delivered.append((method, params))

        event_bus.on("provider.event", subscriber)
        with (
            patch.object(settings, "EVENT_BUS_SLOW_CALLBACK_S", 0.0),
            patch.object(logging.getLogger("server.event_bus"), "warning") as warning,
        ):
            await event_bus.emit("provider.event", {"sequence": 1})

        assert delivered == [("provider.event", {"sequence": 1})]
        warning.assert_called_once()
        assert warning.call_args.args[1:3] == (
            "provider.event",
            "test_slow_callback_is_attributed_without_changing_delivery.<locals>.scenario.<locals>.subscriber",
        )

    asyncio.run(scenario())


async def test_emit_now_delivers_to_all_subscribers_with_awaitable_results() -> None:
    event_bus = EventBus()
    future = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(asyncio.sleep(0))
    custom_awaited = asyncio.Event()
    last_delivered = asyncio.Event()

    class CustomAwaitable:
        def __await__(self):
            custom_awaited.set()
            return future.__await__()

    async def last_subscriber(method, params):
        last_delivered.set()

    event_bus.on("work.updated", lambda *_: future)
    event_bus.on("work.updated", lambda *_: task)
    event_bus.on("work.updated", lambda *_: CustomAwaitable())
    event_bus.on("work.updated", last_subscriber)
    event_bus.emit_now("work.updated")
    future.set_result(None)
    await asyncio.wait_for(asyncio.gather(custom_awaited.wait(), last_delivered.wait(), task), 1)


if __name__ == "__main__":
    test_slow_callback_is_attributed_without_changing_delivery()
    print("ok: event backpressure is attributed to the responsible subscriber")
