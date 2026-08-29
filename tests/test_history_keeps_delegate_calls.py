"""Conversation history must record the call, not just the promise.

Measured 2026-07-31: a first-turn file request emitted its DELEGATE tag 6/6,
while an anaphoric follow-up ("change the colour in that file") emitted it
roughly 1/12 — and three separate system-prompt rewrites moved it not at all.
The reason is in the transcript the model reads on the second turn: history
stored the tag-stripped reply, so the assistant's own strongest in-context
example showed it answering a file request with a spoken promise and no tag.

History therefore keeps the DELEGATE tags the model emitted. EMO retention is
selected independently by EMO_HISTORY_POLICY. The UI stream and TTS keep
reading clean text, and stored tags are stripped again at the session display
boundary.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.chat_runtime import ChatRuntime, _TurnState
from server.handlers.session_handler import _display_dialog, _display_text

_TAG = '[DELEGATE provider="locus" task="create theme.txt with color=blue"]'


def _consume(runtime: ChatRuntime, st: _TurnState, chunk: str) -> str:
    cleaned = runtime._consume_stream_chunk(st, chunk)
    st.full_response += cleaned
    return cleaned


def test_history_keeps_the_tag_while_the_ui_stream_stays_clean() -> None:
    runtime = ChatRuntime.__new__(ChatRuntime)
    st = _TurnState(gui_callback=None, turn_id="t1")

    _consume(runtime, st, "了解したわ。今すぐ作るわね。")
    # This test owns the history projection only. Host dispatch wiring is a
    # separate assembly contract and must be explicit now that VTS no longer
    # silently drops unwired work.
    with patch("core.chat_runtime.record_actions"):
        _consume(runtime, st, _TAG)

    # What the UI and TTS see: no machine syntax.
    assert "DELEGATE" not in st.full_response
    assert st.full_response == "了解したわ。今すぐ作るわね。"

    # What the next turn's prompt sees: the words, then the call.
    assert st.history_response.startswith("了解したわ。今すぐ作るわね。")
    assert _TAG in st.history_response
    assert st.history_response.index("了解") < st.history_response.index("[DELEGATE")


def test_default_expression_policy_stays_out_of_history() -> None:
    runtime = ChatRuntime.__new__(ChatRuntime)
    st = _TurnState(gui_callback=None, turn_id="t2")

    _consume(runtime, st, "そうね。[EMO preset=normal dur=4s] 分かったわ。")

    assert "EMO" not in st.history_response, "expression tags cost tokens for nothing"
    assert "そうね。" in st.history_response


def test_expressive_only_history_keeps_non_neutral_emo_in_exact_position() -> None:
    runtime = ChatRuntime.__new__(ChatRuntime)
    st = _TurnState(gui_callback=None, turn_id="t-expressive")

    with patch(
        "core.chat_history_projection.EMO_HISTORY_POLICY",
        "expressive_only",
    ):
        _consume(runtime, st, "前[EMO preset=thinking")
        _consume(
            runtime,
            st,
            " dur=12s]中[EMO preset=normal dur=4s]後",
        )

    assert st.full_response == "前中後"
    assert st.history_response == "前[EMO preset=thinking dur=12s]中後"


def test_preserve_history_keeps_all_emo_while_live_surface_stays_clean() -> None:
    runtime = ChatRuntime.__new__(ChatRuntime)
    st = _TurnState(gui_callback=None, turn_id="t-preserve")

    with patch("core.chat_history_projection.EMO_HISTORY_POLICY", "preserve"):
        _consume(
            runtime,
            st,
            "前[EMO preset=thinking dur=12s]中[EMO preset=normal dur=4s]後",
        )

    assert st.full_response == "前中後"
    assert st.history_response == (
        "前[EMO preset=thinking dur=12s]中[EMO preset=normal dur=4s]後"
    )
    assert _display_text(st.history_response) == "前中後"
    assert _display_text("[emo preset=smile dur=2s]小文字") == "小文字"


def test_a_turn_without_tags_records_identically() -> None:
    runtime = ChatRuntime.__new__(ChatRuntime)
    st = _TurnState(gui_callback=None, turn_id="t3")

    _consume(runtime, st, "ニュージーランドの首都はウェリントンよ。")

    assert st.history_response == st.full_response


def test_display_boundary_strips_stored_tags() -> None:
    stored = [
        {"role": "user", "content": "请创建 theme.txt"},
        {"role": "assistant", "content": f"了解したわ。{_TAG}", "turn_id": "t1"},
    ]
    shown = _display_dialog(stored)

    assert shown[1]["content"] == "了解したわ。"
    assert shown[1]["turn_id"] == "t1", "display must not drop turn identity"
    assert stored[1]["content"].endswith("]"), "the record itself is unchanged"
    assert _display_text("[EMO preset=smile dur=2s] やった！") == "やった！"


if __name__ == "__main__":
    test_history_keeps_the_tag_while_the_ui_stream_stays_clean()
    print("ok: history keeps the delegate call, the UI stream stays clean")
    test_default_expression_policy_stays_out_of_history()
    print("ok: expression tags stay out of history")
    test_expressive_only_history_keeps_non_neutral_emo_in_exact_position()
    print("ok: expressive-only history keeps only non-neutral EMO")
    test_preserve_history_keeps_all_emo_while_live_surface_stays_clean()
    print("ok: preserve history never leaks EMO to the visible surface")
    test_a_turn_without_tags_records_identically()
    print("ok: a tagless turn records identically")
    test_display_boundary_strips_stored_tags()
    print("ok: the display boundary strips stored tags")
    print("all history delegate-call tests passed")
