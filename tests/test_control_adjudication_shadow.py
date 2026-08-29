"""Deterministic invariants for the product-inert control shadow helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.probes.control_adjudication_shadow import (
    filter_known_fact_controls,
    merge_proposal_controls,
    normalize_control_actions,
    project_control_actions,
)


def test_split_focus_set_folds_into_the_adjacent_operation() -> None:
    assert normalize_control_actions(
        [
            {"provider": "workspace", "intent": "focus", "project_id": "p1"},
            {"provider": "workspace", "intent": "execute", "task": "edit README"},
        ]
    ) == [
        {
            "provider": "workspace",
            "intent": "execute",
            "project_id": "p1",
            "focus": "set",
            "task": "edit README",
        }
    ]


def test_split_focus_clear_folds_without_rewriting_payload() -> None:
    assert normalize_control_actions(
        [
            {"provider": "workspace", "intent": "focus", "task": ""},
            {
                "provider": "workspace",
                "intent": "execute",
                "focus": "clear",
                "task": "create note.txt",
                "query": "payload stays byte-for-byte",
            },
        ]
    ) == [
        {
            "provider": "workspace",
            "intent": "execute",
            "focus": "clear",
            "task": "create note.txt",
            "query": "payload stays byte-for-byte",
        }
    ]


def test_non_equivalent_actions_are_never_folded() -> None:
    cases = [
        [
            {"provider": "workspace", "intent": "focus", "project_id": "p1"},
            {"provider": "browser", "intent": "execute", "action": "open"},
        ],
        [
            {"provider": "workspace", "intent": "focus", "project_id": "p1"},
            {"provider": "workspace", "intent": "execute", "project_id": "p2"},
        ],
        [
            {"provider": "workspace", "intent": "focus", "project_id": "p1"},
            {"provider": "workspace", "intent": "execute", "focus": "clear"},
        ],
        [
            {
                "provider": "workspace",
                "intent": "focus",
                "project_id": "p1",
                "task": "this is not a pure modifier",
            },
            {"provider": "workspace", "intent": "execute", "task": "other"},
        ],
    ]
    for actions in cases:
        assert normalize_control_actions(actions) == actions


def test_control_prompt_reuses_contract_without_role_instructions() -> None:
    import config.settings as settings
    from llm.prompts import get_delegate_control_prompt

    with (
        patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
        patch.object(settings, "DELEGATE_AMEND_INTENT", True),
        patch.object(settings, "DELEGATE_RETRACT_INTENT", True),
    ):
        prompt = get_delegate_control_prompt()
    assert "Delegate control decision" in prompt
    assert 'intent="execute"' in prompt
    assert 'intent="report"' in prompt
    assert 'intent="amend"' in prompt
    assert 'intent="retract"' in prompt
    assert "Provider routing" in prompt
    assert "pure Project context switch" in prompt
    assert "Kurisu" not in prompt
    assert "emotion" not in prompt.lower()
    assert "TTS" not in prompt


def test_control_projection_never_creates_a_second_payload_source() -> None:
    assert project_control_actions(
        [
            {
                "provider": "browser",
                "intent": "execute",
                "branch": "continue",
                "action": "search",
                "task": "search the page",
                "url": "https://example.test",
                "query": "Amadeus",
                "text": "visible link",
            }
        ]
    ) == [
        {
            "provider": "browser",
            "intent": "execute",
            "branch": "continue",
            "action": "search",
        }
    ]


def test_host_fact_filter_rejects_only_unregistered_identities() -> None:
    actions, rejected = filter_known_fact_controls(
        [
            {"provider": "locus", "intent": "focus", "project_id": "known"},
            {"provider": "locus", "intent": "focus", "project_id": "invented"},
            {"provider": "invented", "intent": "execute"},
            {"provider": "browser", "intent": "execute", "branch": "new"},
        ],
        provider_ids={"locus", "browser"},
        project_ids={"known"},
    )
    assert actions == [
        {"provider": "locus", "intent": "focus", "project_id": "known"},
        {"provider": "browser", "intent": "execute", "branch": "new"},
    ]
    assert rejected == [
        "action 2: project_id is not registered",
        "action 3: provider is not registered",
    ]


def test_proposal_gate_corrects_controls_without_rewriting_payload() -> None:
    merged, notes = merge_proposal_controls(
        [
            {
                "provider": "locus",
                "intent": "amend",
                "task": "edit README exactly as requested",
                "query": "role-owned payload",
            }
        ],
        [
            {
                "provider": "locus",
                "intent": "execute",
                "project_id": "p1",
            }
        ],
    )
    assert notes == []
    assert merged == [
        {
            "provider": "locus",
            "intent": "execute",
            "project_id": "p1",
            "task": "edit README exactly as requested",
            "query": "role-owned payload",
        }
    ]


def test_proposal_gate_never_synthesizes_or_mis_pairs_actions() -> None:
    merged, notes = merge_proposal_controls(
        [],
        [{"provider": "locus", "intent": "execute"}],
    )
    assert merged == []
    assert notes == ["canonical action ignored because no role proposal exists"]

    merged, notes = merge_proposal_controls(
        [{"provider": "locus", "intent": "focus", "task": "must be discarded"}],
        [{"provider": "locus", "intent": "focus", "project_id": "p1"}],
    )
    assert merged == [{"provider": "locus", "intent": "focus", "project_id": "p1"}]
    assert notes == []

    merged, notes = merge_proposal_controls(
        [{"provider": "locus", "intent": "execute", "task": "first"}],
        [
            {"provider": "locus", "intent": "execute"},
            {"provider": "browser", "intent": "execute"},
        ],
    )
    assert merged == [
        {"provider": "locus", "intent": "execute", "task": "first"}
    ]
    assert notes == ["action count mismatch: proposals=1 controls=2"]


if __name__ == "__main__":
    test_split_focus_set_folds_into_the_adjacent_operation()
    test_split_focus_clear_folds_without_rewriting_payload()
    test_non_equivalent_actions_are_never_folded()
    test_control_prompt_reuses_contract_without_role_instructions()
    test_control_projection_never_creates_a_second_payload_source()
    test_host_fact_filter_rejects_only_unregistered_identities()
    test_proposal_gate_corrects_controls_without_rewriting_payload()
    test_proposal_gate_never_synthesizes_or_mis_pairs_actions()
    print("all control adjudication shadow tests passed")
