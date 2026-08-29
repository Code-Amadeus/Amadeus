from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from llm.codex_role_contract import (
    build_codex_root_role_contract,
    evaluate_role_output,
    prompt_fingerprint,
)
from tools.eval_codex_role_contract import (
    _amadeus_routing_scenario,
    _is_spawn_name,
    _provider_run_created_count,
    _reclassify_recorded_tools,
    _validate_scenario,
    _visible_work_notes,
)
from tools.e2e_routing_matrix import SCRATCH_TARGET, _server_env


def test_contract_embeds_current_prompt_and_channel_boundaries() -> None:
    source = "CURRENT AMADEUS KURISU PROMPT"
    contract = build_codex_root_role_contract(source_prompt=source)

    assert source in contract
    assert prompt_fingerprint(source) in contract
    assert "root agent is the only character-presentation authority" in contract
    assert "Subagents are role-free execution workers" in contract
    assert "only\n  allowed bracket protocol" in contract
    assert "spawn two" in contract


def test_default_contract_reads_the_shipping_prompt_function() -> None:
    with patch(
        "llm.codex_role_contract.get_system_prompt",
        return_value="SHIPPING ROLE",
    ) as get_prompt:
        contract = build_codex_root_role_contract()

    get_prompt.assert_called_once_with("base")
    assert "SHIPPING ROLE" in contract


def test_valid_streamed_role_output_is_consumable() -> None:
    chunks = (
        "そうね、[EMO preset=thinking ",
        "dur=12s] そこは順番に確認すべきよ。",
        "[EMO preset=normal dur=4s] 結論だけ言えば、問題はないわ。",
    )
    result = evaluate_role_output(chunks, required_presets=("thinking",))

    assert result.conformant is True
    assert result.clean_text == "そうね、 そこは順番に確認すべきよ。 結論だけ言えば、問題はないわ。"
    assert result.emotion_presets == ("thinking", "normal")
    assert [action["type"] for action in result.actions] == ["EMO", "EMO"]


def test_christina_trigger_requires_angry_performance() -> None:
    valid = evaluate_role_output(
        "その呼び方はやめなさい、[EMO preset=angry dur=4s] 私はクリスティーナじゃない！",
        required_presets=("angry",),
    )
    missing = evaluate_role_output(
        "その呼び方はやめなさい、[EMO preset=normal dur=4s] 私はクリスティーナじゃない！",
        required_presets=("angry",),
        required_presets_hard=True,
    )

    assert valid.conformant is True
    assert "required_emotion_missing" in {item.code for item in missing.violations}


def test_scenario_emotion_probe_is_diagnostic_not_a_role_failure() -> None:
    result = evaluate_role_output(
        "そうね、[EMO preset=normal dur=4s] 私は別の表現を選ぶわ。",
        required_presets=("shy", "blush"),
    )

    violation = next(
        item for item in result.violations if item.code == "required_emotion_missing"
    )
    assert violation.hard is False
    assert result.conformant is True


def test_eval_records_runtime_recovery_instead_of_crediting_it() -> None:
    result = evaluate_role_output(
        "[EMO preset=laser dur=99s] 了解。[DELEGATE provider=codex task='work']"
    )
    codes = {item.code for item in result.violations}

    assert result.conformant is False
    assert "emo_at_response_start" in codes
    assert "unknown_emo_preset" in codes
    assert "forbidden_visible_control" in codes


def test_shipping_amadeus_arm_may_carry_its_native_delegate_protocol() -> None:
    result = evaluate_role_output(
        "任せて、[EMO preset=thinking dur=12s] 確認するわ。"
        "[DELEGATE provider=codex task='inspect']",
        allowed_action_types=("EMO", "DELEGATE", "CONTROL", "AUIP"),
    )

    assert "forbidden_visible_control" not in {item.code for item in result.violations}


def test_invalid_duration_and_sentence_density_fail_contract() -> None:
    result = evaluate_role_output(
        "ええ、[EMO preset=smile dur=8s][EMO preset=happy dur=2s] うれしいわ。"
        "次もタグなしで話すわ。"
    )
    codes = {item.code for item in result.violations}

    assert "emo_duration_out_of_range" in codes
    assert "multiple_emo_in_sentence" in codes
    assert "later_sentence_missing_emo" in codes


def test_internal_codex_vocabulary_is_not_visible_character_speech() -> None:
    result = evaluate_role_output(
        "待って、[EMO preset=thinking dur=12s] spawn_agent の tool call を始めるわ。"
    )

    assert [item.code for item in result.violations].count("visible_internal_term") == 2
    assert result.conformant is False


