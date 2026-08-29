from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.provider_requirements import (
    DelegateRequirementFacts,
    compile_delegate_requirements,
)


def _compile(
    attrs: dict | None = None,
    *,
    task_mutation: bool = False,
    source_mutation: bool = False,
    target_workspace_mode: str = "",
    continuation_provider: str = "",
    source_address: bool = False,
    required_workspace_access: str = "",
):
    return compile_delegate_requirements(
        DelegateRequirementFacts.from_delegate(
            attrs,
            task_requests_workspace_mutation=task_mutation,
            source_requests_workspace_mutation=source_mutation,
            target_workspace_mode=target_workspace_mode,
            continuation_provider=continuation_provider,
            source_has_browser_address=source_address,
            required_workspace_access=required_workspace_access,
        )
    )


def test_execution_intent_and_mutation_fact_matrix() -> None:
    cases = (
        ({}, False, False, "general", "none"),
        ({"intent": "execute"}, False, False, "general", "none"),
        ({"intent": "amend"}, False, False, "workspace_mutation", "write"),
        ({"intent": "execute"}, True, False, "workspace_mutation", "write"),
        ({"intent": "execute"}, False, True, "workspace_mutation", "write"),
        ({"intent": "amend"}, True, True, "workspace_mutation", "write"),
    )
    for attrs, task_fact, source_fact, task_kind, access in cases:
        requirements = _compile(
            attrs,
            task_mutation=task_fact,
            source_mutation=source_fact,
        )
        assert requirements.task_kind == task_kind, attrs
        assert requirements.workspace_access == access, attrs
        assert requirements.ownership == "managed", attrs


def test_adjudicated_workspace_effect_is_provider_neutral() -> None:
    read = _compile(
        {"provider": "codex", "intent": "execute"},
        required_workspace_access="read",
    )
    assert read.task_kind == "workspace_read"
    assert read.workspace_access == "read"

    write = _compile(
        {"provider": "codex", "intent": "execute"},
        required_workspace_access="write",
    )
    assert write.task_kind == "workspace_mutation"
    assert write.workspace_access == "write"

    # Exact source evidence remains a fail-safe if an adjudicator understates
    # the effect; a model decision cannot erase an explicit file write.
    understated = _compile(
        {"provider": "codex", "intent": "execute"},
        source_mutation=True,
        required_workspace_access="none",
    )
    assert understated.workspace_access == "write"


def test_browser_label_does_not_manufacture_page_state() -> None:
    for provider in ("browser", "web", "browser_provider", "playwright"):
        requirements = _compile({"provider": provider})
        assert requirements.preferred_provider == "browser", provider
        assert requirements.task_kind == "research", provider
        assert requirements.preference_policy == "prefer", provider

    addressless_open = _compile({"provider": "browser", "action": "open"})
    assert addressless_open.task_kind == "research"
    assert addressless_open.preference_policy == "prefer"

    exact_open = _compile(
        {"provider": "browser", "action": "open"},
        source_address=True,
    )
    assert exact_open.task_kind == "browser"
    assert exact_open.preference_policy == "require"

    for action in ("observe", "click_ref", "back"):
        requirements = _compile({"provider": "browser", "action": action})
        assert requirements.task_kind == "browser", action
        assert requirements.preference_policy == "require", action

    current_page_search = _compile(
        {"provider": "browser", "action": "search", "branch": "continue"}
    )
    assert current_page_search.task_kind == "browser"
    assert current_page_search.preference_policy == "require"

    new_page_search = _compile(
        {"provider": "browser", "action": "search", "branch": "new"}
    )
    assert new_page_search.task_kind == "browser"
    assert new_page_search.preference_policy == "require"

    guessed_branch_open = _compile(
        {"provider": "browser", "action": "open", "branch": "continue"}
    )
    assert guessed_branch_open.task_kind == "research"
    assert guessed_branch_open.preference_policy == "prefer"

    independent_search = _compile({"provider": "browser", "action": "search"})
    assert independent_search.task_kind == "research"
    assert independent_search.preference_policy == "prefer"

    # A future Provider name must remain exact. In particular, the host does
    # not grow aliases merely to make a specific Adapter easier to select.
    requirements = _compile({"provider": "direct_codex"}, task_mutation=True)
    assert requirements.preferred_provider == "direct_codex"
    assert requirements.task_kind == "workspace_mutation"
    assert requirements.preference_policy == "require"


