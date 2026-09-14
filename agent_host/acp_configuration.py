"""Explicit Host configuration for external ACP agents; never workspace discovery."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ACP_PROVIDERS_ENV = "AMADEUS_ACP_PROVIDERS"
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_RESERVED = {"browser", "codex", "openclaw"}


@dataclass(frozen=True, slots=True)
class AcpAgentSpec:
    provider_id: str
    name: str
    command: str
    args: tuple[str, ...] = ()
    enabled: bool = False
    environment: dict[str, str] = field(default_factory=dict)
    config_options: dict[str, str] = field(default_factory=dict)
    resume: bool = False

    @classmethod
    def from_dict(cls, value: Any) -> AcpAgentSpec:
        if not isinstance(value, dict):
            raise ValueError("ACP agent must be an object")
        unknown = set(value) - {
            "id",
            "name",
            "command",
            "args",
            "enabled",
            "environment",
            "config_options",
            "resume",
        }
        if unknown:
            raise ValueError("Unknown ACP agent configuration field")
        provider_id = value.get("id", "")
        if not isinstance(provider_id, str) or not _ID.fullmatch(provider_id):
            raise ValueError("ACP id must be a lowercase provider identifier")
        if provider_id in _RESERVED:
            raise ValueError("ACP id collides with a built-in Provider")
        name, command = value.get("name", provider_id), value.get("command", "")
        for label, text, limit in (("name", name, 80), ("command", command, 4096)):
            if not isinstance(text, str) or not text.strip() or "\0" in text or len(text) > limit:
                raise ValueError(f"Invalid ACP {label}")
        args = value.get("args", [])
        if (
            not isinstance(args, list)
            or len(args) > 64
            or any(not isinstance(arg, str) or "\0" in arg or len(arg) > 4096 for arg in args)
        ):
            raise ValueError("ACP args must be a bounded string array")
        for key in ("enabled", "resume"):
            if not isinstance(value.get(key, False), bool):
                raise ValueError(f"ACP {key} must be boolean")
        environment = value.get("environment", {})
        if (
            not isinstance(environment, dict)
            or len(environment) > 64
            or any(
                not isinstance(k, str)
                or not _ENV.fullmatch(k)
                or not isinstance(v, str)
                or not _ENV.fullmatch(v)
                for k, v in environment.items()
            )
        ):
            raise ValueError("ACP environment maps child variable names to Host variable names")
        options = value.get("config_options", {})
        if (
            not isinstance(options, dict)
            or len(options) > 32
            or any(
                not isinstance(k, str)
                or not k
                or len(k) > 128
                or "\0" in k
                or not isinstance(v, str)
                or not v
                or len(v) > 512
                or "\0" in v
                for k, v in options.items()
            )
        ):
            raise ValueError("ACP config_options must contain string selections")
        return cls(
            provider_id,
            name.strip(),
            command.strip(),
            tuple(args),
            value.get("enabled", False),
            dict(environment),
            dict(options),
            value.get("resume", False),
        )

    def process_environment(self) -> dict[str, str]:
        result = {}
        for child_key, host_key in self.environment.items():
            if host_key not in os.environ:
                raise ValueError(f"ACP credential/configuration variable is missing: {host_key}")
            result[child_key] = os.environ[host_key]
        return result

    def executable(self) -> str:
        # Use executable + argv, never shell parsing. On Windows use node.exe
        # with the installed JS entry point instead of npm's .cmd launchers.
        resolved = shutil.which(self.command)
        if not resolved or (
            os.name == "nt" and Path(resolved).suffix.lower() in {".cmd", ".bat", ".ps1"}
        ):
            raise ValueError("ACP executable unavailable; use an executable and separate arguments")
        return str(Path(resolved).resolve())

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "name": self.name,
            "command": self.command,
            "args": list(self.args),
            "enabled": self.enabled,
            "environment": dict(self.environment),
            "config_options": dict(self.config_options),
            "resume": self.resume,
        }


def load_acp_agents(raw: str | None = None) -> tuple[AcpAgentSpec, ...]:
    encoded = os.environ.get(ACP_PROVIDERS_ENV, "") if raw is None else raw
    if not encoded.strip():
        return ()
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("ACP configuration exceeds 64 KiB")
    try:
        values = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError("ACP configuration is not valid JSON") from exc
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError("ACP configuration must be an array of at most 32 agents")
    specs = tuple(AcpAgentSpec.from_dict(value) for value in values)
    if len({spec.provider_id for spec in specs}) != len(specs):
        raise ValueError("Duplicate ACP Provider id")
    return specs
