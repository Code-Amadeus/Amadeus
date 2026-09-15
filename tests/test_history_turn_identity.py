"""Conversation-history turn identity and legacy JSON compatibility tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import session_manager as sm
from core.session_manager import ConversationHistory


def test_assistant_entry_carries_turn_id():
    history = ConversationHistory()
    history.add_assistant("reply", turn_id="turn_normal")

    assert history.dialog == [
        {"role": "assistant", "content": "reply", "turn_id": "turn_normal"}
    ]


def test_interruption_targets_requested_turn_not_latest_assistant():
    history = ConversationHistory()
    history.add_assistant("target reply", turn_id="turn_target")
    history.add_assistant("later reply", turn_id="turn_later")

    assert history.mark_last_assistant_interrupted(
        "heard target",
        turn_id="turn_target",
    )
    assert history.dialog[0]["content"] == "heard target [interrupted by user]"
    assert history.dialog[1]["content"] == "later reply"


def test_missing_or_unknown_turn_id_falls_back_to_latest_assistant():
    history = ConversationHistory()
    history.add_assistant("first", turn_id="turn_first")
    history.add_assistant("latest")

    assert history.mark_last_assistant_interrupted("legacy heard")
    assert history.dialog[-1]["content"] == "legacy heard [interrupted by user]"

    history.add_assistant("new latest")
    assert history.mark_last_assistant_interrupted(
        "unknown heard",
        turn_id="turn_missing",
    )
    assert history.dialog[-1]["content"] == "unknown heard [interrupted by user]"


def test_interruption_preserves_an_already_committed_delegate_fact():
    history = ConversationHistory()
    control = (
        '[DELEGATE provider="codex" intent="execute" subject="project" '
        'target="desktop" task="Create gomoku.html"]'
    )
    history.add_assistant(f"I will start it.\n\n{control}", turn_id="turn_work")

    assert history.mark_last_assistant_interrupted(
        "I will start it.",
        turn_id="turn_work",
    )
    assert history.dialog[-1]["content"] == (
        "I will start it. [interrupted by user]\n\n" + control
    )

    # A repeated annotation remains idempotent and never duplicates control.
    assert history.mark_last_assistant_interrupted(
        "I will start it.",
        turn_id="turn_work",
    ) is False
    assert history.dialog[-1]["content"].count("[DELEGATE") == 1


def _assert_interrupted_records(controls):
    history = ConversationHistory()
    history.add_assistant("Before." + "Between.".join(controls), turn_id="compound")
    history.add_assistant("A later unrelated reply.", turn_id="later")

    assert history.mark_last_assistant_interrupted("Heard.", turn_id="compound")
    expected = "Heard. [interrupted by user]\n\n" + "".join(controls)
    assert history.dialog[0]["content"] == expected
    assert history.dialog[1]["content"] == "A later unrelated reply."
    # Keep multiplicity, even identical records: rewriting audio history is
    # neither semantic deduplication nor another execution/acceptance pass.
    assert not history.mark_last_assistant_interrupted("Heard.", turn_id="compound")
    assert history.dialog[0]["content"] == expected


def test_interruption_preserves_compound_delegate_records():
    _assert_interrupted_records((
        '[DELEGATE provider="codex" intent="amend" task="Change Game"]',
        '[DELEGATE provider="codex" intent="amend" task="Change Page"]',
    ))


def test_interruption_preserves_compound_control_envelopes():
    _assert_interrupted_records((
        '[CONTROL delegate="true" provider="codex" intent="amend" task="Change Game"]',
        '[CONTROL delegate="true" provider="codex" intent="amend" task="Change Page"]',
    ))


def test_interruption_preserves_work_and_app_records():
    _assert_interrupted_records((
        '[DELEGATE provider="codex" intent="amend" task="Change Game"]',
        '[AUIP action="launch"]',
    ))


def test_interruption_preserves_no_work_and_app_leave_records():
    _assert_interrupted_records(('[CONTROL delegate="false"]', '[AUIP action="leave"]'))


def test_interruption_does_not_deduplicate_identical_recorded_controls():
    _assert_interrupted_records(('[DELEGATE provider="codex" intent="report" subject="project"]',) * 2)


def test_interruption_keeps_stored_control_spelling_and_noop_without_inventing_work():
    history = ConversationHistory()
    control = '[control delegate="false"]'
    history.add_assistant("No work." + control + '[EMO preset="smile"]', turn_id="noop")
    assert history.mark_last_assistant_interrupted("No", turn_id="noop")
    assert history.dialog[0]["content"] == "No [interrupted by user]\n\n" + control
    assert "DELEGATE" not in history.dialog[0]["content"]


def test_interruption_never_completes_a_truncated_control_record():
    history = ConversationHistory()
    complete = '[DELEGATE provider="codex" intent="amend" task="Change Game"]'
    history.add_assistant(complete + '[DELEGATE provider="codex" task="unfinished', turn_id="partial")
    assert history.mark_last_assistant_interrupted("", turn_id="partial")
    assert history.dialog[0]["content"] == "[interrupted by user]\n\n" + complete


def test_history_reader_still_excludes_hidden_thought_and_presentation_tags():
    history = ConversationHistory()
    control = '[AUIP action="leave"]'
    history.add_assistant(
        '<think>[DELEGATE provider="codex" task="not a recorded call"]</think>'
        '[EMO preset="smile"]Visible.' + control,
        turn_id="visible",
    )
    assert history.mark_last_assistant_interrupted("Visible.", turn_id="visible")
    assert history.dialog[0]["content"] == "Visible. [interrupted by user]\n\n" + control


def test_legacy_session_json_loads_and_can_be_annotated():
    old_session_dir = sm._SESSION_DIR
    old_dialog = sm.conversation_history.dialog
    old_session_id = sm._CURRENT_SESSION_ID
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            sm._SESSION_DIR = temp_dir
            legacy_payload = {
                "session_id": "legacy",
                "dialog": [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "legacy reply"},
                ],
                "enable_conversation": True,
            }
            Path(temp_dir, "legacy.json").write_text(
                json.dumps(legacy_payload, ensure_ascii=False),
                encoding="utf-8",
            )

            loaded, enabled = sm.load_session("legacy")

            assert loaded is True
            assert enabled is True
            assert sm.conversation_history.dialog[-1].get("turn_id") is None
            assert sm.conversation_history.mark_last_assistant_interrupted(
                "legacy heard",
                turn_id="unavailable_turn",
            )
            assert (
                sm.conversation_history.dialog[-1]["content"]
                == "legacy heard [interrupted by user]"
            )
    finally:
        sm._SESSION_DIR = old_session_dir
        sm.conversation_history.dialog = old_dialog
        sm._CURRENT_SESSION_ID = old_session_id


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all history turn identity tests passed")


if __name__ == "__main__":
    _main()
