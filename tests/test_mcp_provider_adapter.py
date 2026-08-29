"""Standard MCP Provider boundary tests against the official SDK."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from agent_host.adapters.mcp_provider import (
    MCP_OPERATION_INPUT_KEY,
    McpProviderAdapter,
    McpToolBinding,
)
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderPermissionResponse, ProviderRunRequest
from server.capability_catalog import CapabilityCatalog
from server.capability_composition import sync_provider_capabilities
from server.outcome_verification import assess_provider_outcome


class _InventoryResult(BaseModel):
    sku: str
    available: int
    session_id: str
    attempt_id: str
    revision: int


async def _wait_for_event(record: Any, event_type: str, *, timeout: float = 2.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        match = next(
            (item for item in record.events if item.get("type") == event_type),
            None,
        )
        if match is not None:
            return match
        await asyncio.sleep(0.01)
    raise TimeoutError(event_type)


def test_standard_mcp_discovery_call_resource_and_host_identity_boundary() -> None:
    async def scenario() -> None:
        server = MCPServer("warehouse-provider", version="1.0")
        observed_arguments: list[dict[str, Any]] = []

        @server.tool(
            name="lookup_inventory",
            annotations=ToolAnnotations(readOnlyHint=True),
            structured_output=True,
        )
        def lookup_inventory(sku: str) -> _InventoryResult:
            observed_arguments.append({"sku": sku})
            return _InventoryResult(
                sku=sku,
                available=7,
                session_id="server-session-must-stay-nested",
                attempt_id="server-attempt-must-stay-nested",
                revision=999,
            )

        @server.resource(
            "warehouse://inventory/A-17",
            name="inventory_snapshot",
            mime_type="application/json",
        )
        def inventory_snapshot() -> str:
            return json.dumps({"sku": "A-17", "available": 7})

        adapter = McpProviderAdapter(
            provider_id="warehouse",
            display_name="Warehouse",
            server=server,
            task_kinds=("inventory",),
            bindings=(
                McpToolBinding(
                    operation_id="lookup_inventory",
                    tool_name="lookup_inventory",
                    argument_mapper=lambda value: {"sku": value["stock_code"]},
                    resource_uris=("warehouse://inventory/A-17",),
                    expected={
                        "structured_content": {"sku": "A-17", "available": 7}
                    },
                ),
            ),
        )
        runtime = ProviderRuntime()
        runtime.register(adapter)
        catalog = CapabilityCatalog()
        sync_provider_capabilities(catalog, runtime.provider_manifests())
        projected = catalog.snapshot()
        assert {
            contribution["kind"]
            for package in projected["packages"]
            for contribution in package["contributions"]
        } == {"provider", "mcp_server"}
        record = await runtime.start(
            ProviderRunRequest(
                provider="warehouse",
                task="Check stock for A-17",
                mode="lookup_inventory",
                metadata={
                    "session_id": "host-session",
                    "turn_id": "host-turn",
                    "work": {
                        "work_item_id": "host-work",
                        "attempt_id": "host-attempt",
                    },
                    MCP_OPERATION_INPUT_KEY: {"stock_code": "A-17"},
                },
            )
        )
        assert record.task_handle is not None
        await record.task_handle

        assert record.status == "done"
        assert observed_arguments == [{"sku": "A-17"}]
        assert record.metadata["session_id"] == "host-session"
        assert record.metadata["work"] == {
            "work_item_id": "host-work",
            "attempt_id": "host-attempt",
        }
        assert record.metadata["mcp"]["tool"] == "lookup_inventory"
        assert record.metadata["mcp"]["protocol_version"] == "2026-07-28"
        assert [item["type"] for item in record.events].count("tool.call") == 1
        assert [item["type"] for item in record.events].count("tool.result") == 1
        call = next(item for item in record.events if item["type"] == "tool.call")
        assert call["payload"]["tool"] == "lookup_inventory"
        assert call["payload"]["arguments"] == {"sku": "A-17"}
        assert "session_id" not in call["payload"]["arguments"]
        result = next(item for item in record.events if item["type"] == "tool.result")
        assert result["payload"]["is_error"] is False
        assert result["payload"]["structured_content"]["session_id"] == (
            "server-session-must-stay-nested"
        )

        verdict = assess_provider_outcome(
            execution_status=record.status,
            provider_report=record.result,
            metadata=record.metadata,
        )
        assert verdict is not None
        assert verdict.verified is True
        assert verdict.completeness == "complete"
        assert verdict.observed["resources"][0]["uri"] == (
            "warehouse://inventory/A-17"
        )
        await runtime.close()

    asyncio.run(scenario())


def test_malformed_mcp_arguments_fail_before_the_server_tool_runs() -> None:
    async def scenario() -> None:
        server = MCPServer("typed-provider")
        calls = 0

        @server.tool(
            name="reserve_part",
            annotations=ToolAnnotations(readOnlyHint=True),
        )
        def reserve_part(part_id: str, quantity: int) -> str:
            nonlocal calls
            calls += 1
            return f"reserved {quantity} {part_id}"

        adapter = McpProviderAdapter(
            provider_id="typed_mcp",
            display_name="Typed MCP",
            server=server,
            bindings=(
                McpToolBinding(
                    operation_id="reserve_part",
                    tool_name="reserve_part",
                ),
            ),
        )
        runtime = ProviderRuntime()
        runtime.register(adapter)
        record = await runtime.start(
            ProviderRunRequest(
                provider="typed_mcp",
                task="Reserve a part",
                mode="reserve_part",
                metadata={MCP_OPERATION_INPUT_KEY: {"part_id": "P-9"}},
            )
        )
        assert record.task_handle is not None
        await record.task_handle

        assert record.status == "error"
        assert record.error is not None
        assert "mcp_invalid_tool_arguments" in record.error
        assert calls == 0
        assert not any(item["type"] == "tool.call" for item in record.events)
        assert assess_provider_outcome(
            execution_status=record.status,
            provider_report=record.result,
            metadata=record.metadata,
        ).verified is False
        await runtime.close()

    asyncio.run(scenario())


def test_mcp_tool_error_and_missing_result_never_become_completion() -> None:
    async def scenario(operation: str) -> tuple[str, str | None]:
        server = MCPServer("failure-provider")

        @server.tool(
            name="explode",
            annotations=ToolAnnotations(readOnlyHint=True),
        )
        def explode() -> str:
            raise RuntimeError("external failure")

        @server.tool(
            name="empty_result",
            annotations=ToolAnnotations(readOnlyHint=True),
        )
        def empty_result() -> None:
            return None

        adapter = McpProviderAdapter(
            provider_id=f"failure_{operation}",
            display_name="Failure MCP",
            server=server,
            bindings=(McpToolBinding(operation, operation),),
        )
        runtime = ProviderRuntime()
        runtime.register(adapter)
        record = await runtime.start(
            ProviderRunRequest(
                provider=adapter.provider_id,
                task="Exercise a failing result",
                mode=operation,
            )
        )
        assert record.task_handle is not None
        await record.task_handle
        verdict = assess_provider_outcome(
            execution_status=record.status,
            provider_report=record.result,
            metadata=record.metadata,
        )
        assert verdict is not None and verdict.verified is False
        await runtime.close()
        return record.status, record.error

    exploded = asyncio.run(scenario("explode"))
    empty = asyncio.run(scenario("empty_result"))
    assert exploded[0] == "error" and "mcp_tool_error" in str(exploded[1])
    assert empty[0] == "error" and "mcp_result_missing" in str(empty[1])


def test_non_read_only_mcp_tool_uses_existing_host_permission_lane() -> None:
    async def scenario(*, allow: bool) -> tuple[int, str, list[str]]:
        server = MCPServer("mutating-provider")
        calls = 0

        @server.tool(
            name="reserve_part",
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
        )
        def reserve_part(part_id: str) -> str:
            nonlocal calls
            calls += 1
            return f"reserved {part_id}"

        adapter = McpProviderAdapter(
            provider_id=f"mutating_{'allow' if allow else 'deny'}",
            display_name="Mutating MCP",
            server=server,
            permission_timeout_s=2,
            bindings=(McpToolBinding("reserve_part", "reserve_part"),),
        )
        runtime = ProviderRuntime()
        runtime.register(adapter)
        record = await runtime.start(
            ProviderRunRequest(
                provider=adapter.provider_id,
                task="Reserve part P-9",
                mode="reserve_part",
                metadata={MCP_OPERATION_INPUT_KEY: {"part_id": "P-9"}},
            )
        )
        requested = await _wait_for_event(record, "permission.requested")
        permission = requested["payload"]["permissionRequest"]
        assert permission["tool"] == "reserve_part"
        assert permission["argument_keys"] == ["part_id"]
        resolved = await runtime.resolve_permission(
            record.run_id,
            ProviderPermissionResponse(
                request_id=permission["request_id"],
                allow=allow,
            ),
        )
        assert resolved["accepted"] is True
        assert record.task_handle is not None
        await record.task_handle
        event_types = [item["type"] for item in record.events]
        await runtime.close()
        return calls, record.status, event_types

    allowed = asyncio.run(scenario(allow=True))
    denied = asyncio.run(scenario(allow=False))
    assert allowed[0:2] == (1, "done")
    assert "permission.allowed" in allowed[2]
    assert denied[0:2] == (0, "error")
    assert "permission.denied" in denied[2]
    assert "tool.call" not in denied[2]
