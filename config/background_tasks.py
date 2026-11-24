"""Owned fire-and-forget tasks.

The event loop keeps only a weak reference to a task, so a task created and
then discarded can be garbage-collected before it finishes, and an exception
it raises is reported only when (if ever) the task object is collected.
``spawn_background`` keeps a strong reference until the task completes and
logs a failure when it happens. Scheduling and cancellation are unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _finished(task: asyncio.Task[Any]) -> None:
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "background task %r failed",
            task.get_name(),
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def spawn_background(
    coro: Coroutine[Any, Any, _T],
    *,
    name: str | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> asyncio.Task[_T]:
    """Schedule ``coro`` like ``asyncio.create_task`` and keep it alive.

    ``loop`` selects ``loop.create_task`` for callers that schedule onto an
    explicit loop; it must be called from that loop's thread.
    """
    if loop is None:
        task = asyncio.create_task(coro, name=name)
    else:
        task = loop.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_finished)
    return task
