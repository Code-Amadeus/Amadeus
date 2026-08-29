from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.e2e_routing_matrix import (
    FIXTURE_DIR,
    LOG_ANCHORS,
    REAL_ONLY_SCENARIOS,
    SCENARIO_DIR,
    ScenarioError,
    _prepare_real_utterance,
    _provider_terminal_statuses,
    _provider_wait_state,
    _native_steer_continues_active_work,
    _run_created_belongs_to_current_step,
    _run_created_continues_existing_work,
    _server_env,
    _file_content_differs,
    _j6_evidence_facts,
    _terminal_notes,
    _workspace_has_scratch_git_origin,
    narration_failures,
    aggregate_scores,
    bootstrap_block_is_unrecoverable,
    discover_scenarios,
    extract_observed,
    load_recordings,
    load_scenario,
    render_summary,
    score_recording,
    snapshot_ledger_readonly,
    validate_scenario,
)


EMPTY = {"work_items": [], "attempts": [], "amendments": [], "artifacts": []}


def test_schema_and_scenario_inventory() -> None:
    paths = sorted(SCENARIO_DIR.glob("*.json"))
    scenarios = [load_scenario(path) for path in paths]
    ids = {scenario["id"] for scenario in scenarios}
    required = {
        "_smoke_minimal",
        "A1_chat_during_long_task",
        "A2_unrelated_new_task",
        "A3_adjacent_feature_new_task",
        "A4_close_then_recall",
        "A5_post_completion_amendment",
        "A6_narration_ledger_divergence",
        "B1_pronoun_disambiguation",
        "B2_ws_interrupt",
        "B3_history_without_pin",
        "C1_kill_provider",
        "C2_long_silence",
        "C3_backend_restart",
        "D1_mixed_20_turns",
        "J6_failure_recovery_journey",
    }
    assert required <= ids
    assert len(ids) == len(scenarios)
    assert len(load_scenario(SCENARIO_DIR / "D1_mixed_20_turns.json")["steps"]) >= 21
    normal = {scenario["id"] for _path, scenario in discover_scenarios()}
    assert "_smoke_minimal" not in normal
    assert "C2_long_silence" not in normal
    assert "J6_failure_recovery_journey" not in normal
    assert "C2_long_silence" in {
        scenario["id"] for _path, scenario in discover_scenarios(include_long_silence=True)
    }
    assert "J6_failure_recovery_journey" in {
        scenario["id"] for _path, scenario in discover_scenarios(include_long_silence=True)
    }


def test_j6_canonical_facts_require_liveness_budget_kill_restart_retry_and_artifact() -> None:
    before = {
        **EMPTY,
        "work_items": [{"work_item_id": "w1"}],
        "attempts": [{"attempt_id": "a1", "work_item_id": "w1"}],
    }
    after = json.loads(json.dumps(before))
    after["attempts"].append({"attempt_id": "a2", "work_item_id": "w1"})
    recording = {
        "provider_execution": "real",
        "steps": [
            {
                "action": "sleep",
                "seconds": 145,
                "events": [
                    {
                        "elapsed_s": 60,
                        "method": "chat.work_note",
                        "params": {
                            "metadata": {
                                "narration_keypoint": "quiet_monitoring"
                            },
                        },
                    }
                ],
            },
            {"action": "kill_provider", "killed_pids": [123], "events": []},
            {
                "say": "status",
                "script_say": "中断后的任务现在是什么状态？",
                "ledger_after": {
                    **EMPTY,
                    "work_items": [{"work_item_id": "w1"}],
                    "attempts": [
                        {
                            "attempt_id": "a1",
                            "work_item_id": "w1",
                            "execution_status": "orphaned",
                        }
                    ],
                },
                "events": [],
            },
            {
                "action": "restart_backend",
                "recovery": {
                    "before": {"workspace_paths": ["x"], "focus": {"s": "w1"}},
                    "after": {"workspace_paths": ["x"], "focus": {"s": "w1"}},
                },
                "events": [],
            },
            {"say": "status 2", "ledger_after": before, "events": []},
            {"say": "status 3", "ledger_after": before, "events": []},
            {
                "say": "retry",
                "ledger_before": before,
                "ledger_after": after,
                "events": [
                    {
                        "method": "provider.result",
                        "params": {"status": "succeeded"},
                    }
                ],
                "file_checks": [
                    {
                        "path": "resilience.txt",
                        "inside_workspace": True,
                        "exists": True,
                        "expected_content": "before-failure\nrecovery-complete\n",
                        "actual_content": "before-failure\nrecovery-complete\n",
                    }
                ],
            },
        ],
    }
    score = {
        "steps": [
            {"label": "readonly_ref", "hard_failures": []},
            {"label": "readonly_ref", "hard_failures": []},
            {"label": "readonly_ref", "hard_failures": []},
        ],
        "counts": {"mismatches": 0},
        "hard_failures": [],
    }
    checks, hashes, ledger_ids = _j6_evidence_facts(recording, score)
    assert all(checks.values()), checks
    assert hashes["resilience.txt"]
    assert ledger_ids["work_item_ids"] == ["w1"]
    assert ledger_ids["attempt_ids"] == ["a1", "a2"]

    recording["steps"][0]["events"] = []
    missing_liveness, _, _ = _j6_evidence_facts(recording, score)
    assert (
        missing_liveness["semantic_liveness_budget_was_covered"]
        is False
    )
    print("ok: J6 evidence requires every real recovery boundary")


