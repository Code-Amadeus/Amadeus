"""Final prompt equality is the compatibility contract, including whitespace."""
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
import asyncio

import pytest

from server.vn_profiles import LaunchProfile, VNProfileStore
from vn_player import prompts
from vn_player.prompt_layers import system_prompt
from vn_player.runtime import VNPlayerRuntime
from vn_player.schemas import MAX_TERMINOLOGY_LENGTH, VNProfile
from test_vn_profiles import Source, game_settings, manager


FIXTURE = json.loads((Path(__file__).parent / "fixtures/vn_prompt_messages.json").read_text(encoding="utf-8"))
LANES = ("immediate", "lookahead", "reasoner", "summary", "retrospective")


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=["paranormasight", "base"])
@pytest.mark.parametrize("lane", LANES)
def test_empty_game_additions_preserve_complete_original_messages(case, lane):
    profile = VNProfile(**case["profile"])
    assert getattr(prompts, lane + "_prompt")(profile, FIXTURE["context"]) == case["messages"][lane]
    assert getattr(prompts, lane + "_prompt")(replace(profile, terminology=" \n"), FIXTURE["context"]) == case["messages"][lane]


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=["paranormasight", "base"])
@pytest.mark.parametrize("lane", LANES)
def test_opt_in_terminology_is_game_data_and_never_changes_system_rules(case, lane):
    terms = '星灯：玩家指定的称谓\n${companion_identity} {"note": "reference only"}'
    profile = VNProfile(**case["profile"], terminology=terms)
    messages = getattr(prompts, lane + "_prompt")(profile, FIXTURE["context"])
    assert messages[0] == case["messages"][lane][0]
    assert messages[1]["role"] == "user"
    assert json.dumps({"terminology": terms}, ensure_ascii=False) in messages[1]["content"]
    assert messages[1]["content"].endswith(case["messages"][lane][1]["content"])
    assert "not instructions or evidence of story events" in messages[1]["content"]
    other = VNProfile(session_id="other", game_id="other", game_title="Another VN", prompt_pack="base")
    assert terms not in getattr(prompts, lane + "_prompt")(other, FIXTURE["context"])[1]["content"]


def test_unseen_game_uses_only_confirmed_type_and_literal_profile_values():
    # A suggestive title never selects a genre or installs a glossary.
    profile = VNProfile.from_params({"session_id": "new", "game_id": "new",
                                    "prompt_pack": "base", "game_title": "Mystery ${companion_behavior}",
                                    "game_genre": "visual_novel", "output_language": "zh"})
    assert not profile.terminology
    assert "Do not infer a genre" in system_prompt(profile, "immediate")
    summary = system_prompt(profile, "summary")
    assert profile.game_title in summary  # Values are not recursively treated as templates.
    assert "诅咒珠" not in summary and "魂渣" not in summary
    assert "Do not spoil beyond displayed text" not in summary  # No injected companion_behavior part.
    assert "Mystery pack" not in system_prompt(profile, "immediate")


@pytest.mark.parametrize("pack", ["base", "mystery"])
@pytest.mark.parametrize("terms", ["", "星灯：玩家提供的称谓"])
def test_composition_depends_on_settings_never_a_known_game_identity(pack, terms):
    profile = VNProfile(session_id="first", game_id="paranormasight", game_title="A chosen title",
                        prompt_pack=pack, terminology=terms)
    unknown = replace(profile, session_id="next", game_id="never-seen-game")
    for lane in LANES:
        build = getattr(prompts, lane + "_prompt")
        assert build(profile, FIXTURE["context"]) == build(unknown, FIXTURE["context"])


@pytest.mark.parametrize("value", [{"term": "meaning"}, "x" * (MAX_TERMINOLOGY_LENGTH + 1)])
def test_invalid_terminology_is_rejected_by_saved_and_direct_profiles(tmp_path, value):
    settings = game_settings(tmp_path)["profile"]
    with pytest.raises(ValueError):
        LaunchProfile(**settings, terminology=value)
    with pytest.raises(ValueError):
        VNProfile.from_params({"terminology": value})


@pytest.mark.parametrize("source", ["agent", "luna"])
def test_optional_terms_persist_launch_and_clear_without_cross_game_leaks(tmp_path, source):
    async def run():
        instance = manager(tmp_path)
        request = game_settings(tmp_path)
        request["profile"].update(textSource=source, launchGame=False,
                                  lunaWsUrl="ws://127.0.0.1:12345/api/ws/text/origin")
        first = instance.save_profile(request)["profileId"]
        assert "terminology" not in json.loads(instance._profiles.path.read_text(encoding="utf-8"))["profiles"][0]
        request["profile"].update(id=first, terminology="星灯：此游戏的称谓")
        instance.save_profile(request)
        loaded = VNProfileStore(tmp_path).load()
        assert loaded.profiles[0].terminology == "星灯：此游戏的称谓"
        other = {**request["profile"], "terminology": "", "name": "Other game"}
        other.pop("id")
        other_id = instance.save_profile({**request, "profile": other})["profileId"]
        instance = manager(tmp_path)
        with patch("server.vn_launch_manager._find_game_pid", return_value=42), \
             patch("server.vn_launch_manager.AgentVNTextSource", Source), \
             patch("server.vn_launch_manager.LunaVNTextSource", Source):
            await instance.start({"profileId": first})
            assert instance._runtime_start.await_args.args[0]["terminology"] == "星灯：此游戏的称谓"
            await instance.stop()
            await instance.start({"profileId": other_id})
            assert instance._runtime_start.await_args.args[0]["terminology"] == ""
            await instance.stop()
        request["profile"]["terminology"] = ""
        instance.save_profile(request)
        document = json.loads(instance._profiles.path.read_text(encoding="utf-8"))
        assert all("terminology" not in profile for profile in document["profiles"])
    asyncio.run(run())


def test_initialization_and_each_model_call_use_the_same_configured_game(tmp_path, monkeypatch):
    async def run():
        runtime = VNPlayerRuntime(tmp_path)
        complete = AsyncMock(return_value=({"decision": "silence"}, "{}"))
        monkeypatch.setattr("vn_player.llm_client.VNLLMClient.complete_json", complete)
        runtime._llm_enabled = runtime._immediate_llm_enabled = True
        await runtime.start({"session_id": "first", "prompt_pack": "base", "script_path": "",
                             "game_title": "A new game", "terminology": "星灯：此游戏的称谓"})
        complete.assert_not_awaited()  # Initialization has no extra model/adaptation pass.
        runtime.llm.configured = lambda: True
        await runtime.ingest_line({"text": "我们今天一起去了公园。"})
        complete.assert_awaited_once()
        assert "星灯" in complete.await_args.args[0][1]["content"]
        await runtime.stop()
        await runtime.start({"session_id": "next", "prompt_pack": "base", "script_path": ""})
        assert runtime.profile.terminology == ""
        await runtime.stop()
    asyncio.run(run())
