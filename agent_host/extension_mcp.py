"""Project validated installed packages into the existing MCP owner.

This module neither installs nor activates packages. The Host supplies the
validated startup snapshot; Provider selection and MCP authorization retain
their existing contracts.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from agent_host.mcp_connections import McpConnectionSpec


_TEMPLATE = re.compile(r"\$\{([^}]+)\}")


def extension_mcp_connection_id(package_id: str, contribution_id: str) -> str:
    identity = f"{package_id}/{contribution_id}".encode("utf-8")
    return "ext_" + hashlib.sha256(identity).hexdigest()[:40]


def _label(value: Any, fallback: str) -> str:
    if isinstance(value, Mapping):
        value = value.get("en") or value.get("en-US") or next(iter(value.values()), fallback)
    return str(value or fallback)[:80]


def project_extension_mcp_connections(entry: Mapping[str, Any]) -> tuple[McpConnectionSpec, ...]:
    """Bind only package-owned values; no fallback to the Host environment.

    Manifest/config validation is owned by the extension loader. This boundary
    still forbids credentials in argv/URLs because its output is a native MCP
    execution specification. Generated native ids are stable and do not alias
    package ids or the user's hand-written connection ids.
    """
    if not entry.get("enabled"):
        return ()
    manifest = entry["manifest"]
    package_id = str(manifest["id"])
    config = entry.get("config") or {}
    secrets = entry.get("secrets") or {}
    fields = manifest.get("config") or {}
    providers = tuple(entry.get("providers") or ())
    package_path = str(entry.get("package_path") or "")
    paths = {
        "package": package_path,
        "python": str(entry.get("python_path") or ""),
        "data": str(entry.get("data_path") or ""),
    }

    def render(value: str, *, allow_secret: bool = False) -> str:
        if not isinstance(value, str):
            raise ValueError("MCP package template must be text")

        def substitute(match):
            variable = match.group(1)
            if variable in paths:
                if not paths[variable]:
                    raise ValueError("MCP package path binding is unavailable")
                return paths[variable]
            if variable.startswith("config."):
                key = variable[7:]
                if key not in fields:
                    raise ValueError("MCP package references an undeclared setting")
                field = fields[key]
                if field.get("secret"):
                    if not allow_secret:
                        raise ValueError("MCP package secrets cannot appear in command, arguments or URL")
                    result = secrets.get(key, "")
                else:
                    result = config.get(key, field.get("default", ""))
                return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            raise ValueError("Unknown MCP package template variable")

        rendered = _TEMPLATE.sub(substitute, value)
        # Validate syntax on the template, never reinterpret substituted data.
        if "${" in _TEMPLATE.sub("", value):
            raise ValueError("Invalid MCP package template")
        return rendered

    result = []
    for contribution in manifest.get("contributes", {}).get("mcp_server", ()):
        contribution_id = str(contribution["id"])
        native_id = extension_mcp_connection_id(package_id, contribution_id)
        common = {
            "id": native_id,
            "name": _label(contribution.get("title") or manifest.get("display_name"),
                           f"{package_id}/{contribution_id}"),
            "transport": contribution["transport"],
            "enabled": bool(providers),
            "provider_ids": providers,
        }
        if contribution["transport"] == "stdio":
            command_template = contribution["command"]
            command = render(command_template)
            if command_template.startswith("${package}/"):
                root = Path(package_path).resolve(strict=True)
                executable = Path(command).resolve(strict=True)
                if root not in executable.parents or not executable.is_file():
                    raise ValueError("MCP executable must remain inside its package")
            environment = {key: render(value, allow_secret=True)
                           for key, value in contribution.get("env", {}).items()}
            # The native owner treats absent optional values as unset.
            common.update(command=command,
                          arguments=[render(item) for item in contribution.get("args", ())],
                          environment={key: value for key, value in environment.items() if value})
            # Do not require a package cwd: ACP's native cwd is the Work project.
            # Package resources use absolute paths or installed Python modules.
        else:
            common["url"] = render(contribution["url"])
            token_template = contribution.get("bearer_token")
            if token_template:
                match = re.fullmatch(r"\$\{config\.([a-z][a-z0-9_]{0,63})\}", token_template)
                if match is None or not fields.get(match[1], {}).get("secret"):
                    raise ValueError("MCP bearer token must reference a declared package secret")
                token = render(token_template, allow_secret=True)
                if not token:
                    raise ValueError("MCP package bearer credential is unavailable")
                key = "AMADEUS_MCP_TOKEN_" + native_id[4:].upper()
                common.update(bearer_token_env_var=key, environment={key: token})
        result.append(replace(McpConnectionSpec.from_dict(common), extension_package_id=package_id))
    return tuple(result)
