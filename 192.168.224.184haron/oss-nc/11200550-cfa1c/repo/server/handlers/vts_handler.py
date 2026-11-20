"""Adapter for VTS connection management."""

from __future__ import annotations

import asyncio
from typing import Any

from server.event_bus import bus
from server.protocol import Method
from server.ws_handler import RequestHandler


class VtsHandler(RequestHandler):
    methods = [Method.VTS_CONNECT, Method.VTS_DISCONNECT]

    def __init__(self) -> None:
        self._vts_manager = None

    def configure(self, vts_manager) -> None:
        self._vts_manager = vts_manager
        # wire reconnect callback
        async def on_reconnect() -> None:
            await bus.emit(Method.VTS_CONNECTED, {})
        vts_manager.on_reconnect_callback = on_reconnect

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.VTS_CONNECT:
            return await self._connect(params)
        if method == Method.VTS_DISCONNECT:
            return await self._disconnect(params)
        return None

    async def _connect(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self._vts_manager:
            raise RuntimeError("vts handler not configured")
        ok = await asyncio.to_thread(self._vts_manager.connect)
        if ok:
            await bus.emit(Method.VTS_CONNECTED, {})
        return {"connected": ok}

    async def _disconnect(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._vts_manager:
            self._vts_manager.disconnect()
        await bus.emit(Method.VTS_DISCONNECTED, {})
        return {"connected": False}
