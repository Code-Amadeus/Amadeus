from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.chat_control_envelope import parse_inline_control_chunk
from core.chat_history_projection import (
    project_completed_turn,
    project_inline_role_history,
)
from core.chat_stream_consumption import consume_role_stream_text, iter_sync_stream
from llm.stream_parser import StreamTagParser


def test_inline_control_parser_returns_protocol_facts_without_dispatch() -> None:
    parser = StreamTagParser(control_envelope_enabled=True)
    parsed = parse_inline_control_chunk(
        parser,
        '了解。[CONTROL delegate="true" provider="locus" '
        'intent="execute" task="create x"]',
    )

    assert parsed.cleaned_text == "了解。"
    assert parsed.control_seen is True
    assert parsed.control_valid is True
    assert parsed.explicit_no_control is False
    assert len(parsed.delegate_actions) == 1
    assert parsed.delegate_actions[0]["attrs"]["provider"] == "locus"


def test_inline_no_control_is_history_evidence_not_an_action() -> None:
    parser = StreamTagParser(control_envelope_enabled=True)
    parsed = parse_inline_control_chunk(
        parser,
        'そうね。[CONTROL delegate="false"]',
    )

    assert parsed.cleaned_text == "そうね。"
    assert parsed.delegate_actions == ()
    assert parsed.explicit_no_control is True
    assert parsed.history_control_text == '[CONTROL delegate="false"]'


def test_inline_parser_preserves_exact_text_action_order_for_history() -> None:
    parser = StreamTagParser()
    first = parse_inline_control_chunk(parser, "前[EMO preset=thinking")
    second = parse_inline_control_chunk(
        parser,
        " dur=12s]中[EMO preset=normal dur=4s]後",
    )

    assert project_inline_role_history(first.ordered_parts, policy="preserve") == "前"
    assert project_inline_role_history(second.ordered_parts, policy="preserve") == (
        "[EMO preset=thinking dur=12s]中[EMO preset=normal dur=4s]後"
    )
    assert project_inline_role_history(
        second.ordered_parts,
        policy="expressive_only",
    ) == "[EMO preset=thinking dur=12s]中後"
    assert project_inline_role_history(second.ordered_parts, policy="strip") == "中後"


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
            patch("core.chat_history_projection.stamp_active_branch_entries") as stamp,
        ):
            projected = await project_completed_turn(
                session_id="session_a",
                question="continue",
                history_response="reply[DELEGATE ...]",
                visible_response="reply",
                turn_id="turn_a",
                branch_continue_seen=True,
            )

        assert projected is True
        history.add_user.assert_called_once_with("continue")
        history.add_assistant.assert_called_once_with(
            "reply[DELEGATE ...]",
            turn_id="turn_a",
        )
        stamp.assert_called_once_with(2)

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
                branch_continue_seen=False,
            )

        assert projected is False
        assert not history.add_user.called
        assert not history.add_assistant.called

    asyncio.run(run())


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


def test_sync_sdk_stream_is_consumed_off_the_event_loop() -> None:
    async def run() -> None:
        observed = [item async for item in iter_sync_stream(iter((1, 2, 3)))]
        assert observed == [1, 2, 3]

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
