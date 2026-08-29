"""Read-only status and discovery probes for Host-managed MCP connections."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from agent_host.mcp_connections import McpConnectionSpec, open_mcp_connection
from server.protocol import Method
from server.ws_handler import RequestHandler


logger = logging.getLogger(__name__)


class McpConnectionHandler(RequestHandler):
    methods = [Method.MCP_CONNECTION_LIST, Method.MCP_CONNECTION_TEST]

    def __init__(
        self,
        connections: tuple[McpConnectionSpec, ...],
        *,
        open_connection: Callable[
            [McpConnectionSpec], AbstractAsyncContextManager[Any]
        ] = open_mcp_connection,
    ) -> None:
        self._connections = {item.connection_id: item for item in connections}
        self._open_connection = open_connection

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.MCP_CONNECTION_LIST:
            return {
                "connections": [
                    self._connections[key].public_dict()
                    for key in sorted(self._connections)
                ],
                "main_chat_access": False,
            }
        if method == Method.MCP_CONNECTION_TEST:
            return await self._test(params)
        return None

    async def _test(self, params: dict[str, Any]) -> dict[str, Any]:
        connection_id = str(params.get("connection_id") or "").strip().lower()
        connection = self._connections.get(connection_id)
        if connection is None:
            raise ValueError("MCP connection is not loaded; restart the backend to apply changes")
        timeout_s = 20.0
        try:
            async with asyncio.timeout(timeout_s):
                async with self._open_connection(connection) as client:
                    tools = await _list_tools(client)
        except TimeoutError:
            return {
                "connection_id": connection_id,
                "status": "error",
                "code": "connection_timeout",
                "detail": f"Connection did not complete within {timeout_s:g} seconds.",
                "main_chat_access": False,
            }
        except Exception as exc:
            logger.warning(
                "MCP connection probe failed id=%s error=%s",
                connection_id,
                type(exc).__name__,
            )
            return {
                "connection_id": connection_id,
                "status": "error",
                "code": "connection_failed",
                "detail": _safe_error(exc, connection),
                "main_chat_access": False,
            }
        names = sorted(
            str(getattr(item, "name", "") or "")
            for item in tools
            if str(getattr(item, "name", "") or "")
        )
        return {
            "connection_id": connection_id,
            "status": "connected",
            "tool_count": len(names),
            "tools": names[:100],
            "main_chat_access": False,
        }


async def _list_tools(client: Any) -> list[Any]:
    values: list[Any] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        result = await client.list_tools(cursor=cursor)
        if str(getattr(result, "result_type", "complete") or "complete") != "complete":
            raise RuntimeError("MCP discovery requires additional input")
        values.extend(list(getattr(result, "tools", None) or []))
        cursor = str(getattr(result, "next_cursor", "") or "").strip() or None
        if cursor is None:
            return values
        if cursor in seen:
            raise RuntimeError("MCP discovery returned a repeated cursor")
        seen.add(cursor)


def _safe_error(exc: Exception, connection: McpConnectionSpec) -> str:
    text = str(exc or exc.__class__.__name__).strip() or exc.__class__.__name__
    for secret in connection.environment.values():
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:500]
