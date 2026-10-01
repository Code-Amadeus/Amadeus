"""Main Chat persona edits preserve host-owned prompt contracts."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from llm import prompts
from server.handlers.system_handler import SystemHandler
from server.inherited_role_prompt import inherited_main_role_prompt


@pytest.fixture(autouse=True)
def built_in_character(monkeypatch):
    monkeypatch.setattr(prompts, "_character_prompt_ja", "")
    monkeypatch.setattr("tts.pipeline.TTS_OUTPUT_LANGUAGE", "日文")


@pytest.mark.parametrize("tool,envelope,intent", [
    (False, False, False), (False, False, True),
    (False, True, True), (True, True, True),
])
def test_persona_override_preserves_output_and_execution_contracts(monkeypatch, tool, envelope, intent):
    monkeypatch.setattr(prompts, "_delegate_tool_transport", lambda: tool)
    monkeypatch.setattr(prompts, "_delegate_intent_required", lambda: intent)
    monkeypatch.setattr(prompts, "registered_provider_ids", lambda: ("browser", "codex"))
    monkeypatch.setattr("llm.action_existence_protocol.control_envelope_enabled", lambda: envelope)
    variants = ("base", "with_delegate", "bedrock", "local_fallback", "hybrid_local")
    original = {v: prompts.get_system_prompt(v) for v in variants}
    controls = (prompts.get_delegate_control_prompt(), prompts.get_structured_control_prompt())
    inherited = inherited_main_role_prompt()
    default = prompts.get_character_prompt_config()["main_chat_character_prompt_preview"]["default"]

    override = "あなたは牧瀬紅莉栖。落ち着いて、親しみのある口調で話してください。"
    assert prompts.set_character_prompt("  " + override + "\n") == [prompts.CHARACTER_PROMPT_SETTING]
    preview = prompts.get_character_prompt_config()
    assert preview[prompts.CHARACTER_PROMPT_SETTING] == override
    assert preview["main_chat_character_prompt_preview"] == {
        "default": default, "effective": override, "active": True,
    }
    custom_base = prompts.get_system_prompt("base")
    assert custom_base.startswith(override + "\n\n")
    assert prompts._JA_CHARACTER_RULE not in custom_base
    assert prompts._JA_LANGUAGE_RULES in custom_base
    assert prompts._JA_OUTPUT_RULES in custom_base
    for variant in ("with_delegate", "bedrock"):
        custom = prompts.get_system_prompt(variant)
        assert custom[len(custom_base):] == original[variant][len(original["base"]):]
    assert prompts.get_system_prompt("local_fallback") == (
        override + "\n\n" + prompts._JA_LOCAL_FALLBACK_LANGUAGE)
    assert prompts.get_system_prompt("hybrid_local") == original["hybrid_local"]
    assert (prompts.get_delegate_control_prompt(), prompts.get_structured_control_prompt()) == controls
    assert inherited_main_role_prompt() == inherited
    assert prompts.get_system_prompt("base", use_character_override=False) == original["base"]
    assert prompts.set_character_prompt("\n \t") == [prompts.CHARACTER_PROMPT_SETTING]
    assert {v: prompts.get_system_prompt(v) for v in variants} == original
    assert prompts.get_character_prompt_config()["main_chat_character_prompt_preview"]["effective"] == default


def test_japanese_override_does_not_change_english_role(monkeypatch):
    monkeypatch.setattr("tts.pipeline.TTS_OUTPUT_LANGUAGE", "英文")
    variants = ("base", "with_delegate", "bedrock", "local_fallback", "hybrid_local")
    original = {v: prompts.get_system_prompt(v) for v in variants}
    prompts.set_character_prompt("日本語の人物設定")
    assert {v: prompts.get_system_prompt(v) for v in variants} == original
    assert prompts.get_character_prompt_config()["main_chat_character_prompt_preview"]["active"] is False
    monkeypatch.setattr("tts.pipeline.TTS_OUTPUT_LANGUAGE", "日文")
    assert prompts.get_system_prompt("base").startswith("日本語の人物設定")


@pytest.mark.parametrize("value", [None, False, {}, "bad\0prompt", "字" * 8193],
    ids=["null", "boolean", "object", "nul", "oversized"])
def test_invalid_persona_is_rejected_without_changing_the_role(value):
    original = prompts.get_system_prompt("base")
    with pytest.raises(ValueError):
        prompts.set_character_prompt(value)
    assert prompts.get_system_prompt("base") == original


def test_system_settings_publish_and_clear_the_same_role_used_by_chat():
    async def run():
        handler = SystemHandler()
        original = prompts.get_system_prompt("base")
        with patch("server.handlers.system_handler.bus.emit", new=AsyncMock()) as emit:
            result = await handler._set_config({"values": {
                prompts.CHARACTER_PROMPT_SETTING: "  あなたは穏やかな牧瀬紅莉栖。  ",
            }})
            assert result["updated"] == [prompts.CHARACTER_PROMPT_SETTING]
            assert result["values"][prompts.CHARACTER_PROMPT_SETTING] == "あなたは穏やかな牧瀬紅莉栖。"
            assert prompts.get_system_prompt("base").startswith(
                result["values"]["main_chat_character_prompt_preview"]["effective"])
            assert emit.await_args.args[1]["values"] == result["values"]
            restored = await handler._set_config({"values": {prompts.CHARACTER_PROMPT_SETTING: " \n"}})
            assert restored["values"][prompts.CHARACTER_PROMPT_SETTING] == ""
            assert prompts.get_system_prompt("base") == original
            with pytest.raises(ValueError):
                await handler._set_config({"values": {prompts.CHARACTER_PROMPT_SETTING: False}})
            assert prompts.get_system_prompt("base") == original
    asyncio.run(run())


def test_existing_cooperative_chat_reads_updated_persona_on_the_next_request(tmp_path):
    from agent_host.provider_runtime import ProviderRuntime
    from server.cooperative_provider_loop import CooperativeProviderLoop

    async def run():
        observed = []
        async def query(messages):
            observed.append(messages[0]["content"])
            return '{"action":null,"say":"受け取ったわ。"}'
        loop = CooperativeProviderLoop(ProviderRuntime(), query, lambda *_: tmp_path,
            provider="unused", context_requirements={}, owns_runtime=False,
            persona=lambda: prompts.get_system_prompt("base"))
        try:
            await loop.submit("こんにちは", turn_id="first")
            prompts.set_character_prompt("あなたは落ち着いた牧瀬紅莉栖。")
            await loop.submit("もう一度こんにちは", turn_id="second")
            assert not observed[0].startswith("あなたは落ち着いた牧瀬紅莉栖。")
            assert observed[-1].startswith("あなたは落ち着いた牧瀬紅莉栖。")
            assert loop.presentation_system.startswith("あなたは落ち着いた牧瀬紅莉栖。")
            prompts.set_character_prompt("")
            await loop.submit("元の設定でこんにちは", turn_id="third")
            assert observed[-1] == observed[0]
        finally:
            await loop.close()
    asyncio.run(run())
