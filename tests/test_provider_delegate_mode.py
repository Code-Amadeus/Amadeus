"""Provider routing consumes typed requirements and registered manifests."""

import pytest

from config import settings
from agent_host.provider_catalog import (
    BROWSER_MANIFEST,
    CODEX_APP_SERVER_MANIFEST,
    OPENCLAW_MANIFEST,
    PI_MANIFEST,
)
from agent_host.provider_contract import ProviderRequirements, ProviderSelectionError, select_provider
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderRunRequest


MANIFESTS = (BROWSER_MANIFEST, CODEX_APP_SERVER_MANIFEST, OPENCLAW_MANIFEST)


def test_daily_default_is_pi_while_explicit_openclaw_remains_available(monkeypatch):
    monkeypatch.setattr(settings, "WORK_EXECUTION_PROVIDER", "pi")
    manifests = (*MANIFESTS, PI_MANIFEST)
    daily = select_provider(ProviderRequirements(), manifests, default_provider=settings.WORK_EXECUTION_PROVIDER)
    explicit = select_provider(ProviderRequirements(preferred_provider="openclaw", preference_policy="require"), manifests)
    assert daily.provider_id == "pi"
    assert explicit.provider_id == "openclaw"


@pytest.mark.parametrize("manifest,mode", [
    (CODEX_APP_SERVER_MANIFEST, "agent"),
    (CODEX_APP_SERVER_MANIFEST, "plan"),
    (CODEX_APP_SERVER_MANIFEST, "inspect"),
    (OPENCLAW_MANIFEST, "agent"),
    (BROWSER_MANIFEST, "observe"),
])
def test_explicit_provider_mode_survives_the_canonical_manifest_contract(manifest, mode):
    requirements = ProviderRequirements(task_kind="browser" if manifest.provider_id == "browser" else "general",
        preferred_provider=manifest.provider_id, preference_policy="require")
    selected = select_provider(requirements, MANIFESTS)
    request = ProviderRunRequest(provider=selected.provider_id, task="Synthetic operation", mode=mode,
        requirements=requirements)
    ProviderRuntime._apply_request_contract(request, manifest=manifest)
    assert request.mode == mode
    assert request.metadata["provider_manifest"]["provider_id"] == selected.provider_id
    if manifest.provider_id == "browser":
        assert request.metadata["provider_operation"] == "observe"
    else:
        assert "provider_operation" not in request.metadata


def test_file_routing_is_explained_by_requirements_and_manifest():
    requirements = ProviderRequirements(task_kind="workspace_mutation", workspace_access="write",
        preferred_provider="openclaw", preference_policy="prefer")
    selection = select_provider(requirements, MANIFESTS)
    assert selection.provider_id == "codex"
    assert selection.reason == "preferred_provider_incompatible"
    assert "workspace_access:write" in selection.rejected["openclaw"]


def test_explicit_provider_selection_preserves_the_declared_task_requirements():
    requirements = ProviderRequirements(task_kind="workspace_mutation", workspace_access="write",
        preferred_provider="codex", preference_policy="require")
    before = requirements.to_dict()
    selection = select_provider(requirements, MANIFESTS)
    assert requirements.to_dict() == before
    assert requirements.workspace_ownership is None
    assert selection.provider_id == "codex"
    assert selection.compatible_candidates == ("codex",)


def test_incompatible_required_provider_fails_closed_without_rewriting_requirements():
    requirements = ProviderRequirements(task_kind="workspace_mutation", workspace_access="write",
        preferred_provider="openclaw", preference_policy="require")
    with pytest.raises(ProviderSelectionError, match="required provider openclaw is incompatible"):
        select_provider(requirements, MANIFESTS)
    assert requirements.workspace_access == "write"


def test_explicit_host_force_is_distinct_from_ordinary_provider_preference():
    requirements = ProviderRequirements(task_kind="workspace_mutation", workspace_access="write",
        preferred_provider="openclaw", preference_policy="force")
    selection = select_provider(requirements, MANIFESTS)
    assert selection.provider_id == "openclaw"
    assert selection.reason == "forced_provider"
    assert "workspace_access:write" in selection.rejected["openclaw"]


def test_research_and_verified_browser_operations_use_distinct_manifest_capabilities():
    research = select_provider(ProviderRequirements(task_kind="research", preferred_provider="browser"), MANIFESTS)
    assert research.provider_id == "openclaw"
    assert research.reason == "preferred_provider_incompatible"
    assert "task_kind:research" in research.rejected["browser"]
    browser = select_provider(ProviderRequirements(task_kind="browser", preferred_provider="browser",
        preference_policy="require"), MANIFESTS)
    assert browser.provider_id == "browser"