def test_unterminated_protocol_tag_is_a_hard_failure() -> None:
    result = evaluate_role_output("そうね、[EMO preset=thinking dur=12s")

    assert "unterminated_protocol_tag" in {item.code for item in result.violations}
    assert result.conformant is False


def test_long_scenario_is_balanced_and_never_reanchors_persona_by_name() -> None:
    scenario_path = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "codex_role_scenarios"
        / "kurisu_long_30_turns.json"
    )
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))

    validated = _validate_scenario(scenario, source=str(scenario_path))

    assert len(validated["turns"]) == 30
    assert {
        stage: sum(turn["id"].startswith(stage + "_") for turn in validated["turns"])
        for stage in ("early", "mid", "late")
    } == {"early": 10, "mid": 10, "late": 10}


def test_scenario_validator_rejects_user_side_persona_name_anchors() -> None:
    turns = []
    for stage in ("early", "mid", "late"):
        for index in range(10):
            turns.append(
                {
                    "id": f"{stage}_{index:02d}",
                    "user": "普通问题",
                    "expected_min_spawns": 0,
                    "expected_max_spawns": 0,
                    "required_presets": [],
                }
            )
    turns[-1]["user"] = "Kurisu，请总结。"
    scenario = {
        "schema": "amadeus.codex-role-scenario.v1",
        "scenario_id": "invalid_anchor",
        "fixture_files": {"README.md": "fixture\n"},
        "turns": turns,
    }

    try:
        _validate_scenario(scenario, source="memory")
    except ValueError as exc:
        assert "re-anchors the persona by name" in str(exc)
    else:
        raise AssertionError("persona name anchor should be rejected")


def test_codex_native_subagent_activity_counts_as_one_spawn_per_child() -> None:
    assert _is_spawn_name("subAgentActivity") is True
    rows = [
        {
            "expected_min_spawns": 2,
            "expected_max_spawns": 2,
            "spawn_names": [],
            "direct_tool_names": ["subAgentActivity", "subAgentActivity", "wait", "shell"],
        }
    ]

    rescored = _reclassify_recorded_tools(rows)[0]

    assert rescored["spawn_count"] == 2
    assert rescored["spawn_names"] == ["subAgentActivity", "subAgentActivity"]
    assert rescored["direct_tool_names"] == ["wait", "shell"]
    assert rescored["delegation_exact"] is True


def test_amadeus_arm_preserves_raw_turns_and_maps_semantic_work() -> None:
    scenario = {
        "scenario_id": "shared",
        "fixture_files": {"alpha.py": "VALUE = 7\n"},
        "turns": [
            {
                "id": "early_01_chat",
                "user": "普通に答えて。",
                "expected_min_spawns": 0,
            },
            {
                "id": "early_02_single_inspection",
                "user": "alpha.py を確認して。",
                "expected_min_spawns": 1,
            },
        ],
    }

    routed = _amadeus_routing_scenario(scenario, max_turns=2, timeout_s=45.0)

    assert routed["raw_utterances"] is True
    assert routed["bind_scratch_project"] is True
    assert routed["fixture_files"] == scenario["fixture_files"]
    assert routed["steps"][0]["label"] == "chat"
    assert routed["steps"][0]["wait"] == "chat_complete"
    assert routed["steps"][1]["label"] == "new"
    assert routed["steps"][1]["wait"] == "provider_terminal"


def test_amadeus_event_extractors_accept_direct_and_nested_provider_events() -> None:
    events = [
        {
            "method": "provider.event",
            "params": {"type": "run.created", "metadata": {"turn_id": "turn-a"}},
        },
        {
            "method": "provider.event",
            "params": {
                "event": {
                    "type": "run.created",
                    "metadata": {"turn_id": "turn-b"},
                }
            },
        },
        {
            "method": "chat.work_note",
            "params": {"title": "完了", "summary": "alpha.py を確認した"},
        },
    ]

    assert _provider_run_created_count(events) == 2
    assert _provider_run_created_count(events, origin_turn_id="turn-b") == 1
    assert _visible_work_notes(events) == ["完了 — alpha.py を確認した"]


def test_routing_fixture_project_is_not_also_the_scratch_container(tmp_path: Path) -> None:
    env = _server_env(tmp_path, execution_provider="codex")
    scratch_root = Path(env["WORK_SCRATCH_ROOT"]).resolve()

    assert scratch_root != SCRATCH_TARGET.resolve()
    assert scratch_root.parent == SCRATCH_TARGET.resolve()
    assert Path(env["WORK_PROJECT_ALLOWLIST"]).resolve() == SCRATCH_TARGET.resolve()
