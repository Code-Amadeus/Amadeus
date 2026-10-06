from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.chat_history_projection import (
    project_completed_turn,
    project_inline_role_history,
    stamp_branch_entries,
)
from core.chat_stream_consumption import consume_role_stream_text
from llm.stream_parser import StreamTagParser, presentation_parts


def test_presentation_parser_keeps_expression_order_and_discards_controls() -> None:
    parser = StreamTagParser(control_envelope_enabled=True, stop_after_control=False)
    cleaned, parts = presentation_parts(parser,
        '[EMO shy]前[AUIP action="launch"]中'
        '[DELEGATE provider="codex" intent="execute" task="synthetic"]'
        '[CONTROL delegate="false"][PARAM id="ParamAngleX" value="1"]後')
    assert cleaned == "前中後"
    assert [kind for kind, _ in parts] == ['action', 'text', 'text', 'action', 'text']
    assert [value['type'] for kind, value in parts if kind == 'action'] == ['EMO', 'PARAM']
    assert project_inline_role_history(parts, policy='preserve') == '[EMO shy]前中後'


def test_live_stream_keeps_its_single_control_gate_when_history_can_read_many() -> None:
    first = '[DELEGATE provider="codex" intent="amend" task="Change Game"]'
    second = '[DELEGATE provider="codex" intent="amend" task="Change Page"]'
    parser = StreamTagParser()
    cleaned, actions = parser.process_chunk("Before." + first + "After." + second)
    assert cleaned == "Before."
    assert [action["raw"] for action in actions] == [first]
    assert parser.process_chunk(second) == ("", [])
    parser.reset()
    assert [action["raw"] for action in parser.process_chunk(first + second)[1]] == [first]


def test_inline_parser_preserves_exact_text_action_order_for_history() -> None:
    parser = StreamTagParser()
    _, first = presentation_parts(parser, "前[EMO preset=thinking")
    _, second = presentation_parts(
        parser,
        " dur=12s]中[EMO preset=normal dur=4s]後",
    )

    assert project_inline_role_history(first, policy="preserve") == "前"
    assert project_inline_role_history(second, policy="preserve") == (
        "[EMO preset=thinking dur=12s]中[EMO preset=normal dur=4s]後"
    )
    assert project_inline_role_history(
        second,
        policy="expressive_only",
    ) == "[EMO preset=thinking dur=12s]中後"
    assert project_inline_role_history(second, policy="strip") == "中後"


def test_stream_parser_hides_multilingual_delegate_payload_from_visible_text() -> None:
    parser = StreamTagParser()
    chunks = (
        "私についてのページね。",
        '[DELEGATE provider="codex" intent="execute" task="你能做一个关于',
        '你自己的网页吗？"]',
    )
    visible: list[str] = []
    actions: list[dict] = []

    for chunk in chunks:
        clean, found = parser.process_chunk(chunk)
        visible.append(clean)
        actions.extend(found)

    assert "".join(visible) == "私についてのページね。"
    assert len(actions) == 1
    assert actions[0]["type"] == "DELEGATE"
    assert actions[0]["attrs"]["task"] == "你能做一个关于你自己的网页吗？"


def test_history_projection_keeps_session_and_turn_guards_in_one_owner() -> None:
    async def run() -> None:
        history = SimpleNamespace(add_user=Mock(), add_assistant=Mock())
        with (
            patch(
                "core.chat_history_projection.get_current_session_id",
                return_value="session_a",
            ),
            patch("core.chat_history_projection.conversation_history", history),
            patch(
                "core.chat_history_projection.turn_allows_history",
                new=AsyncMock(return_value=True),
            ),
            patch("core.chat_history_projection.stamp_branch_entries") as stamp,
        ):
            projected = await project_completed_turn(
                session_id="session_a",
                question="continue",
                history_response="reply[DELEGATE ...]",
                visible_response="reply",
                turn_id="turn_a",
                interaction_branch_id="branch-a",
            )

        assert projected is True
        history.add_user.assert_called_once_with("continue")
        history.add_assistant.assert_called_once_with(
            "reply[DELEGATE ...]",
            turn_id="turn_a",
        )
        stamp.assert_called_once_with("branch-a", 2)

    asyncio.run(run())


def test_history_projection_rejects_a_late_turn_from_an_old_session() -> None:
    async def run() -> None:
        history = SimpleNamespace(add_user=Mock(), add_assistant=Mock())
        with (
            patch(
                "core.chat_history_projection.get_current_session_id",
                return_value="session_new",
            ),
            patch("core.chat_history_projection.conversation_history", history),
        ):
            projected = await project_completed_turn(
                session_id="session_old",
                question="late",
                history_response="late reply",
                visible_response="late reply",
                turn_id="turn_old",
                interaction_branch_id="",
            )

        assert projected is False
        assert not history.add_user.called
        assert not history.add_assistant.called

    asyncio.run(run())


def test_history_projection_rechecks_session_after_pending_gate_wait() -> None:
    async def run() -> None:
        history = SimpleNamespace(add_user=Mock(), add_assistant=Mock())
        with (
            patch(
                "core.chat_history_projection.get_current_session_id",
                side_effect=["session_a", "session_b"],
            ),
            patch("core.chat_history_projection.conversation_history", history),
            patch(
                "core.chat_history_projection.turn_allows_history",
                new=AsyncMock(return_value=True),
            ),
        ):
            projected = await project_completed_turn(
                session_id="session_a",
                question="late after gate",
                history_response="must stay out of session b",
                visible_response="must stay out of session b",
                turn_id="turn_gate_switch",
            )

        assert projected is False
        history.add_user.assert_not_called()
        history.add_assistant.assert_not_called()

    asyncio.run(run())


def test_history_stamp_never_rebinds_a_stale_turn_to_replacement_branch() -> None:
    history = SimpleNamespace(
        dialog=[
            {"role": "user", "content": "continue old branch"},
            {"role": "assistant", "content": "acknowledged"},
        ]
    )
    replacement = SimpleNamespace(branch_id="branch-b")
    coordinator = SimpleNamespace(
        active_branch_for_session=Mock(return_value=replacement)
    )
    with (
        patch(
            "server.interaction_branch.get_interaction_branch_coordinator",
            return_value=coordinator,
        ),
        patch(
            "core.chat_history_projection.get_current_session_id",
            return_value="session-a",
        ),
        patch("core.chat_history_projection.conversation_history", history),
    ):
        stamp_branch_entries("branch-a", 2)

    assert all("branch_id" not in entry for entry in history.dialog)


def test_stream_consumer_preserves_parse_projection_dispatch_order() -> None:
    async def run() -> None:
        events: list[tuple[str, str]] = []
        state = SimpleNamespace(
            full_response="before ",
            gui_callback=lambda text: events.append(("gui", text)),
        )

        def parse(raw: str) -> str:
            events.append(("parse", raw))
            return "clean"

        async def dispatch(text: str) -> None:
            events.append(("dispatch", text))

        accepted = await consume_role_stream_text(
            state,
            "raw[CONTROL]",
            parse_control=parse,
            dispatch_text=dispatch,
        )

        assert accepted == "clean"
        assert state.full_response == "before clean"
        assert events == [
            ("parse", "raw[CONTROL]"),
            ("gui", "before clean"),
            ("dispatch", "clean"),
        ]

    asyncio.run(run())


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"ok: {test.__name__}")
    print("all chat protocol role tests passed")
