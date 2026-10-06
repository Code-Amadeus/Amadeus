from __future__ import annotations

import os
import sys
from unittest.mock import patch


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm.prompts import finalize_system_prompt_language, wrap_user_message_for_language_lock


def test_language_finalizer_is_last_and_idempotent() -> None:
    with patch("llm.prompts.get_language_lock_prompt", return_value="\n\n[LOCK]\nJapanese only.\n"):
        once = finalize_system_prompt_language("persona\n\n[runtime context]")
        twice = finalize_system_prompt_language(once)

    assert once.endswith("[LOCK]\nJapanese only.")
    assert once.index("[runtime context]") < once.index("[LOCK]")
    assert twice == once
    assert twice.count("[LOCK]") == 1


def test_language_wrapper_preserves_real_action_semantics() -> None:
    with patch("tts.pipeline.TTS_OUTPUT_LANGUAGE", "日文"):
        wrapped = wrap_user_message_for_language_lock(
            "OpenClawでページを開いて内容を調べて。"
        )

    assert "ユーザーの実際の発言" in wrapped
    assert "依頼内容は通常どおり解釈" in wrapped
    assert "必要な場合" in wrapped
    assert "内容理解用" not in wrapped
    assert "OpenClawでページを開いて内容を調べて。" in wrapped