def test_preference_policy_matrix_is_explicit_and_bounded() -> None:
    cases = (
        ({}, False, "", "prefer"),
        ({"provider": "codex"}, False, "codex", "require"),
        ({"provider": "codex"}, True, "codex", "require"),
        ({"provider": "openclaw"}, False, "openclaw", "require"),
        ({"provider": "openclaw"}, True, "openclaw", "prefer"),
        (
                {"provider": "openclaw", "fallback": "locus_failed"},
                True,
                "openclaw",
                "prefer",
        ),
        (
            {"provider": "codex", "force_provider": "user"},
            True,
            "codex",
            "force",
        ),
    )
    for attrs, mutation, provider, policy in cases:
        requirements = _compile(attrs, task_mutation=mutation)
        assert requirements.preferred_provider == provider, attrs
        assert requirements.preference_policy == policy, attrs


def test_browser_label_cannot_erase_a_write_requirement() -> None:
    requirements = _compile(
        {"provider": "browser", "intent": "execute"},
        source_mutation=True,
    )
    assert requirements.task_kind == "workspace_mutation"
    assert requirements.workspace_access == "write"
    assert requirements.preference_policy == "prefer"


def test_control_plane_can_require_interaction_semantics() -> None:
    requirements = compile_delegate_requirements(
        DelegateRequirementFacts(
            requested_provider="browser",
            required_steering="next_turn",
            required_interaction="bidirectional",
        )
    )
    assert requirements.task_kind == "browser"
    assert requirements.steering == "next_turn"
    assert requirements.interaction == "bidirectional"
    assert requirements.preference_policy == "require"


def test_exact_page_state_can_override_a_model_provider_label() -> None:
    requirements = _compile(
        {"provider": "openclaw", "action": "open"},
        source_address=True,
    )
    assert requirements.task_kind == "browser"
    assert requirements.preferred_provider == "openclaw"
    assert requirements.preference_policy == "prefer"


def test_amend_uses_the_target_work_item_workspace_fact() -> None:
    workspace_less = _compile(
        {"intent": "amend"},
        target_workspace_mode="none",
        continuation_provider="openclaw",
    )
    assert workspace_less.task_kind == "general"
    assert workspace_less.workspace_access == "none"
    assert workspace_less.preferred_provider == "openclaw"
    assert workspace_less.preference_policy == "prefer"

    workspace_backed = _compile(
        {"intent": "amend"},
        target_workspace_mode="worktree",
        continuation_provider="locus",
    )
    assert workspace_backed.task_kind == "workspace_mutation"
    assert workspace_backed.workspace_access == "write"
    assert workspace_backed.preferred_provider == "locus"

    # Durable target facts refine an otherwise ambiguous semantic verb; they
    # never erase explicit file-mutation evidence from either source.
    explicit_file_change = _compile(
        {"intent": "amend"},
        source_mutation=True,
        target_workspace_mode="none",
        continuation_provider="openclaw",
    )
    assert explicit_file_change.task_kind == "workspace_mutation"
    assert explicit_file_change.workspace_access == "write"


def test_browser_continuation_is_a_soft_capability_preference() -> None:
    requirements = _compile(
        {"intent": "amend"},
        target_workspace_mode="none",
        continuation_provider="browser",
    )
    assert requirements.task_kind == "browser"
    assert requirements.preferred_provider == "browser"
    assert requirements.preference_policy == "prefer"


def _main() -> None:
    test_execution_intent_and_mutation_fact_matrix()
    test_adjudicated_workspace_effect_is_provider_neutral()
    test_browser_label_does_not_manufacture_page_state()
    test_preference_policy_matrix_is_explicit_and_bounded()
    test_browser_label_cannot_erase_a_write_requirement()
    test_control_plane_can_require_interaction_semantics()
    test_exact_page_state_can_override_a_model_provider_label()
    test_amend_uses_the_target_work_item_workspace_fact()
    test_browser_continuation_is_a_soft_capability_preference()
    print("ok: delegate facts compile into bounded Provider requirements")


if __name__ == "__main__":
    _main()
