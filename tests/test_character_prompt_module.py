"""Character data boundaries, literal interpolation and shipped resources."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from llm import character_prompts as characters


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("key,value", [
    ("ja_identity", None), ("ja_identity", False), ("ja_identity", {}),
    ("ja_identity", "bad\0text"), ("ja_identity", "x" * 8193),
])
def test_character_values_reject_invalid_text(key, value):
    values = dict(characters.load().values)
    values[key] = value
    with pytest.raises(ValueError):
        characters.CharacterPrompts("kurisu", values)


def test_character_values_require_complete_disjoint_keys():
    values = dict(characters.load().values)
    del values["ja_identity"]
    with pytest.raises(ValueError):
        characters.CharacterPrompts("kurisu", values)
    values = dict(characters.load().values) | {"host_instruction": "unexpected"}
    with pytest.raises(ValueError):
        characters.CharacterPrompts("kurisu", values)
    with pytest.raises(TypeError):
        characters.load().values["ja_identity"] = "changed"


@pytest.mark.parametrize("value", [None, "", "../kurisu", "Kurisu", " kurisu", "kurisu ", "a" * 65])
def test_character_id_has_no_path_or_normalization_alias(value):
    with pytest.raises(ValueError):
        characters.validate_character_id(value)


def test_historic_identity_validation_does_not_load_a_pack():
    assert characters.validate_character_id("removed-role_1") == "removed-role_1"
    with pytest.raises(FileNotFoundError):
        characters.load("missing-test-character")


@pytest.mark.parametrize("failure", ["missing_key", "wrong_type", "extra_table", "invalid_toml"])
def test_malformed_selected_file_fails_without_default_fallback(monkeypatch, tmp_path, failure):
    document = tomllib.loads((ROOT / "characters/kurisu.toml").read_text(encoding="utf-8"))
    document["names"]["character_id"] = "invalid-test"
    if failure == "missing_key":
        del document["texts"]["ja_identity"]
    if failure == "wrong_type":
        document["texts"]["ja_identity"] = 42
    if failure == "extra_table":
        document["execution"] = {"command": "forbidden"}
    contents = "\n\n".join(f"[{table}]\n" + "\n".join(
        f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in values.items())
        for table, values in document.items())
    path = tmp_path / "invalid-test.toml"
    path.write_text("invalid = [" if failure == "invalid_toml" else contents, encoding="utf-8")
    monkeypatch.setattr(characters, "files", lambda _package: tmp_path)
    with pytest.raises(ValueError):
        characters.load("invalid-test")


def synthetic_character(monkeypatch):
    values = dict(characters.load().values)
    values.update(character_id="synthetic-role", short_name='A "quoted" \\ name\n${short_name}',
                  cooperative_json_say='Quoted "speech" \\ path\n${display_name} $short_name',
                  vn_voice="Literal ${display_name} and $short_name",
                  vn_analyst="Literal ${game_context} and $output_language",
                  vn_retrospective_posture="Literal ${reflection_scope}")
    character = characters.CharacterPrompts("synthetic-role", values)
    original = characters.load
    monkeypatch.setattr(characters, "load", lambda character_id="kurisu":
                        character if character_id == character.character_id else original(character_id))
    return character


def test_render_values_are_literal_and_cannot_replace_host_character_slots(monkeypatch):
    character = synthetic_character(monkeypatch)
    assert characters.render("${vn_analyst}", character_id=character.character_id) == character.values["vn_analyst"]
    assert characters.active_character_id() == "kurisu"
    with pytest.raises(KeyError):
        characters.render("${missing_host_slot}")
    with pytest.raises(ValueError):
        characters.render("${short_name}", short_name="override")
    sample = characters.render('{"say":${cooperative_json_say_json}}', character_id=character.character_id)
    assert json.loads(sample)["say"] == character.values["cooperative_json_say"]


def test_cooperative_json_samples_escape_character_speech_at_the_real_builder(monkeypatch):
    from server.cooperative_provider_loop import _role_coordination_contract

    character = synthetic_character(monkeypatch)
    prompt = _role_coordination_contract(False, character_id=character.character_id)
    marker = '{"action":{"op":"work","intent":"execute"},"say":'
    start = prompt.index(marker)
    sample, _ = json.JSONDecoder().raw_decode(prompt, start)
    assert sample["say"] == character.values["cooperative_json_say"]


@pytest.mark.parametrize("lane", ["immediate", "lookahead", "reasoner", "summary", "retrospective"])
def test_nested_vn_templates_never_expand_inserted_character_values(monkeypatch, lane):
    from vn_player.prompt_layers import system_prompt
    from vn_player.schemas import VNProfile

    character = synthetic_character(monkeypatch)
    profile = VNProfile(session_id="synthetic", prompt_pack="mystery")
    result = system_prompt(profile, lane, character_id=character.character_id)
    if lane == "immediate":
        assert character.values["vn_analyst"] in result
        assert character.values["vn_voice"] in result
        start = result.index("{", result.index("Speak object if decision=speak:"))
        sample, _ = json.JSONDecoder().raw_decode(result, start)
        assert sample["text"] == f"spoken {character.values['short_name']} line with optional [EMO preset=thinking dur=8s]"
    if lane == "retrospective":
        assert character.values["vn_retrospective_posture"] in result
    # References in inserted names remain literal even inside nested PARTS.
    if character.values["short_name"] in result:
        assert "${short_name}" in result


def test_user_override_remains_scoped_to_kurisu_japanese(monkeypatch):
    from llm import prompts

    monkeypatch.setattr(prompts, "_character_prompt_ja", "Literal ${short_name} $display_name")
    assert characters.character_override("kurisu", "ja") == "Literal ${short_name} $display_name"
    assert characters.character_override("kurisu", "en") == ""
    assert characters.character_override("synthetic-role", "ja") == ""


def test_auip_display_mapping_preserves_wire_receipt_and_application_data(monkeypatch):
    from server import auip_structured_presentation as presentation
    from server.auip_contract import AUIP_PARTICIPANT_ACTOR

    observation = {"state": {"winner": "black", "roleBindings": {"participant": "black", "user": "white"},
                              "note": "kurisu is application data"}, "event": {
        "event_id": "synthetic-event", "type": "game.finished", "actor": AUIP_PARTICIPANT_ACTOR,
        "revision": 2, "terminal": True, "payload": {"winner": "black", "note": "kurisu"}},
        "latest_verified_self_action": {"action_id": "synthetic-action", "type": "game.move",
            "payload": {"position": 3}, "accepted": True, "resulting_revision": 2, "effects": {"placed": 3}}}
    original = copy.deepcopy(observation)
    monkeypatch.setattr(presentation, "active_character_id", lambda: "synthetic-role")
    facts = presentation.compile_auip_host_facts(observation)
    assert facts[0]["actor"]["verified"] == "synthetic-role"
    assert facts[1]["actor"]["verified"] == "synthetic-role"
    assert facts[1]["outcome"]["winner_owner"] == "synthetic-role"
    assert facts[1]["details"]["payload"]["note"] == "kurisu"
    assert facts[1]["details"]["state"]["note"] == "kurisu is application data"
    assert observation == original
    observation["latest_verified_self_action"]["resulting_revision"] = 1
    facts = presentation.compile_auip_host_facts(observation)
    assert len(facts) == 1 and facts[0]["actor"]["verified"] == "unknown"
    assert AUIP_PARTICIPANT_ACTOR == "kurisu"


def test_auip_envelope_bound_applies_after_long_presentation_identity(monkeypatch):
    from server import auip_structured_presentation as presentation
    from server.auip_contract import AuipProtocolError

    observation = {"state": {"winner": "black", "roleBindings": {
        "participant": "black", "user": "white"}, "notes": ["x" * 180] * 4}, "event": {
        "event_id": "e", "type": "game.finished", "actor": "kurisu", "revision": 2,
        "terminal": True, "payload": {**{f"padding{i}": "x" * 180 for i in range(22)}, "winner": "black"}},
        "latest_verified_self_action": {"action_id": "a", "type": "game.move", "payload": {"position": 3},
            "accepted": True, "resulting_revision": 2, "effects": {"placed": 3}}}
    baseline = presentation.compile_auip_host_facts(observation)
    assert len(json.dumps(baseline, ensure_ascii=False, separators=(",", ":"))) <= 6000
    monkeypatch.setattr(presentation, "active_character_id", lambda: "a" * 64)
    with pytest.raises(AuipProtocolError, match="presentation_fact_envelope_too_large"):
        presentation.compile_auip_host_facts(observation)


def test_source_archive_contains_builtin_character_data(tmp_path):
    import zipfile
    from tools.build_source_release import create_archive, select_paths

    policy = json.loads((ROOT / "release/source_release_policy.json").read_text(encoding="utf-8"))
    paths = {"characters/__init__.py", "characters/kurisu.toml", "llm/character_prompts.py"}
    selected, excluded = select_paths(paths, policy)
    assert not excluded and paths <= set(policy["required_files"])
    report = {"release_ready": True, "version": "test", "source_date_epoch": 1700000000,
              "selected_files": [{"path": path} for path in selected]}
    output = tmp_path / "character-source.zip"
    create_archive(root=ROOT, output=output, report=report, policy=policy, modes={})
    with zipfile.ZipFile(output) as archive:
        assert archive.read("amadeus-test/characters/kurisu.toml") == (ROOT / "characters/kurisu.toml").read_bytes()


def test_installed_package_data_loads_from_an_unrelated_working_directory(tmp_path):
    from setuptools import Distribution

    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]
    distribution = Distribution({"name": "amadeus-character-test", "packages": configuration["packages"],
                                 "package_data": configuration["package-data"]})
    distribution.script_name = str(ROOT / "setup.py")
    distribution.get_command_obj("egg_info").egg_base = str(tmp_path)
    build = distribution.get_command_obj("build_py")
    build.build_lib = str(tmp_path / "installed")
    build.ensure_finalized()
    build.run()
    assert (Path(build.build_lib) / "characters/kurisu.toml").read_bytes() == (ROOT / "characters/kurisu.toml").read_bytes()
    cwd = tmp_path / "unrelated"
    cwd.mkdir()
    code = (f"import sys; sys.path.insert(0, {build.build_lib!r}); "
            "from llm.character_prompts import character_identity; "
            "assert character_identity() == {'character_id':'kurisu','display_name':'Makise Kurisu (牧瀬紅莉栖)'}")
    subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code], cwd=cwd,
                   capture_output=True, encoding="utf-8", check=True, timeout=30)
