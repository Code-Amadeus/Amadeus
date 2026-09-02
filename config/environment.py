"""Single environment parsing boundary for Amadeus startup configuration."""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Mapping

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when a configured value cannot be parsed unambiguously."""


@dataclass(frozen=True)
class ConfigField:
    key: str
    value_type: str
    default: object
    aliases: tuple[str, ...] = ()


class EnvironmentReader:
    """Parse one process environment and record the declared configuration schema."""

    _TRUE = frozenset({"1", "true", "yes"})

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = values
        self._fields: dict[str, ConfigField] = {}

    def _register(
        self,
        key: str,
        value_type: str,
        default: object,
        aliases: tuple[str, ...],
    ) -> None:
        field = ConfigField(key, value_type, default, aliases)
        existing = self._fields.get(key)
        if existing is not None and existing != field:
            raise ConfigurationError(
                f"conflicting declarations for {key}: {existing!r} vs {field!r}"
            )
        self._fields[key] = field

    def _raw(self, key: str, aliases: tuple[str, ...]) -> str | None:
        value = self._values.get(key)
        if value is not None:
            return value
        for alias in aliases:
            value = self._values.get(alias)
            if value is not None:
                warnings.warn(
                    f"{alias} is deprecated; use {key}",
                    DeprecationWarning,
                    stacklevel=3,
                )
                return value
        return None

    def boolean(self, key: str, default: bool, *, aliases: tuple[str, ...] = ()) -> bool:
        self._register(key, "bool", default, aliases)
        raw = self._raw(key, aliases)
        if raw is None:
            return bool(default)
        normalized = str(raw).strip().lower()
        # Preserve the legacy settings contract: known truthy values enable a
        # feature; every other configured value is false. Tightening this is a
        # separate user-visible validation decision.
        return normalized in self._TRUE

    def integer(self, key: str, default: int, *, aliases: tuple[str, ...] = ()) -> int:
        self._register(key, "int", default, aliases)
        raw = self._raw(key, aliases)
        if raw is None:
            return int(default)
        try:
            return int(str(raw).strip())
        except ValueError as exc:
            raise ConfigurationError(f"{key} must be an integer; observed {raw!r}") from exc

    def number(self, key: str, default: float, *, aliases: tuple[str, ...] = ()) -> float:
        self._register(key, "float", default, aliases)
        raw = self._raw(key, aliases)
        if raw is None:
            return float(default)
        try:
            return float(str(raw).strip())
        except ValueError as exc:
            raise ConfigurationError(f"{key} must be a number; observed {raw!r}") from exc

    def string(self, key: str, default: str = "", *, aliases: tuple[str, ...] = ()) -> str:
        self._register(key, "str", default, aliases)
        raw = self._raw(key, aliases)
        return str(default) if raw is None else str(raw)

    def fields(self) -> tuple[ConfigField, ...]:
        return tuple(self._fields[key] for key in sorted(self._fields))


def load_project_environment(project_root: Path) -> EnvironmentReader:
    """Load the local dotenv once; an already-set process value keeps authority."""

    resolved_root = Path(project_root).resolve()
    with _PROJECT_ENVIRONMENTS_LOCK:
        reader = _PROJECT_ENVIRONMENTS.get(resolved_root)
        if reader is None:
            load_dotenv(resolved_root / ".env", override=False)
            reader = EnvironmentReader(os.environ)
            _PROJECT_ENVIRONMENTS[resolved_root] = reader
        return reader


_PROJECT_ENVIRONMENTS: dict[Path, EnvironmentReader] = {}
_PROJECT_ENVIRONMENTS_LOCK = RLock()


def venv_python(root: Path, name: str) -> Path:
    """Platform-correct Python executable path inside a project venv directory."""
    if os.name == "nt":
        return root / name / "Scripts" / "python.exe"
    return root / name / "bin" / "python3"
