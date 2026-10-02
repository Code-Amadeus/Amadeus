"""The bounded Session window never injects maintenance work into a reply."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def test_prompts_carry_the_recent_window_while_the_record_keeps_every_round() -> None:
    history = ConversationHistory(max_rounds=2)
    for index in range(4):
        history.add_user(f"user {index}")
        history.add_assistant(f"reply {index}")

    assert [message["content"] for message in history.dialog][:2] == ["user 0", "reply 0"]
    assert len(history.dialog) == 8
    sent = history.build_deepseek_messages("system", "latest")[1:-1]
    assert [message["content"] for message in sent] == ["user 2", "reply 2", "user 3", "reply 3"]
    prompt = history.build_gemini_full_prompt("system", "latest")
    assert "user 1" not in prompt and "user 2" in prompt and "reply 3" in prompt


if __name__ == "__main__":
    test_openai_style_history_has_one_authoritative_latest_user_message()
    print("ok: OpenAI-style history adds no in-band maintenance request")
    test_gemini_history_does_not_mix_summary_maintenance_into_the_reply()
    print("ok: Gemini history adds no in-band maintenance request")
    test_prompts_carry_the_recent_window_while_the_record_keeps_every_round()
    print("ok: prompts carry the recent window; the record keeps every round")
