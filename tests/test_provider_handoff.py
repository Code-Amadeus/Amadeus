from agent_host.provider_handoff import (
    CODEX_HANDOFF_CONVERSATION_CONTRACT,
    codex_handoff_presentation,
)


def test_handoff_prefers_exact_user_wording_and_hides_host_protocol() -> None:
    presentation = codex_handoff_presentation(
        "3x3のライトトグルゲームを作成する。",
        source_user_text="先帮我做一个很简单的三乘三点灯小游戏吧。",
        source_user_context="文件放在桌面，之后我想继续修改。",
        presentation_locale="zh-CN",
    )

    assert presentation.user_message.startswith("Amadeus 任务交接")
    assert "先帮我做一个很简单的三乘三点灯小游戏吧。" in presentation.user_message
    assert "文件放在桌面，之后我想继续修改。" in presentation.user_message
    assert "3x3のライトトグルゲーム" not in presentation.user_message
    assert "Attempt" not in presentation.user_message
    assert "authoring capability" not in presentation.user_message
    assert presentation.thread_name.startswith("Amadeus · 先帮我做")
    assert len(presentation.thread_name) <= 96


def test_handoff_falls_back_to_provider_task_without_source_text() -> None:
    presentation = codex_handoff_presentation("Create result.txt")

    assert "Create result.txt" in presentation.user_message
    assert presentation.thread_name == "Amadeus · Create result.txt"


def test_hidden_contract_is_written_for_a_person_who_may_take_over() -> None:
    assert "opened and continued directly" in CODEX_HANDOFF_CONVERSATION_CONTRACT
    assert "Host-only policies" in CODEX_HANDOFF_CONVERSATION_CONTRACT
    assert 'calling it "the Host"' in CODEX_HANDOFF_CONVERSATION_CONTRACT
    assert "temporary `apply_patch.bat` wrapper" in CODEX_HANDOFF_CONVERSATION_CONTRACT
    assert "JS REPL" in CODEX_HANDOFF_CONVERSATION_CONTRACT
    assert "writeFileSync" in CODEX_HANDOFF_CONVERSATION_CONTRACT
    assert "fresh block scope" in CODEX_HANDOFF_CONVERSATION_CONTRACT


if __name__ == "__main__":
    test_handoff_prefers_exact_user_wording_and_hides_host_protocol()
    test_handoff_falls_back_to_provider_task_without_source_text()
    test_hidden_contract_is_written_for_a_person_who_may_take_over()
    print("ok: persisted Codex threads present a readable Amadeus handoff")
