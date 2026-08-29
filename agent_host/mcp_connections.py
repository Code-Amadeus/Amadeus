"""Host-managed MCP connections projected only into compatible Providers.

The desktop owns persistence and secret encryption.  At process launch it
passes one bounded registry snapshot to the backend.  This module validates
that snapshot, builds Provider-specific Codex configuration overrides, and
opens protocol clients for connection probes.  It never publishes MCP tool
schemas to Main Chat.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse


MCP_CONNECTIONS_ENV = "AMADEUS_MCP_CONNECTIONS"
MCP_CONNECTION_PROJECTION = "mcp_connection"
_REGISTRY_LIMIT_BYTES = 512 * 1024
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ENVIRONMENT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


McpTransport = Literal["stdio", "http"]


def _bounded_text(value: object, field_name: str, *, limit: int) -> str:
    text = str(value or "").strip()
    if "\x00" in text or len(text) > limit:
        raise ValueError(f"invalid MCP {field_name}")
    return text


def _string_tuple(
    value: object,
    field_name: str,
    *,
    item_limit: int = 4096,
    count_limit: int = 64,
) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > count_limit:
        raise ValueError(f"invalid MCP {field_name}")
    return tuple(
        _bounded_text(item, field_name, limit=item_limit)
        for item in value
    )


def _environment(value: object) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, Mapping) or len(value) > 64:
        raise ValueError("invalid MCP environment")
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip()
        if not _ENVIRONMENT_KEY_RE.fullmatch(key):
            raise ValueError(f"invalid MCP environment key: {key!r}")
        secret = str(raw_value or "")
        if not secret or "\x00" in secret or len(secret) > 16_384:
            raise ValueError("invalid MCP environment value")
        result[key] = secret
    return result


@dataclass(frozen=True, slots=True)
class McpConnectionSpec:
    connection_id: str
    display_name: str
    transport: McpTransport
    enabled: bool = False
    provider_ids: tuple[str, ...] = ()
    command: str = ""
    arguments: tuple[str, ...] = ()
    cwd: str = ""
    url: str = ""
    bearer_token_env_var: str = ""
    environment: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        connection_id = str(self.connection_id or "").strip().lower()
        if not _IDENTIFIER_RE.fullmatch(connection_id):
            raise ValueError(f"invalid MCP connection id: {connection_id!r}")
        display_name = _bounded_text(self.display_name, "display name", limit=80)
        if not display_name:
            raise ValueError("MCP display name is required")
        transport = str(self.transport or "").strip().lower()
        if transport not in {"stdio", "http"}:
            raise ValueError(f"unsupported MCP transport: {transport!r}")
        providers = tuple(
            dict.fromkeys(
                provider_id
                for provider_id in (
                    str(value or "").strip().lower()
                    for value in self.provider_ids
                )
                if _IDENTIFIER_RE.fullmatch(provider_id)
            )
        )
        command = _bounded_text(self.command, "command", limit=4096)
        cwd = _bounded_text(self.cwd, "working directory", limit=4096)
        url = _bounded_text(self.url, "URL", limit=4096)
        bearer = _bounded_text(
            self.bearer_token_env_var,
            "bearer token environment variable",
            limit=128,
        )
        if bearer and not _ENVIRONMENT_KEY_RE.fullmatch(bearer):
            raise ValueError("invalid MCP bearer token environment variable")
        if transport == "stdio" and not command:
            raise ValueError("stdio MCP connection requires a command")
        if transport == "http":
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("HTTP MCP connection requires an HTTP(S) URL")
        if self.enabled and not providers:
            raise ValueError("enabled MCP connection requires a compatible Work Provider")
        object.__setattr__(self, "connection_id", connection_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "transport", transport)
        object.__setattr__(self, "provider_ids", providers)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "arguments", tuple(self.arguments))
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "bearer_token_env_var", bearer)
        object.__setattr__(self, "environment", dict(self.environment))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "McpConnectionSpec":
        return cls(
            connection_id=str(value.get("id") or ""),
            display_name=str(value.get("name") or value.get("id") or ""),
            transport=str(value.get("transport") or "stdio"),  # type: ignore[arg-type]
            enabled=bool(value.get("enabled", False)),
            provider_ids=_string_tuple(
                value.get("provider_ids"),
                "provider ids",
                item_limit=64,
                count_limit=16,
            ),
            command=str(value.get("command") or ""),
            arguments=_string_tuple(value.get("arguments"), "arguments"),
            cwd=str(value.get("cwd") or ""),
            url=str(value.get("url") or ""),
            bearer_token_env_var=str(value.get("bearer_token_env_var") or ""),
            environment=_environment(value.get("environment")),
        )

    def supports_provider(self, provider_id: str) -> bool:
        return self.enabled and str(provider_id or "").strip().lower() in self.provider_ids

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.connection_id,
            "name": self.display_name,
            "transport": self.transport,
            "enabled": self.enabled,
            "provider_ids": list(self.provider_ids),
            "command": self.command,
            "arguments": list(self.arguments),
            "cwd": self.cwd,
            "url": self.url,
            "bearer_token_env_var": self.bearer_token_env_var,
            "environment_keys": sorted(self.environment),
            "main_chat_access": False,
        }


def load_mcp_connections(raw: str | None = None) -> tuple[McpConnectionSpec, ...]:
    encoded = os.environ.get(MCP_CONNECTIONS_ENV, "") if raw is None else raw
    if not str(encoded or "").strip():
        return ()
    if len(encoded.encode("utf-8")) > _REGISTRY_LIMIT_BYTES:
        raise ValueError("MCP registry exceeds 512 KiB")
    try:
        parsed = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError("MCP registry is not valid JSON") from exc
    values = parsed.get("connections") if isinstance(parsed, dict) else parsed
    if not isinstance(values, list) or len(values) > 64:
        raise ValueError("MCP registry must contain at most 64 connections")
    connections = tuple(
        McpConnectionSpec.from_dict(value)
        for value in values
        if isinstance(value, Mapping)
    )
    ids = [item.connection_id for item in connections]
    if len(ids) != len(set(ids)):
        raise ValueError("MCP registry contains duplicate connection ids")
    return connections


def connections_for_provider(
    connections: tuple[McpConnectionSpec, ...],
    provider_id: str,
) -> tuple[McpConnectionSpec, ...]:
    return tuple(item for item in connections if item.supports_provider(provider_id))


def _toml_literal(value: object) -> str:
    # JSON strings and arrays are valid TOML values and give deterministic
    # escaping without adding another serializer dependency.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def codex_mcp_config_overrides(
    connections: tuple[McpConnectionSpec, ...],
) -> tuple[str, ...]:
    overrides: list[str] = []
    for connection in connections_for_provider(connections, "codex"):
        prefix = f"mcp_servers.{connection.connection_id}"
        if connection.transport == "stdio":
            overrides.append(f"{prefix}.command={_toml_literal(connection.command)}")
            overrides.append(f"{prefix}.args={_toml_literal(list(connection.arguments))}")
            if connection.cwd:
                overrides.append(f"{prefix}.cwd={_toml_literal(connection.cwd)}")
        else:
            overrides.append(f"{prefix}.url={_toml_literal(connection.url)}")
            if connection.bearer_token_env_var:
                overrides.append(
                    f"{prefix}.bearer_token_env_var="
                    f"{_toml_literal(connection.bearer_token_env_var)}"
                )
        overrides.append(f"{prefix}.enabled=true")
    return tuple(overrides)


def mcp_provider_environment(
    connections: tuple[McpConnectionSpec, ...],
    provider_id: str,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for connection in connections_for_provider(connections, provider_id):
        for key, value in connection.environment.items():
            if key in result and result[key] != value:
                raise ValueError(
                    f"MCP environment variable {key!r} has conflicting values "
                    f"for Provider {provider_id!r}"
                )
            result[key] = value
    return result


@asynccontextmanager
async def open_mcp_connection(
    connection: McpConnectionSpec,
) -> AsyncIterator[Any]:
    """Open one official MCP client for a read-only discovery probe."""

    from mcp import Client

    if connection.transport == "stdio":
        from mcp.client.stdio import StdioServerParameters, stdio_client

        parameters = StdioServerParameters(
            command=connection.command,
            args=list(connection.arguments),
            cwd=connection.cwd or None,
            env={**os.environ, **connection.environment},
        )
        async with Client(stdio_client(parameters), raise_exceptions=False) as client:
            yield client
        return

    from httpx2 import AsyncClient
    from mcp.client.streamable_http import streamable_http_client

    headers: dict[str, str] = {}
    if connection.bearer_token_env_var:
        token = connection.environment.get(connection.bearer_token_env_var) or os.environ.get(
            connection.bearer_token_env_var,
            "",
        )
        if not token:
            raise RuntimeError("configured bearer token is unavailable")
        headers["Authorization"] = f"Bearer {token}"
    async with AsyncClient(headers=headers) as http_client:
        transport = streamable_http_client(connection.url, http_client=http_client)
        async with Client(transport, raise_exceptions=False) as client:
            yield client