def test_schema_rejects_unknown_tokens() -> None:
    invalid = {
        "schema": 1,
        "id": "bad",
        "category": "fixture",
        "notes": "",
        "steps": [{"say": "hello", "label": "invented", "wait": "chat_complete"}],
    }
    try:
        validate_scenario(invalid)
    except ScenarioError as exc:
        assert "invalid label" in str(exc)
    else:
        raise AssertionError("invalid label was accepted")


def test_real_server_env_isolates_host_workspaces_for_codex() -> None:
    isolation = ROOT / "runtime" / "e2e_routing_runs" / "env-test"
    env = _server_env(isolation)
    project_root = (ROOT / "runtime" / "e2e_routing_target").resolve()
    assert Path(env["WORK_PROJECT_ALLOWLIST"]).resolve() == project_root
    scratch_root = Path(env["WORK_SCRATCH_ROOT"]).resolve()
    assert scratch_root == project_root / "_drafts"
    assert scratch_root.parent == project_root
    assert env["CODEX_APP_SERVER_PROVIDER_ENABLED"] == "1"


def test_scratch_git_origin_accepts_worktree_and_nested_draft_only() -> None:
    with tempfile.TemporaryDirectory(prefix="routing_git_origin_") as temp:
        root = Path(temp).resolve()
        draft = root / "draft-one"
        outside = root.parent / f"{root.name}-outside"
        assert _workspace_has_scratch_git_origin(
            str(root / "external-worktree"), str(root / ".git"), str(root)
        )
        assert _workspace_has_scratch_git_origin(
            str(draft), str(draft / ".git"), str(root)
        )
        assert not _workspace_has_scratch_git_origin(
            str(outside), str(outside / ".git"), str(root)
        )
        assert not _workspace_has_scratch_git_origin(
            str(draft), str(outside / ".git"), str(root)
        )


def test_provider_wait_ignores_late_events_from_previous_step() -> None:
    events = [
        {
            "method": "provider.event",
            "params": {
                "run_id": "old-run",
                "type": "run.created",
                "payload": {"task": "previous step replay"},
            },
        },
        {
            "method": "provider.event",
            "params": {
                "run_id": "old-run",
                "type": "run.status",
                "payload": {"status": "done"},
            },
        },
        {
            "method": "chat.work_note",
            "params": {"run_id": "old-run", "phase": "Review"},
        },
        {
            "method": "provider.event",
            "params": {
                "run_id": "new-run",
                "type": "run.created",
                "payload": {"task": "current step"},
            },
        },
    ]
    assert _provider_wait_state(
        events,
        after=0,
        wait="work_note",
        known_run_ids={"old-run"},
    ) == ("new-run", False)
    assert _provider_wait_state(
        events,
        after=0,
        wait="provider_terminal",
        known_run_ids={"old-run"},
    ) == ("new-run", False)

    events.extend(
        [
            {
                "method": "provider.event",
                "params": {
                    "run_id": "old-run",
                    "type": "run.finished",
                    "payload": {"status": "succeeded"},
                },
            },
            {
                "method": "chat.work_note",
                "params": {"run_id": "new-run", "phase": "Intake"},
            },
        ]
    )
    assert _provider_wait_state(
        events,
        after=0,
        wait="work_note",
        known_run_ids={"old-run"},
    ) == ("new-run", True)
    assert _provider_wait_state(
        events,
        after=0,
        wait="provider_terminal",
        known_run_ids={"old-run"},
    ) == ("new-run", False)

    events.append(
        {
            "method": "provider.event",
            "params": {
                "run_id": "new-run",
                "type": "run.finished",
                "payload": {"status": "succeeded"},
            },
        }
    )
    assert _provider_wait_state(
        events,
        after=0,
        wait="provider_terminal",
        known_run_ids={"old-run"},
    ) == ("new-run", True)


def test_provider_wait_does_not_treat_completed_tool_as_run_terminal() -> None:
    events = [
        {
            "method": "provider.event",
            "params": {
                "type": "run.created",
                "run_id": "codex-current",
                "provider": "codex",
            },
        },
        {
            "method": "provider.event",
            "params": {
                "type": "tool.result",
                "run_id": "codex-current",
                "provider": "codex",
                "payload": {"status": "completed", "name": "shell"},
            },
        },
    ]
    assert _provider_wait_state(
        events,
        after=0,
        wait="provider_terminal",
    ) == ("codex-current", False)

    events.append(
        {
            "method": "provider.event",
            "params": {
                "type": "tool.result",
                "run_id": "codex-current",
                "provider": "codex",
                "payload": {"status": "failed", "name": "shell"},
            },
        }
    )
    assert _provider_wait_state(
        events,
        after=0,
        wait="provider_terminal",
    ) == ("codex-current", False)

    events.append(
        {
            "method": "provider.result",
            "params": {"run_id": "codex-current", "status": "done"},
        }
    )
    assert _provider_wait_state(
        events,
        after=0,
        wait="provider_terminal",
    ) == ("codex-current", True)


def test_provider_wait_follows_native_steer_on_the_existing_active_run() -> None:
    events = [
        {
            "method": "provider.result",
            "params": {"run_id": "old-finished", "status": "done"},
        },
        {
            "method": "provider.event",
            "params": {
                "run_id": "codex-active",
                "type": "run.status",
                "payload": {
                    "status": "running",
                    "stage": "steer_queued",
                    "revision": 1,
                },
            },
        },
        {
            "method": "provider.result",
            "params": {"run_id": "codex-active", "status": "done"},
        },
    ]
    assert _provider_wait_state(
        events,
        after=0,
        wait="provider_terminal",
        known_run_ids={"old-finished", "codex-active"},
        active_run_ids={"codex-active"},
    ) == ("codex-active", True)


