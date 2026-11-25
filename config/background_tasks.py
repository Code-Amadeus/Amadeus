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

_BACKGROUND_TASKS: set[asyncio.Future[Any]] = set()


def _finished(future: asyncio.Future[Any]) -> None:
    _BACKGROUND_TASKS.discard(future)
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        name = future.get_name() if isinstance(future, asyncio.Task) else type(future).__name__
        logger.error(
            "background task %r failed",
            name,
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def track_background(future: asyncio.Future[Any]) -> None:
    """Keep a scheduled future alive and report its failure on completion."""
    if future not in _BACKGROUND_TASKS:
        _BACKGROUND_TASKS.add(future)
        future.add_done_callback(_finished)


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
    track_background(task)
    return task
