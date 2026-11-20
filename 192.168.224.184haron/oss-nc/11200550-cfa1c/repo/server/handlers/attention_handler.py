"""WebSocket API for bounded, Session-scoped attention requests."""

from __future__ import annotations

import logging
from collections.abc import Callable

from server.attention_request import AttentionRequestCoordinator
from server.protocol import Method
from server.ws_handler import RequestHandler


logger = logging.getLogger(__name__)


class AttentionRequestHandler(RequestHandler):
    methods = [Method.ATTENTION_LIST, Method.ATTENTION_RESOLVE]

    def __init__(
        self,
        coordinator: AttentionRequestCoordinator,
        *,
        current_session_id: Callable[[], str],
    ) -> None:
        self.coordinator = coordinator
        self._current_session_id = current_session_id

    def _session_id(self) -> str:
        return str(self._current_session_id() or "").strip()

    async def route_canvas_action(self, params: dict) -> dict:
        """Resolve or acknowledge one Slice action without trusting its Session."""

        session_id = self._session_id()
        if not session_id:
            return {"ok": False, "error": "attention_session_unavailable"}
        action = str(params.get("action") or "").strip().lower()
        request_id = str(
            params.get("request_id") or params.get("requestId") or ""
        ).strip()
        if not request_id:
            return {"ok": False, "error": "missing_attention_request_id"}
        if action == "presented":
            pending = self.coordinator.list_pending(session_id)
            if not any(item.get("id") == request_id for item in pending):
                return {"ok": False, "error": "attention_request_not_found"}
            logger.info(
                "[ATTENTION-PRESENTATION] presented session=%s request=%s surface=electron_slice",
                session_id,
                request_id,
            )
            return {
                "ok": True,
                "requestId": request_id,
                "surface": "electron_slice",
                "requests": pending,
            }
        if action != "resolve":
            return {"ok": False, "error": "unsupported_action"}
        return await self.coordinator.resolve(
            session_id=session_id,
            request_id=request_id,
            option_id=str(
                params.get("option_id") or params.get("optionId") or ""
            ),
        )

    async def handle(self, method: str, params: dict) -> dict | None:
        session_id = self._session_id()
        if method == Method.ATTENTION_LIST:
            return {
                "sessionId": session_id,
                "requests": self.coordinator.list_pending(session_id),
            }
        if method == Method.ATTENTION_RESOLVE:
            return await self.route_canvas_action({**params, "action": "resolve"})
        return None
