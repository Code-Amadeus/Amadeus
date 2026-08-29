"""Observer role-word cleanup must not mutate technical literals."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.work_observer_llm import (
    OBSERVER_SYSTEM_PROMPT,
    _compact_note,
    _normalize_decision,
    _sanitize_role_line,
    should_use_observer_llm,
)


def test_standalone_internal_role_terms_are_still_sanitized() -> None:
    cleaned = _sanitize_role_line("The provider result is ready for the user.")

    assert "provider" not in cleaned.casefold()
    assert "user" not in cleaned.casefold()


def test_role_terms_inside_identifiers_are_opaque() -> None:
    line = (
        "Verified cross-provider-ok in provider_result.json and "
        "https://example.test/provider/report."
    )

    cleaned = _sanitize_role_line(line)

    assert "cross-provider-ok" in cleaned
    assert "provider_result.json" in cleaned
    assert "https://example.test/provider/report" in cleaned


def test_explicit_status_query_is_a_required_narrator_reply() -> None:
    note = {
        "phase": "Checkpoint",
        "metadata": {
            "status_query": True,
            "status_facts": {
                "execution": "running",
                "fact_kind": "diagnostic",
            },
        },
    }

    compact = _compact_note(note)
    decision = _normalize_decision(
        {
            "action": "subtitle",
            "display_text": "原因已经定位，验证还在继续。",
            "append_to_main_chat": False,
            "speak": False,
        },
        note,
        "simplified_chinese",
    )

    assert compact["progress_context"]["status_query"] is True
    assert compact["progress_context"]["status_facts"]["execution"] == "running"
    assert decision["action"] == "speak"
    assert decision["speak"] is True
    assert decision["append_to_main_chat"] is True
    assert "Translate or naturally paraphrase Provider prose" in OBSERVER_SYSTEM_PROMPT


def test_merged_semantic_keypoints_reach_the_role_narrator() -> None:
    note = {
        "phase": "Work",
        "importance": "normal",
        "summary": "Implement the board, then verify both launch modes.",
        "metadata": {
            "narration_keypoints": [
                "directional_progress",
                "semantic_progress",
            ],
            "narration_merged_count": 2,
        },
    }

    compact = _compact_note(note)

    assert should_use_observer_llm(note) is True
    assert compact["progress_context"]["narration_keypoints"] == [
        "directional_progress",
        "semantic_progress",
    ]
    assert compact["progress_context"]["narration_merged_count"] == 2


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all work observer role sanitization tests passed")


if __name__ == "__main__":
    _main()
