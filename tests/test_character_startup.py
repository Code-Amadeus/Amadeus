"""Real minimal profiles, boot isolation, owned override/RAG and prompt leakage."""
from __future__ import annotations

import json
from pathlib import Path
import tomllib

import pytest

from llm import character_prompts as characters
from test_character_prompt_snapshots import _isolated_capture, FIXTURE


ROOT = Path(__file__).resolve().parents[1]
LEAK_TOKENS = ("kurisu", "牧瀬", "紅莉栖", "红莉栖", "クリス", "christina",
               "steins", "tsundere", "prickly", "skeptical")


@pytest.fixture(scope="module")
def mira_capture():
    return _isolated_capture("mira", include_startup=True)


def test_real_name_only_fixture_derives_neutral_persona_and_names(mira_capture):
    startup = mira_capture["startup"]
    assert startup["identity"] == {"character_id": "mira", "display_name": "Mira"}
    values = startup["values"]
    assert all(values[key] == "Mira" for key in characters.NAME_KEYS - {"character_id", "work_title"})
    assert values["work_title"] == ""
    assert values["vn_voice"] == "If speaking, stay in Mira's voice."
    assert values["vn_christina"] == "Stay consistent with Mira's established personality and reactions."
    assert values["vn_no_orientation"] == "No retrospective orientation yet. Avoid overclaiming."
    assert "//" not in mira_capture["prompts"]["main/ja/with_delegate/intent=1,amend=1,retract=1"]


def test_every_captured_prompt_surface_has_no_old_persona(mira_capture):
    expected_keys = json.loads(FIXTURE.read_text(encoding="utf-8"))["prompts"].keys()
    assert mira_capture["prompts"].keys() == expected_keys
    for key, value in mira_capture["prompts"].items():
        # These synthetic observations deliberately carry raw fixed wire
        # values in app state. Check only Host presentation identity fields.
        if key.startswith("auip/structured_facts/"):
            facts = json.loads(value)
            for fact in facts:
                assert fact["actor"]["verified"] in {"mira", "unknown"}
                assert "kurisu" not in fact["subject_owners"]
            continue
        assert not [token for token in LEAK_TOKENS if token in value.lower()], key
    for key in ("main/ja/base/intent=0,amend=0,retract=0", "main/en/base/intent=0,amend=0,retract=0",
                "cooperative/ja/json/combined", "auip/ja/narrator", "browser/ja/system",
                "work_observer/decision/system", "vn_tts/zh_to_ja/system", "openclaw/execution/system"):
        assert "Mira" in mira_capture["prompts"][key]


def test_startup_identity_and_text_stay_pinned_when_settings_and_cache_change(mira_capture):
    startup = mira_capture["startup"]
    assert startup["identity_after_configuration_change"] == startup["identity"]
    assert startup["text_after_cache_clear"] == startup["values"]["ja_identity"]


def test_current_pack_edits_do_not_replace_pinned_object(monkeypatch, tmp_path):
    before = characters.character_identity()
    original_text = characters.text("ja_identity")
    resource = (ROOT / "characters/kurisu.toml").read_text(encoding="utf-8")
    changed_text = "Edited after startup."
    document = tomllib.loads(resource)
    document["names"]["display_name"] = "Edited display"
    document["texts"]["ja_identity"] = changed_text
    resource = "\n\n".join(f"[{table}]\n" + "\n".join(
        f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in values.items())
        for table, values in document.items())
    (tmp_path / "kurisu.toml").write_text(resource, encoding="utf-8")
    monkeypatch.setattr(characters, "files", lambda _package: tmp_path)
    characters.load.cache_clear()
    try:
        assert characters.load("kurisu").values["ja_identity"] == changed_text
        assert characters.character_identity("kurisu") == before
        assert characters.text("ja_identity", character_id="kurisu") == original_text
        assert characters.bindings(character_id="kurisu")["ja_identity"] == original_text
    finally:
        characters.load.cache_clear()


def test_saved_kurisu_edit_and_corpus_are_ignored_under_mira_and_recovered_after_restart():
    edit = "SAVED_KURISU_JA_EDIT ${short_name}"
    mira = _isolated_capture("mira", edit, include_startup=True)
    kurisu = _isolated_capture("kurisu", edit, include_startup=True)
    for key, value in mira["prompts"].items():
        assert edit not in value, key
    preview = mira["startup"]["editor"]["main_chat_character_prompt_preview"]
    assert preview["active"] is False and preview["effective"] == edit
    assert "牧瀬紅莉栖" in preview["default"]
    assert mira["startup"]["editor"]["main_chat_character_prompt_ja"] == edit
    assert mira["startup"]["rag_calls"] == [] and mira["startup"]["rag_result"] == ""
    assert kurisu["startup"]["editor"]["main_chat_character_prompt_preview"]["active"] is True
    assert kurisu["prompts"]["main/ja/base/intent=0,amend=0,retract=0"].startswith(edit)
    assert kurisu["startup"]["rag_calls"] == ["synthetic topic"]
    assert kurisu["startup"]["rag_result"] == "Synthetic Kurisu corpus"
    for key in ("vn_tts/zh_to_ja/system", "work_observer/decision/system", "vn/base/immediate/messages"):
        assert edit not in kurisu["prompts"][key]


@pytest.mark.parametrize("identity", ["app", "user", "system", "application", "unknown"])
def test_character_ids_are_disjoint_from_host_participant_authority(identity):
    with pytest.raises(ValueError):
        characters.validate_character_id(identity)


@pytest.mark.parametrize("name", ["", " ", " Mira", "Mira ", "Mira\n", None, False])
def test_empty_or_invalid_primary_name_fails_at_resource_loader(monkeypatch, tmp_path, name):
    rendered = json.dumps(name) if name is not None else "42"
    (tmp_path / "invalid-name.toml").write_text(
        f'[names]\ncharacter_id="invalid-name"\nname={rendered}\n', encoding="utf-8")
    monkeypatch.setattr(characters, "files", lambda _package: tmp_path)
    with pytest.raises(ValueError):
        characters.load("invalid-name")


def test_invalid_explicit_display_name_fails_before_work_admission(monkeypatch, tmp_path):
    (tmp_path / "invalid-display.toml").write_text(
        '[names]\ncharacter_id="invalid-display"\nname="Mira"\ndisplay_name=" Mira "\n',
        encoding="utf-8")
    monkeypatch.setattr(characters, "files", lambda _package: tmp_path)
    with pytest.raises(ValueError, match="display_name"):
        characters.load("invalid-display")


def test_neutral_authoring_description_retains_task_scope():
    description = (ROOT / "skills/auip-authoring/SKILL.md").read_text(encoding="utf-8").splitlines()[2]
    assert "Amadeus/Kurisu" not in description
    assert "the main assistant to watch, comment, play, or operate" in description
    assert "games, simulations, or interactive tools" in description
    assert "Do not use for static pages, reports, ordinary sites, or unrelated code work" in description
