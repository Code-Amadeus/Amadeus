"""Single composition root for built-in Provider adapters.

The server consumes these declarations generically. Adding an adapter changes
this composition root, not routing, Work Ledger, UI, or ProviderRuntime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from agent_host.provider_types import ProviderAdapter
from config import settings


@dataclass(frozen=True, slots=True)
class BuiltinProviderSpec:
    provider_id: str
    factory: Callable[[], ProviderAdapter]
    runtime_enabled: bool
    instantiate_when_disabled: bool = False
    required: bool = False


def _browser_branch_adapter() -> ProviderAdapter:
    from agent_host.adapters import BrowserBranchAdapter

    return BrowserBranchAdapter()


def _openclaw_adapter() -> ProviderAdapter:
    from agent_host.adapters import OpenClawAdapter

    return OpenClawAdapter()


def _direct_codex_adapter() -> ProviderAdapter:
    from agent_host.adapters import DirectCodexAdapter

    adapter = DirectCodexAdapter()
    adapter.require_startup_ready()
    return adapter


def _codex_app_server_adapter() -> ProviderAdapter:
    from agent_host.adapters import CodexAppServerAdapter

    adapter = CodexAppServerAdapter()
    adapter.require_startup_ready()
    return adapter


def builtin_provider_specs(
    *,
    direct_codex_enabled: bool | None = None,
    codex_app_server_enabled: bool | None = None,
) -> tuple[BuiltinProviderSpec, ...]:
    """Return runtime availability separately from static capability facts."""

    codex_on = (
        bool(settings.DIRECT_CODEX_PROVIDER_ENABLED)
        if direct_codex_enabled is None
        else bool(direct_codex_enabled)
    )
    app_server_on = (
        bool(settings.CODEX_APP_SERVER_PROVIDER_ENABLED)
        if codex_app_server_enabled is None
        else bool(codex_app_server_enabled)
    )
    if codex_on and app_server_on:
        raise RuntimeError(
            "Direct Codex and Codex App Server cannot both own Provider id 'codex'"
        )
    return (
        BuiltinProviderSpec("browser", _browser_branch_adapter, True),
        BuiltinProviderSpec("openclaw", _openclaw_adapter, True),
        BuiltinProviderSpec(
            "codex",
            _codex_app_server_adapter if app_server_on else _direct_codex_adapter,
            app_server_on or codex_on,
        ),
    )
