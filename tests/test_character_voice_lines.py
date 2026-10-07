"""Host phrase migration preserves accepted facts and literal interpolation."""
from dataclasses import replace
import json
from pathlib import Path
import re
from string import Template

import pytest

from llm import character_prompts as characters
from llm.character_voice_lines import VOICE_LINES, voice_line
from server.auip_runtime import _receipt_fact, _state_fact, _capability_fact
from server.browser_page_outcome import _host_summary
from server.task_lookup import render_current_status_facts
from vn_player.mystery_policy import _fallback_speech


ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads((ROOT / "tests/fixtures/character_voice_line_baseline.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def pinned_kurisu(monkeypatch):
    monkeypatch.setattr(characters, "_ACTIVE_CHARACTER", characters.load("kurisu"))
    yield
    characters.load.cache_clear()


@pytest.fixture
def mira(monkeypatch, tmp_path):
    (tmp_path / "mira.toml").write_bytes((ROOT / "tests/fixtures/mira_character.toml").read_bytes())
    with monkeypatch.context() as loading:
        loading.setattr(characters, "files", lambda _package: tmp_path)
        role = characters.load("mira")
    monkeypatch.setattr(characters, "_ACTIVE_CHARACTER", role)
    return role


@pytest.mark.parametrize("key", BASELINE)
def test_kurisu_templates_preserve_original_host_wording(key):
    record = BASELINE[key]
    assert characters.active_character().voice_lines[key] == record["template"]
    assert VOICE_LINES[key].facts == frozenset(record["facts"])
    facts = {name: 'literal ${foreign} $name {braces} "quotes" \\ path\n次の行'
        for name in record["facts"]}
    assert voice_line(key, **facts) == Template(record["template"]).substitute(facts)


def test_catalog_coverage_matches_the_original_phrase_inventory():
    assert VOICE_LINES.keys() == BASELINE.keys()


def test_name_only_role_has_neutral_defaults_without_user_persona(mira, monkeypatch):
    from llm import prompts
    monkeypatch.setattr(prompts, "_character_prompt_ja", "私の口調よ。${project_name}")
    for key, spec in VOICE_LINES.items():
        assert mira.voice_lines[key] == spec.template
        assert "私" not in spec.template
        assert not re.search(r"(?:わ(?:ね|よ)?|よ|ね)[。！]|(?:わ(?:ね|よ)?|よ|ね)$", spec.template)
        facts = {name: "literal value" for name in spec.facts}
        assert voice_line(key, **facts) == voice_line(key, character_id="mira", **facts)


def test_facts_with_template_syntax_are_interpolated_once(mira):
    project = 'Alpha ${project_name} $other {braces} "quoted" \\ path\nnext line'
    assert voice_line("focus_voice_project", project_name=project) == (
        f"「{project}」プロジェクトへの切り替えを確認しました。次の作業はここから続けます。")
    assert "${project_name}" in voice_line("focus_voice_project", project_name=project)


@pytest.mark.parametrize("facts", [{}, {"project_name": "A", "unknown": "B"}])
def test_call_requires_exact_host_declared_facts(facts):
    with pytest.raises(ValueError, match="exactly these facts"):
        voice_line("focus_voice_project", **facts)


@pytest.mark.parametrize("overrides", [
    None, False, [], {"unknown_semantic_key": "text"},
    {"focus_voice_project": False}, {"focus_voice_project": ""},
    {"focus_voice_project": "bad\0text"}, {"focus_voice_project": "x" * 8193},
    {"focus_voice_project": "${project_name} ${unknown}"},
    {"focus_voice_project": "missing required fact"},
    {"focus_voice_project": "${project_name.value}"},
    {"focus_voice_project": "${project_name[0]}"},
    {"focus_voice_project": "${project_name:-fallback}"},
    {"focus_voice_project": "${project_name} $"},
])
def test_loader_rejects_invalid_voice_line_tables(monkeypatch, tmp_path, overrides):
    # A role's ordinary text may contain dollar syntax; only this separate table
    # has the Host template contract.
    role = characters.active_character()
    with pytest.raises(ValueError):
        replace(role, voice_lines=overrides)
    if isinstance(overrides, dict):
        document = '[names]\ncharacter_id = "invalid-voice"\nname = "Test"\n[voice_lines]\n'
        document += "\n".join(f"{json.dumps(key)} = {json.dumps(value)}" for key, value in overrides.items())
        (tmp_path / "invalid-voice.toml").write_text(document, encoding="utf-8")
        monkeypatch.setattr(characters, "files", lambda _package: tmp_path)
        with pytest.raises(ValueError):
            characters.load("invalid-voice")


def test_overrides_are_immutable_and_explicit_active_id_uses_the_startup_snapshot(monkeypatch, tmp_path):
    pinned = characters.active_character()
    expected = voice_line("focus_voice_drafts")
    with pytest.raises(TypeError):
        pinned.voice_lines["focus_voice_drafts"] = "changed"
    (tmp_path / "kurisu.toml").write_text(
        '[names]\ncharacter_id = "kurisu"\nname = "Renamed"\n[voice_lines]\n'
        'focus_voice_drafts = "Later file edit."\n', encoding="utf-8")
    monkeypatch.setattr(characters, "files", lambda _package: tmp_path)
    characters.load.cache_clear()
    assert voice_line("focus_voice_drafts") == expected
    assert voice_line("focus_voice_drafts", character_id="kurisu") == expected


def test_ordinary_character_values_keep_dollar_syntax_literal():
    role = characters.active_character()
    values = dict(role.values) | {"ja_identity": "Literal ${project_name} $name"}
    changed = replace(role, values=values)
    assert changed.values["ja_identity"] == values["ja_identity"]


def test_receipt_facts_preserve_verified_pending_rejected_and_missing_boundaries(mira):
    accepted = _receipt_fact({"latest_verified_self_action": {"accepted": True, "resulting_revision": 9}}, japanese=True)
    assert "キャラクター側の直近の操作" in accepted and "状態更新 9 まで反映済み" in accepted
    pending = _receipt_fact({"pending_action": {"type": "move"}}, japanese=True)
    assert "確認待ち" in pending and "反映済みとは言えません" in pending
    rejected = _receipt_fact({"operator_error": "action_rejected", "operator_error_detail": "occupied"}, japanese=True)
    assert "受理されませんでした" in rejected and "occupied" in rejected
    expired = _receipt_fact({"last_expired_action": {"type": "move"}}, japanese=True)
    assert "反映されたかは確認できません" in expired


def test_auip_owner_labels_distinguish_character_side_from_user(mira):
    participant = _state_fact({"state": {"turn": "kurisu"}}, japanese=True)
    user = _state_fact({"state": {"turn": "user"}}, japanese=True)
    assert "現在の手番はキャラクター側です。" in participant
    assert "現在の手番はあなたです。" in user
    assert "私" not in participant and "私" not in user
    capability = _capability_fact({"available_modes": ["collaborate"]}, japanese=True)
    assert "キャラクター側は" in capability and "共同参加" in capability


def test_controller_history_does_not_become_current_execution(mira):
    earlier = _state_fact({"state": {}, "controller_execution_evidence": {"scope": "earlier_policy"}}, japanese=True)
    current = _state_fact({"state": {}, "controller_execution_evidence": {"scope": "current_policy"}}, japanese=True)
    assert "以前のController方針" in earlier and "現在の方針についての実行証明ではありません" in earlier
    assert "現在の方針がHost発行のController lease中" in current
    assert "過去の確認済み実行結果を取り消すものではありません" in earlier


def test_status_fragments_keep_quote_boundaries_and_reported_modality(mira):
    facts = {"stage_key": "running", "fact_kind": "capability", "fact_verified": "false",
        "recent_ja": "作業を終えたと報告しています", "recent_zh": "执行侧报告已结束",
        "blocker_zh": "没有已知阻碍", "next_ja": "実行を始める", "next_zh": "开始执行"}
    _, report = render_current_status_facts(facts)
    assert "実行側からは「作業を終えたと報告しています」と報告されています" in report
    assert "まだホスト確認済みの結果ではありません" in report
    facts.update(stage_key="queued", fact_kind="")
    _, queued = render_current_status_facts(facts)
    assert "次の対応は「実行を始める」です。" in queued
    facts.update(stage_key="running", recent_ja="", title="A task")
    _, running = render_current_status_facts(facts)
    assert "次の対応は「実行を始める」です。検証結果が出たら知らせます。" in running


def test_browser_finished_and_verified_results_stay_distinct(mira):
    common = dict(execution_status="succeeded", attention="none", title="Result", url="", display_language="japanese")
    verified = _host_summary(**common, verified=True)
    unverified = _host_summary(**common, verified=False)
    assert "操作は完了しました" in verified
    assert "操作は終了しました" in unverified and "まだ確認が必要" in unverified
    assert _host_summary(**(common | {"attention": "conflict"}), verified=True).startswith("操作結果の報告と現在のページが一致していません")


@pytest.mark.parametrize("kind,text,key", [
    ("new_evidence", "复活只能使用一次", "vn_voice_resurrection_once"),
    ("new_evidence", "复活秘术", "vn_voice_resurrection"),
    ("new_evidence", "拥有诅咒珠", "vn_voice_curse_orb"),
    ("new_evidence", "满足条件诅咒杀人", "vn_voice_lethal_condition"),
    ("new_evidence", "确凿的证据", "vn_voice_evidence"),
    ("new_evidence", "咒主与魂渣", "vn_voice_resource_rules"),
    ("new_evidence", "真货", "vn_voice_real_claim"),
    ("new_evidence", "看不见", "vn_voice_visibility"),
    ("rule_anomaly", "", "vn_voice_rule_anomaly"),
    ("choice", "", "vn_voice_choice"),
    ("emotional_beat", "", "vn_voice_emotion"),
    ("contradiction", "", "vn_voice_contradiction"),
    ("new_evidence", "", "vn_voice_new_evidence"),
    ("low_density", "", "vn_voice_density"),
])
def test_vn_reaction_semantics_select_the_same_key_for_each_character(mira, kind, text, key):
    assert _fallback_speech(kind, "ja", text) == voice_line(key)
    assert _fallback_speech(kind, "en", text) == "Hold on.[EMO preset=thinking dur=8s] That line feels more like a clue than filler."
