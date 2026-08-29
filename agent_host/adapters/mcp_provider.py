"""Standard MCP client adapter for an already-selected Provider.

The external server keeps its ordinary MCP tool and resource vocabulary.  This
adapter adds only the Amadeus control-plane envelope around that surface:
Provider selection, Work identity, permission, event attribution, and outcome
evidence remain Host-owned and are never copied into MCP tool arguments.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from mcp import Client

from agent_host.provider_contract import (
    ProviderCapabilities,
    ProviderManifest,
    ProviderOperation,
)
from agent_host.provider_outcome import ProviderOutcomeEvidence
from agent_host.provider_types import (
    EmitProviderEvent,
    ProviderEvent,
    ProviderPermissionResponse,
    ProviderRunRequest,
    ProviderRunResult,
)


MCP_OPERATION_INPUT_KEY = "mcp_operation_input"
MCP_OUTCOME_FACET = "mcp.tool_result"


def _identity_arguments(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return value


@dataclass(frozen=True, slots=True)
class McpToolBinding:
    """Bind one canonical Provider operation to one real server-owned tool.

    ``argument_mapper`` sees only ``mcp_operation_input``.  It cannot
    accidentally forward the surrounding Work/Session/permission metadata.
    The returned mapping is the exact payload sent to the discovered MCP tool;
    the generic adapter does not rename, flatten, or otherwise abstract it.
    """

    operation_id: str
    tool_name: str
    argument_mapper: Callable[[Mapping[str, Any]], Mapping[str, Any]] = (
        _identity_arguments
    )
    resource_uris: tuple[str, ...] = ()
    expected: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        operation_id = str(self.operation_id or "").strip().lower()
        tool_name = str(self.tool_name or "").strip()
        if not operation_id:
            raise ValueError("MCP Provider operation_id is required")
        if not tool_name:
            raise ValueError("MCP Provider tool_name is required")
        if not callable(self.argument_mapper):
            raise TypeError("MCP Provider argument_mapper must be callable")
        resource_uris = tuple(
            dict.fromkeys(
                str(value or "").strip()
                for value in self.resource_uris
                if str(value or "").strip()
            )
        )
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "tool_name", tool_name)
        object.__setattr__(self, "resource_uris", resource_uris)
        object.__setattr__(self, "expected", dict(self.expected))


@dataclass(slots=True)
class _PendingPermission:
    run_id: str
    request_id: str
    decision: asyncio.Future[bool]


class McpProviderAdapter:
    """Run declared operations through the public MCP protocol.

    The constructor accepts anything supported by the official ``mcp.Client``:
    an in-process ``MCPServer``, URL, or Transport such as ``stdio_client``.
    Production composition owns endpoint/configuration choice; this class owns
    only protocol discovery, exact invocation, and result normalization.
    """

    def __init__(
        self,
        *,
        provider_id: str,
        display_name: str,
        server: Any,
        bindings: tuple[McpToolBinding, ...],
        task_kinds: tuple[str, ...] = ("external_action",),
        client_factory: Callable[[Any], Any] | None = None,
        permission_timeout_s: float = 120.0,
    ) -> None:
        clean_provider = str(provider_id or "").strip().lower()
        if not clean_provider:
            raise ValueError("MCP Provider id is required")
        if not bindings:
            raise ValueError("MCP Provider requires at least one tool binding")
        by_operation: dict[str, McpToolBinding] = {}
        for binding in bindings:
            if not isinstance(binding, McpToolBinding):
                raise TypeError("MCP Provider bindings must be McpToolBinding values")
            if binding.operation_id in by_operation:
                raise ValueError(
                    f"duplicate MCP Provider operation: {binding.operation_id}"
                )
            by_operation[binding.operation_id] = binding
        self.provider_id = clean_provider
        self._server = server
        self._bindings = by_operation
        self._client_factory = client_factory or (
            lambda target: Client(target, raise_exceptions=False)
        )
        self._permission_timeout_s = max(1.0, float(permission_timeout_s))
        self._pending_permissions: dict[str, _PendingPermission] = {}
        self._call_started: set[str] = set()
        self.manifest = ProviderManifest(
            provider_id=clean_provider,
            display_name=str(display_name or clean_provider).strip(),
            runtime_kind="mcp_server",
            contract_version="0.3",
            selection_priority=30,
            capabilities=ProviderCapabilities(
                task_kinds=tuple(
                    dict.fromkeys(
                        str(value or "").strip().lower()
                        for value in task_kinds
                        if str(value or "").strip()
                    )
                )
                or ("external_action",),
                workspace_access="none",
                workspace_ownership="none",
                durability="turn",
                steering="none",
                resume="none",
                cancellation="best_effort",
                interaction="bidirectional",
                event_model="canonical+native",
                operations=tuple(
                    ProviderOperation(
                        binding.operation_id,
                        outcome_facet=MCP_OUTCOME_FACET,
                    )
                    for binding in bindings
                ),
            ),
        )

    async def run(
        self,
        request: ProviderRunRequest,
        run_id: str,
        emit: EmitProviderEvent,
    ) -> ProviderRunResult:
        operation = str(
            request.metadata.get("provider_operation") or request.mode or ""
        ).strip().lower()
        binding = self._bindings.get(operation)
        if binding is None:
            return self._error(
                "mcp_operation_not_declared",
                f"MCP Provider operation {operation!r} is not declared.",
            )
        operation_input = request.metadata.get(MCP_OPERATION_INPUT_KEY)
        operation_input = (
            operation_input if isinstance(operation_input, Mapping) else {}
        )
        try:
            raw_arguments = binding.argument_mapper(
                _json_mapping(operation_input, "MCP operation input")
            )
            arguments = _json_mapping(raw_arguments, "MCP tool arguments")
        except Exception as exc:
            return self._error(
                "mcp_argument_mapping_failed",
                f"MCP argument mapping failed: {_error_text(exc)}",
            )

        try:
            client_context = self._client_factory(self._server)
            async with client_context as client:
                tools = await _list_tools(client)
                tool = next(
                    (
                        item
                        for item in tools
                        if str(getattr(item, "name", "") or "")
                        == binding.tool_name
                    ),
                    None,
                )
                if tool is None:
                    return self._error(
                        "mcp_tool_not_discovered",
                        f"MCP server did not advertise tool {binding.tool_name!r}.",
                        metadata={"discovered_tools": _tool_names(tools)},
                    )
                try:
                    Draft202012Validator(
                        dict(getattr(tool, "input_schema", {}) or {})
                    ).validate(arguments)
                except Exception as exc:
                    return self._error(
                        "mcp_invalid_tool_arguments",
                        f"MCP tool arguments do not match the discovered schema: {_error_text(exc)}",
                        metadata={"tool": binding.tool_name},
                    )

                resources: list[Any] = []
                if binding.resource_uris:
                    resources = await _list_resources(client)
                    discovered_uris = {
                        str(getattr(item, "uri", "") or "") for item in resources
                    }
                    missing = [
                        uri for uri in binding.resource_uris if uri not in discovered_uris
                    ]
                    if missing:
                        return self._error(
                            "mcp_resource_not_discovered",
                            "MCP server did not advertise required resources: "
                            + ", ".join(missing),
                            metadata={"discovered_resources": sorted(discovered_uris)},
                        )

                await emit(
                    ProviderEvent(
                        provider=self.provider_id,
                        run_id=run_id,
                        type="run.status",
                        payload={
                            "status": "running",
                            "stage": "mcp_discovered",
                            "tool": binding.tool_name,
                            "tool_count": len(tools),
                            "resource_count": len(resources),
                        },
                    )
                )
                if not _tool_is_read_only(tool):
                    allowed = await self._request_permission(
                        run_id=run_id,
                        tool_name=binding.tool_name,
                        arguments=arguments,
                        emit=emit,
                    )
                    if not allowed:
                        return self._error(
                            "mcp_permission_denied",
                            f"MCP tool {binding.tool_name!r} was not authorized.",
                            metadata={"tool": binding.tool_name},
                        )

                await emit(
                    ProviderEvent(
                        provider=self.provider_id,
                        run_id=run_id,
                        type="tool.call",
                        payload={
                            "tool": binding.tool_name,
                            "arguments": arguments,
                            "protocol": "mcp",
                        },
                    )
                )
                self._call_started.add(run_id)
                result = await client.call_tool(binding.tool_name, arguments)
                observed_result = _tool_result(result)
                await emit(
                    ProviderEvent(
                        provider=self.provider_id,
                        run_id=run_id,
                        type="tool.result",
                        payload={
                            "tool": binding.tool_name,
                            "protocol": "mcp",
                            **observed_result,
                        },
                    )
                )
                if observed_result["is_error"] is True:
                    return self._error(
                        "mcp_tool_error",
                        _result_text(observed_result)
                        or f"MCP tool {binding.tool_name!r} returned an error.",
                        metadata={"tool": binding.tool_name},
                    )
                if observed_result["result_type"] != "complete":
                    return self._error(
                        "mcp_input_required",
                        f"MCP tool {binding.tool_name!r} requires more input.",
                        metadata={"tool": binding.tool_name},
                    )
                if observed_result["result_present"] is not True:
                    return self._error(
                        "mcp_result_missing",
                        f"MCP tool {binding.tool_name!r} returned no result.",
                        metadata={"tool": binding.tool_name},
                    )

                resource_snapshots: list[dict[str, Any]] = []
                for uri in binding.resource_uris:
                    resource_result = await client.read_resource(uri)
                    snapshot = _resource_result(uri, resource_result)
                    if snapshot["result_type"] != "complete" or not snapshot["contents"]:
                        return self._error(
                            "mcp_resource_result_missing",
                            f"MCP resource {uri!r} returned no complete content.",
                            metadata={"tool": binding.tool_name, "resource_uri": uri},
                        )
                    resource_snapshots.append(snapshot)

                server_info = _model_value(getattr(client, "server_info", None), 1200)
                protocol_version = str(
                    getattr(client, "protocol_version", "") or ""
                )
                observed = {
                    "tool": binding.tool_name,
                    **observed_result,
                    "resources": resource_snapshots,
                }
                expected = {
                    "tool": binding.tool_name,
                    "result_present": True,
                    **dict(binding.expected),
                    **(
                        {"resource_uris": list(binding.resource_uris)}
                        if binding.resource_uris
                        else {}
                    ),
                }
                text = _result_text(observed_result)
                return ProviderRunResult(
                    status="done",
                    result=text or _compact_json(
                        observed_result.get("structured_content"), 4000
                    ),
                    metadata={
                        "mcp": {
                            "protocol_version": protocol_version,
                            "server_info": server_info,
                            "operation": operation,
                            "tool": binding.tool_name,
                            "discovered_tools": _tool_names(tools),
                            "discovered_resources": [
                                str(getattr(item, "uri", "") or "")
                                for item in resources
                            ],
                            "result_type": observed_result["result_type"],
                        }
                    },
                    outcome_evidence=ProviderOutcomeEvidence(
                        facet=MCP_OUTCOME_FACET,
                        operation=operation,
                        expected=expected,
                        observed=observed,
                    ),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._error(
                "mcp_transport_failed",
                f"MCP transport failed: {_error_text(exc)}",
                metadata={"tool": binding.tool_name},
            )
        finally:
            self._call_started.discard(run_id)
            for request_id, pending in tuple(self._pending_permissions.items()):
                if pending.run_id == run_id:
                    if not pending.decision.done():
                        pending.decision.set_result(False)
                    self._pending_permissions.pop(request_id, None)

    async def resolve_permission(
        self,
        run_id: str,
        response: ProviderPermissionResponse,
    ) -> dict[str, Any]:
        pending = self._pending_permissions.get(response.request_id)
        if pending is None:
            return {"accepted": False, "reason": "permission_request_not_pending"}
        if pending.run_id != str(run_id or "").strip():
            return {"accepted": False, "reason": "permission_run_mismatch"}
        if not pending.decision.done():
            pending.decision.set_result(bool(response.allow))
        return {"accepted": True}

    async def cancel(self, run_id: str) -> dict[str, Any]:
        clean_run = str(run_id or "").strip()
        pending = next(
            (
                item
                for item in self._pending_permissions.values()
                if item.run_id == clean_run
            ),
            None,
        )
        if pending is not None and not pending.decision.done():
            pending.decision.set_result(False)
            return {"confirmed": True, "cancelled": True, "reason": "before_call"}
        if clean_run in self._call_started:
            return {
                "confirmed": False,
                "cancelled": False,
                "reason": "mcp_tool_outcome_unknown",
            }
        return {"confirmed": True, "cancelled": True, "reason": "not_started"}

    async def _request_permission(
        self,
        *,
        run_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        emit: EmitProviderEvent,
    ) -> bool:
        request_id = f"mcp:{run_id}:{uuid.uuid4().hex[:12]}"[:240]
        loop = asyncio.get_running_loop()
        pending = _PendingPermission(
            run_id=run_id,
            request_id=request_id,
            decision=loop.create_future(),
        )
        self._pending_permissions[request_id] = pending
        await emit(
            ProviderEvent(
                provider=self.provider_id,
                run_id=run_id,
                type="permission.requested",
                payload={
                    "permissionRequest": {
                        "request_id": request_id,
                        "capability": "mcp.tool.call",
                        "action": "call_tool",
                        "tool": tool_name,
                        "scope": [self.provider_id, tool_name],
                        "reason": (
                            f"The external MCP server did not declare {tool_name!r} "
                            "read-only, so this call requires explicit approval."
                        ),
                        "reversibility": "unknown",
                        "options": ["allow_once", "deny"],
                        "retryRequired": False,
                        "diagnosticOnly": False,
                        "argument_keys": sorted(str(key) for key in arguments)[:32],
                    }
                },
            )
        )
        try:
            return bool(
                await asyncio.wait_for(
                    asyncio.shield(pending.decision),
                    timeout=self._permission_timeout_s,
                )
            )
        except asyncio.TimeoutError:
            return False
        finally:
            self._pending_permissions.pop(request_id, None)

    @staticmethod
    def _error(
        code: str,
        detail: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProviderRunResult:
        clean_code = str(code or "mcp_provider_error").strip()
        clean_detail = str(detail or clean_code).strip()[:2000]
        return ProviderRunResult(
            status="error",
            result="",
            error=f"{clean_code}: {clean_detail}",
            metadata={"mcp": {"status": "error", **dict(metadata or {})}},
        )


async def _list_tools(client: Any) -> list[Any]:
    values: list[Any] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        result = await client.list_tools(cursor=cursor)
        if str(getattr(result, "result_type", "complete") or "complete") != "complete":
            raise RuntimeError("MCP tool discovery requires more input")
        values.extend(list(getattr(result, "tools", None) or []))
        cursor = str(getattr(result, "next_cursor", "") or "").strip() or None
        if cursor is None:
            return values
        if cursor in seen:
            raise RuntimeError("MCP tool discovery returned a repeated cursor")
        seen.add(cursor)


async def _list_resources(client: Any) -> list[Any]:
    values: list[Any] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        result = await client.list_resources(cursor=cursor)
        if str(getattr(result, "result_type", "complete") or "complete") != "complete":
            raise RuntimeError("MCP resource discovery requires more input")
        values.extend(list(getattr(result, "resources", None) or []))
        cursor = str(getattr(result, "next_cursor", "") or "").strip() or None
        if cursor is None:
            return values
        if cursor in seen:
            raise RuntimeError("MCP resource discovery returned a repeated cursor")
        seen.add(cursor)


def _tool_is_read_only(tool: Any) -> bool:
    annotations = getattr(tool, "annotations", None)
    return getattr(annotations, "read_only_hint", None) is True


def _tool_names(tools: list[Any]) -> list[str]:
    return [
        str(getattr(item, "name", "") or "")
        for item in tools
        if str(getattr(item, "name", "") or "")
    ]


def _tool_result(result: Any) -> dict[str, Any]:
    content = [
        _model_value(item, 4000)
        for item in list(getattr(result, "content", None) or [])[:8]
    ]
    structured = _model_value(getattr(result, "structured_content", None), 8000)
    result_present = bool(
        _meaningful_value(structured)
        or any(_content_value_present(item) for item in content)
    )
    return {
        "is_error": bool(getattr(result, "is_error", False)),
        "result_type": str(getattr(result, "result_type", "complete") or "complete"),
        "result_present": result_present,
        "content": content,
        "structured_content": structured,
    }


def _resource_result(uri: str, result: Any) -> dict[str, Any]:
    contents = [
        _model_value(item, 4000)
        for item in list(getattr(result, "contents", None) or [])[:8]
    ]
    return {
        "uri": uri,
        "result_type": str(getattr(result, "result_type", "complete") or "complete"),
        "contents": [item for item in contents if item is not None],
    }


def _content_value_present(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return value not in (None, "", [], {})
    for key in ("text", "data", "blob", "resource", "uri"):
        if value.get(key) not in (None, "", [], {}):
            return True
    return False


def _meaningful_value(value: Any) -> bool:
    """Treat explicit false/zero as results, but not null-only wrappers."""

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(_meaningful_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_meaningful_value(item) for item in value)
    return True


def _result_text(result: Mapping[str, Any]) -> str:
    texts: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, Mapping) and str(item.get("type") or "") == "text":
            text = str(item.get("text") or "").strip()
            if text:
                texts.append(text)
    return "\n\n".join(texts)[:4000]


def _model_value(value: Any, max_chars: int) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump(by_alias=True, exclude_none=True)
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = json.dumps(str(value), ensure_ascii=False)
    if len(encoded) <= max_chars:
        return json.loads(encoded)
    return {
        "__truncated__": True,
        "characters": len(encoded),
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def _json_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} must be JSON-serializable") from exc
    if not isinstance(decoded, dict):
        raise TypeError(f"{label} must be an object")
    return decoded


def _compact_json(value: Any, max_chars: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(value or "")
    return text[:max_chars]


def _error_text(error: BaseException) -> str:
    return " ".join(str(error or error.__class__.__name__).split())[:800]
