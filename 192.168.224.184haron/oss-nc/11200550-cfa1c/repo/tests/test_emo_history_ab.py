from __future__ import annotations

from tools.eval_emo_history_ab import project_assistant_history, response_metrics


def test_history_projection_changes_only_emo_markup() -> None:
    raw = (
        "そうね、[EMO preset=thinking dur=12s]考えてみるわ。"
        "[EMO preset=normal dur=4s]結論を話す。"
    )

    assert project_assistant_history(raw, "preserve") == raw
    assert project_assistant_history(raw, "strip") == "そうね、考えてみるわ。結論を話す。"
    assert project_assistant_history(raw, "expressive_only") == (
        "そうね、[EMO preset=thinking dur=12s]考えてみるわ。結論を話す。"
    )


def test_response_metrics_separate_neutral_and_expressive_tags() -> None:
    result = response_metrics(
        "そうね、[EMO preset=thinking dur=12s]考えるわ。"
        "[EMO preset=normal dur=4s]結論は後で話す。",
        role_prompt="role",
    )

    assert result["has_emo"] is True
    assert result["valid_emo_count"] == 2
    assert result["neutral_emo_count"] == 1
    assert result["expressive_emo_count"] == 1
    assert result["neutral_only_or_untagged"] is False
