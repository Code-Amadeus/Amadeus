"""Adapter for expression/emotion presets and backend switching."""

from __future__ import annotations

from typing import Any

from server.protocol import Method
from server.ws_handler import RequestHandler


class ExpressionHandler(RequestHandler):
    methods = [
        Method.EXPRESSION_TRIGGER,
        Method.EXPRESSION_SET_BACKEND,
    ]

    def __init__(self) -> None:
        self._controller = None

    def configure(self, expression_controller) -> None:
        self._controller = expression_controller

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.EXPRESSION_TRIGGER:
            return await self._trigger(params)
        if method == Method.EXPRESSION_SET_BACKEND:
            return await self._set_backend(params)
        return None

    async def _trigger(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self._controller:
            raise RuntimeError("expression handler not configured")
        name = params.get("name", "")
        self._controller.transition_to(name)
        return {"emotion": name}

    async def _set_backend(self, params: dict[str, Any]) -> dict[str, Any]:
        backend = params.get("backend", "vts")
        if self._controller and hasattr(self._controller, '_backend'):
            self._controller._backend = backend
        return {"backend": backend}
