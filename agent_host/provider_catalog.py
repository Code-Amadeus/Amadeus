"""Built-in provider manifests without importing their runtime dependencies."""

from __future__ import annotations

from agent_host.provider_contract import (
    ProviderCapabilities,
    ProviderManifest,
    ProviderOperation,
)


OPENCLAW_MANIFEST = ProviderManifest(
    provider_id="openclaw",
    display_name="OpenClaw",
    runtime_kind="agent",
    contract_version="0.3",
    selection_priority=50,
    capabilities=ProviderCapabilities(
        task_kinds=("general", "research", "external_action"),
        workspace_access="none",
        workspace_ownership="none",
        durability="process",
        steering="immediate",
        resume="attach",
        cancellation="confirmed",
        interaction="none",
        event_model="canonical+native",
    ),
)

BROWSER_MANIFEST = ProviderManifest(
    provider_id="browser",
    display_name="Browser",
    runtime_kind="stateful_tool",
    selection_priority=40,
    experience_extensions=("browser.snapshot",),
    contract_version="0.3",
    capabilities=ProviderCapabilities(
        task_kinds=("browser",),
        workspace_access="none",
        workspace_ownership="none",
        durability="process",
        steering="immediate",
        resume="none",
        cancellation="best_effort",
        interaction="bidirectional",
        event_model="canonical+native",
        operations=(
            ProviderOperation("open", outcome_facet="browser.page_state"),
            ProviderOperation(
                "search",
                execution="observe_then_plan",
                atomic=False,
                outcome_facet="browser.page_state",
            ),
            ProviderOperation(
                "click_text",
                execution="observe_then_plan",
                outcome_facet="browser.page_state",
            ),
            ProviderOperation(
                "click_ref",
                execution="observe_then_plan",
                outcome_facet="browser.page_state",
            ),
            ProviderOperation(
                "fill_ref",
                execution="observe_then_plan",
                outcome_facet="browser.page_state",
            ),
            ProviderOperation(
                "back",
                execution="observe_then_plan",
                outcome_facet="browser.page_state",
            ),
            ProviderOperation("observe", outcome_facet="browser.page_state"),
            ProviderOperation("snapshot", outcome_facet="browser.page_state"),
            ProviderOperation("extract", outcome_facet="browser.page_state"),
            ProviderOperation("close"),
            ProviderOperation("close_session"),
        ),
    ),
)


DIRECT_CODEX_MANIFEST = ProviderManifest(
    provider_id="codex",
    display_name="Direct Codex",
    runtime_kind="coding_agent",
    selection_priority=70,
    capabilities=ProviderCapabilities(
        task_kinds=("general", "workspace_read", "workspace_mutation"),
        workspace_access="write",
        workspace_ownership="caller",
        durability="turn",
        steering="none",
        resume="none",
        cancellation="confirmed",
        interaction="none",
        event_model="canonical+native",
        capability_projections=("agent_skill", "mcp_connection"),
    ),
)


# The App Server implementation intentionally keeps the stable public provider
# id (``codex``).  Bootstrap selects exactly one Codex transport, so the Host
# never sees Direct Codex and App Server as two competing semantic providers.
# Native approval callbacks cross the same Host permission authority boundary
# as every other Provider; native wire details remain inside the adapter.
CODEX_APP_SERVER_MANIFEST = ProviderManifest(
    provider_id="codex",
    display_name="Codex App Server",
    runtime_kind="coding_agent",
    contract_version="0.3",
    selection_priority=100,
    capabilities=ProviderCapabilities(
        task_kinds=("general", "workspace_read", "workspace_mutation"),
        workspace_access="write",
        workspace_ownership="caller",
        durability="host_restart",
        steering="immediate",
        resume="attach",
        cancellation="confirmed",
        interaction="bidirectional",
        event_model="canonical+native",
        capability_projections=("agent_skill", "mcp_connection"),
    ),
)


KNOWN_PROVIDER_MANIFESTS: tuple[ProviderManifest, ...] = (
    BROWSER_MANIFEST,
    DIRECT_CODEX_MANIFEST,
    OPENCLAW_MANIFEST,
)
