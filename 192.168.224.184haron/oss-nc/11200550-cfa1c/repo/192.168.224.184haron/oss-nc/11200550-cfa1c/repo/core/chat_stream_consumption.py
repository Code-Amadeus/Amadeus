"""Provider-neutral consumption of role-model stream text.

Provider adapters own how bytes or SDK chunks become text.  This module owns
the invariant order after that boundary: parse inline controls, append visible
text, publish the GUI projection, dispatch sentence work, then optionally yield
to the event loop.  It deliberately knows nothing about Provider identity.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

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
