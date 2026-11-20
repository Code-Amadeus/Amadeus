"""Capability selection is invariant to brand names and registration order."""
from dataclasses import replace
from itertools import permutations

import pytest

from agent_host.provider_contract import (
    ProviderCapabilities, ProviderManifest, ProviderRequirements,
    ProviderSelectionError, select_provider,
)


CAPABILITY_CASES = [
    ("task_kind", {"task_kind": "browser"}, {"task_kinds": ("browser",)}),
    ("workspace_access", {"workspace_access": "write"}, {"workspace_access": "write"}),
    ("workspace_ownership", {"workspace_ownership": "caller"}, {"workspace_ownership": "caller"}),
    ("durability", {"durability": "host_restart"}, {"durability": "host_restart"}),
    ("steering", {"steering": "immediate"}, {"steering": "immediate"}),
    ("resume", {"resume": "attach"}, {"resume": "attach"}),
    ("interaction", {"interaction": "bidirectional"}, {"interaction": "bidirectional"}),
]


@pytest.mark.parametrize("axis,required,capable", CAPABILITY_CASES,
    ids=[row[0] for row in CAPABILITY_CASES])
@pytest.mark.parametrize("names", [("codex", "browser", "openclaw"),
    ("anonymous-b", "anonymous-c", "anonymous-a")], ids=["brands", "anonymous"])
def test_only_eligible_manifest_wins_even_when_default_and_priority_disagree(axis, required, capable, names):
    requirements = ProviderRequirements(**required)
    eligible = ProviderManifest(names[0], "Eligible", capabilities=ProviderCapabilities(**capable))
    # An incompatible declared default with a larger priority cannot invent the
    # missing capability. A third irrelevant candidate must not change that.
    default = ProviderManifest(names[1], "Default", selection_priority=10000)
    irrelevant = ProviderManifest(names[2], "Unrelated", selection_priority=100)
    for catalog in permutations((eligible, default, irrelevant)):
        assert select_provider(requirements, catalog,
            default_provider=default.provider_id).provider_id == eligible.provider_id
    with pytest.raises(ProviderSelectionError):
        select_provider(requirements, (default, irrelevant), default_provider=default.provider_id)


@pytest.mark.parametrize("policy", ["prefer", "require"])
def test_explicit_unavailable_or_incompatible_provider_cannot_gain_capability(policy):
    capable = ProviderManifest("writer", "Writer", capabilities=ProviderCapabilities(workspace_access="write"))
    incapable = ProviderManifest("reader", "Reader")
    for preferred in ("missing", "reader"):
        requirements = ProviderRequirements(workspace_access="write",
            preferred_provider=preferred, preference_policy=policy)
        if policy == "prefer":
            assert select_provider(requirements, (incapable, capable)).provider_id == "writer"
        else:
            with pytest.raises(ProviderSelectionError):
                select_provider(requirements, (incapable, capable))


def test_current_manifest_revocation_changes_selection_without_cached_brand_authority():
    capabilities = ProviderCapabilities(workspace_access="write")
    first = ProviderManifest("first", "First", capabilities=capabilities, selection_priority=100)
    second = ProviderManifest("second", "Second", capabilities=capabilities)
    requirements = ProviderRequirements(workspace_access="write")
    assert select_provider(requirements, (first, second)).provider_id == "first"
    revoked = replace(first, capabilities=ProviderCapabilities())
    assert select_provider(requirements, (revoked, second), default_provider="first").provider_id == "second"
    with pytest.raises(ProviderSelectionError):
        select_provider(requirements, (revoked,), default_provider="first")
