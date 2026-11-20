"""Ordinary stdio MCP server used by the standard Provider Journey."""

from __future__ import annotations

import asyncio
import json

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel


server = MCPServer("warehouse-fixture", version="1.0")


class InventoryResult(BaseModel):
    sku: str
    available: int
    session_id: str
    attempt_id: str
    revision: int


@server.tool(
    name="lookup_inventory",
    annotations=ToolAnnotations(readOnlyHint=True),
    structured_output=True,
)
def lookup_inventory(sku: str) -> InventoryResult:
    return InventoryResult(
        sku=sku,
        available=7,
        session_id="external-session",
        attempt_id="external-attempt",
        revision=999,
    )


@server.tool(
    name="reserve_part",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)
def reserve_part(part_id: str, quantity: int) -> str:
    return f"reserved {quantity} of {part_id}"


@server.tool(
    name="explode",
    annotations=ToolAnnotations(readOnlyHint=True),
)
def explode() -> str:
    raise RuntimeError("fixture failure")


@server.tool(
    name="empty_result",
    annotations=ToolAnnotations(readOnlyHint=True),
)
def empty_result() -> None:
    return None


@server.resource(
    "warehouse://inventory/A-17",
    name="inventory_snapshot",
    mime_type="application/json",
)
def inventory_snapshot() -> str:
    return json.dumps({"sku": "A-17", "available": 7})


if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
