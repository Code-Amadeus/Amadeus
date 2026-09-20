"""Session history: a bounded prompt window over the complete stored transcript."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import session_manager as sm
from core.session_manager import ConversationHistory


def _large_history() -> ConversationHistory:
    history = ConversationHistory(max_rounds=10, summary_token_threshold=20)
    history.add_user("an old user turn with enough text to cross the threshold")
    history.add_assistant("an old assistant answer with enough text as well")
    return history


def test_openai_style_history_has_one_authoritative_latest_user_message() -> None:
    history = _large_history()
    messages = history.build_deepseek_messages("system", "answer this latest question")
    assert messages[-1] == {"role": "user", "content": "answer this latest question"}
    assert sum(message["role"] == "user" for message in messages) == 2
    assert all("SUMMARY" not in message["content"] for message in messages)


def test_gemini_history_does_not_mix_summary_maintenance_into_the_reply() -> None:
    prompt = _large_history().build_gemini_full_prompt(
        "system",
        "answer this latest question",
    )
    assert prompt.endswith("質問:answer this latest question")
    assert "SUMMARY" not in prompt
    assert "要約してください" not in prompt


def test_long_session_keeps_full_dialog_but_prompts_only_the_window() -> None:
    history = ConversationHistory(max_rounds=2)
    for index in range(6):
        history.add_user(f"user {index}")
        history.add_assistant(f"assistant {index}")

    assert [message["content"] for message in history.dialog] == [
        expected
        for index in range(6)
        for expected in (f"user {index}", f"assistant {index}")
    ]

    messages = history.build_deepseek_messages("system", "latest question")
    assert [message["content"] for message in messages if message["role"] != "system"] == [
        "user 4",
        "assistant 4",
        "user 5",
        "assistant 5",
        "latest question",
    ]
    prompt = history.build_gemini_full_prompt("system", "latest question")
    assert "user 0" not in prompt
    assert "assistant 5" in prompt


def test_saved_session_keeps_turns_older_than_the_prompt_window(tmp_path) -> None:
    old_dir = sm._SESSION_DIR
    old_session_id = sm._CURRENT_SESSION_ID
    old_dialog = list(sm.conversation_history.dialog)
    old_summary = sm.conversation_history.last_summary
    old_rounds = sm.conversation_history.max_rounds
    try:
        sm._SESSION_DIR = str(tmp_path)
        sm._CURRENT_SESSION_ID = "session-retention"
        sm.conversation_history.reset()
        sm.conversation_history.max_rounds = 1
        sm.conversation_history.add_user("first question")
        sm.conversation_history.add_assistant("first answer")
        sm.conversation_history.add_user("second question")
        sm.conversation_history.add_assistant("second answer")
        assert sm.save_session("session-retention", enable_conversation=True) is True

        payload = json.loads(
            (tmp_path / "session-retention.json").read_text(encoding="utf-8")
        )
        assert [message["content"] for message in payload["dialog"]] == [
            "first question",
            "first answer",
            "second question",
            "second answer",
        ]

        sm.conversation_history.reset()
        loaded, enabled = sm.load_session("session-retention")
        assert loaded is True and enabled is True
        assert [message["content"] for message in sm.conversation_history.dialog] == [
            "first question",
            "first answer",
            "second question",
            "second answer",
        ]

        # The reloaded transcript stays complete while the model still sees one
        # bounded round, so switching surfaces cannot drop accepted history.
        messages = sm.conversation_history.build_deepseek_messages("system", "latest")
        assert [message["content"] for message in messages if message["role"] != "system"] == [
            "second question",
            "second answer",
            "latest",
        ]
    finally:
        sm._SESSION_DIR = old_dir
        sm._CURRENT_SESSION_ID = old_session_id
        sm.conversation_history.reset()
        sm.conversation_history.dialog[:] = old_dialog
        sm.conversation_history.last_summary = old_summary
        sm.conversation_history.max_rounds = old_rounds


if __name__ == "__main__":
    test_openai_style_history_has_one_authoritative_latest_user_message()
    print("ok: OpenAI-style history adds no in-band maintenance request")
    test_gemini_history_does_not_mix_summary_maintenance_into_the_reply()
    print("ok: Gemini history adds no in-band maintenance request")
