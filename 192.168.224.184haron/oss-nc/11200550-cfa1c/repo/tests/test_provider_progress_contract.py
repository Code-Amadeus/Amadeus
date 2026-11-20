from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_progress import (
    PROVIDER_PROGRESS_CONTRACT,
    progress_payload,
    split_progress_milestones,
    split_progress_stream,
    with_progress_contract,
    is_progress_only_workspace_completion,
)
from agent_host.provider_types import ProviderActivityEvidence
from server.work_semantic_progress import semantic_progress_fact


def test_contract_is_sparse_and_does_not_add_task_intents() -> None:
    prompt = with_progress_contract("Implement the counter.")
    assert prompt.startswith("Implement the counter.")
    assert "[PROGRESS:DESIGN]" in prompt
    assert "[PROGRESS:DIAGNOSTIC]" in prompt
    assert "[PROGRESS:CAPABILITY]" in prompt
    assert "[PROGRESS:VALIDATION]" in prompt
    assert "intent=" not in PROVIDER_PROGRESS_CONTRACT.lower()
    assert "one concise, user-facing sentence" in PROVIDER_PROGRESS_CONTRACT
    assert "before lengthy inspection or implementation" in PROVIDER_PROGRESS_CONTRACT
    assert "current/future wording" in PROVIDER_PROGRESS_CONTRACT
    assert "must not imply that work already succeeded" in PROVIDER_PROGRESS_CONTRACT
    assert "what now needs to be verified" in " ".join(
        PROVIDER_PROGRESS_CONTRACT.split()
    )
    assert "Do not start the next phase" in PROVIDER_PROGRESS_CONTRACT
    assert "first visible provider output must be DESIGN" in PROVIDER_PROGRESS_CONTRACT


def test_reporting_language_uses_the_slice_presentation_locale() -> None:
    chinese = with_progress_contract("Implement the counter.", presentation_locale="zh-CN")
    japanese = with_progress_contract("Implement the counter.", presentation_locale="ja-JP")
    english = with_progress_contract("Implement the counter.", presentation_locale=None)

    assert "final user-facing result summary in Simplified Chinese" in chinese
    assert "final user-facing result summary in Japanese" in japanese
    assert "final user-facing result summary in English" in english
    assert "paths, URLs, quoted source text, and artifact contents verbatim" in chinese


def test_four_milestones_are_stripped_and_normalised() -> None:
    text, milestones = split_progress_milestones(
        "ordinary text\n"
        "[PROGRESS:DESIGN] Use turn ownership because simultaneous input can race.\n"
        "[PROGRESS:DIAGNOSTIC] The browser import failed; use the bundled runtime entry.\n"
        "[progress:capability] Two players can now alternate turns.\n"
        "[PROGRESS:VALIDATION] Ran 12 tests: 12 passed; no regression found.\n"
        "final prose\n"
    )
    assert text == "ordinary text\nfinal prose\n"
    assert [item["milestone"] for item in milestones] == [
        "design",
        "diagnostic",
        "capability",
        "validation",
    ]
    assert all(item["verified"] is False for item in milestones)
    assert all(item["status"] == "reported" for item in milestones)


def test_legacy_or_future_markers_do_not_become_semantic_facts() -> None:
    raw = "[PROGRESS] vague update\n[PROGRESS:PLAN] future work\n"
    text, milestones = split_progress_milestones(raw)
    assert text == raw
    assert milestones == []

    text, milestones = split_progress_milestones(
        "[PROGRESS:DESIGN] Keep explicit ownership. "
        "[PROGRESS:CAPABILITY] Two players can alternate. "
        "[PROGRESS:VALIDATION] 12 tests passed.\n"
    )
    assert text == ""
    assert [item["milestone"] for item in milestones] == [
        "design",
        "capability",
        "validation",
    ]


def test_inline_milestone_keeps_visible_prefix_and_never_leaks_the_marker() -> None:
    text, milestones = split_progress_milestones(
        "I checked RFC 2606. [PROGRESS:VALIDATION] The primary sources agree.\n"
        "Final report.\n"
    )
    assert text == "I checked RFC 2606. \nFinal report.\n"
    assert [item["milestone"] for item in milestones] == ["validation"]
    assert milestones[0]["summary"] == "The primary sources agree."

    visible, milestones, pending = split_progress_stream(
        "",
        "I checked RFC 2606. [PROG",
    )
    assert visible == ""
    assert milestones == []
    assert pending.endswith("[PROG")
    visible, milestones, pending = split_progress_stream(
        pending,
        "RESS:VALIDATION] The primary sources agree.\nFinal report.\n",
    )
    assert visible == "I checked RFC 2606. \nFinal report.\n"
    assert [item["milestone"] for item in milestones] == ["validation"]
    assert pending == ""


def test_host_and_provider_evidence_keep_distinct_strength() -> None:
    provider = semantic_progress_fact(
        "semantic.progress",
        progress_payload(
            "validation",
            "The provider says the tests passed.",
            source="test_provider",
            explicit=True,
            verified=False,
        ),
    )
    host = semantic_progress_fact(
        "tool.result",
        {"tool": "command_execution", "command": "python -m unittest", "ok": True},
    )
    assert provider is not None and provider.milestone == "validation"
    assert provider.verified is False
    assert provider.evidence == "reported"
    assert host is not None and host.milestone == "validation"
    assert host.verified is True
    assert host.evidence == "observed"


def test_stream_filter_holds_only_a_possible_marker_tail() -> None:
    visible, milestones, pending = split_progress_stream("", "Before.\n[PROG")
    assert visible == "Before.\n"
    assert milestones == []
    assert pending == "[PROG"

    visible, milestones, pending = split_progress_stream(
        pending,
        "RESS:CAPABILITY] Two players can now alternate.\nAfter.\n[PROGRESS:VAL",
    )
    assert visible == "After.\n"
    assert [item["milestone"] for item in milestones] == ["capability"]
    assert pending == "[PROGRESS:VAL"

    visible, milestones, pending = split_progress_stream(
        pending,
        "IDATION] 12 tests passed.",
        final=True,
    )
    assert visible == ""
    assert [item["milestone"] for item in milestones] == ["validation"]
    assert pending == ""


def test_progress_only_completion_requires_typed_zero_execution_write_evidence() -> None:
    evidence = ProviderActivityEvidence(
        terminal_observed=True,
        progress_milestones=1,
        execution_items=0,
    )
    assert is_progress_only_workspace_completion(
        status="done",
        result_text="",
        task_kind="workspace_mutation",
        workspace_access="write",
        activity_evidence=evidence,
    )
    assert not is_progress_only_workspace_completion(
        status="done",
        result_text="",
        task_kind="workspace_read",
        workspace_access="read",
        activity_evidence=evidence,
    )
    assert not is_progress_only_workspace_completion(
        status="done",
        result_text="",
        task_kind="workspace_mutation",
        workspace_access="write",
        activity_evidence=ProviderActivityEvidence(
            terminal_observed=True,
            progress_milestones=1,
            execution_items=1,
        ),
    )
    assert not is_progress_only_workspace_completion(
        status="done",
        result_text="",
        task_kind="workspace_mutation",
        workspace_access="write",
        activity_evidence=None,
    )


if __name__ == "__main__":
    test_contract_is_sparse_and_does_not_add_task_intents()
    test_four_milestones_are_stripped_and_normalised()
    test_legacy_or_future_markers_do_not_become_semantic_facts()
    test_host_and_provider_evidence_keep_distinct_strength()
    test_stream_filter_holds_only_a_possible_marker_tail()
    print("ok: provider progress uses sparse, evidence-aware milestones")