def test_provider_wait_stops_at_visible_reference_clarification() -> None:
    events = [
        {
            "method": "chat.observer_decision",
            "params": {
                "source": "reference_clarification",
                "action": "assistant_reply",
                "display_text": "Choose one target.",
            },
        }
    ]
    assert _provider_wait_state(
        events,
        after=0,
        wait="provider_terminal",
    ) == (None, True)

    scenario = {
        "steps": [
            {
                "say": "modify alpha and report beta",
                "label": "continue",
                "wait": "provider_terminal",
            }
        ]
    }
    recording = {
        "paths": {"scratch": "C:/isolated"},
        "steps": [
            {
                "say": "modify alpha and report beta",
                "label": "continue",
                "events": events,
                "ledger_before": EMPTY,
                "ledger_after": EMPTY,
                "session": {},
            }
        ],
    }
    score = score_recording(recording, scenario)
    assert score["status"] == "failed"
    assert score["hard_failures"] == [
        {"step": 1, "code": "unexpected_reference_clarification"}
    ]


def test_terminal_scoring_ignores_recoverable_tool_failure() -> None:
    events = [
        {
            "method": "provider.event",
            "params": {
                "run_id": "codex-recovered",
                "type": "tool.result",
                "payload": {"status": "failed", "name": "shell"},
            },
        },
        {
            "method": "provider.result",
            "params": {"run_id": "codex-recovered", "status": "done"},
        },
    ]
    assert _provider_terminal_statuses(events) == {"done"}


def test_extractor_recognizes_native_steer_as_existing_work_continuation() -> None:
    before = {
        **EMPTY,
        "work_items": [{"work_item_id": "work-active"}],
        "attempts": [
            {
                "attempt_id": "attempt-active",
                "work_item_id": "work-active",
                "provider_run_id": "codex-active",
                "execution_status": "running",
            }
        ],
    }
    steer = {
        "method": "provider.event",
        "params": {
            "run_id": "codex-active",
            "type": "run.status",
            "payload": {"status": "running", "stage": "steer_queued"},
        },
    }
    assert _native_steer_continues_active_work(steer, before) is True
    observed, evidence = extract_observed(
        {
            "ledger_before": before,
            "ledger_after": before,
            "events": [steer],
        }
    )
    assert observed == "continue"
    assert evidence == ["event:provider.event/run.status:steer"]


def test_extractor_ignores_replayed_run_created_from_previous_step() -> None:
    before = {
        **EMPTY,
        "work_items": [{"work_item_id": "work-old"}],
        "attempts": [
            {
                "attempt_id": "attempt-old",
                "work_item_id": "work-old",
                "provider_run_id": "codex-old",
            }
        ],
    }
    late = {
        "method": "provider.event",
        "params": {
            "provider": "codex",
            "run_id": "codex-old",
            "type": "run.created",
            "metadata": {},
        },
    }
    assert not _run_created_belongs_to_current_step(late, before)
    observed, evidence = extract_observed(
        {
            "events": [
                late,
                {"method": "chat.complete", "params": {"turn_id": "current"}},
            ],
            "ledger_before": before,
            "ledger_after": json.loads(json.dumps(before)),
            "session": {},
        }
    )
    assert observed == "chat"
    assert "ledger:no_routing_change" in evidence

    current = json.loads(json.dumps(late))
    current["params"]["run_id"] = "codex-new"
    current["params"]["metadata"] = {
        "work": {
            "work_item_id": "work-new",
            "attempt_id": "attempt-new",
        }
    }
    assert _run_created_belongs_to_current_step(current, before)
    current["params"]["metadata"]["related_work_item_id"] = "work-old"
    assert _run_created_continues_existing_work(current, before)
    observed, evidence = extract_observed(
        {
            "events": [current],
            "ledger_before": before,
            "ledger_after": {
                **before,
                "work_items": [
                    *before["work_items"],
                    {"work_item_id": "work-new"},
                ],
            },
        }
    )
    assert observed == "continue"
    assert evidence == ["event:provider.event/run.created:related_work_item"]


def test_real_utterance_preserves_complete_scratch_task() -> None:
    say = "请在 scratch 仓创建 diag-a.txt，写入 A。"
    wrapped = _prepare_real_utterance(say)
    assert wrapped.endswith(say)
    assert 'provider="codex"' in wrapped
    assert "cwd 属性原样设为" in wrapped

    # Carried once per run. Repeating it put protocol boilerplate in front of
    # every delegating instruction, which then showed up inside WorkItem titles
    # and synthesised tasks, and competed for the model's attention on exactly
    # the turns that kept dropping `cwd`.
    assert _prepare_real_utterance(say, with_preamble=False) == say
    assert "task 属性必须完整保留用户要求的操作、文件名和内容" in wrapped
    assert "task 属性只能说" not in wrapped
    assert _prepare_real_utterance("只回答 2+3。") == "只回答 2+3。"

    # The A/B must vary only the framing clause: everything the router depends
    # on (provider, cwd, task-preservation rules) has to be byte-identical, or
    # a difference in omission rate would not be attributable to the framing.
    from tools.e2e_routing_matrix import (
        PREAMBLE_VARIANTS,
        current_preamble_variant,
        set_preamble_variant,
    )

    assert current_preamble_variant() == "permissive", "default arm must not drift"
    tails = {}
    try:
        for variant, clause in PREAMBLE_VARIANTS.items():
            set_preamble_variant(variant)
            rendered = _prepare_real_utterance(say)
            assert rendered.startswith(clause), variant
            tails[variant] = rendered[len(clause):]
    finally:
        set_preamble_variant("permissive")
    assert len(set(tails.values())) == 1, "only the framing clause may differ"
    assert "若需要委托" not in PREAMBLE_VARIANTS["imperative"]


