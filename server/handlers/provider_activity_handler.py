"""Read-only API and event wiring for the local Provider activity journal."""

from __future__ import annotations

import asyncio
from typing import Any

from agent_host.provider_activity_journal import ProviderActivityJournal
from server.event_bus import bus
from server.protocol import Method
from server.ws_handler import RequestHandler


class ProviderActivityHandler(RequestHandler):
    methods = [Method.PROVIDER_ACTIVITY_LIST]

    def __init__(self, journal: ProviderActivityJournal) -> None:
        self.journal = journal
        bus.on(Method.PROVIDER_EVENT, self._on_provider_event)
        bus.on(Method.PROVIDER_RESULT, self._on_provider_result)

    async def handle(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        if method != Method.PROVIDER_ACTIVITY_LIST:
            return None
        session_id = str(params.get("session_id") or params.get("sessionId") or "").strip()
        if not session_id:
            return {"runs": []}
        try:
            limit = max(1, min(int(params.get("limit") or 100), 128))
        except (TypeError, ValueError):
            limit = 100
        runs = await asyncio.to_thread(
            self.journal.list_runs,
            session_id,
            limit=limit,
        )
        return {"runs": runs}

    async def close(self) -> None:
        bus.off(Method.PROVIDER_EVENT, self._on_provider_event)
        bus.off(Method.PROVIDER_RESULT, self._on_provider_result)
        await asyncio.to_thread(self.journal.close)

    async def _on_provider_event(self, _method: str, params: dict[str, Any]) -> None:
        await asyncio.to_thread(self.journal.record_event, params)

    async def _on_provider_result(self, _method: str, params: dict[str, Any]) -> None:
        await asyncio.to_thread(self.journal.record_result, params)


__all__ = ["ProviderActivityHandler"]
