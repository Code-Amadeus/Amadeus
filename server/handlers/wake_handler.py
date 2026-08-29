"""Wake word handler."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from server.event_bus import bus
from server.protocol import Method
from server.ws_handler import RequestHandler

logger = logging.getLogger(__name__)


class WakeHandler(RequestHandler):
    methods = [Method.WAKE_START, Method.WAKE_STOP, Method.WAKE_STATUS]

    def __init__(self) -> None:
        self._wake_service = None
        self._wake_service_factory: Callable[[], Any] | None = None

    def configure(self, wake_service_factory: Callable[[], Any] | None = None) -> None:
        self._wake_service_factory = wake_service_factory

    def service(self):
        return self._wake_service

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.WAKE_START:
            return await self.start(params)
        if method == Method.WAKE_STOP:
            return await self.stop(params)
        if method == Method.WAKE_STATUS:
            return await self.status()
        return None

    async def start(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._wake_service is None:
            if self._wake_service_factory is None:
                return {"status": "error", "error": "wake service unavailable"}
            self._wake_service = self._wake_service_factory()
        try:
            result = await asyncio.to_thread(self._wake_service.start, asyncio.get_running_loop())
            self._notify_coordinator(result, default_running=True)
            await bus.emit(Method.WAKE_STATUS, result)
            return result
        except Exception as exc:
            logger.exception("wake start failed")
            payload = {"status": "error", "error": str(exc)}
            await bus.emit(Method.WAKE_STATUS, payload)
            return payload

    async def stop(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._wake_service is None:
            return {"status": "stopped", "running": False}
        params = params or {}
        result = await asyncio.to_thread(self._wake_service.stop)
        self._notify_coordinator(result, default_running=False)
        if bool(params.get("close_shared_mic", True)):
            try:
                from asr.mic_input_service import close_mic_input_service

                close_mic_input_service()
            except Exception:
                logger.exception("failed to close shared mic after wake stop")
        await bus.emit(Method.WAKE_STATUS, result)
        return result

    async def status(self) -> dict[str, Any]:
        if self._wake_service is None:
            return {"status": "idle", "running": False}
        return self._wake_service.status()

    @staticmethod
    def _notify_coordinator(result: dict[str, Any], *, default_running: bool) -> None:
        try:
            from core.turn_coordinator import get_turn_coordinator

            running = bool((result or {}).get("running", default_running))
            get_turn_coordinator().on_wake_listening(running=running)
        except Exception:
            logger.debug("turn coordinator notify failed", exc_info=True)