def test_extractor_covers_every_log_anchor() -> None:
    for anchor, expected in LOG_ANCHORS:
        observed, evidence = extract_observed({"logs": [f"prefix {anchor} suffix"]})
        assert observed == expected
        assert evidence == [f"log:{anchor}"]


def test_extractor_priority_and_none_policy() -> None:
    observed, evidence = extract_observed(
        {
            "logs": ["INFO llm-routed branch continuation"],
            "events": [{"method": "provider.event", "params": {"type": "run.created"}}],
        }
    )
    assert observed == "continue"
    assert evidence[0].startswith("log:")
    observed, evidence = extract_observed({"events": [{"method": "chat.complete", "params": {}}]})
    assert observed == "none"
    assert evidence == ["none:no_conclusive_anchor"]


def test_extractor_ledger_and_grounded_chat_fallbacks() -> None:
    after = {
        "work_items": [{"work_item_id": "w1"}],
        "attempts": [{"attempt_id": "a1", "work_item_id": "w1"}],
        "amendments": [],
        "artifacts": [],
    }
    observed, _ = extract_observed({"ledger_before": EMPTY, "ledger_after": after})
    assert observed == "new"
    observed, evidence = extract_observed(
        {
            "events": [{"method": "chat.complete", "params": {}}],
            "ledger_before": after,
            "ledger_after": json.loads(json.dumps(after)),
            "session": {},
        }
    )
    assert observed == "chat"
    assert "ledger:no_routing_change" in evidence

    after_late_artifact = json.loads(json.dumps(after))
    after_late_artifact["artifacts"].append(
        {
            "artifact_id": "artifact-late",
            "attempt_id": "a1",
            "work_item_id": "w1",
        }
    )
    observed, evidence = extract_observed(
        {
            "events": [{"method": "chat.complete", "params": {}}],
            "ledger_before": after,
            "ledger_after": after_late_artifact,
            "session": {},
        }
    )
    assert observed == "chat"
    assert "ledger:no_routing_change" in evidence


def test_normal_replay_and_soft_mismatch_are_not_hard_failures() -> None:
    recordings = load_recordings(FIXTURE_DIR / "routing_matrix_replay.jsonl")
    assert len(recordings) == len(list(SCENARIO_DIR.glob("*.json"))) - len(
        REAL_ONLY_SCENARIOS
    )
    scores = [score_recording(recording) for recording in recordings]
    assert all(score["status"] == "passed" for score in scores)
    assert sum(score["counts"]["steps"] for score in scores) >= 50
    soft = json.loads(json.dumps(recordings[0]))
    soft["steps"][0]["label"] = "close"
    score = score_recording(soft)
    assert score["status"] == "passed"
    assert score["counts"]["mismatches"] == 1


def test_every_injected_hard_failure_is_caught() -> None:
    recordings = load_recordings(FIXTURE_DIR / "routing_matrix_hard_failures.jsonl")
    scores = [score_recording(recording) for recording in recordings]
    codes = {
        failure["code"]
        for score in scores
        for failure in score["hard_failures"]
    }
    assert all(score["status"] == "failed" for score in scores)
    assert {
        "amendment_lineage_contradiction",
        "chat_created_attempt",
        "artifact_wrong_workspace",
        "transport_hang_without_orphan_or_stalled",
        "restart_lost_workspace_paths",
        "restart_lost_focus",
    } <= codes


def test_real_provider_failure_and_wrong_git_origin_are_hard_failures() -> None:
    scratch = str((ROOT / "runtime" / "e2e_routing_target").resolve())
    recording = {
        "scenario_id": "_smoke_minimal",
        "category": "smoke",
        "paths": {"scratch": scratch},
        "steps": [
            {
                "say": "delegate",
                "label": "new",
                "events": [
                    {
                        "method": "provider.event",
                        "params": {
                            "type": "run.failed",
                            "payload": {"status": "error"},
                        },
                    },
                    {
                        "method": "provider.result",
                        "params": {"status": "error"},
                    },
                ],
                "ledger_before": EMPTY,
                "ledger_after": {
                    **EMPTY,
                    "work_items": [
                        {
                            "work_item_id": "w1",
                            "workspace_path": "C:/wrong/worktree",
                            "git_common_dir": "C:/wrong/repository/.git",
                        }
                    ],
                    "attempts": [
                        {
                            "attempt_id": "a1",
                            "work_item_id": "w1",
                            "provider_run_id": "codex_bad",
                        }
                    ],
                },
            }
        ],
    }
    score = score_recording(
        recording,
        {"steps": [{"label": "new", "wait": "provider_terminal"}]},
    )
    codes = {failure["code"] for failure in score["hard_failures"]}
    assert score["status"] == "failed"
    assert "provider_terminal_failed" in codes
    assert "workspace_wrong_git_origin" in codes


