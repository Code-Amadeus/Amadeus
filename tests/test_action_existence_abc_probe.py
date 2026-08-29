from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.probes.probe_action_existence_abc import (
    build_messages,
    classify_result,
    conversation_phase,
    natural_long_journey,
    parse_protocol_outcome,
    render_history_reply,
)


def test_journey_is_long_and_puts_known_failure_samples_late() -> None:
    journey = natural_long_journey()
    assert len(journey) >= 50
    by_user = {turn.user: index for index, turn in enumerate(journey, 1)}
    assert by_user["帮我打开维基百科，找到你自己的页面。"] >= 40
    assert by_user["没有使用openclaw找paxos论文"] >= 25
    assert by_user["先做个简单的五子棋吧，能在浏览器里玩的那种。"] >= 50
    assert by_user["先不玩了。顺便新建个一次性草稿，只写一句 attach check done。"] >= 50
    assert sum(turn.expects_delegate is True for turn in journey) >= 15
    assert sum(turn.expects_delegate is False for turn in journey) >= 20


def test_history_is_semantically_paired_but_protocol_specific() -> None:
    journey = natural_long_journey()
    action = journey[4]
    chat = journey[0]
    assert "[DELEGATE" in render_history_reply(action, "optional")
    assert "[CHAT]" not in render_history_reply(chat, "optional")
    assert render_history_reply(chat, "explicit").endswith("[CHAT]")
    assert 'delegate="true"' in render_history_reply(action, "boolean")
    assert render_history_reply(chat, "boolean").endswith('[CONTROL delegate="false"]')

    optional = build_messages(
        system_prompt="base optional",
        protocol="optional",
        journey=journey,
        turn_index=7,
    )
    explicit = build_messages(
        system_prompt="base explicit",
        protocol="explicit",
        journey=journey,
        turn_index=7,
    )
    assert [message["content"].splitlines()[0] for message in optional[1:]] == [
        message["content"].splitlines()[0] for message in explicit[1:]
    ]
    assert len(optional) == len(explicit) == 16


def test_production_boolean_prompt_is_not_layered_over_delegate_vocabulary() -> None:
    from unittest.mock import patch

    import config.settings as settings
    from llm.prompts import get_system_prompt

    with (
        patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", True),
        patch.object(settings, "CONTROL_DECISION_AUTHORITY_ENABLED", True),
        patch.object(settings, "LLM_DELEGATE_TOOL_CALLS", False),
        patch("tts.pipeline.TTS_OUTPUT_LANGUAGE", "英文"),
        patch(
            "llm.prompts.registered_provider_ids",
            return_value=("browser", "codex", "openclaw"),
        ),
    ):
        candidate = get_system_prompt("with_delegate", control_envelope=True)
        baseline = get_system_prompt("with_delegate", control_envelope=False)

    assert candidate.count("[CONTROL OUTCOME CONTRACT]") == 1
    assert "DELEGATE" not in candidate
    assert '[CONTROL delegate="true" provider="codex"' in candidate
    assert '[CONTROL delegate="false"]' in candidate
    assert "[DELEGATE" in baseline
    assert "[CONTROL OUTCOME CONTRACT]" not in baseline


def test_optional_protocol_distinguishes_action_from_implicit_chat() -> None:
    action = parse_protocol_outcome(
        "optional",
        'すぐ確認するわ。\n[DELEGATE provider="browser" intent="execute" task="open"]',
    )
    chat = parse_protocol_outcome("optional", "それは設計の話ね。")
    assert action.predicted_delegate is True and action.protocol_valid
    assert chat.predicted_delegate is False and chat.protocol_valid
    assert action.visible_text == "すぐ確認するわ。"


def test_explicit_protocol_requires_exactly_one_outcome() -> None:
    chat = parse_protocol_outcome("explicit", "そうね。\n[CHAT]")
    missing = parse_protocol_outcome("explicit", "そうね。")
    both = parse_protocol_outcome(
        "explicit",
        '[CHAT][DELEGATE provider="locus" intent="execute" task="x"]',
    )
    assert chat.predicted_delegate is False and chat.protocol_valid
    assert missing.predicted_delegate is None and missing.protocol_missing
    assert both.predicted_delegate is True and not both.protocol_valid


def test_boolean_protocol_owns_presence_and_payload_in_one_envelope() -> None:
    action = parse_protocol_outcome(
        "boolean",
        'やるわ。\n[CONTROL delegate="true" provider="locus" intent="amend" task="edit x"]',
    )
    chat = parse_protocol_outcome("boolean", '話だけね。\n[CONTROL delegate="false"]')
    incomplete = parse_protocol_outcome("boolean", '[CONTROL delegate="true" intent="execute"]')
    assert action.predicted_delegate is True and action.protocol_valid
    assert action.delegate_attrs[0]["provider"] == "locus"
    assert chat.predicted_delegate is False and chat.protocol_valid
    assert incomplete.predicted_delegate is True and not incomplete.protocol_valid


def test_scoring_keeps_ambiguous_real_samples_out_of_error_rates() -> None:
    journey = natural_long_journey()
    ambiguous = next(turn for turn in journey if turn.expects_delegate is None)
    positive = next(turn for turn in journey if turn.expects_delegate is True)
    no_marker = parse_protocol_outcome("explicit", "分かった。")
    assert classify_result(ambiguous, no_marker) == "unscored"
    assert classify_result(positive, no_marker) == "false_negative"


def test_conversation_phases_isolate_protocol_cold_start() -> None:
    assert conversation_phase(1) == "cold_start"
    assert conversation_phase(2) == "cold_start"
    assert conversation_phase(3) == "early"
    assert conversation_phase(18) == "middle"
    assert conversation_phase(35) == "late"


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all action existence A/B/C probe tests passed")


if __name__ == "__main__":
    _main()
