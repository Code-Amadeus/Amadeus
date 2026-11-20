"""Presentation text and playback cannot acquire execution authority."""

import asyncio
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.chat_runtime import ChatRuntime
from vts.expression_controller import ExpressionController


@pytest.fixture
def presentation(monkeypatch):
    queue = asyncio.Queue()
    expression = SimpleNamespace(register_sentence_actions=Mock())
    monkeypatch.setattr("core.chat_runtime._pre_translation_enabled", lambda: False)
    monkeypatch.setattr("core.chat_runtime._get_expr_ctrl", lambda: expression)
    runtime = ChatRuntime()
    runtime.configure(pending_sentence_items=queue)
    assert not hasattr(runtime, "stream_llm_query")
    assert not hasattr(runtime, "_record_turn_actions")
    assert not hasattr(runtime, "_consume_stream_chunk")
    return runtime, queue, expression


@pytest.mark.parametrize("speech", [True, False])
@pytest.mark.parametrize("emotion_routing", [True, False])
@pytest.mark.parametrize("chunk_size", [1, 17, 1000])
async def test_controls_are_dropped_and_suffix_survives(
    presentation, monkeypatch, caplog, speech, emotion_routing, chunk_size,
):
    runtime, queue, expression = presentation
    monkeypatch.setattr("tts.pipeline.emotion_reference_key", lambda preset: preset)
    monkeypatch.setattr("core.chat_history_projection.EMO_HISTORY_POLICY", "expressive_only")
    visible = []
    stream = runtime.begin_role_text_stream(
        turn_id="presentation", speech=speech, gui_callback=visible.append)
    stream.state.tts_emotion_routing = emotion_routing
    text = ('前[DELEGATE task="do work"]中[CONTROL decision=delegate]後'
            '[AUIP action=start][ANIM name=run][EMO shy]。末句。')
    for index in range(0, len(text), chunk_size):
        await stream.feed(text[index:index + chunk_size])
    await stream.finish()

    assert visible[-1] == stream.state.full_response == "前中後。末句。"
    assert all(tag not in stream.state.history_response
               for tag in ("DELEGATE", "CONTROL", "AUIP", "ANIM"))
    assert "[EMO shy]" in stream.state.history_response
    assert "[PRESENTATION] dropped" in caplog.text
    queued = [queue.get_nowait() for _ in range(queue.qsize())]
    assert "".join(item.text for item in queued) == ("前中後。末句。" if speech else "")
    if speech:
        assert expression.register_sentence_actions.called
        actions = [action for call in expression.register_sentence_actions.call_args_list
                   for action in call.args[1]]
        assert [action["type"] for action in actions] == ["EMO"]


async def test_residual_sentence_cleanup_has_no_dispatch_callback(presentation):
    runtime, queue, expression = presentation
    stream = runtime.begin_role_text_stream(turn_id="residual")
    # Bypass the stream parser to test the final cleanup boundary independently.
    stream.state.current_sentence = (
        '先[DELEGATE task="do work"][CONTROL invalid]後'
        '[EXPR name=smile][PARAM id=Mouth value=1][HOTKEY name=wave]')
    await stream.finish()
    assert queue.get_nowait().text == "先後"
    actions = expression.register_sentence_actions.call_args.args[1]
    assert [action["type"] for action in actions] == ["EXPR", "PARAM", "HOTKEY"]


def test_expression_playback_uses_expression_sink_without_host_dispatch(monkeypatch):
    from vts import action

    queue = Queue()
    monkeypatch.setattr(action, "_pending_actions", queue)
    controller = ExpressionController()
    controller.transition_to = Mock()
    expressions = [{"type": kind, "attrs": {"preset": "shy"}}
                   for kind in ("EMO", "EXPR", "PARAM", "HOTKEY")]
    controller.register_sentence_actions("sentence", expressions)
    controller.on_sentence_start("sentence")
    controller.transition_to.assert_called_once_with("shy")
    assert [queue.get_nowait() for _ in range(queue.qsize())] == expressions[1:]

    action.record_expression_actions([
        {"type": kind, "attrs": {}} for kind in ("DELEGATE", "CONTROL", "AUIP", "ANIM")])
    assert queue.empty()