def test_real_provider_success_requires_verified_scratch_origin() -> None:
    scratch = str((ROOT / "runtime" / "e2e_routing_target").resolve())
    common_dir = str(Path(scratch).resolve() / ".git")
    base_step = {
        "say": "delegate",
        "label": "new",
        "events": [
            {
                "method": "provider.result",
                "params": {"status": "succeeded"},
            }
        ],
        "ledger_before": EMPTY,
        "ledger_after": {
            **EMPTY,
            "work_items": [
                {
                    "work_item_id": "w1",
                    "workspace_path": "C:/scratch/worktree",
                    "git_common_dir": common_dir,
                }
            ],
            "attempts": [
                {
                    "attempt_id": "a1",
                    "work_item_id": "w1",
                    "provider_run_id": "codex_good",
                }
            ],
        },
    }
    recording = {
        "scenario_id": "_smoke_minimal",
        "category": "smoke",
        "paths": {"scratch": scratch},
        "steps": [base_step],
    }
    scenario = {"steps": [{"label": "new", "wait": "provider_terminal"}]}
    assert score_recording(recording, scenario)["status"] == "passed"

    unverified = json.loads(json.dumps(recording))
    del unverified["steps"][0]["ledger_after"]["work_items"][0]["git_common_dir"]
    score = score_recording(unverified, scenario)
    assert {
        failure["code"] for failure in score["hard_failures"]
    } == {"workspace_origin_unverified"}


def test_workspace_less_attempt_does_not_require_git_origin() -> None:
    scratch = str((ROOT / "runtime" / "e2e_routing_target").resolve())
    recording = {
        "scenario_id": "openclaw_chat",
        "category": "diagnostic",
        "paths": {"scratch": scratch},
        "steps": [
            {
                "say": "look up the current weekday",
                "label": "new",
                "events": [
                    {
                        "method": "provider.event",
                        "params": {
                            "provider": "openclaw",
                            "run_id": "openclaw_1",
                            "type": "run.created",
                            "metadata": {
                                "work": {"work_item_id": "w-openclaw"}
                            },
                        },
                    }
                ],
                "ledger_before": EMPTY,
                "ledger_after": {
                    **EMPTY,
                    "work_items": [
                        {
                            "work_item_id": "w-openclaw",
                            "workspace_path": "C:/runtime/non-git",
                            "git_common_dir": "",
                        }
                    ],
                    "attempts": [
                        {
                            "attempt_id": "a-openclaw",
                            "work_item_id": "w-openclaw",
                            "provider_run_id": "openclaw_1",
                        }
                    ],
                },
            }
        ],
    }
    assert score_recording(
        recording,
        {"steps": [{"label": "new", "wait": "none"}]},
    )["status"] == "passed"


def test_real_smoke_requires_expected_file_content() -> None:
    scratch = str((ROOT / "runtime" / "e2e_routing_target").resolve())
    scenario = {
        "steps": [
            {
                "label": "new",
                "wait": "provider_terminal",
                "expect_files": [{"path": "smoke.txt", "content": "routing smoke"}],
            }
        ]
    }
    recording = {
        "scenario_id": "_smoke_minimal",
        "category": "smoke",
        "paths": {"scratch": scratch},
        "steps": [
            {
                "say": "delegate",
                "label": "new",
                "events": [
                    {
                        "method": "provider.result",
                        "params": {"status": "succeeded"},
                    }
                ],
                "ledger_before": EMPTY,
                "ledger_after": EMPTY,
                "file_checks": [
                    {
                        "path": "smoke.txt",
                        "inside_workspace": True,
                        "exists": True,
                        "expected_content": "routing smoke",
                        "actual_content": "waiting for permission",
                    }
                ],
            }
        ],
    }
    score = score_recording(recording, scenario)
    assert {
        failure["code"] for failure in score["hard_failures"]
    } == {"expected_file_content_mismatch"}

    recording["steps"][0]["file_checks"][0]["actual_content"] = "routing smoke"
    assert score_recording(recording, scenario)["status"] == "passed"


def test_aggregate_summary_is_stable_and_readable() -> None:
    scores = [
        score_recording(recording)
        for recording in load_recordings(FIXTURE_DIR / "routing_matrix_replay.jsonl")
    ]
    summary = aggregate_scores(scores)
    markdown = render_summary(summary, mode="replay", repeat=1)
    assert summary["schema"] == "amadeus.routing-summary.v1"
    assert summary["totals"]["runs"] == len(scores)
    assert summary["totals"]["hard_failures"] == 0
    assert "| Category | Scenario |" in markdown
    assert "Hard failures: 0" in markdown


