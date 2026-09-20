"""In-process pub/sub so handlers can subscribe to each other's events.

Example: TTS handler subscribes to chat.token events to know when to
play audio; ExpressionController subscribes to tts.sentence_start to
drive speaking animations. All without direct module coupling."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

Callback = Callable[[str, dict[str, Any]], Awaitable[None]]  # (method, params) -> None


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callback]] = defaultdict(list)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Remember the serving loop so emit_now() works off the loop thread.

        Called once at server startup. Without it emit_now() can only reach
        subscribers when it happens to run on the event loop, so any sync
        caller dispatched to a worker thread drops its events silently.
        """
        self._loop = loop if loop is not None else asyncio.get_running_loop()

    def on(self, method: str, callback: Callback) -> None:
        """Subscribe to events matching `method`."""
        self._subscribers[method].append(callback)

    def off(self, method: str, callback: Callback) -> None:
        """Unsubscribe."""
        try:
            self._subscribers[method].remove(callback)
        except ValueError:
            pass

    async def emit(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Fire an event to all subscribers. Each callback runs in its own task."""
        params = params or {}
        callbacks = self._subscribers.get(method, [])
        if not callbacks:
            return
        tasks = [
            asyncio.create_task(self._invoke(cb, method, params))
            for cb in callbacks
        ]
        for t in tasks:
            try:
                await t
            except Exception:
                logger.exception("event bus callback failed for %s", method)

    @staticmethod
    async def _invoke(
        callback: Callback,
        method: str,
        params: dict[str, Any],
    ) -> None:
        started = time.monotonic()
        try:
            await callback(method, params)
        finally:
            elapsed = time.monotonic() - started
            try:
                from config import settings

                threshold = max(
                    0.0,
                    float(getattr(settings, "EVENT_BUS_SLOW_CALLBACK_S", 1.0)),
                )
            except (ImportError, TypeError, ValueError):
                threshold = 1.0
            if elapsed >= threshold:
                name = getattr(callback, "__qualname__", "") or getattr(
                    callback,
                    "__name__",
                    callback.__class__.__name__,
                )
                logger.warning(
                    "slow event bus callback method=%s callback=%s elapsed_ms=%d",
                    method,
                    name,
                    round(elapsed * 1000),
                )

    def emit_now(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Fire-and-forget — safe to call from sync context, on or off the loop
        thread. Schedules tasks on the serving event loop without waiting."""
        params = params or {}
        callbacks = list(self._subscribers.get(method, []))
        if not callbacks:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            # Running on the loop already: keep the original inline behaviour.
            self._loop = loop
            self._schedule(loop, callbacks, method, params)
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.warning(
                "event bus dropped %s: no serving loop bound (call bind_loop at startup)",
                method,
            )
            return
        try:
            loop.call_soon_threadsafe(self._schedule, loop, callbacks, method, params)
        except RuntimeError:
            logger.warning("event bus dropped %s: serving loop is gone", method)

    @staticmethod
    def _schedule(
        loop: asyncio.AbstractEventLoop,
        callbacks: list[Callback],
        method: str,
        params: dict[str, Any],
    ) -> None:
        """Create the callback tasks. Always runs on the serving loop thread."""
        for cb in callbacks:
            asyncio.ensure_future(cb(method, params), loop=loop)

    def subscriber_count(self, method: str) -> int:
        return len(self._subscribers.get(method, []))


# singleton for this process
bus = EventBus()
