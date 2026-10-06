"""Role presentation history keeps expression policy and filters control tags.

Persisted legacy machine tags remain hidden at the Session display boundary.
"""
from unittest.mock import patch

from core.chat_runtime import ChatRuntime
from server.handlers.session_handler import _display_dialog, _display_text

_TAG = '[DELEGATE provider="locus" task="create theme.txt with color=blue"]'


async def test_role_history_and_visible_stream_drop_execution_tags():
    stream = ChatRuntime().begin_role_text_stream(turn_id="t1", speech=False)
    await stream.feed("了解したわ。今すぐ作るわね。" + _TAG + "続けるわ。")
    assert stream.state.full_response == "了解したわ。今すぐ作るわね。続けるわ。"
    assert stream.state.history_response == stream.state.full_response


async def test_default_expression_policy_stays_out_of_history():
    stream = ChatRuntime().begin_role_text_stream(turn_id="t2", speech=False)
    with patch("core.chat_history_projection.EMO_HISTORY_POLICY", "strip"):
        await stream.feed("そうね。[EMO preset=normal dur=4s] 分かったわ。")
    assert "EMO" not in stream.state.history_response
    assert "そうね。" in stream.state.history_response


async def test_expressive_only_history_keeps_non_neutral_emo_in_exact_position():
    stream = ChatRuntime().begin_role_text_stream(turn_id="t-expressive", speech=False)
    with patch("core.chat_history_projection.EMO_HISTORY_POLICY", "expressive_only"):
        await stream.feed("前[EMO preset=thinking")
        await stream.feed(" dur=12s]中[EMO preset=normal dur=4s]後")
    assert stream.state.full_response == "前中後"
    assert stream.state.history_response == "前[EMO preset=thinking dur=12s]中後"


async def test_preserve_history_keeps_all_emo_while_live_surface_stays_clean():
    stream = ChatRuntime().begin_role_text_stream(turn_id="t-preserve", speech=False)
    with patch("core.chat_history_projection.EMO_HISTORY_POLICY", "preserve"):
        await stream.feed("前[EMO preset=thinking dur=12s]中[EMO preset=normal dur=4s]後")
    assert stream.state.full_response == "前中後"
    assert stream.state.history_response == (
        "前[EMO preset=thinking dur=12s]中[EMO preset=normal dur=4s]後")
    assert _display_text(stream.state.history_response) == "前中後"
    assert _display_text("[emo preset=smile dur=2s]小文字") == "小文字"


async def test_a_turn_without_tags_records_identically():
    stream = ChatRuntime().begin_role_text_stream(turn_id="t3", speech=False)
    await stream.feed("ニュージーランドの首都はウェリントンよ。")
    assert stream.state.history_response == stream.state.full_response


def test_display_boundary_strips_stored_tags():
    stored = [
        {"role": "user", "content": "请创建 theme.txt"},
        {"role": "assistant", "content": f"了解したわ。{_TAG}", "turn_id": "t1"},
    ]
    shown = _display_dialog(stored)
    assert shown[1]["content"] == "了解したわ。"
    assert shown[1]["turn_id"] == "t1"
    assert stored[1]["content"].endswith("]")
    assert _display_text("[EMO preset=smile dur=2s] やった！") == "やった！"
