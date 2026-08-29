"""The schema path must be interchangeable with the tag path.

DELEGATE was always a tool call in everything but transport. Carrying it as
one makes provider, its three legal values and the presence of a task into
constraints instead of requests — the class of failure that accounted for the
whole 2026-07-31 investigation. A complete structured operation is also a
valid instruction without duplicated prose. The two paths therefore have to
agree on the action shape, so nothing below record_actions can tell them
apart, and the call has to reach conversation history either way: a turn
recorded without it teaches the model that file work is answered with words
alone.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config.settings as settings
from core.chat_runtime import ChatRuntime, _TurnState
from llm.delegate_tool import (
    DELEGATE_TOOL,
    ToolCallAccumulator,
    as_tag_text,
    delegate_tool_for_registered_providers,
)
from llm.stream_parser import StreamTagParser


def _call(index: int, name: str | None, arguments: str):
    return SimpleNamespace(
        index=index,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_streamed_fragments_reassemble_into_one_action() -> None:
    acc = ToolCallAccumulator()
    acc.feed([_call(0, "delegate", '{"provider": "loc')])
    acc.feed([_call(0, None, 'us", "task": "create theme.txt')])
    acc.feed([_call(0, None, ' and write color=blue"}')])

    actions = acc.actions()
    assert len(actions) == 1
    assert actions[0]["type"] == "DELEGATE"
    assert actions[0]["attrs"]["provider"] == "locus"
    assert actions[0]["attrs"]["task"] == "create theme.txt and write color=blue"


def test_incomplete_or_foreign_calls_are_dropped_not_guessed() -> None:
    for name, arguments in (
        ("delegate", '{"provider": "locus"}'),          # no task
        ("delegate", '{"task": "do something"}'),        # no provider
        ("delegate", '{"provider": "locus", "task"'),    # truncated stream
        ("something_else", '{"provider": "locus", "task": "x"}'),
    ):
        acc = ToolCallAccumulator()
        acc.feed([_call(0, name, arguments)])
        assert acc.actions() == [], (name, arguments)


def test_action_shape_matches_the_tag_parser() -> None:
    """Downstream of record_actions the two transports must be identical."""

    attrs = {"provider": "locus", "task": "edit theme.txt", "workspace_ref": "work_1"}
    acc = ToolCallAccumulator()
    acc.feed([_call(0, "delegate", '{"provider": "locus", "task": "edit theme.txt",'
                                   ' "workspace_ref": "work_1"}')])
    from_tool = acc.actions()[0]

    # The rendered tag must parse back to the same action the tag path yields.
    _clean, parsed = StreamTagParser().process_chunk(as_tag_text(attrs))
    from_tag = [a for a in parsed if a.get("type") == "DELEGATE"][0]

    assert from_tool["type"] == from_tag["type"]
    assert from_tool["attrs"] == from_tag["attrs"]


def test_taskless_focus_and_structured_operation_are_valid_calls() -> None:
    acc = ToolCallAccumulator()
    acc.feed([
        _call(
            0,
            "delegate",
            '{"provider": "locus", "intent": "focus", "project_id": "project_1"}',
        )
    ])
    actions = acc.actions()
    assert len(actions) == 1
    assert actions[0]["attrs"]["intent"] == "focus"
    assert "task" not in actions[0]["attrs"]

    operation = ToolCallAccumulator()
    operation.feed([
        _call(
            0,
            "delegate",
            '{"provider": "browser", "intent": "execute", "action": "open", '
            '"url": "https://example.test/page"}',
        )
    ])
    actions = operation.actions()
    assert len(actions) == 1
    assert actions[0]["attrs"]["action"] == "open"
    assert "task" not in actions[0]["attrs"]


def test_dispatch_records_the_call_in_history_and_marks_the_turn() -> None:
    st = _TurnState(gui_callback=None, turn_id="t1")
    st.question = "请在我的桌面创建 theme.txt"
    st.full_response = "了解、すぐ作るわ。"
    st.history_response = "了解、すぐ作るわ。"
    acc = ToolCallAccumulator()
    acc.feed([_call(0, "delegate",
                    '{"provider": "locus", "task": "create theme.txt"}')])

    runtime = ChatRuntime.__new__(ChatRuntime)
    with patch("core.chat_runtime.record_actions") as record:
        runtime._dispatch_tool_delegates(st, acc)

    assert record.called, "the call must reach the same dispatcher as a tag"
    dispatched = record.call_args.args[0][0]
    assert dispatched["attrs"]["_host_source_user_text"] == st.question
    assert dispatched["attrs"]["_host_turn_id"] == st.turn_id
    assert st.delegate_seen is True, "otherwise the host would try to repair it"
    assert "DELEGATE" not in st.full_response, "the UI stream stays clean"
    assert "[DELEGATE" in st.history_response, (
        "history must show the assistant delegating, not merely promising"
    )
    assert st.history_response.index("了解") < st.history_response.index("[DELEGATE")


def test_transport_is_off_by_default_and_leaves_the_request_untouched() -> None:
    kwargs: dict = {"model": "x"}
    assert ChatRuntime._delegate_tool_accumulator(kwargs) is None
    assert "tools" not in kwargs

    with (
        patch.object(settings, "LLM_DELEGATE_TOOL_CALLS", True),
        patch(
            "llm.prompts.registered_provider_ids",
            return_value=("browser", "codex"),
        ),
    ):
        acc = ChatRuntime._delegate_tool_accumulator(kwargs)
    assert acc is not None
    provider_schema = kwargs["tools"][0]["function"]["parameters"]["properties"]["provider"]
    assert provider_schema["enum"] == ["browser", "codex"]
    assert kwargs["tool_choice"] == "auto"


def test_schema_keeps_the_constraints_prose_could_only_request() -> None:
    params = DELEGATE_TOOL["function"]["parameters"]
    assert params["required"] == ["provider"]
    assert {"required": ["task"]} in params["anyOf"]
    assert any(
        option.get("required") == ["intent", "action"]
        for option in params["anyOf"]
    )
    assert "enum" not in params["properties"]["provider"], (
        "the reusable schema must not carry a second provider registry"
    )
    assert "fallback" not in params["properties"], (
        "retired provider recovery must not be advertised to new calls"
    )
    assert params["properties"]["branch"]["enum"] == ["continue", "new", "close"]

    with patch("llm.prompts.registered_provider_ids", return_value=("future_agent",)):
        live = delegate_tool_for_registered_providers()
    assert live["function"]["parameters"]["properties"]["provider"]["enum"] == [
        "future_agent"
    ]


if __name__ == "__main__":
    test_streamed_fragments_reassemble_into_one_action()
    print("ok: streamed fragments reassemble into one action")
    test_incomplete_or_foreign_calls_are_dropped_not_guessed()
    print("ok: incomplete or foreign calls are dropped, never guessed")
    test_action_shape_matches_the_tag_parser()
    print("ok: the action shape matches the tag parser")
    test_taskless_focus_and_structured_operation_are_valid_calls()
    print("ok: taskless controls and structured operations share the schema transport")
    test_dispatch_records_the_call_in_history_and_marks_the_turn()
    print("ok: dispatch records the call in history and marks the turn")
    test_transport_is_off_by_default_and_leaves_the_request_untouched()
    print("ok: the transport is off by default and leaves the request untouched")
    test_schema_keeps_the_constraints_prose_could_only_request()
    print("ok: the schema keeps the constraints prose could only request")
    print("all delegate tool transport tests passed")
