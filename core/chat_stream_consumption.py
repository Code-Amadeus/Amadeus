"""Provider-neutral consumption of role-model stream text.

Provider adapters own how bytes or SDK chunks become text.  This module owns
the invariant order after that boundary: parse inline controls, append visible
text, publish the GUI projection, dispatch sentence work, then optionally yield
to the event loop.  It deliberately knows nothing about Provider identity.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any, TypeVar

T = TypeVar("T")


def _next_or_sentinel(iterator):
    try:
        return True, next(iterator)
    except StopIteration:
        return False, None


async def iter_sync_stream(sync_iterable: Iterable[T]) -> AsyncIterator[T]:
    """Read a synchronous SDK stream without blocking the event loop."""

    iterator = iter(sync_iterable)
    while True:
        has_item, item = await asyncio.to_thread(_next_or_sentinel, iterator)
        if not has_item:
            break
        yield item


async def consume_role_stream_text(
    state: Any,
    raw_content: str,
    *,
    parse_control: Callable[[str], str],
    dispatch_text: Callable[[str], Awaitable[None]],
    pace_s: float = 0.0,
) -> str:
    """Apply one provider-neutral text fragment in the canonical order."""

    content = parse_control(raw_content)
    state.full_response += content
    if state.gui_callback:
        state.gui_callback(state.full_response)
    await dispatch_text(content)
    if pace_s > 0:
        await asyncio.sleep(pace_s)
    return content
