"""Bridge Amadeus custom providers into Codex Desktop's user config.

Amadeus launches its own App Server with request-scoped config overrides, but
Codex Desktop resumes persisted threads in a different App Server process.
That process must be able to resolve the same provider from the user's Codex
config.  Only non-secret connection metadata is synchronized here; provider
credentials stay in Amadeus and are fetched through Codex's command-backed
authentication contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
import tomllib
from typing import Any


class CodexDesktopProviderConflict(RuntimeError):
    """A user-owned provider definition conflicts with Amadeus's provider."""


@dataclass(frozen=True, slots=True)
class CodexDesktopProviderBridge:
    provider_id: str
    config_path: Path
    definition: dict[str, Any]
    needs_write: bool
    merge_strategy: str = "upsert"

    def config_edit(self) -> dict[str, Any]:
        return {
            "keyPath": f"model_providers.{self.provider_id}",
            "value": self.definition,
            "mergeStrategy": self.merge_strategy,
        }


def build_codex_desktop_provider_bridge(
    *,
    provider_id: str,
    base_url: str,
    api_key_env: str,
    project_root: Path,
    env_file: Path,
    python_executable: str | None = None,
    config_path: Path | None = None,
) -> CodexDesktopProviderBridge | None:
    """Describe the user-level config needed to resume an Amadeus thread.

    Existing user-owned providers are never silently replaced. Definitions
    previously managed by Amadeus are refreshed so moving the checkout or its
    Python environment does not strand persisted threads.
    """

    provider = str(provider_id or "").strip().lower()
    endpoint = str(base_url or "").strip().rstrip("/")
    key_env = str(api_key_env or "").strip()
    if not provider or not endpoint or not key_env:
        return None
    if provider in {"openai", "ollama", "lmstudio"}:
        # Codex owns these built-ins and explicitly forbids overriding them in
        # model_providers. They need no Desktop bridge.
        return None

    root = Path(project_root).resolve()
    credential_helper = root / "tools" / "codex_provider_auth.py"
    if not credential_helper.is_file():
        raise FileNotFoundError(f"Codex provider credential helper not found: {credential_helper}")

    resolved_config = (
        Path(config_path).expanduser().resolve()
        if config_path is not None
        else _default_codex_config_path()
    )
    definition: dict[str, Any] = {
        "name": f"{provider} (managed by Amadeus)",
        "base_url": endpoint,
        "wire_api": "responses",
        "auth": {
            "command": str(python_executable or sys.executable),
            "args": [
                str(credential_helper),
                "--env-file",
                str(Path(env_file).resolve()),
                "--env-key",
                key_env,
            ],
            "cwd": str(root),
            "timeout_ms": 5000,
            "refresh_interval_ms": 300000,
        },
    }

    existing = _read_user_provider(resolved_config, provider)
    if existing is None:
        needs_write = True
        merge_strategy = "upsert"
    elif _is_amadeus_managed(existing, provider):
        needs_write = existing != definition
        # Replace the complete managed table so an older env_key or bearer
        # token cannot survive alongside command auth, which Codex forbids.
        merge_strategy = "replace"
    elif _is_compatible_user_provider(existing, endpoint):
        needs_write = False
        definition = existing
        merge_strategy = "upsert"
    else:
        raise CodexDesktopProviderConflict(
            f"Codex user config already defines model provider {provider!r} "
            "with settings that differ from Amadeus"
        )

    return CodexDesktopProviderBridge(
        provider_id=provider,
        config_path=resolved_config,
        definition=definition,
        needs_write=needs_write,
        merge_strategy=merge_strategy,
    )


def provider_auth_overrides(bridge: CodexDesktopProviderBridge) -> tuple[str, ...]:
    """Return App Server flags matching the persistent command auth contract."""

    prefix = f"model_providers.{bridge.provider_id}"
    definition = bridge.definition
    auth = definition.get("auth") if isinstance(definition.get("auth"), dict) else None
    if auth is None:
        return ()
    args = auth.get("args") if isinstance(auth.get("args"), list) else []
    return (
        f"{prefix}.name={_json_string(definition.get('name'))}",
        f"{prefix}.base_url={_json_string(definition.get('base_url'))}",
        f'{prefix}.wire_api="responses"',
        f"{prefix}.auth.command={_json_string(auth.get('command'))}",
        f"{prefix}.auth.args={_json_value(args)}",
        f"{prefix}.auth.cwd={_json_string(auth.get('cwd'))}",
        f"{prefix}.auth.timeout_ms={int(auth.get('timeout_ms') or 5000)}",
        (
            f"{prefix}.auth.refresh_interval_ms="
            f"{int(auth.get('refresh_interval_ms') or 300000)}"
        ),
    )


def _default_codex_config_path() -> Path:
    codex_home = str(os.getenv("CODEX_HOME") or "").strip()
    home = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return (home / "config.toml").resolve()


def _read_user_provider(config_path: Path, provider_id: str) -> dict[str, Any] | None:
    if not config_path.exists():
        return None
    try:
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CodexDesktopProviderConflict(
            f"Cannot safely read Codex user config {config_path}: {exc}"
        ) from exc
    providers = config.get("model_providers")
    if not isinstance(providers, dict):
        return None
    value = providers.get(provider_id)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CodexDesktopProviderConflict(
            f"Codex model provider {provider_id!r} is not a TOML table"
        )
    return dict(value)


def _is_amadeus_managed(value: dict[str, Any], provider_id: str) -> bool:
    return value.get("name") == f"{provider_id} (managed by Amadeus)"


def _is_compatible_user_provider(value: dict[str, Any], base_url: str) -> bool:
    configured_url = str(value.get("base_url") or "").strip().rstrip("/")
    wire_api = str(value.get("wire_api") or "responses").strip().lower()
    has_auth = isinstance(value.get("auth"), dict) or bool(value.get("env_key"))
    return configured_url == base_url and wire_api == "responses" and has_auth


def _json_string(value: Any) -> str:
    return _json_value(str(value or ""))


def _json_value(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
