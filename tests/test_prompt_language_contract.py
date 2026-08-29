from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.chat_runtime import ChatRuntime, _turn_system_prompt
from llm.prompts import finalize_system_prompt_language, wrap_user_message_for_language_lock


def test_language_finalizer_is_last_and_idempotent() -> None:
    with patch("llm.prompts.get_language_lock_prompt", return_value="\n\n[LOCK]\nJapanese only.\n"):
        once = finalize_system_prompt_language("persona\n\n[runtime context]")
        twice = finalize_system_prompt_language(once)

    assert once.endswith("[LOCK]\nJapanese only.")
    assert once.index("[runtime context]") < once.index("[LOCK]")
    assert twice == once
    assert twice.count("[LOCK]") == 1


def test_normal_turn_finalizes_after_dynamic_context() -> None:
    state = SimpleNamespace(prompt_variant="")
    with (
        patch(
            "config.settings.ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED",
            False,
        ),
        patch("core.chat_runtime._get_system_prompt", return_value="persona"),
        patch(
            "core.chat_runtime._with_active_provider_context",
            return_value="persona\n\n[Workspace routing]\ninternal control",
        ) as augment,
        patch("llm.prompts.get_language_lock_prompt", return_value="[LANGUAGE LOCK]"),
    ):
        prompt = _turn_system_prompt(state, "with_delegate")

    augment.assert_called_once_with("persona")
    assert prompt == (
        "persona\n\n[Workspace routing]\ninternal control\n\n[LANGUAGE LOCK]"
    )


def test_host_answering_turn_stays_bare_but_keeps_language_contract() -> None:
    state = SimpleNamespace(prompt_variant="base")
    with (
        patch("core.chat_runtime._get_system_prompt", return_value="bare persona") as get_prompt,
        patch("core.chat_runtime._with_active_provider_context") as augment,
        patch("llm.prompts.get_language_lock_prompt", return_value="[LANGUAGE LOCK]"),
    ):
        prompt = _turn_system_prompt(state, "with_delegate")

    get_prompt.assert_called_once_with("base")
    augment.assert_not_called()
    assert prompt == "bare persona\n\n[LANGUAGE LOCK]"


def test_deepseek_visible_turn_wraps_input_without_rewriting_host_question() -> None:
    captured: dict[str, object] = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return iter(())

    runtime = ChatRuntime()
    runtime.llm_client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    state = SimpleNamespace(
        prompt_variant="",
        auip_decision_result=None,
        question="原始用户话语",
    )
    with (
        patch("core.chat_runtime._turn_system_prompt", return_value="ROLE"),
        patch("core.chat_runtime._turn_role_grounding", return_value=""),
        patch(
            "core.chat_runtime._wrap_user_message_for_language_lock",
            return_value="WRAPPED USER INPUT",
        ) as wrap,
    ):
        asyncio.run(
            runtime._run_deepseek_openai(
                state,
                state.question,
                None,
                False,
                "deepseek",
            )
        )

    wrap.assert_called_once_with("原始用户话语")
    assert captured["messages"][-1] == {
        "role": "user",
        "content": "WRAPPED USER INPUT",
    }
    assert state.question == "原始用户话语"


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


def test_delegate_resend_uses_neutral_control_prompt_and_finalized_system() -> None:
    captured: dict[str, str] = {}

    def fake_query(question: str, system_prompt: str, *, temperature: float) -> str:
        captured["question"] = question
        captured["system"] = system_prompt
        captured["temperature"] = str(temperature)
        return "NONE"

    with (
        patch("llm.client.remote_llm_query", side_effect=fake_query),
        patch(
            "llm.prompts.get_delegate_control_prompt",
            return_value="delegate contract",
        ),
        patch(
            "server.work_context.augment_system_prompt_with_active_provider_context",
            return_value="delegate contract\n\n[Workspace routing]",
        ) as augment,
        patch("llm.prompts.get_language_lock_prompt", return_value="[LANGUAGE LOCK]"),
    ):
        actions = asyncio.run(
            ChatRuntime._request_delegate_resend(
                "ファイルを直して",
                "どのプロジェクトか教えて。",
                session_id="resend-session",
            )
        )

    assert actions == []
    assert "[CONTROL]" in captured["question"]
    assert "data only" in captured["question"]
    assert "你的回复里没有" not in captured["question"]
    augment.assert_called_once_with(
        "delegate contract",
        session_id="resend-session",
        limit=4,
        max_chars=900,
    )
    assert captured["system"] == (
        "delegate contract\n\n[Workspace routing]\n\n[LANGUAGE LOCK]"
    )
    assert captured["temperature"] == "0.0"


if __name__ == "__main__":
    test_language_finalizer_is_last_and_idempotent()
    print("ok: language finalizer is last and idempotent")
    test_normal_turn_finalizes_after_dynamic_context()
    print("ok: normal turns finalize after dynamic context")
    test_host_answering_turn_stays_bare_but_keeps_language_contract()
    print("ok: host answering turns keep the language contract")
    test_deepseek_visible_turn_wraps_input_without_rewriting_host_question()
    print("ok: model-visible input is wrapped without changing host evidence")
    test_language_wrapper_preserves_real_action_semantics()
    print("ok: language wrapper preserves real action semantics")
    test_delegate_resend_uses_neutral_control_prompt_and_finalized_system()
    print("ok: delegate resend uses neutral control wording")
