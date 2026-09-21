"""Role policy follows configuration without mutating registered capabilities."""
from dataclasses import replace
import json
from types import SimpleNamespace

from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST, PI_MANIFEST, OPENCLAW_MANIFEST
from agent_host.provider_roles import work_context_requirements, work_role_candidates
from config import settings
from server.work_planner_examples import augment_messages
from server.work_planner_prompt import get_work_planner_prompt


def test_registered_capabilities_determine_role_choices_without_assigning_roles():
    custom = replace(PI_MANIFEST, provider_id="custom-agent")
    manifests = (CODEX_APP_SERVER_MANIFEST, PI_MANIFEST, OPENCLAW_MANIFEST, custom)
    choices = work_role_candidates(manifests)
    assert choices["coding"] == ["codex", "custom-agent", "pi"]
    assert choices["execution"] == ["codex", "custom-agent", "openclaw", "pi"]
    assert OPENCLAW_MANIFEST.capabilities.workspace_access == "none"


def test_role_switch_alone_composes_both_recipients_with_their_actual_capabilities():
    manifests = {m.provider_id:m for m in (CODEX_APP_SERVER_MANIFEST, PI_MANIFEST, OPENCLAW_MANIFEST)}
    runtime = SimpleNamespace(get_manifest=manifests.get, provider_manifests=lambda: tuple(manifests.values()))
    before = dict(manifests)
    contexts = work_context_requirements(runtime,
        roles={"coding":"pi", "execution":"openclaw"}, primary_policy={}, additional_policies={})
    assert set(contexts) == {"codex", "pi", "openclaw"}
    assert contexts["pi"].workspace_access == "write"
    assert contexts["openclaw"].workspace_access == "none"
    assert contexts["openclaw"].workspace_ownership == "none"
    assert manifests == before


def test_one_agent_can_own_both_roles_without_duplicate_contexts():
    runtime = SimpleNamespace(get_manifest=lambda _: PI_MANIFEST, provider_manifests=lambda: (PI_MANIFEST,))
    contexts = work_context_requirements(runtime,
        roles={"coding":"pi", "execution":"pi"}, primary_policy={"workspace_access":"read"}, additional_policies={})
    assert list(contexts) == ["pi"]
    assert contexts["pi"].workspace_access == "read"


def test_planner_and_creation_examples_follow_changed_roles_but_continuations_keep_owner(monkeypatch):
    monkeypatch.setattr(settings, "WORK_CODING_PROVIDER", "custom-coder")
    monkeypatch.setattr(settings, "WORK_EXECUTION_PROVIDER", "openclaw")
    prompt = get_work_planner_prompt(("custom-coder", "pi", "openclaw", "codex"))
    assert 'Coding role: provider="custom-coder" (available)' in prompt
    assert 'Everyday execution role: provider="openclaw" (available)' in prompt
    messages = augment_messages([{"role":"system", "content":prompt}])
    decisions = [d for m in messages if m["role"] == "assistant"
        for d in json.loads(m["content"])["decisions"]]
    creates = [d for d in decisions if d["intent"] == "execute"]
    assert [d["provider"] for d in creates] == ["openclaw", "custom-coder", "custom-coder"]
    assert all(d["provider"] == "codex" for d in decisions if d["intent"] in {"amend", "message", "retract"})


def test_unavailable_assigned_agent_is_not_replaced_by_registered_priority(monkeypatch):
    monkeypatch.setattr(settings, "WORK_CODING_PROVIDER", "missing-coder")
    prompt = get_work_planner_prompt(("codex", "pi"))
    assert 'Coding role: provider="missing-coder" (unavailable)' in prompt
    assert 'Coding role: provider="codex"' not in prompt
