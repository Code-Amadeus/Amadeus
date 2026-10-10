"""Trusted built-in setting declarations; environment parsing stays in EnvironmentReader.

These files are packaged application data, never extension manifests or user
configuration. Computed defaults are supplied by their existing runtime owner.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from config.environment import EnvironmentReader


@cache
def configuration_groups() -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    keys: set[str] = set()
    for path in sorted(Path(__file__).parent.rglob("*.json")):
        group = json.loads(path.read_text(encoding="utf-8"))
        if group["id"] in groups or keys.intersection(group["config"]):
            raise ValueError(f"Duplicate configuration declaration in {path.name}")
        groups[group["id"]] = group
        keys.update(group["config"])
    return groups


def voice_backend_groups() -> list[dict[str, Any]]:
    return sorted(
        (group for group in configuration_groups().values() if "voice_backend" in group),
        key=lambda group: group["voice_backend"]["order"],
    )


def option_values(field: dict[str, Any]) -> tuple[str, ...]:
    return tuple(option if isinstance(option, str) else option["value"] for option in field["options"])


def configuration_field(key: str) -> dict[str, Any]:
    return next(group["config"][key] for group in configuration_groups().values() if key in group["config"])


def application_policy(key: str) -> str:
    group = next(group for group in configuration_groups().values() if key in group["config"])
    return group["config"][key].get("apply", group["apply"])


def runtime_fields() -> dict[str, dict[str, Any]]:
    return {field["runtime_key"]: {"key": key, **field}
            for group in configuration_groups().values() for key, field in group["config"].items()
            if "runtime_key" in field}


def read_catalog_value(reader: EnvironmentReader, key: str) -> Any:
    """Read a declared session input without importing the application facade."""
    field = configuration_field(key)
    if field.get("computed_default") or field.get("scope") in {"virtual", "desktop"}:
        raise ValueError(f"{key} requires its owning configuration context")
    read = reader.secret if field.get("secret") else {
        "boolean": reader.boolean, "integer": reader.integer, "number": reader.number,
    }.get(field["type"], reader.string)
    if field["type"] == "boolean" and "true_values" in field:
        return reader.boolean(key, field["default"], aliases=tuple(field.get("aliases", ())),
                              true_values=tuple(field["true_values"]))
    return read(key, field.get("default", ""), aliases=tuple(field.get("aliases", ())))


def read_catalog_environment(
    reader: EnvironmentReader, *, computed_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Expose existing settings.NAME callers without repeating each declaration.

Do not eagerly enforce provider readiness here: an unselected provider with an
incomplete configuration must not prevent the application from starting.
"""
    values = {}
    for group in configuration_groups().values():
        for key, field in group["config"].items():
            if field.get("scope", "backend") != "backend":
                continue
            if field.get("computed_default"):
                if computed_defaults is None or key not in computed_defaults:
                    continue
                default = computed_defaults[key]
            else:
                if computed_defaults is not None:
                    continue
                default = field.get("default", "")
            read = reader.secret if field.get("secret") else {
                "boolean": reader.boolean, "integer": reader.integer,
                "number": reader.number,
            }.get(field["type"], reader.string)
            values[key] = (reader.boolean(key, default, aliases=tuple(field.get("aliases", ())),
                                          true_values=tuple(field["true_values"]))
                           if field["type"] == "boolean" and "true_values" in field else
                           read(key, default, aliases=tuple(field.get("aliases", ()))))
    return values
