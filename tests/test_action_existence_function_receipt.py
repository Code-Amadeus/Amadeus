from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.probes.probe_action_existence_cascade import (
    FUNCTION_BOOLEAN_NAME,
    FUNCTION_BOOLEAN_TOOL,
    FunctionCall,
    ModelReply,
    append_reply_history,
    expected_for_turn,
    parse_function_boolean_outcome,
    SCENARIOS,
)


def _reply(text: str, *, declared: bool | None) -> ModelReply:
    calls = ()
    if declared is not None:
        calls = (
            FunctionCall(
                call_id="call_test",
                name=FUNCTION_BOOLEAN_NAME,
                arguments=json.dumps({"delegate": declared}),
            ),
        )
    return ModelReply(text, calls, ("tool_calls",), 0.1, 0.2, 0.3, 0.4)


def test_function_schema_is_boolean_only_and_cannot_carry_action_payload() -> None:
    function = FUNCTION_BOOLEAN_TOOL["function"]
    assert function["name"] == FUNCTION_BOOLEAN_NAME
    parameters = function["parameters"]
    assert parameters["required"] == ["delegate"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"delegate"}
    assert parameters["properties"]["delegate"]["type"] == "boolean"


def test_function_true_does_not_execute_without_inline_delegate() -> None:
    outcome = parse_function_boolean_outcome(_reply("今から作るわ。", declared=True))
    assert outcome.declared_delegate is True
    assert outcome.predicted_delegate is False
    assert not outcome.protocol_valid
    assert "disagreed" in " ".join(outcome.protocol_errors)


def test_function_receipt_must_match_the_inline_delegate() -> None:
    action = _reply(
        '作るわ。[DELEGATE provider="codex" intent="execute" task="五子棋を作る"]',
        declared=True,
    )
    valid = parse_function_boolean_outcome(action)
    assert valid.predicted_delegate is True
    assert valid.declared_delegate is True
    assert valid.protocol_valid

    contradiction = parse_function_boolean_outcome(
        _reply(action.text, declared=False)
    )
    assert contradiction.predicted_delegate is True
    assert not contradiction.protocol_valid


def test_function_false_with_no_inline_delegate_is_valid_chat() -> None:
    outcome = parse_function_boolean_outcome(_reply("設計の話だけね。", declared=False))
    assert outcome.predicted_delegate is False
    assert outcome.declared_delegate is False
    assert outcome.protocol_valid


def test_function_history_preserves_content_and_closes_the_tool_call() -> None:
    reply = _reply(
        '作るわ。[DELEGATE provider="codex" intent="execute" task="五子棋を作る"]',
        declared=True,
    )
    messages: list[dict] = []
    append_reply_history(messages, protocol="function_boolean", reply=reply)
    assert messages[0]["role"] == "assistant"
    assert "[DELEGATE" in messages[0]["content"]
    assert messages[0]["tool_calls"][0]["function"]["name"] == FUNCTION_BOOLEAN_NAME
    assert messages[1]["role"] == "tool"
    assert messages[1]["tool_call_id"] == "call_test"
    assert json.loads(messages[1]["content"])["executed"] is False


def test_recovery_scenario_freezes_the_real_missing_action_history() -> None:
    recovery = next(
        scenario for scenario in SCENARIOS
        if scenario.scenario_id == "desktop_gomoku_recovery"
    )
    assert recovery.seed_messages[0][0] == "user"
    assert recovery.seed_messages[1][0] == "assistant"
    assert "[DELEGATE" not in recovery.seed_messages[1][1]
    assert recovery.turns[0].user == "那你去做吧"
    assert expected_for_turn(recovery.turns[0]) is True


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all action-existence function receipt tests passed")


if __name__ == "__main__":
    _main()