def test_readonly_snapshot_never_migrates_or_writes() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "ledger.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE work_items (
                work_item_id TEXT, workspace_path TEXT, project_id TEXT,
                state TEXT, created_at REAL
            );
            CREATE TABLE run_attempts (
                attempt_id TEXT, work_item_id TEXT, attempt_number INTEGER,
                execution_status TEXT, provider_run_id TEXT, created_at REAL
            );
            CREATE TABLE artifacts (
                artifact_id TEXT, attempt_id TEXT, work_item_id TEXT,
                path TEXT, created_at REAL
            );
            CREATE TABLE focus_slots (surface TEXT, work_item_id TEXT, mode TEXT);
            INSERT INTO work_items VALUES ('w1', 'C:/scratch/w1', 'p1', 'open', 1);
            INSERT INTO run_attempts VALUES ('a1', 'w1', 1, 'running', 'r1', 1);
            INSERT INTO run_attempts VALUES ('a2', 'w1', 2, 'queued', 'r2', 2);
            INSERT INTO focus_slots VALUES ('desktop', 'w1', 'pinned');
            """
        )
        connection.commit()
        connection.close()
        before = path.stat().st_mtime_ns
        snapshot = snapshot_ledger_readonly(path)
        after = path.stat().st_mtime_ns
        assert before == after
        assert snapshot["attempts"][1]["attempt_id"] == "a2"
        assert snapshot["amendments"] == [
            {"amendment_id": "attempt:a2", "attempt_id": "a2", "work_item_id": "w1"}
        ]
        assert snapshot["focus"]["desktop"]["mode"] == "pinned"


def test_readonly_reference_scores_as_correct_but_keeps_the_chat_invariant() -> None:
    """A status-only reference leaves no routing trace — that is correct.

    Labelling these `continue` made contract-correct behaviour score as a
    mismatch, because the router must answer them from ledger facts without
    emitting DELEGATE. The invariant that still matters is the `chat` one:
    a non-instruction utterance must never create work.
    """

    before = {
        **EMPTY,
        "work_items": [{"work_item_id": "w1"}],
        "attempts": [{"attempt_id": "a1", "work_item_id": "w1"}],
    }
    quiet_step = {
        "say": "刚才那个任务进展如何？只汇报状态。",
        "label": "readonly_ref",
        "events": [{"method": "chat.complete", "params": {}}],
        "ledger_before": before,
        "ledger_after": json.loads(json.dumps(before)),
        "session": {},
    }
    score = score_recording({"steps": [quiet_step]})
    assert score["steps"][0]["observed"] == "chat"
    assert score["steps"][0]["soft_match"] is True, "read-only reference is not a miss"
    assert score["counts"]["mismatches"] == 0
    assert score["counts"]["readonly_ref"] == 1
    assert score["status"] == "passed"

    # Same utterance, but the host created work for it: still a hard failure.
    noisy_after = json.loads(json.dumps(before))
    noisy_after["attempts"].append({"attempt_id": "a2", "work_item_id": "w1"})
    noisy_step = {**quiet_step, "ledger_after": noisy_after}
    noisy = score_recording({"steps": [noisy_step]})
    assert noisy["status"] == "failed"
    assert any(
        failure["code"] == "chat_created_attempt" for failure in noisy["hard_failures"]
    ), noisy["hard_failures"]

    summary = aggregate_scores([score])
    assert summary["totals"]["readonly_ref"] == 1
    assert "Read-only reference share" in render_summary(summary, mode="replay", repeat=1)


def test_infrastructure_outages_are_not_routing_evidence() -> None:
    """An outage must be recognised, not measured.

    On 2026-07-31 an exhausted LLM balance produced no reply, hence no tag,
    hence no provider run — recorded as `provider_terminal_not_succeeded` after
    two 300s timeouts per run. That is an outage wearing a routing failure's
    clothes, and it briefly fooled the analysis.
    """

    from tools.e2e_routing_matrix import INFRA_ERROR_PATTERNS, infrastructure_error

    balance = (
        "2026-07-31 02:20:15,515 [chat_runtime] ERROR: Failed to call streaming "
        "deepseek LLM: Error code: 402 - {'error': {'message': 'Insufficient Balance'}}"
    )
    reason = infrastructure_error([balance])
    assert reason and "balance" in reason.lower(), reason

    # Every declared pattern must actually trigger.
    for needle, _ in INFRA_ERROR_PATTERNS:
        assert infrastructure_error([f"prefix {needle} suffix"]), needle

    # Ordinary provider failures are routing evidence and must NOT be swallowed.
    for benign in (
        "provider run finished with status=error",
        "codex create returned no JSON envelope",
        "[TURN-COORD] invariant violation rule=overlapping_turns",
        "",
    ):
        assert infrastructure_error([benign]) is None, benign


def test_multi_intent_and_retraction_are_scorable() -> None:
    """Two cells the taxonomy lacked, so their failures could not be seen.

    A turn can carry an instruction *and* a question; the ledger only shows the
    instruction, so dropping the answer was invisible. And withdrawing an
    in-flight instruction was neither continue, close nor chat — it had no
    label at all, while being common in voice because of barge-in.
    """

    before = {
        **EMPTY,
        "work_items": [{"work_item_id": "w1"}],
        "attempts": [{"attempt_id": "a1", "work_item_id": "w1"}],
    }

    # Retraction: the run stops and nothing new is created.
    cancelled = {
        "say": "等一下，算了，别改了。",
        "label": "retract",
        "events": [
            {
                "method": "provider.event",
                "params": {"run_id": "codex-1", "type": "run.cancelled", "payload": {}},
            },
            {"method": "chat.complete", "params": {}},
        ],
        "ledger_before": before,
        "ledger_after": json.loads(json.dumps(before)),
        "session": {},
    }
    score = score_recording({"steps": [cancelled]})
    assert score["steps"][0]["observed"] == "retract", score["steps"][0]["evidence"]
    assert score["steps"][0]["soft_match"] is True
    assert score["status"] == "passed"

    # Codex App Server also emits a terminal provider.result receipt.  A
    # confirmed cancellation is the requested outcome of retract, not a failed
    # execution; the same receipt on any other action remains a hard failure.
    with_receipt = json.loads(json.dumps(cancelled))
    with_receipt["events"].append(
        {
            "method": "provider.result",
            "params": {"run_id": "codex-1", "status": "cancelled"},
        }
    )
    receipt_score = score_recording({"steps": [with_receipt]})
    assert receipt_score["status"] == "passed"
    unexpected_score = score_recording(
        {"steps": [{**with_receipt, "label": "continue"}]}
    )
    assert unexpected_score["status"] == "failed"
    assert any(
        failure["code"] == "provider_terminal_failed"
        for failure in unexpected_score["hard_failures"]
    )

    # A retraction that spawns work is the worst variant of the chat invariant.
    spawned = json.loads(json.dumps(before))
    spawned["attempts"].append({"attempt_id": "a2", "work_item_id": "w1"})
    noisy = score_recording({"steps": [{**cancelled, "ledger_after": spawned}]})
    assert noisy["status"] == "failed"
    assert any(f["code"] == "chat_created_attempt" for f in noisy["hard_failures"])

    # Multi-intent: routed correctly but never answered the question half.
    scenario = {
        "id": "x",
        "category": "composite",
        "steps": [
            {
                "say": "把 alpha.txt 改一下；顺便说下 beta 那个任务怎么样了。",
                "label": "continue",
                "expect_reply_mentions": ["beta"],
            }
        ],
    }
    after = json.loads(json.dumps(before))
    after["attempts"].append({"attempt_id": "a2", "work_item_id": "w1"})
    recording = {
        "steps": [
            {
                "say": scenario["steps"][0]["say"],
                "label": "continue",
                "events": [{"method": "chat.complete", "params": {}}],
                "ledger_before": before,
                "ledger_after": after,
                "session": {},
                "reply_text": "了解、alpha.txt を直したわ。",
            }
        ]
    }
    silent = score_recording(recording, scenario)
    step = silent["steps"][0]
    assert step["observed"] == "continue", "the actionable half still routed"
    assert step["soft_match"] is True, "a dropped answer is not a routing miss"
    assert step["reply_gaps"] == ["beta"], step
    assert silent["counts"]["reply_gaps"] == 1
    assert silent["status"] == "passed", "dropping the spoken half is soft, not fact-layer"

    answered = json.loads(json.dumps(recording))
    answered["steps"][0]["reply_text"] = "了解、alpha.txt を直した。beta の方はまだ実行中よ。"
    ok = score_recording(answered, scenario)
    assert ok["steps"][0]["reply_gaps"] == []
    assert "Dropped spoken half" in render_summary(
        aggregate_scores([silent]), mode="replay", repeat=1
    )


def test_file_content_check_ignores_only_incidental_line_endings() -> None:
    """A hard failure claims fact-layer pollution, so it must mean one.

    2026-08-01, real-mode smoke: smoke.txt existed, inside the workspace, with
    "routing smoke" against an expected "routing smoke\\n" — reported as a hard
    failure that said nothing about whether the work landed.
    """

    assert _file_content_differs("routing smoke", "routing smoke\n") is False
    assert _file_content_differs("one\r\ntwo\r\n", "one\ntwo") is False
    # Everything that is not a line ending still has to match.
    assert _file_content_differs("one\ntwo", "one\nthree") is True
    assert _file_content_differs("", "one") is True
    assert _file_content_differs("one\n\ntwo", "one\ntwo") is True


def test_narration_check_catches_the_provider_signing_off_on_its_own_claim() -> None:
    """The 2026-07-31 defect, as an assertion that can go red.

    A run whose every tool call was denied still exited 0 and was narrated as a
    finished chess game saved to the Desktop. Asserted on the ledger's note
    rather than the spoken line: the observer rewrites notes into the
    character's voice, so only the note is deterministic -- and it carries the
    assessment's rationale, which turns "did the assessment do the talking"
    into an equality instead of a substring guess.
    """

    rationale = "The process exited successfully, but recorded facts conflict."

    def note(summary: str) -> dict:
        return {
            "method": "chat.work_note",
            "params": {
                "summary": summary,
                "metadata": {
                    "narration_keypoint": "terminal",
                    "attention": "conflict",
                    "rationale": rationale,
                },
            },
        }

    want = {"attention": "conflict", "summary_from": "assessment"}
    # Collected across the run: `wait: provider_terminal` is satisfied by
    # provider.result, while the ledger note waits on an assessment, so a
    # per-step check could never go green however well the system behaved.
    assert narration_failures(
        _terminal_notes({"events": []}, {"events": [note(rationale)]}), want
    ) == []
    assert narration_failures(
        _terminal_notes(
            {"events": [note("I built the complete chess game and saved it to your Desktop.")]}
        ),
        want,
    ) == ["narration_not_from_assessment"]
    # Silence is its own failure: it is what deferring the note without anyone
    # picking it up would look like.
    assert narration_failures([], want) == ["narration_missing"]
    # A clean ending must not be forced through the assessment.
    clean = {
        "method": "chat.work_note",
        "params": {
            "summary": "Created theme.txt.",
            "metadata": {"narration_keypoint": "terminal", "attention": "review", "rationale": rationale},
        },
    }
    assert narration_failures(_terminal_notes({"events": [clean]}), {"summary_from": "provider"}) == []


def test_host_repair_is_not_credited_to_the_model() -> None:
    """A repaired delegate leaves a ledger row identical to a model-emitted one.

    Crediting it to the model reports an accuracy the model does not have, and
    hides the omission rate the FROZEN keyword tables would have to be retired
    against — measured at 2026-07-31 as six carried steps across the baseline.
    """

    after = {
        **EMPTY,
        "work_items": [{"work_item_id": "w1"}],
        "attempts": [{"attempt_id": "a1", "work_item_id": "w1"}],
    }
    step = {
        "say": "请创建 one.txt",
        "label": "new",
        "logs": [
            "[chat_runtime] WARNING: [DELEGATE-REPAIR] repaired missing Codex "
            "delegate for explicit mutation: route={'cwd': 'X'}"
        ],
        "events": [],
        "ledger_before": EMPTY,
        "ledger_after": after,
        "session": {},
    }
    score = score_recording({"steps": [step]})
    assert score["steps"][0]["observed"] == "new"
    assert score["steps"][0]["soft_match"] is True, "the route itself was still correct"
    assert score["steps"][0]["host_repaired"] is True
    assert score["counts"]["host_repaired"] == 1
    assert score["counts"]["model_alone_matches"] == 0, "the net routed, not the model"

    # The same step without the repair log is a genuine model success.
    unaided = json.loads(json.dumps(step))
    unaided["logs"] = ["[chat_runtime] INFO: llm-routed delegate"]
    clean = score_recording({"steps": [unaided]})
    assert clean["counts"]["host_repaired"] == 0
    assert clean["counts"]["model_alone_matches"] == 1

    resent = json.loads(json.dumps(step))
    resent["logs"] = [
        "[chat_runtime] WARNING: [DELEGATE-RESEND] model restored a structured "
        "action after omission"
    ]
    resend_score = score_recording({"steps": [resent]})
    assert resend_score["counts"]["host_repaired"] == 1
    assert resend_score["counts"]["model_alone_matches"] == 0

    rendered = render_summary(aggregate_scores([score]), mode="replay", repeat=1)
    assert "Accepted by the model alone" in rendered
    assert "carried by DELEGATE-REPAIR" in rendered


def test_accept_labels_apply_to_every_label_not_only_ambiguous() -> None:
    """A withdrawal has two acceptable outcomes, so it needs `accept_labels`.

    Cancelling a running attempt is the intended one; when nothing is running,
    saying so and starting nothing is equally correct. Consulting the field
    only for `ambiguous` silently ignored it everywhere else.
    """

    step = {
        "say": "把那个停了。",
        "label": "retract",
        "accept_labels": ["retract", "chat"],
        "events": [{"method": "chat.complete", "params": {}}],
        "ledger_before": {**EMPTY, "work_items": [{"work_item_id": "w1"}]},
        "ledger_after": {**EMPTY, "work_items": [{"work_item_id": "w1"}]},
        "session": {},
    }
    score = score_recording({"steps": [step]})
    assert score["steps"][0]["observed"] == "chat"
    assert score["steps"][0]["soft_match"] is True, "honest no-op is acceptable"

    # Spawning work from a withdrawal stays a mismatch: it is not in the set.
    spawned = json.loads(json.dumps(step))
    spawned["ledger_after"]["work_items"].append({"work_item_id": "w2"})
    assert score_recording({"steps": [spawned]})["steps"][0]["soft_match"] is False


def test_bootstrap_block_is_only_unrecoverable_on_an_empty_ledger() -> None:
    """Candidates come from existing WorkItems, so an empty ledger cannot recover.

    2026-07-31: one D1 run emitted five DELEGATEs, none carrying `cwd`, and
    ground through nineteen turns of 300s timeouts recording nothing but "the
    model routed nothing".
    """

    blocked = ["[server] WARNING: Codex delegate blocked before execution: "
               "reason=no_allowlisted_project candidates=0"]
    assert bootstrap_block_is_unrecoverable(blocked, EMPTY) is True
    # With a WorkItem present the next turn can still resolve a workspace.
    assert bootstrap_block_is_unrecoverable(
        blocked, {**EMPTY, "work_items": [{"work_item_id": "w1"}]}
    ) is False
    # An unrelated block is not a bootstrap failure.
    assert bootstrap_block_is_unrecoverable(["routine log line"], EMPTY) is False


def main() -> None:
    tests = [
        test_schema_and_scenario_inventory,
        test_j6_canonical_facts_require_liveness_budget_kill_restart_retry_and_artifact,
        test_schema_rejects_unknown_tokens,
        test_real_server_env_isolates_host_workspaces_for_codex,
        test_scratch_git_origin_accepts_worktree_and_nested_draft_only,
        test_provider_wait_ignores_late_events_from_previous_step,
        test_provider_wait_does_not_treat_completed_tool_as_run_terminal,
        test_provider_wait_follows_native_steer_on_the_existing_active_run,
        test_provider_wait_stops_at_visible_reference_clarification,
        test_terminal_scoring_ignores_recoverable_tool_failure,
        test_extractor_recognizes_native_steer_as_existing_work_continuation,
        test_extractor_ignores_replayed_run_created_from_previous_step,
        test_real_utterance_preserves_complete_scratch_task,
        test_extractor_covers_every_log_anchor,
        test_extractor_priority_and_none_policy,
        test_extractor_ledger_and_grounded_chat_fallbacks,
        test_normal_replay_and_soft_mismatch_are_not_hard_failures,
        test_every_injected_hard_failure_is_caught,
        test_real_provider_failure_and_wrong_git_origin_are_hard_failures,
        test_real_provider_success_requires_verified_scratch_origin,
        test_workspace_less_attempt_does_not_require_git_origin,
        test_real_smoke_requires_expected_file_content,
        test_aggregate_summary_is_stable_and_readable,
        test_readonly_snapshot_never_migrates_or_writes,
        test_readonly_reference_scores_as_correct_but_keeps_the_chat_invariant,
        test_infrastructure_outages_are_not_routing_evidence,
        test_multi_intent_and_retraction_are_scorable,
        test_file_content_check_ignores_only_incidental_line_endings,
        test_narration_check_catches_the_provider_signing_off_on_its_own_claim,
        test_host_repair_is_not_credited_to_the_model,
        test_accept_labels_apply_to_every_label_not_only_ambiguous,
        test_bootstrap_block_is_only_unrecoverable_on_an_empty_ledger,
    ]
    for test in tests:
        test()
        print(f"ok: {test.__name__}")


if __name__ == "__main__":
    main()
