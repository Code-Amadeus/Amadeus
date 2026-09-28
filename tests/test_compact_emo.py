from unittest.mock import Mock

import pytest

from core.chat_history_projection import project_inline_role_history
from llm.codex_role_contract import evaluate_role_output
from llm.stream_parser import StreamTagParser
from tools.text_utils import parse_tags_and_clean
from vts.expression_controller import ExpressionController


@pytest.mark.parametrize("preset", ["normal", "surprised", "thinking", "serious_speaking"])
def test_compact_emo_uses_same_action_and_playback_owner(preset):
    text = f"そうね、[EMO {preset}] 確認するわ。"
    parser = StreamTagParser()
    chunks = [parser.process_chunk_parts(c) for c in text]
    actions = [a for _, aa, _ in chunks for a in aa]
    assert parse_tags_and_clean(text) == ("そうね、 確認するわ。", actions)
    assert actions[0]["attrs"] == {"preset": preset}
    assert project_inline_role_history(
        [part for _, _, parts in chunks for part in parts], policy="expressive_only"
    ) == (text if preset != "normal" else "そうね、 確認するわ。")
    controller = ExpressionController()
    animator = Mock()
    controller.set_animator(animator, backend="graph")
    controller.register_sentence_actions("s", actions)
    animator.trigger_expression.assert_not_called()
    controller.on_sentence_start("s")
    if preset == "normal":
        animator.trigger_expression.assert_not_called()
    else:
        animator.trigger_expression.assert_called_once_with(preset)
    animator.on_speaking.assert_called_once_with(True)
    assert evaluate_role_output(text).conformant


def test_legacy_duration_and_non_emo_attributes_are_preserved():
    _, actions = parse_tags_and_clean(
        "[EMO preset=angry dur=4s][PARAM id=MouthOpen value=0.2 dur=2s]")
    assert actions[0]["attrs"] == {"preset": "angry", "dur": "4s"}
    assert actions[1]["attrs"] == {"id": "MouthOpen", "value": "0.2", "dur": "2s"}
