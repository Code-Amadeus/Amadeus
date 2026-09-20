"""C7 bounded Host API for Continuity UI, diagnostics, and user control.

Electron receives explicit request/response DTOs only. SQLite remains owned by
ContinuityService / ContinuityStore and is never opened by UI code.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from core.continuity.service import ContinuityService
from server.protocol import Method
from server.ws_handler import RequestHandler


class ContinuityHandler(RequestHandler):
    methods = [
        Method.CONTINUITY_STATUS,
        Method.CONTINUITY_MEMORY_LIST,
        Method.CONTINUITY_MEMORY_PIN,
        Method.CONTINUITY_MEMORY_FORGET,
        Method.CONTINUITY_INDEX_REBUILD,
        Method.CONTINUITY_MAINTENANCE_RUN,
        Method.CONTINUITY_RELATIONSHIP_DEBUG,
        Method.CONTINUITY_LIFE_SCHEDULE,
        Method.CONTINUITY_RETRIEVAL_TRACE,
    ]

    def __init__(self, service: ContinuityService) -> None:
        self._service = service

    @staticmethod
    def _memory_id(params: dict[str, Any]) -> str:
        value = str(params.get("memory_id") or "").strip()
        if not value:
            raise ValueError("memory_id is required")
        return value

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.CONTINUITY_STATUS:
            return await asyncio.to_thread(self._service.c7_status)
        if method == Method.CONTINUITY_MEMORY_LIST:
            scope = str(params.get("scope") or "").strip() or None
            tier = str(params.get("tier") or "").strip().lower() or None
            try:
                limit = int(params.get("limit") or 250)
            except (TypeError, ValueError):
                raise ValueError("limit must be an integer") from None
            memories = await asyncio.to_thread(
                self._service.c7_list_memories, scope=scope, tier=tier, limit=limit
            )
            return {"ok": True, "memories": memories}
        if method == Method.CONTINUITY_MEMORY_PIN:
            memory_id = self._memory_id(params)
            if "pinned" not in params or not isinstance(params.get("pinned"), bool):
                raise ValueError("pinned must be a boolean")
            memory = await asyncio.to_thread(
                self._service.c7_set_memory_pinned, memory_id, bool(params["pinned"])
            )
            return {"ok": True, "memory": memory}
        if method == Method.CONTINUITY_MEMORY_FORGET:
            result = await asyncio.to_thread(
                self._service.c7_forget_memory, self._memory_id(params)
            )
            return {"ok": True, **result}
        if method == Method.CONTINUITY_INDEX_REBUILD:
            return {"ok": True, **(await self._service.c7_rebuild_indexes())}
        if method == Method.CONTINUITY_MAINTENANCE_RUN:
            return {"ok": True, **(await self._service.run_maintenance(reason="c7_user_control"))}
        if method == Method.CONTINUITY_RELATIONSHIP_DEBUG:
            status = await asyncio.to_thread(self._service.c7_status)
            return {"ok": True, "relationship": status["relationship"]}
        if method == Method.CONTINUITY_RETRIEVAL_TRACE:
            try:
                limit = int(params.get("limit") or 20)
            except (TypeError, ValueError):
                raise ValueError("limit must be an integer") from None
            return {"ok": True, "traces": await asyncio.to_thread(self._service.c8_retrieval_traces, limit=limit)}
        if method == Method.CONTINUITY_LIFE_SCHEDULE:
            local_date = str(params.get("local_date") or "").strip() or None
            if local_date is not None:
                try:
                    date.fromisoformat(local_date)
                except ValueError:
                    raise ValueError("local_date must use YYYY-MM-DD") from None
            result = await asyncio.to_thread(
                self._service.c7_schedule_inspector, local_date=local_date
            )
            return {"ok": True, **result}
        return None


__all__ = ["ContinuityHandler"]
