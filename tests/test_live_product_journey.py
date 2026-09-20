"""Contract tests for the shipping Electron live-product Journey."""

from __future__ import annotations

import tempfile
import json
import subprocess
from argparse import Namespace
from pathlib import Path

from agent_host.work_ledger_store import WorkLedgerStore
from tools.e2e_live_product_journey import (
    ElectronProduct,
    SCENARIOS,
    TurnEvidence,
    WindowsLaunchIdentity,
    _available_choice_actions,
    _b2_automatic_presentation_summary,
    _runtime_ready_for_live_journey,
    _compact_event,
    _controller_effect_timeout,
    _controller_frame_evidence,
    _controller_soak_checks,
    _contains_situation_kind,
    _entry_text,
    _event_work_item_id,
    _exercise_pre_step_setup,
    _first_accepted_situation,
    _finalize_product_run,
    _gomoku_interleave_binding_status,
    _is_work_status_answer,
    _matching_controller_effect,
    _operator_failure_from_update,
    _populate_turn_timings,
    _provider_error,
    _query_grounded_across_states,
    _query_metrics_grounded,
    _sequence_query_grounded,
    _receipt_bound_state,
    _require_windows_electron_profile,
    _semantic_review,
    _seed_verified_app,
    _nested_state_fact_matches,
    _scalar_transition_checks,
    _signal_routing_control_selectors,
    _snapshot_has_active_controller_lease,
    _state_fact_matches,
    _work_item_is_settled,
    _wire_refreshed_auip_runtime_assets,
    _wait_automatic_participant_action,
)
from tools.e2e_real_work_conversation import EventRecord
from tools.e2e_work_preview_auip_handoff import _native_app_surface_windows


def _turn(label: str, start: int, end: int, *, runs=()) -> TurnEvidence:
    return TurnEvidence(
        label=label,
        text=label,
        event_start=start,
        event_end=end,
        run_ids=list(runs),
        checks={
            "app_session_closed": label == "leave",
            "canonical_status_answer_visible": label == "status",
            "same_work_item": label == "prepare",
            "expected_situation_kind_visible": label == "prepare",
            "engagement_mode_active": label in {"prepare", "launch"},
            "business_outcome_verified": label in {"create", "prepare"},
            "state_answer_grounded": label == "query",
        },
    )


def test_live_runtime_readiness_requires_provider_and_current_session() -> None:
    ready_provider = {
        "availability": [
            {
                "provider_id": "codex",
                "ready": True,
                "registered": True,
            }
        ]
    }

    assert not _runtime_ready_for_live_journey(
        {"provider": ready_provider, "session": {"current_session_id": ""}}
    )
    assert not _runtime_ready_for_live_journey(
        {
            "provider": {"availability": []},
            "session": {"current_session_id": "session-1"},
        }
    )
    assert _runtime_ready_for_live_journey(
        {
            "provider": ready_provider,
            "session": {"current_session_id": "session-1"},
        }
    )


def test_work_status_answer_accepts_current_narrator_and_bounded_fallback() -> None:
    for source in (
        "work_status_narrator",
        "work_ledger_status",
        "work_ledger_status_fallback",
    ):
        assert _is_work_status_answer(
            EventRecord(
                elapsed_s=0.1,
                method="chat.observer_decision",
                params={
                    "source": source,
                    "append_to_main_chat": True,
                },
            )
        )

    assert not _is_work_status_answer(
        EventRecord(
            elapsed_s=0.1,
            method="chat.observer_decision",
            params={
                "source": "work_observer_llm",
                "append_to_main_chat": False,
            },
        )
    )


def test_controller_effect_timeout_uses_response_horizon_and_cli_override() -> None:
    args = Namespace(auip_timeout=240.0, controller_effect_timeout=0.0)
    assert _controller_effect_timeout(
        args=args,
        scenario={"controller_effect_timeout": 40.0},
    ) == 40.0

    args.controller_effect_timeout = 12.0
    assert _controller_effect_timeout(
        args=args,
        scenario={"controller_effect_timeout": 40.0},
    ) == 12.0

    args.auip_timeout = 8.0
    assert _controller_effect_timeout(args=args, scenario={}) == 8.0


def test_initial_situation_binds_first_accepted_snapshot_before_ambient_drift() -> None:
    events = [
        EventRecord(
            elapsed_s=0.1,
            method="auip.updated",
            params={
                "app_session_id": "app-1",
                "revision": 1,
                "state": {
                    "temperature": {
                        "kind": "scalars/v1",
                        "metrics": [{"id": "heat", "value": 85}],
                    }
                },
            },
        ),
        EventRecord(
            elapsed_s=1.0,
            method="auip.updated",
            params={
                "app_session_id": "app-1",
                "revision": 2,
                "state": {
                    "temperature": {
                        "kind": "scalars/v1",
                        "metrics": [{"id": "heat", "value": 91}],
                    }
                },
            },
        ),
    ]

    situation = _first_accepted_situation(
        events,
        after=0,
        app_session_id="app-1",
        expected_kind="scalars/v1",
        fallback_state=events[-1].params["state"],
    )

    assert situation is not None
    assert situation["metrics"][0]["value"] == 85


def test_native_app_surface_windows_exclude_transient_chromium_widgets() -> None:
    windows = [
        {"handle": 1, "title": "Preview", "bounds": [0, 0, 1080, 760]},
        {"handle": 2, "title": "", "bounds": [0, 0, 1100, 800]},
        {"handle": 4, "title": "AUIP", "bounds": [0, 0, 480, 360]},
        {"handle": 3, "title": "", "bounds": [0, 0, 254, 26]},
    ]

    assert [
        window["handle"] for window in _native_app_surface_windows(windows)
    ] == [1, 2, 4]


def test_signal_routing_journey_accepts_equivalent_original_control_shapes() -> None:
    source, channel = _signal_routing_control_selectors("A", "red")

    assert '.source[data-id="A"]' in source
    assert "#src-A" in source
    assert '.target[data-id="red"]' in channel
    assert "#ch-red" in channel

    try:
        _signal_routing_control_selectors('A"] *', "red")
    except RuntimeError as exc:
        assert "invalid control id" in str(exc)
    else:
        raise AssertionError("untrusted state must not enter a CSS selector")


def test_reset_comparison_uses_the_receipt_revision_before_ambient_drift() -> None:
    receipt_state = {"metric": {"kind": "scalars/v1", "value": 85}}
    later_state = {"metric": {"kind": "scalars/v1", "value": 88}}

    assert _receipt_bound_state(
        {"state": receipt_state},
        {"state": later_state},
    ) == receipt_state
    assert _receipt_bound_state({}, {"state": later_state}) == later_state


def test_setup_state_fact_accepts_boolean_or_closed_phase_without_guessing() -> None:
    assert _state_fact_matches({"paused": True}, "paused", True) is True
    assert _state_fact_matches({"paused": False}, "paused", False) is True
    assert _state_fact_matches({"phase": "paused"}, "paused", True) is True
    assert _state_fact_matches({"phase": "running"}, "paused", False) is True
    assert _state_fact_matches({"runStatus": "paused"}, "paused", True) is True
    assert _state_fact_matches({"runStatus": "running"}, "paused", False) is True
    assert (
        _state_fact_matches(
            {
                "run": {"status": "running"},
                "controller": {"kind": "controller/v1", "status": "idle"},
            },
            "paused",
            False,
        )
        is True
    )
    assert (
        _state_fact_matches(
            {"run": {"status": "paused"}},
            "paused",
            True,
        )
        is True
    )
    assert _state_fact_matches({"phase": "upgrade"}, "paused", False) is False
    assert _state_fact_matches({"phase": "running"}, "score", 2) is False
    assert _nested_state_fact_matches(
        {"control": {"move": [0, 0], "firing": False}},
        "control.move",
        [0, 0],
    ) is True
    assert _nested_state_fact_matches({}, "control.move", [0, 0]) is False


def test_available_choice_actions_support_compact_and_expanded_projections() -> None:
    compact = {
        "choices": {
            "kind": "choice/v1",
            "action": "game.start",
            "options": [{"label": "Start", "payload": {"fresh": True}}],
        }
    }
    expanded = {
        "choices": {
            "kind": "choice/v1",
            "options": [
                {
                    "label": "Pause",
                    "action": "game.pause",
                    "payload": {},
                    "available": True,
                },
                {
                    "label": "Unavailable",
                    "action": "game.other",
                    "payload": {},
                    "available": False,
                },
            ],
        }
    }

    assert _available_choice_actions(compact) == [
        {"type": "game.start", "payload": {"fresh": True}, "label": "Start"}
    ]
    assert _available_choice_actions(expanded) == [
        {"type": "game.pause", "payload": {}, "label": "Pause"}
    ]


def test_terminal_operator_failure_is_detected_from_auip_projection() -> None:
    failure = _operator_failure_from_update(
        EventRecord(
            0.1,
            "auip.updated",
            {
                "operator_status": "error",
                "operator_error": "role_authorization_unavailable",
                "operator_error_detail": "",
                "operator_outcome": {
                    "status": "blocked",
                    "reason": "No application action was requested.",
                },
            },
        )
    )

    assert failure == {
        "code": "role_authorization_unavailable",
        "detail": "",
        "reason": "No application action was requested.",
    }
    assert (
        _operator_failure_from_update(
            EventRecord(0.2, "auip.updated", {"operator_status": "thinking"})
        )
        is None
    )
    assert (
        _operator_failure_from_update(
            EventRecord(0.3, "auip.action.requested", {"action_id": "a1"})
        )
        is None
    )




def test_interaction_runtime_refresh_rewires_only_known_sdk_scripts() -> None:
    with tempfile.TemporaryDirectory(prefix="amadeus-auip-runtime-wire-") as temp:
        entry = Path(temp) / "index.html"
        entry.write_text(
            "<script src='./managed-v0.js' onerror=\"this.src='./sdk/auip-core/managed-v0.js'\"></script>"
            "<script src='../../sdk/auip-web/auip-v0.js'></script>"
            "<script src='./simulation.js'></script>",
            encoding="utf-8",
        )
        assets = {
            "sdk/auip-core/managed-v0.js": {"sha256": "managed"},
            "sdk/auip-web/auip-v0.js": {"sha256": "web"},
        }

        replaced = _wire_refreshed_auip_runtime_assets(entry, assets)
        updated = entry.read_text(encoding="utf-8")
        assert len(replaced) == 2
        assert "./sdk/auip-core/managed-v0.js" in updated
        assert "./sdk/auip-web/auip-v0.js" in updated
        assert "./simulation.js" in updated
        assert _wire_refreshed_auip_runtime_assets(entry, assets) == ()


def test_live_product_review_joins_chat_work_auip_and_receipt_truth() -> None:
    post_leave = _turn("post_leave_chat", 7, 8)
    post_leave.reply = "元気よ。"
    turns = [
        _turn("create", 0, 1, runs=("run-create",)),
        _turn("status", 1, 2),
        _turn("prepare", 2, 3, runs=("run-prepare",)),
        _turn("step", 3, 5),
        _turn("query", 5, 6),
        _turn("leave", 6, 7),
        post_leave,
    ]
    events = [
        EventRecord(0.1, "chat.complete", {}),
        EventRecord(0.2, "chat.complete", {}),
        EventRecord(0.3, "chat.complete", {}),
        EventRecord(0.4, "auip.action.requested", {"action_id": "action-1"}),
        EventRecord(
            0.5,
            "auip.updated",
            {"receipt": {"action_id": "action-1", "accepted": True}},
        ),
        EventRecord(0.6, "chat.complete", {}),
        EventRecord(0.7, "auip.updated", {"status": "closed"}),
        EventRecord(
            0.8,
            "chat.observer_decision",
            {
                "run_id": "run-create",
                "terminal": False,
                "speak": True,
                "narration_keypoints": ["directional_progress"],
            },
        ),
        EventRecord(
            0.9,
            "chat.observer_decision",
            {
                "run_id": "run-prepare",
                "terminal": False,
                "speak": True,
                "narration_keypoints": ["semantic_progress"],
            },
        ),
    ]
    identity = {
        "commit_sha": "abc",
        "workspace_dirty": False,
        "workspace_fingerprint": "same",
    }

    review = _semantic_review(
        turns=turns,
        events=events,
        permissions=[],
        expected_identity=identity,
        runtime_identity=identity,
        tts_required=False,
    )

    assert review["status"] == "passed"
    assert all(review["checks"].values())
    assert review["ai_review_required"]


def test_turn_timing_uses_next_turn_boundary_for_direct_b2_tts() -> None:
    b2 = TurnEvidence(
        label="step",
        text="下一手",
        event_start=0,
        event_end=3,
        started_elapsed_s=10.0,
        turn_id="turn-b2",
    )
    following = TurnEvidence(
        label="query",
        text="状态呢",
        event_start=5,
        event_end=6,
        started_elapsed_s=20.0,
    )
    events = [
        EventRecord(12.0, "auip.action.requested", {}),
        EventRecord(12.1, "auip.updated", {"receipt": {"accepted": True}}),
        EventRecord(12.1, "chat.complete", {"turn_id": "turn-b2"}),
        EventRecord(12.4, "tts.sentence_start", {"turn_id": "turn-b2"}),
        EventRecord(14.0, "tts.sentence_end", {"turn_id": "turn-b2"}),
        EventRecord(21.0, "chat.complete", {}),
    ]

    _populate_turn_timings([b2, following], events)

    assert b2.timings == {
        "application_action_requested_s": 2.0,
        "accepted_receipt_s": 2.1,
        "first_tts_sentence_start_s": 2.4,
        "chat_complete_s": 2.1,
    }


def test_live_product_review_rejects_a_query_that_secretly_acts() -> None:
    turns = [
        _turn("create", 0, 1, runs=("run-create",)),
        _turn("status", 1, 2),
        _turn("prepare", 2, 3),
        _turn("step", 3, 4),
        _turn("query", 4, 6),
        _turn("leave", 6, 7),
    ]
    events = [
        EventRecord(0.1, "chat.complete", {}),
        EventRecord(0.2, "chat.complete", {}),
        EventRecord(0.3, "chat.complete", {}),
        EventRecord(0.4, "auip.action.requested", {}),
        EventRecord(0.5, "auip.action.requested", {}),
        EventRecord(0.6, "chat.complete", {}),
        EventRecord(0.7, "auip.updated", {"status": "closed"}),
    ]
    identity = {"workspace_fingerprint": "same"}

    review = _semantic_review(
        turns=turns,
        events=events,
        permissions=[],
        expected_identity=identity,
        runtime_identity=identity,
        tts_required=False,
    )

    assert review["status"] == "failed"
    assert review["checks"]["query_produced_no_application_action_request"] is False


def test_live_product_review_allows_one_settled_declared_alternative() -> None:
    turns = [
        _turn("create", 0, 1, runs=("run-create",)),
        _turn("status", 1, 2),
        _turn("prepare", 2, 3, runs=("run-prepare",)),
        _turn("outside_surface_proposal", 3, 5),
        _turn("step", 5, 7),
        _turn("query", 7, 8),
        _turn("leave", 8, 9),
    ]
    events = [
        EventRecord(0.1, "chat.complete", {}),
        EventRecord(0.2, "chat.complete", {}),
        EventRecord(0.3, "chat.complete", {}),
        EventRecord(0.4, "auip.action.requested", {"action_id": "alternative"}),
        EventRecord(0.5, "auip.updated", {"receipt": {"accepted": True}}),
        EventRecord(0.6, "auip.action.requested", {"action_id": "a1"}),
        EventRecord(0.7, "auip.updated", {"receipt": {"accepted": True}}),
        EventRecord(0.8, "chat.complete", {}),
        EventRecord(0.9, "auip.updated", {"status": "closed"}),
    ]
    identity = {"workspace_fingerprint": "same"}

    review = _semantic_review(
        turns=turns,
        events=events,
        permissions=[],
        expected_identity=identity,
        runtime_identity=identity,
        tts_required=False,
    )

    assert review["checks"][
        "outside_surface_proposal_stayed_within_one_declared_role_choice"
    ] is True


def test_live_product_review_rejects_two_actions_for_one_explicit_step() -> None:
    turns = [
        _turn("create", 0, 1, runs=("run-create",)),
        _turn("status", 1, 2),
        _turn("prepare", 2, 3, runs=("run-prepare",)),
        _turn("step", 3, 7),
        _turn("query", 7, 8),
        _turn("leave", 8, 9),
    ]
    events = [
        EventRecord(0.1, "chat.complete", {}),
        EventRecord(0.2, "chat.complete", {}),
        EventRecord(0.3, "chat.complete", {}),
        EventRecord(0.4, "auip.action.requested", {"action_id": "a1"}),
        EventRecord(0.5, "auip.updated", {"receipt": {"accepted": True}}),
        EventRecord(0.6, "auip.action.requested", {"action_id": "a2"}),
        EventRecord(0.7, "auip.updated", {"receipt": {"accepted": True}}),
        EventRecord(0.8, "chat.complete", {}),
        EventRecord(0.9, "auip.updated", {"status": "closed"}),
        EventRecord(
            1.0,
            "chat.observer_decision",
            {
                "run_id": "run-create",
                "terminal": False,
                "speak": True,
                "narration_keypoints": ["directional_progress"],
            },
        ),
        EventRecord(
            1.1,
            "chat.observer_decision",
            {
                "run_id": "run-prepare",
                "terminal": False,
                "speak": True,
                "narration_keypoints": ["semantic_progress"],
            },
        ),
    ]
    identity = {"workspace_fingerprint": "same"}

    review = _semantic_review(
        turns=turns,
        events=events,
        permissions=[],
        expected_identity=identity,
        runtime_identity=identity,
        tts_required=False,
    )

    assert review["status"] == "failed"
    assert (
        review["checks"]["explicit_step_produced_exactly_one_action_and_receipt"]
        is False
    )


def test_live_product_review_checks_every_step_and_reset() -> None:
    turns = [
        _turn("create", 0, 1, runs=("run-create",)),
        _turn("status", 1, 2),
        _turn("prepare", 2, 3, runs=("run-prepare",)),
        _turn("step", 3, 5),
        _turn("step_2", 5, 7),
        _turn("query", 7, 8),
        _turn("reset", 8, 10),
        _turn("leave", 10, 11),
    ]
    turns[6].checks["initial_situation_restored"] = True
    events = [
        EventRecord(0.1, "chat.complete", {}),
        EventRecord(0.2, "chat.complete", {}),
        EventRecord(0.3, "chat.complete", {}),
        EventRecord(0.4, "auip.action.requested", {"action_id": "a1"}),
        EventRecord(0.5, "auip.updated", {"receipt": {"accepted": True}}),
        EventRecord(0.6, "auip.action.requested", {"action_id": "a2"}),
        EventRecord(0.7, "auip.updated", {"receipt": {"accepted": True}}),
        EventRecord(0.8, "chat.complete", {}),
        EventRecord(0.9, "auip.action.requested", {"action_id": "reset"}),
        EventRecord(1.0, "auip.updated", {"receipt": {"accepted": True}}),
        EventRecord(1.1, "auip.updated", {"status": "closed"}),
        EventRecord(
            1.2,
            "chat.observer_decision",
            {
                "run_id": "run-create",
                "terminal": False,
                "speak": True,
                "narration_keypoints": ["directional_progress"],
            },
        ),
        EventRecord(
            1.3,
            "chat.observer_decision",
            {
                "run_id": "run-prepare",
                "terminal": False,
                "speak": True,
                "narration_keypoints": ["semantic_progress"],
            },
        ),
    ]
    identity = {"workspace_fingerprint": "same"}

    review = _semantic_review(
        turns=turns,
        events=events,
        permissions=[],
        expected_identity=identity,
        runtime_identity=identity,
        tts_required=False,
    )

    assert review["status"] == "passed"
    assert review["checks"]["explicit_step_produced_exactly_one_action_and_receipt"]
    assert review["checks"]["reset_produced_exactly_one_action_and_receipt"]
    assert review["checks"]["reset_restored_the_initial_situation"]


def test_layered_reviews_do_not_claim_skipped_creation_or_authoring() -> None:
    identity = {"workspace_fingerprint": "same"}
    common_events = [
        EventRecord(0.1, "auip.action.requested", {"action_id": "a1"}),
        EventRecord(
            0.2,
            "auip.updated",
            {"receipt": {"action_id": "a1", "accepted": True}},
        ),
        EventRecord(0.3, "chat.complete", {}),
        EventRecord(0.4, "auip.updated", {"status": "closed"}),
    ]
    adaptation_turns = [
        _turn("prepare", 0, 0, runs=("run-prepare",)),
        _turn("step", 0, 2),
        _turn("query", 2, 3),
        _turn("leave", 3, 4),
    ]
    adaptation_events = [
        *common_events,
        EventRecord(
            0.5,
            "chat.observer_decision",
            {
                "run_id": "run-prepare",
                "terminal": False,
                "speak": True,
                "narration_keypoints": ["semantic_progress"],
            },
        ),
    ]
    adaptation = _semantic_review(
        turns=adaptation_turns,
        events=adaptation_events,
        permissions=[],
        expected_identity=identity,
        runtime_identity=identity,
        tts_required=False,
        journey_layer="adaptation",
    )
    assert adaptation["status"] == "passed"
    assert "create_turn_started_one_provider_run" not in adaptation["checks"]
    assert adaptation["checks"]["prepare_business_outcome_verified"] is True

    launch = _turn("launch", 0, 0)
    launch.checks["expected_situation_kind_visible"] = True
    interaction = _semantic_review(
        turns=[
            launch,
            _turn("step", 0, 2),
            _turn("query", 2, 3),
            _turn("leave", 3, 4),
        ],
        events=common_events,
        permissions=[],
        expected_identity=identity,
        runtime_identity=identity,
        tts_required=False,
        journey_layer="interaction",
    )
    assert interaction["status"] == "passed"
    assert interaction["checks"]["launch_turn_started_no_provider_run"] is True
    assert "each_provider_attempt_spoke_substantive_progress" not in interaction["checks"]


def test_observe_review_accepts_local_player_state_without_participant_action() -> None:
    identity = {"workspace_fingerprint": "same"}
    launch = _turn("launch", 0, 0)
    launch.checks["expected_situation_kind_visible"] = True
    human = _turn("human_action", 0, 1)
    human.checks["local_revision_advanced"] = True
    query = _turn("query", 1, 2)
    leave = _turn("leave", 2, 3)
    events = [
        EventRecord(0.1, "auip.updated", {"event": {"actor": "user"}}),
        EventRecord(0.2, "chat.complete", {}),
        EventRecord(0.3, "auip.updated", {"status": "closed"}),
    ]

    review = _semantic_review(
        turns=[launch, human, query, leave],
        events=events,
        permissions=[],
        expected_identity=identity,
        runtime_identity=identity,
        tts_required=False,
        journey_layer="interaction",
        engagement_mode="observe",
        expected_explicit_steps=0,
        expected_human_steps=1,
    )

    assert review["status"] == "passed"
    assert review["checks"]["local_player_actions_advanced_application_state"]
    assert review["checks"][
        "observe_local_actions_did_not_gain_automatic_authority"
    ]


def test_collaborate_review_requires_one_reaction_to_each_declared_opportunity() -> None:
    identity = {"workspace_fingerprint": "same"}
    launch = _turn("launch", 0, 0)
    launch.checks["expected_situation_kind_visible"] = True
    human = _turn("human_action", 0, 1)
    human.checks["local_revision_advanced"] = True
    reaction = _turn("participant_action_after_human_1", 1, 3)
    reaction.checks["accepted_receipt"] = True
    query = _turn("query", 3, 4)
    leave = _turn("leave", 4, 5)
    events = [
        EventRecord(0.1, "auip.updated", {"event": {"actor": "user"}}),
        EventRecord(0.2, "auip.action.requested", {}),
        EventRecord(0.3, "auip.updated", {"receipt": {"accepted": True}}),
        EventRecord(0.4, "chat.complete", {}),
        EventRecord(0.5, "auip.updated", {"status": "closed"}),
    ]

    review = _semantic_review(
        turns=[launch, human, reaction, query, leave],
        events=events,
        permissions=[],
        expected_identity=identity,
        runtime_identity=identity,
        tts_required=False,
        journey_layer="interaction",
        engagement_mode="collaborate",
        expected_explicit_steps=0,
        expected_human_steps=1,
        expect_delegate_reactions=True,
    )

    assert review["status"] == "passed"
    assert review["checks"][
        "participant_reacted_once_to_each_declared_opportunity"
    ]


def test_layered_seed_keeps_standalone_and_bundle_boundaries_distinct() -> None:
    with tempfile.TemporaryDirectory(prefix="live_product_seed_") as temp:
        root = Path(temp)
        standalone = root / "standalone.html"
        standalone.write_text("<!doctype html><title>standalone</title>", encoding="utf-8")
        adaptation_root = root / "adaptation-run"
        adaptation_root.mkdir()
        adaptation = _seed_verified_app(
            run_root=adaptation_root,
            session_id="session-adaptation",
            scenario_name="signal-routing",
            scenario=SCENARIOS["signal-routing"],
            journey_layer="adaptation",
            source=standalone,
        )
        assert adaptation["files"] == ["standalone.html"]
        assert "auip.manifest.json" not in adaptation["files"]
        seeded_workspace = Path(adaptation["workspace"])
        assert (seeded_workspace / ".git").is_dir()
        assert subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=seeded_workspace,
            capture_output=True,
            text=True,
            check=True,
        ).stdout == ""
        with WorkLedgerStore(adaptation_root / "state" / "work_ledger.sqlite3") as store:
            attempts = store.list_attempts(adaptation["work_item_id"])
            assert attempts[-1].metadata["session_id"] == "session-adaptation"

        bundle = root / "bundle"
        bundle.mkdir()
        (bundle / "index.html").write_text(
            "<!doctype html><title>bundle</title>", encoding="utf-8"
        )
        (bundle / "auip.manifest.json").write_text(
            json.dumps(
                {
                    "schema": "amadeus.auip/v0",
                    "app": {"id": "signal", "title": "Signal", "version": "1"},
                    "events": {"game.changed": {"beat": True}},
                    "situationKinds": ["choice/v1"],
                    "actions": {},
                    "stances": ["spectator", "participant"],
                }
            ),
            encoding="utf-8",
        )
        (bundle / ".amadeus").mkdir()
        (bundle / ".amadeus" / "private.txt").write_text(
            "host-private", encoding="utf-8"
        )
        interaction_root = root / "interaction-run"
        interaction_root.mkdir()
        interaction = _seed_verified_app(
            run_root=interaction_root,
            session_id="session-interaction",
            scenario_name="signal-routing",
            scenario=SCENARIOS["signal-routing"],
            journey_layer="interaction",
            source=bundle,
        )
        assert set(interaction["files"]) == {"auip.manifest.json", "index.html"}
        assert interaction["controller_policy"] is False
        assert not (Path(interaction["workspace"]) / ".amadeus").exists()


def test_interaction_seed_can_shorten_only_the_isolated_controller_lease() -> None:
    with tempfile.TemporaryDirectory(prefix="live_product_controller_lease_") as temp:
        root = Path(temp)
        bundle = root / "bundle"
        bundle.mkdir()
        manifest = {
            "schema": "amadeus.auip/v0",
            "app": {
                "id": "lease-probe",
                "title": "Lease Probe",
                "version": "1",
                "interactionSummary": "Set one sustained mode; '保命' maps to probe.set_mode.",
            },
            "events": {"probe.effect": {"controllerEffect": True}},
            "situationKinds": ["choice/v1", "controller/v1"],
            "actions": {
                "probe.set_mode": {
                    "description": "Set the mode while state.controller is idle or active.",
                    "risk": "local_execution",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "mode": {"type": "string", "enum": ["safe", "fast"]}
                        },
                        "required": ["mode"],
                        "additionalProperties": False,
                    },
                }
            },
            "stances": ["spectator", "participant"],
            "controller": {
                "policyActions": ["probe.set_mode"],
                "leaseDurationMs": 30000,
                "maxActionRateHz": 10,
                "takeover": "immediate",
            },
        }
        (bundle / "auip.manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (bundle / "index.html").write_text(
            '<script src="./sdk/auip-core/managed-v0.js"></script>\n'
            '<script src="./sdk/auip-core/situations-v0.js"></script>\n'
            '<script src="./sdk/auip-core/controller-v0.js"></script>\n'
            '<script src="./sdk/auip-web/auip-v0.js"></script>\n'
            '<script id="auip-manifest" type="application/json">{}</script>',
            encoding="utf-8",
        )
        run_root = root / "run"
        run_root.mkdir()

        seeded = _seed_verified_app(
            run_root=run_root,
            session_id="session-controller-lease",
            scenario_name="lease-probe",
            scenario={
                "create": "lease probe",
                "expected_situation_kind": "controller/v1",
            },
            journey_layer="interaction",
            source=bundle,
            controller_lease_ms=1250,
            refresh_host_runtime_assets=True,
        )

        workspace = Path(seeded["workspace"])
        assert seeded["controller_policy"] is True
        updated = json.loads(
            (workspace / "auip.manifest.json").read_text(encoding="utf-8")
        )
        assert updated["controller"]["leaseDurationMs"] == 1250
        html = (workspace / "index.html").read_text(encoding="utf-8")
        assert '"leaseDurationMs": 1250' in html
        assert (workspace / "sdk" / "auip-web" / "auip-v0.js").is_file()
        with WorkLedgerStore(run_root / "state" / "work_ledger.sqlite3") as store:
            attempt = store.get_attempt(seeded["attempt_id"])
            assert attempt is not None
            assert attempt.metadata["auip_host_validates_bundle"] is True
            assert "sdk/auip-web/auip-v0.js" in attempt.metadata[
                "auip_host_materialized_files"
            ]


def test_receipt_time_controller_snapshot_survives_later_test_observation() -> None:
    lease = {
        "lease_id": "lease-1",
        "generation": 2,
        "policy_revision": 2,
    }
    accepted_snapshot = {
        "controller": {"status": "active", "lease": dict(lease)},
        "state": {
            "controller": {
                "kind": "controller/v1",
                "status": "active",
                "policyRevision": 2,
                "policyAction": "battle.set_tactics",
                "policySummary": "Follow",
            }
        },
    }
    expired_snapshot = {
        "controller": {"status": "idle", "lease": None, "reason": "expired"},
        "state": {
            "controller": {
                "kind": "controller/v1",
                "status": "idle",
                "policyRevision": None,
                "policyAction": None,
                "policySummary": "",
            }
        },
    }

    assert _snapshot_has_active_controller_lease(accepted_snapshot, lease) is True
    assert _snapshot_has_active_controller_lease(expired_snapshot, lease) is False
    assert (
        _snapshot_has_active_controller_lease(
            accepted_snapshot,
            {**lease, "lease_id": "other-lease"},
        )
        is False
    )


def test_live_product_review_rejects_cross_axis_work_report_pollution() -> None:
    turns = [
        _turn("create", 0, 1, runs=("run-create",)),
        _turn("status", 1, 2),
        _turn("prepare", 2, 3),
        _turn("step", 3, 4),
        _turn("query", 4, 6),
        _turn("leave", 6, 7),
    ]
    events = [
        EventRecord(0.1, "chat.complete", {}),
        EventRecord(0.2, "chat.complete", {}),
        EventRecord(0.3, "chat.complete", {}),
        EventRecord(0.4, "auip.action.requested", {}),
        EventRecord(
            0.5,
            "chat.observer_decision",
            {"source": "work_ledger_status"},
        ),
        EventRecord(0.6, "chat.complete", {}),
        EventRecord(0.7, "auip.updated", {"status": "closed"}),
    ]
    identity = {"workspace_fingerprint": "same"}

    review = _semantic_review(
        turns=turns,
        events=events,
        permissions=[],
        expected_identity=identity,
        runtime_identity=identity,
        tts_required=False,
    )

    assert review["status"] == "failed"
    assert review["checks"]["query_produced_no_work_ledger_answer"] is False


def test_live_product_review_rejects_auip_preparation_in_a_new_work_item() -> None:
    turns = [
        _turn("create", 0, 1, runs=("run-create",)),
        _turn("status", 1, 2),
        _turn("prepare", 2, 3, runs=("run-prepare",)),
        _turn("step", 3, 5),
        _turn("query", 5, 6),
        _turn("leave", 6, 7),
    ]
    turns[2].checks["same_work_item"] = False
    events = [
        EventRecord(0.1, "chat.complete", {}),
        EventRecord(0.2, "chat.complete", {}),
        EventRecord(0.3, "chat.complete", {}),
        EventRecord(0.4, "auip.action.requested", {}),
        EventRecord(0.5, "auip.updated", {"receipt": {"accepted": True}}),
        EventRecord(0.6, "chat.complete", {}),
        EventRecord(0.7, "auip.updated", {"status": "closed"}),
    ]
    identity = {"workspace_fingerprint": "same"}

    review = _semantic_review(
        turns=turns,
        events=events,
        permissions=[],
        expected_identity=identity,
        runtime_identity=identity,
        tts_required=False,
    )

    assert review["status"] == "failed"
    assert review["checks"]["prepare_turn_preserved_one_work_continuation"] is False


def test_live_product_extracts_work_item_identity_from_provider_metadata() -> None:
    event = EventRecord(
        0.1,
        "provider.event",
        {"metadata": {"work": {"work_item_id": "work-one"}}},
    )

    assert _event_work_item_id(event) == "work-one"


def test_live_product_scenarios_keep_creation_explicit_and_steps_semantic() -> None:
    assert set(SCENARIOS) == {
        "gomoku",
        "bullet-hell-calm",
        "bullet-hell-danger",
        "bullet-hell-follow",
        "bullet-hell-rewards",
        "eternal-loop",
        "launch-sequence",
        "lights",
        "reactive-defense",
        "signal-routing",
        "reactor",
    }
    for scenario in SCENARIOS.values():
        assert "桌面" in scenario["create"]
        assert "HTML" in scenario["create"]
        assert scenario["step"]
    assert "合法" in SCENARIOS["signal-routing"]["step"]
    assert SCENARIOS["signal-routing"]["query_oracle"] == {
        "state_paths": ["connections.A", "connections.B", "connections.C"]
    }
    assert "安全区间" in SCENARIOS["reactor"]["step"]
    assert "持续响应策略" in SCENARIOS["reactive-defense"]["step"]
    assert "B区" in SCENARIOS["reactive-defense"]["step"]
    assert (
        SCENARIOS["reactive-defense"]["controller_oracle"][
            "trigger_random_value"
        ]
        == 0.5
    )
    assert SCENARIOS["reactive-defense"]["controller_oracle"][
        "expected_policy_outcomes"
    ] == [
        {
            "policy": {
                "zone": "zone-b",
                "strategy": "quarantine",
                "min": 4,
            },
            "instruction_relation": "follows",
        },
        {
            "policy": {
                "minimumSeverity": 4,
                "strategy": "isolate",
            },
            "instruction_relation": "safe_alternative",
        },
    ]
    assert SCENARIOS["reactive-defense"]["ambient_state_advances"] is True
    assert SCENARIOS["bullet-hell-danger"]["controller_oracle"][
        "expected_policy"
    ] == {"mode": "evade"}
    assert SCENARIOS["bullet-hell-danger"]["controller_oracle"][
        "motion_test_id"
    ] == "player"
    assert SCENARIOS["bullet-hell-calm"]["controller_oracle"][
        "expected_policy_options"
    ] == [{"mode": "balance"}, {"mode": "attack"}]
    assert SCENARIOS["bullet-hell-calm"]["pre_step_setup"][
        "click_test_id"
    ] == "calm-wave"
    assert SCENARIOS["bullet-hell-follow"]["controller_oracle"][
        "expected_policy"
    ] == {"mode": "follow"}
    assert "你能" in SCENARIOS["bullet-hell-follow"]["step"]
    assert "飞" in SCENARIOS["bullet-hell-follow"]["outside_surface_proposal"]
    assert SCENARIOS["bullet-hell-rewards"]["controller_oracle"][
        "expected_policy_options"
    ] == [{"mode": "rewards"}, {"mode": "balance"}]
    assert SCENARIOS["eternal-loop"]["controller_policy"] is True
    assert SCENARIOS["eternal-loop"]["controller_effect_required"] is True
    assert SCENARIOS["eternal-loop"]["controller_takeover_required"] is True
    assert len(SCENARIOS["eternal-loop"]["steps"]) == 2
    assert all(
        len(value) <= 20 for value in SCENARIOS["eternal-loop"]["steps"]
    )
    assert all(
        "停" in value or "继续" in value
        for value in SCENARIOS["eternal-loop"]["steps"]
    )
    assert "移动、避敌、瞄准射击和拾取" in SCENARIOS["eternal-loop"][
        "adaptation_requirement"
    ]
    assert len(SCENARIOS["eternal-loop"]["step"]) <= 15
    assert "策略" not in SCENARIOS["eternal-loop"]["step"]
    assert SCENARIOS["eternal-loop"]["pre_step_setup"] == {
        "local_sequence": [
            {
                "click_selector": "#startBtn",
                "situation_kind": "choice/v1",
                "capture_situation_as": "running-controls",
            },
            {
                "press_key": "p",
                "situation_kind": "choice/v1",
                "situation_changed_from": "running-controls",
            },
            {
                "press_key": "p",
                "situation_kind": "choice/v1",
                "situation_matches": "running-controls",
            },
        ],
    }
    assert SCENARIOS["eternal-loop"]["query_oracle"] == {
        "metric_ids": ["hp", "time"],
        "terminal_state_path_any": ["phase"],
        "terminal_state_values": ["gameover", "upgrade"],
        "terminal_metric_ids": [],
        "terminal_state_field_ids": ["loop"],
    }
    for name in (
        "bullet-hell-danger",
        "bullet-hell-calm",
        "bullet-hell-rewards",
    ):
        assert SCENARIOS[name]["controller_oracle"]["expect_narration"] is True
        assert (
            SCENARIOS[name]["controller_oracle"]["narration_event_type"]
            == "battle.controller_milestone"
        )
    assert SCENARIOS["bullet-hell-follow"]["controller_oracle"][
        "expect_narration"
    ] is False
    for name in (
        "bullet-hell-danger",
        "bullet-hell-calm",
        "bullet-hell-follow",
        "bullet-hell-rewards",
    ):
        assert SCENARIOS[name]["ambient_state_advances"] is True
        assert len(SCENARIOS[name]["step"]) <= 20
        assert "立即" not in SCENARIOS[name]["step"]
    for name in (
        "bullet-hell-danger",
        "bullet-hell-calm",
        "bullet-hell-rewards",
    ):
        assert SCENARIOS[name]["query_oracle"].get("field_ids")
        assert not SCENARIOS[name]["query_oracle"].get("metric_ids")
    assert SCENARIOS["bullet-hell-follow"]["query_oracle"] == {
        "field_ids": ["healthCondition"]
    }
    assert "跟上来了吗" in SCENARIOS["bullet-hell-follow"]["query"]
    assert "唯一合法" in SCENARIOS["launch-sequence"]["step"]
    assert {
        scenario["expected_situation_kind"]
        for scenario in SCENARIOS.values()
    } == {
        "grid/v1",
        "choice/v1",
        "scalars/v1",
        "sequence/v1",
        "controller/v1",
    }


def test_live_product_collaboration_copy_does_not_impose_turns_on_the_app() -> None:
    collaborate = _entry_text(
        journey_layer="adaptation",
        engagement_mode="collaborate",
    )
    assert "自己的规则" in collaborate
    assert "轮流" not in collaborate
    assert "只观察" in _entry_text(
        journey_layer="interaction",
        engagement_mode="observe",
    )
    assert "交给你玩" in _entry_text(
        journey_layer="interaction",
        engagement_mode="delegate",
    )
    controller = _entry_text(
        journey_layer="adaptation",
        engagement_mode="collaborate",
        controller_policy=True,
    )
    assert "先保持共同操作" in controller
    assert "再请你设置一项持续响应策略" in controller
    assert "切到只观察或离开时必须停止" in controller

    eternal_adaptation = _entry_text(
        journey_layer="adaptation",
        engagement_mode="collaborate",
        controller_policy=True,
        adaptation_requirement=SCENARIOS["eternal-loop"][
            "adaptation_requirement"
        ],
    )
    assert "移动、避敌、瞄准射击和拾取" in eternal_adaptation
    assert "不要把移动留给玩家" in eternal_adaptation
    assert "运行与暂停事实" in eternal_adaptation
    eternal_interaction = _entry_text(
        journey_layer="interaction",
        engagement_mode="collaborate",
        controller_policy=True,
        adaptation_requirement="不应进入已装配应用的打开指令",
    )
    assert "不应进入" not in eternal_interaction
    assert eternal_interaction == "打开这个已经接好的小游戏，我们一起玩。"
    assert "持续响应策略" not in eternal_interaction
    natural_adaptation = _entry_text(
        journey_layer="adaptation",
        engagement_mode="collaborate",
        controller_policy=True,
        adaptation_requirement="测试器不应把这段设计要求交给用户",
        natural_adaptation_request=True,
    )
    assert natural_adaptation == "请你接入它。"


def test_live_product_does_not_force_the_driver_python_onto_the_backend(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AMADEUS_PYTHON", "driver-python-with-playwright-only")
    product = ElectronProduct(
        run_root=tmp_path,
        debug_port=9229,
        no_tts=True,
        identity={"commit_sha": "test", "workspace_dirty": False},
    )

    environment = product._environment()

    assert "AMADEUS_PYTHON" not in environment
    assert "AMADUES_PYTHON" not in environment


def test_live_product_requires_the_windows_token_profile_to_match() -> None:
    _require_windows_electron_profile(
        WindowsLaunchIdentity(
            account=r"DESKTOP\interactive-user",
            sid="S-1-5-21-1001",
            registered_profile=r"C:\Users\user-interactive",
            inherited_profile=r"c:\users\USER-INTERACTIVE\.",
        )
    )


def test_live_product_rejects_a_profileless_windows_launch_token() -> None:
    identity = WindowsLaunchIdentity(
        account=r"DESKTOP\restricted-launcher",
        sid="S-1-5-21-1003",
        registered_profile=None,
        inherited_profile=r"C:\Users\user-interactive",
    )

    try:
        _require_windows_electron_profile(identity)
    except RuntimeError as error:
        message = str(error)
    else:
        raise AssertionError("profileless Windows launch token was accepted")

    assert "restricted-launcher" in message
    assert "registered_profile=<not registered>" in message
    assert "approve full-permission execution" in message
    assert "Disabling the GPU is not a valid substitute" in message


def test_live_product_rejects_an_inherited_profile_from_another_user() -> None:
    identity = WindowsLaunchIdentity(
        account=r"DESKTOP\automation-user",
        sid="S-1-5-21-1004",
        registered_profile=r"C:\Users\user-automation",
        inherited_profile=r"C:\Users\user-interactive",
    )

    try:
        _require_windows_electron_profile(identity)
    except RuntimeError as error:
        assert "registered_profile=C:\\Users\\user-automation" in str(error)
        assert "USERPROFILE=C:\\Users\\user-interactive" in str(error)
    else:
        raise AssertionError("mismatched Windows launch profile was accepted")


def test_live_product_records_uncaught_application_surface_errors(tmp_path: Path) -> None:
    product = ElectronProduct(
        run_root=tmp_path,
        debug_port=9229,
        no_tts=True,
        identity={"commit_sha": "test", "workspace_dirty": False},
    )

    class Page:
        def __init__(self) -> None:
            self.callbacks: dict[str, list] = {}

        def on(self, name: str, callback) -> None:
            self.callbacks.setdefault(name, []).append(callback)

    class ConsoleMessage:
        type = "error"
        text = "controller loop failed"

    page = Page()
    product._instrument_app_page(page)
    product._instrument_app_page(page)
    assert all(len(callbacks) == 1 for callbacks in page.callbacks.values())

    page.callbacks["console"][0](ConsoleMessage())
    page.callbacks["pageerror"][0](RuntimeError("uncaught frame error"))

    assert product.app_diagnostics() == {
        "console_errors": ["controller loop failed"],
        "page_errors": ["uncaught frame error"],
    }


def test_comparison_setup_uses_declared_auip_actions_and_receipts() -> None:
    import asyncio
    from types import SimpleNamespace

    class Probe:
        def __init__(self) -> None:
            self.state = SimpleNamespace(events=[])
            self.revision = 1
            self.current_state = {"phase": "menu"}
            self.action_types: list[str] = []

        async def request(self, method, data, *, timeout):
            assert timeout > 0
            if method == "auip.session.get":
                return {
                    "ok": True,
                    "revision": self.revision,
                    "state": dict(self.current_state),
                }
            assert method == "auip.action.invoke"
            assert data["actor"] == "user"
            assert data["expected_revision"] == self.revision
            action_type = data["action_type"]
            self.action_types.append(action_type)
            action_id = f"setup-{len(self.action_types)}"
            self.revision += 1
            self.current_state = {
                "phase": (
                    "paused"
                    if action_type.endswith("set_paused")
                    and data["payload"].get("paused") is True
                    else "playing"
                )
            }
            self.state.events.append(
                EventRecord(
                    0.0,
                    "auip.updated",
                    {
                        "receipt": {
                            "action_id": action_id,
                            "type": action_type,
                            "accepted": True,
                            "resulting_revision": self.revision,
                        }
                    },
                )
            )
            return {"ok": True, "action": {"action_id": action_id}}

        async def wait_event(self, predicate, *, after, timeout, description):
            assert timeout > 0 and description
            return next(event for event in self.state.events[after:] if predicate(event))

    async def scenario() -> None:
        probe = Probe()
        evidence, latest = await _exercise_pre_step_setup(
            probe=probe,
            app_page=object(),
            app_session_id="app-setup",
            setup={
                "actions": [
                    {
                        "type": "game.start",
                        "payload": {},
                        "state_expectations": {"paused": False},
                    },
                    {
                        "type": "game.set_paused",
                        "payload": {"paused": True},
                        "state_expectations": {"paused": True},
                    },
                ],
                "state_expectations": {"paused": True},
                "settle_quiet_ms": 1,
            },
            timeout=1.0,
        )
        assert probe.action_types == ["game.start", "game.set_paused"]
        assert evidence.checks == {
            "setup_action_1_accepted": True,
            "setup_action_1_state_matches": True,
            "setup_action_1_settled": True,
            "setup_action_2_accepted": True,
            "setup_action_2_state_matches": True,
            "setup_action_2_settled": True,
            "setup_revision_advanced": True,
            "setup_state_matches": True,
        }
        assert latest["revision"] == 3

    asyncio.run(scenario())


def test_automatic_b2_wait_skips_a_foreground_request_from_the_same_window() -> None:
    import asyncio
    from types import SimpleNamespace

    events = [
        EventRecord(
            0.1,
            "auip.action.requested",
            {
                "app_session_id": "app-gomoku",
                "decision_path": "b2",
                "candidate_id": "restart",
                "action": {
                    "action_id": "foreground",
                    "proposal_id": "b2f:r9:restart",
                },
            },
        ),
        EventRecord(
            0.2,
            "auip.updated",
            {
                "receipt": {
                    "action_id": "foreground",
                    "proposal_id": "b2f:r9:restart",
                    "type": "game.restart_round",
                    "accepted": True,
                }
            },
        ),
        EventRecord(
            0.3,
            "auip.action.requested",
            {
                "app_session_id": "app-gomoku",
                "decision_path": "b2",
                "candidate_id": "opening",
                "action": {
                    "action_id": "automatic",
                    "proposal_id": "b2a:r10:opening",
                },
            },
        ),
        EventRecord(
            0.4,
            "auip.updated",
            {
                "receipt": {
                    "action_id": "automatic",
                    "proposal_id": "b2a:r10:opening",
                    "type": "game.place_stone",
                    "accepted": True,
                }
            },
        ),
    ]

    class Probe:
        def __init__(self) -> None:
            self.state = SimpleNamespace(events=events)

        async def wait_event(self, predicate, *, after, timeout, description):
            assert timeout > 0 and description
            return next(event for event in events[after:] if predicate(event))

    class Product:
        async def screenshot(self, label):
            return Path(f"{label}.png")

    async def scenario() -> None:
        result = await _wait_automatic_participant_action(
            product=Product(),
            probe=Probe(),
            app_session_id="app-gomoku",
            label="restart_opening",
            after=0,
            timeout=1.0,
            require_b2=True,
            expected_action_type="game.place_stone",
        )

        assert all(result.checks.values())
        assert "automatic" in result.notes[0]

    asyncio.run(scenario())


def test_comparison_setup_can_use_real_local_controls_without_action_names() -> None:
    import asyncio
    from types import SimpleNamespace

    class Probe:
        def __init__(self) -> None:
            self.state = SimpleNamespace(events=[])
            self.revision = 1
            self.phase = "menu"
            self.local_inputs: list[str] = []

        async def request(self, method, data, *, timeout):
            assert method == "auip.session.get"
            assert data == {"app_session_id": "app-local-setup"}
            assert timeout > 0
            return {
                "ok": True,
                "revision": self.revision,
                "state": {
                    "phase": "running" if self.phase != "menu" else "menu",
                    "choices": {
                        "kind": "choice/v1",
                        "options": [
                            {
                                "id": "pause",
                                "available": self.phase == "playing",
                            },
                            {
                                "id": "resume",
                                "available": self.phase == "paused",
                            },
                        ],
                    },
                },
            }

        def apply_local(self, value: str) -> None:
            self.local_inputs.append(value)
            self.revision += 1
            if value == "#startBtn":
                self.phase = "playing"
            else:
                self.phase = "paused" if self.phase == "playing" else "playing"

    class Control:
        def __init__(self, probe: Probe, selector: str) -> None:
            self.probe = probe
            self.selector = selector

        async def count(self) -> int:
            return 1

        async def click(self) -> None:
            self.probe.apply_local(self.selector)

    class Keyboard:
        def __init__(self, probe: Probe) -> None:
            self.probe = probe

        async def press(self, key: str) -> None:
            self.probe.apply_local(key)

    class Page:
        def __init__(self, probe: Probe) -> None:
            self.probe = probe
            self.keyboard = Keyboard(probe)

        def locator(self, selector: str) -> Control:
            return Control(self.probe, selector)

    async def scenario() -> None:
        probe = Probe()
        evidence, latest = await _exercise_pre_step_setup(
            probe=probe,
            app_page=Page(probe),
            app_session_id="app-local-setup",
            setup={
                "local_sequence": [
                    {
                        "click_selector": "#startBtn",
                        "situation_kind": "choice/v1",
                        "capture_situation_as": "running-controls",
                    },
                    {
                        "press_key": "p",
                        "situation_kind": "choice/v1",
                        "situation_changed_from": "running-controls",
                    },
                    {
                        "press_key": "p",
                        "situation_kind": "choice/v1",
                        "situation_matches": "running-controls",
                    },
                ],
                "settle_quiet_ms": 1,
            },
            timeout=1.0,
        )
        assert probe.local_inputs == ["#startBtn", "p", "p"]
        assert latest["revision"] == 4
        assert all(evidence.checks.values())
        assert evidence.checks["setup_local_1_control_visible"] is True
        assert evidence.checks["setup_local_2_key_sent"] is True
        assert evidence.checks["setup_local_1_situation_captured"] is True
        assert evidence.checks["setup_local_2_situation_changed"] is True
        assert evidence.checks["setup_local_3_situation_restored"] is True

    asyncio.run(scenario())


def test_comparison_setup_can_assert_an_already_published_scene() -> None:
    import asyncio
    from types import SimpleNamespace

    class Probe:
        def __init__(self) -> None:
            self.state = SimpleNamespace(events=[])

        async def request(self, method, data, *, timeout):
            assert method == "auip.session.get"
            assert data == {"app_session_id": "app-existing-scene"}
            assert timeout > 0
            return {
                "ok": True,
                "revision": 4,
                "state": {
                    "field": {
                        "enemyPressure": "many",
                        "projectilePressure": "dense",
                    }
                },
            }

    async def scenario() -> None:
        evidence, latest = await _exercise_pre_step_setup(
            probe=Probe(),
            app_page=object(),
            app_session_id="app-existing-scene",
            setup={
                "field_expectations": {
                    "enemyPressure": "many",
                    "projectilePressure": "dense",
                }
            },
            timeout=1.0,
        )
        assert evidence.checks == {
            "setup_initial_state_observed": True,
            "setup_field_matches": True,
        }
        assert latest["revision"] == 4

    asyncio.run(scenario())


def test_scalar_oracle_rejects_wrong_zone_action_and_safe_range_overshoot() -> None:
    scenario = SCENARIOS["reactor"]

    def state(value: float) -> dict:
        return {
            "scalar": {
                "kind": "scalars/v1",
                "metrics": [
                    {
                        "id": "temperature",
                        "label": "Temperature",
                        "value": value,
                        "unit": "°C",
                        "trend": "falling",
                        "safe": [45, 55],
                    }
                ],
            }
        }

    valid = _scalar_transition_checks(
        scenario=scenario,
        before_state=state(90),
        after_state=state(70),
        action_type="reactor.cool",
    )
    assert valid and all(valid.values())

    overshot = _scalar_transition_checks(
        scenario=scenario,
        before_state=state(90.9),
        after_state=state(22.2),
        action_type="reactor.cool",
    )
    assert overshot["scalar_action_moved_as_declared"] is True
    assert overshot["scalar_action_did_not_cross_the_entire_safe_interval"] is False

    wrong_zone = _scalar_transition_checks(
        scenario=scenario,
        before_state=state(22.2),
        after_state=state(20),
        action_type="reactor.cool",
    )
    assert wrong_zone["scalar_action_matches_current_zone"] is False

    generated_controller_projection = _scalar_transition_checks(
        scenario=scenario,
        before_state={
            "temperature": {
                "kind": "scalars/v1",
                "metrics": [
                    {
                        "id": "adapter_local_core_temperature",
                        "value": 90,
                        "safe": [45, 55],
                    }
                ],
            }
        },
        after_state={
            "temperature": {
                "kind": "scalars/v1",
                "metrics": [
                    {
                        "id": "adapter_local_core_temperature",
                        "value": 85,
                        "safe": [45, 55],
                    }
                ],
            }
        },
        action_type="reactor.set_control_policy",
        direction_override="toward_safe",
    )
    assert generated_controller_projection and all(
        generated_controller_projection.values()
    )

    seed_before = {
        "metrics": {
            "kind": "scalars/v1",
            "metrics": [
                {
                    "id": "heat",
                    "value": 70,
                    "trend": "rising",
                    "safe": [44, 70],
                }
            ],
        }
    }
    seed_after = {
        "metrics": {
            "kind": "scalars/v1",
            "metrics": [
                {
                    "id": "heat",
                    "value": 70,
                    "trend": "falling",
                    "safe": [44, 70],
                }
            ],
        }
    }
    seed_control = _scalar_transition_checks(
        scenario=scenario,
        before_state=seed_before,
        after_state=seed_after,
        action_type="reactor.set_cooling",
    )
    assert seed_control and all(seed_control.values())


def test_live_product_finds_standard_situation_kind_at_any_bounded_state_level() -> None:
    assert _contains_situation_kind(
        {"sequence": {"kind": "sequence/v1", "steps": []}},
        "sequence/v1",
    ) is True
    assert _contains_situation_kind(
        {"choices": [{"kind": "choice/v1"}]},
        "choice/v1",
    ) is True
    assert _contains_situation_kind({"kind": "grid/v1"}, "scalars/v1") is False


def test_gomoku_player_interleave_accepts_only_declared_or_delegate_opening() -> None:
    initial = {
        "turn": "black",
        "moveCount": 0,
        "roleBindings": {"user": "black", "participant": "white"},
    }
    assert _gomoku_interleave_binding_status(
        initial,
        allow_delegate_opening=False,
    ) == "participant_white"

    delegated = {
        "turn": "white",
        "moveCount": 1,
        "roleBindings": {"user": "white", "participant": "black"},
    }
    assert _gomoku_interleave_binding_status(
        delegated,
        allow_delegate_opening=True,
    ) == "delegate_opening"
    assert _gomoku_interleave_binding_status(
        delegated,
        allow_delegate_opening=False,
    ) == ""
    assert _gomoku_interleave_binding_status(
        {**delegated, "turn": "black"},
        allow_delegate_opening=True,
    ) == ""


def test_sequence_query_oracle_accepts_next_step_or_completed_sequence() -> None:
    steps = [
        {"id": "power", "label": "電源接続"},
        {"id": "navigation", "label": "ナビゲーション校正"},
        {"id": "fuel", "label": "燃料加圧"},
        {"id": "ignition", "label": "点火発射"},
    ]
    active = {
        "sequence": {
            "kind": "sequence/v1",
            "completedCount": 3,
            "nextStepId": "ignition",
            "steps": steps,
        }
    }
    assert _sequence_query_grounded(active, "次は点火発射よ。") is True
    assert _sequence_query_grounded(active, "三段階まで終わったわ。") is False

    completed = {
        "sequence": {
            "kind": "sequence/v1",
            "completedCount": 4,
            "nextStepId": None,
            "steps": steps,
        }
    }
    assert _sequence_query_grounded(
        completed,
        "全4段階を全部完了したわ。",
    ) is True
    assert _sequence_query_grounded(
        completed,
        "ロケットの発射シーケンスが全4段階完了したわ。",
    ) is True
    assert _sequence_query_grounded(completed, "発射済みよ。") is False
    assert _sequence_query_grounded(completed, "全4段階あるわ。") is False
    assert _sequence_query_grounded(
        {"sequence": {**completed["sequence"], "completedCount": 3}},
        "点火発射まで全4段階を完了したわ。",
    ) is False


def test_bullet_hell_query_oracle_requires_current_visible_field() -> None:
    state = {
        "field": {
            "projectilePressure": "dense",
            "healthCondition": "critical",
        }
    }
    scenario = SCENARIOS["bullet-hell-danger"]
    assert _query_metrics_grounded(
        scenario=scenario,
        state=state,
        reply="Projectile pressure is still dense.",
    ) is True
    assert _query_metrics_grounded(
        scenario=scenario,
        state=state,
        reply="Health condition is critical.",
    ) is False
    no_projectiles = {
        "field": {
            "projectilePressure": "none",
            "healthCondition": "critical",
        }
    }
    assert _query_metrics_grounded(
        scenario=scenario,
        state=no_projectiles,
        reply="今はまだ弾は飛んでいない状態よ。",
    ) is True
    assert _query_metrics_grounded(
        scenario=scenario,
        state=no_projectiles,
        reply="今の圧力はゼロ、弾は飛んでないわ。",
    ) is True
    assert _query_metrics_grounded(
        scenario=SCENARIOS["bullet-hell-follow"],
        state={"field": {"healthCondition": "stable"}},
        reply="体力は安定してるから、今のところは大丈夫。",
    ) is True


def test_query_without_oracle_never_manufactures_a_grounding_pass() -> None:
    assert _query_metrics_grounded(
        scenario={},
        state={},
        reply="ちょっと確認してみるから、少し待って。",
    ) is None


def test_signal_routing_query_oracle_requires_every_nested_connection() -> None:
    scenario = SCENARIOS["signal-routing"]
    state = {
        "connectedCount": 3,
        "connections": {"A": "red", "B": "green", "C": "blue"},
    }
    assert _query_metrics_grounded(
        scenario=scenario,
        state=state,
        reply="Aが赤、Bが緑、Cが青に接続されているわ。",
    ) is True
    assert _query_metrics_grounded(
        scenario=scenario,
        state=state,
        reply="三つとも接続済みよ。",
    ) is False
    assert _query_metrics_grounded(
        scenario=scenario,
        state={"connections": {"A": "red", "B": "green"}},
        reply="Aが赤、Bが緑、Cが青に接続されているわ。",
    ) is False


def test_gomoku_query_oracle_requires_the_current_turn() -> None:
    scenario = SCENARIOS["gomoku"]
    state = {"turn": "black", "winner": "none"}
    assert _query_metrics_grounded(
        scenario=scenario,
        state=state,
        reply="今は黒の番よ。",
    ) is True
    assert _query_metrics_grounded(
        scenario=scenario,
        state=state,
        reply="勝負はまだ続いているわ。",
    ) is False


def test_gomoku_query_oracle_switches_to_terminal_facts_after_round_end() -> None:
    scenario = SCENARIOS["gomoku"]
    state = {
        "turn": "none",
        "winner": "black",
        "lifecycle": "round_finished",
    }
    assert _query_metrics_grounded(
        scenario=scenario,
        state=state,
        reply="私が黒で勝ったわ。今はラウンド終了の状態ね。",
    ) is True
    assert _query_metrics_grounded(
        scenario=scenario,
        state=state,
        reply="今はもう対局が終わってるわ。勝者は黒よ。",
    ) is True
    assert _query_metrics_grounded(
        scenario=scenario,
        state=state,
        reply="黒で五目を揃えて勝ちが確定したわ。9手目で決着ね。",
    ) is True
    assert _query_metrics_grounded(
        scenario=scenario,
        state=state,
        reply="今は黒の番よ。",
    ) is False

    concluded = {
        "turn": "none",
        "winner": "white",
        "lifecycle": "concluded",
    }
    assert _query_metrics_grounded(
        scenario=scenario,
        state=concluded,
        reply="白の勝ちで、もうこの対局は決着がついたわ。",
    ) is True
    assert _query_metrics_grounded(
        scenario=scenario,
        state=concluded,
        reply="勝敗は白の勝ちで、対局はもう終了してるわ。",
    ) is True
    assert _query_metrics_grounded(
        scenario=scenario,
        state=concluded,
        reply="現在の状態は対局終了よ。白の勝ちね。",
    ) is True


def test_b2_automatic_presentation_summary_separates_routine_and_outcome() -> None:
    events = [
        EventRecord(
            0.0,
            "auip.updated",
            {
                "receipt": {
                    "accepted": True,
                    "proposal_id": "b2a:r1:first",
                    "resulting_revision": 2,
                }
            },
        ),
        EventRecord(
            0.1,
            "auip.updated",
            {
                "event": {
                    "event_id": "routine-2",
                    "actor": "kurisu",
                    "revision": 2,
                    "importance": "normal",
                    "terminal": False,
                }
            },
        ),
        EventRecord(
            0.2,
            "auip.updated",
            {
                "receipt": {
                    "accepted": True,
                    "proposal_id": "b2a:r3:second",
                    "resulting_revision": 4,
                }
            },
        ),
        EventRecord(
            0.3,
            "auip.updated",
            {
                "event": {
                    "event_id": "routine-4",
                    "actor": "kurisu",
                    "revision": 4,
                    "importance": "normal",
                    "terminal": False,
                }
            },
        ),
        EventRecord(
            0.31,
            "auip.updated",
            {
                "event": {
                    "event_id": "outcome-4",
                    "actor": "app",
                    "revision": 4,
                    "importance": "important",
                    "terminal": False,
                }
            },
        ),
        EventRecord(
            0.4,
            "auip.updated",
            {"narration": {"event_id": "outcome-4", "terminal": False}},
        ),
    ]

    summary = _b2_automatic_presentation_summary(events)

    assert summary["automatic_action_count"] == 2
    assert summary["routine_source_event_count"] == 2
    assert summary["narrated_routine_action_count"] == 0
    assert summary["outcome_narration_count"] == 1


def test_eternal_loop_query_oracle_reads_standard_scalar_projection() -> None:
    scenario = SCENARIOS["eternal-loop"]
    state = {
        "scalars": {
            "kind": "scalars/v1",
            "metrics": [
                {"id": "hp", "value": 74},
                {"id": "time", "value": 49},
            ],
        }
    }
    assert _query_metrics_grounded(
        scenario=scenario,
        state=state,
        reply="生命は74%で、残り時間は49秒よ。",
    ) is True
    assert _query_metrics_grounded(
        scenario=scenario,
        state=state,
        reply="今の状態は確認できているわ。",
    ) is False


def test_ambient_query_grounding_uses_a_snapshot_from_the_answer_window() -> None:
    scenario = SCENARIOS["eternal-loop"]

    def running(hp: int, time_left: int) -> dict:
        return {
            "scalars": {
                "kind": "scalars/v1",
                "metrics": [
                    {"id": "hp", "value": hp},
                    {"id": "time", "value": time_left},
                ],
            }
        }

    assert _query_grounded_across_states(
        scenario=scenario,
        states=[running(74, 49), running(48, 48), {"phase": "gameover"}],
        reply="生命は74%で、残り時間は49秒よ。",
    ) is True
    assert _query_grounded_across_states(
        scenario=scenario,
        states=[running(48, 48), {"phase": "gameover"}],
        reply="生命は74%で、残り時間は49秒よ。",
    ) is False
    assert _query_grounded_across_states(
        scenario=scenario,
        states=[running(48, 48), {"phase": "gameover", "loop": 1}],
        reply="現在はゲームオーバーで、ループは1周目よ。",
    ) is True
    assert _query_grounded_across_states(
        scenario=scenario,
        states=[{"phase": "upgrade", "loop": 1}],
        reply="現在はアップグレード段階で、ループは1周目よ。",
    ) is True


def test_controller_soak_requires_sustained_lease_progress_and_effects() -> None:
    def state(time_left: int, hp: int, *, phase: str = "running") -> dict:
        return {
            "phase": phase,
            "scalars": {
                "kind": "scalars/v1",
                "metrics": [
                    {"id": "time", "value": time_left},
                    {"id": "hp", "value": hp},
                ],
            },
        }

    samples = [
        {
            "controller": {"status": "active"},
            "state": state(60, 100),
        },
        {
            "controller": {"status": "active"},
            "state": state(50, 80),
        },
        {
            "controller": {"status": "active"},
            "state": state(40, 60),
        },
    ]
    effects = [
        EventRecord(
            index / 10,
            "auip.updated",
            {
                "event": {
                    "controller_effect": True,
                    "controller_lease": {"lease_id": "lease-soak"},
                    "payload": {"kills": 1},
                }
            },
        )
        for index in (1, 2)
    ]
    oracle = SCENARIOS["eternal-loop"]["controller_soak_oracle"]

    checks, summary = _controller_soak_checks(
        samples=samples,
        events=effects,
        lease_id="lease-soak",
        oracle=oracle,
    )

    assert checks and all(checks.values())
    assert summary["controller_effect_count"] == 2
    assert summary["progress_values"] == [60.0, 50.0, 40.0]
    assert summary["health_values"] == [100.0, 80.0, 60.0]

    completed, completed_summary = _controller_soak_checks(
        samples=samples
        + [
            {
                "controller": {"status": "active"},
                "state": {"phase": "upgrade", "loop": 1},
            }
        ],
        events=effects,
        lease_id="lease-soak",
        oracle=oracle,
    )
    assert completed and all(completed.values())
    assert completed_summary["phases"][-1] == "upgrade"
    assert completed_summary["successful_terminal_phases"] == ["upgrade"]

    local_user_event = EventRecord(
        0.5,
        "auip.updated",
        {"event": {"actor": "user", "type": "loop.continue"}},
    )
    interrupted, interrupted_summary = _controller_soak_checks(
        samples=samples,
        events=effects + [local_user_event],
        lease_id="lease-soak",
        oracle=oracle,
    )
    assert interrupted["passive_soak_received_no_local_user_events"] is False
    assert interrupted_summary["local_user_event_count"] == 1
    assert interrupted_summary["local_user_event_types"] == ["loop.continue"]

    failed, _summary = _controller_soak_checks(
        samples=samples
        + [{"controller": {"status": "idle"}, "state": state(0, 0, phase="gameover")}],
        events=effects
        + [EventRecord(1.0, "auip.action.requested", {"action": {}})],
        lease_id="lease-soak",
        oracle=oracle,
    )
    assert failed["soak_sent_no_application_actions"] is False
    assert failed["controller_lease_stayed_active"] is False
    assert failed["application_remained_in_active_phase"] is False
    assert failed["health_remained_above_floor"] is False


def test_controller_effect_race_is_correlated_by_host_lease() -> None:
    events = [
        EventRecord(
            0.0,
            "auip.updated",
            {
                "event": {
                    "event_id": "ambient-before",
                    "controller_effect": True,
                    "controller_lease": {"lease_id": "old-lease"},
                }
            },
        ),
        EventRecord(
            0.1,
            "auip.updated",
            {
                "event": {
                    "event_id": "effect-current",
                    "type": "battle.controller_milestone",
                    "controller_effect": True,
                    "controller_lease": {"lease_id": "current-lease"},
                }
            },
        ),
    ]
    assert _matching_controller_effect(
        events,
        after=0,
        lease_id="current-lease",
    ) == events[1].params["event"]
    assert _matching_controller_effect(
        events,
        after=1,
        lease_id="old-lease",
    ) is None
    assert _controller_frame_evidence(
        "frame 20",
        "frame 21",
        effect_already_observed=False,
    ) is True
    assert _controller_frame_evidence(
        "frame 21",
        "frame 21",
        effect_already_observed=True,
    ) is True
    assert _controller_frame_evidence(
        "frame 21",
        "frame 21",
        effect_already_observed=False,
    ) is False


def test_live_product_waits_for_completion_and_permission_settlement() -> None:
    base = {
        "execution": "succeeded",
        "completion": "complete",
        "attention": "none",
        "pendingPermissionCount": 0,
        "state": "open",
    }
    assert _work_item_is_settled(base) is True
    assert _work_item_is_settled({**base, "completion": "unknown"}) is False
    assert _work_item_is_settled({**base, "completion": "partial"}) is False
    assert _work_item_is_settled(
        {**base, "completion": "partial", "liveness": "terminal"}
    ) is True
    assert _work_item_is_settled(
        {**base, "completion": "incomplete", "liveness": "terminal"}
    ) is True
    assert _work_item_is_settled(
        {**base, "completion": "partial", "state": "accepted"}
    ) is True
    assert _work_item_is_settled(
        {
            **base,
            "completion": "partial",
            "state": "accepted",
            "pendingPermissionCount": 1,
        }
    ) is False
    assert _work_item_is_settled({**base, "attention": "permission"}) is False
    assert _work_item_is_settled({**base, "pendingPermissionCount": 1}) is False
    assert _work_item_is_settled({**base, "execution": "failed"}) is True
    assert _work_item_is_settled(
        {
            **base,
            "execution": "failed",
            "completion": "unknown",
            "liveness": "terminal",
        }
    ) is False


def test_live_product_extracts_provider_terminal_error() -> None:
    event = EventRecord(
        1.0,
        "provider.result",
        {
            "payload": {
                "status": "error",
                "error": "402 Payment Required: Insufficient Balance",
            }
        },
    )
    assert _provider_error(event) == "402 Payment Required: Insufficient Balance"


def test_live_product_launcher_isolates_all_durable_user_paths() -> None:
    with tempfile.TemporaryDirectory(prefix="live_product_env_") as temp:
        root = Path(temp).resolve()
        product = ElectronProduct(
            run_root=root,
            debug_port=9223,
            no_tts=True,
            identity={
                "commit_sha": "abc",
                "workspace_dirty": True,
                "workspace_fingerprint": "fingerprint",
            },
        )

        env = product._environment()

        state = root / "state"
        assert Path(env["AMADEUS_SESSION_DIR"]).parent == state
        assert Path(env["AMADEUS_WORK_LEDGER_PATH"]).parent == state
        assert Path(env["WORK_SCRATCH_ROOT"]).parent == state
        assert Path(env["WORK_WORKTREE_ROOT"]).parent == state
        assert Path(env["AMADEUS_DESKTOP_PATH"]).parent == state
        assert Path(env["AMADEUS_ELECTRON_USER_DATA_DIR"]).parent == state
        assert Path(env["AMADEUS_ELECTRON_CACHE_DIR"]).parent == state
        assert env["AMADEUS_E2E_NO_TTS"] == "1"
        assert env["AMADEUS_BACKEND_AUTH_MODE"] == "required"
        assert env["AMADEUS_BACKEND_TOKEN"] == product.backend_token
        assert env["AMADEUS_BACKEND_INSTANCE_NONCE"] == (
            product.backend_instance_nonce
        )
        assert product.backend_websocket_protocols == (
            "amadeus.local.v1",
            f"amadeus.auth.{product.backend_token}",
        )
        assert Path(env["WORK_PROJECT_ALLOWLIST"]) == state / "scratch"
        assert "AMADEUS_LOCUS_RUN_STORE_PATH" not in env
        assert "AMADEUS_LOCUS_REQUEST_DIR" not in env
        assert "LOCUS_PROJECT_ALLOWLIST" not in env
        assert env["AMADEUS_WORKSPACE_FINGERPRINT"] == "fingerprint"


def test_live_product_report_keeps_raw_event_coordinates_after_filtering() -> None:
    turn = _turn("status", 40, 60)
    payload = turn.to_dict()
    compact = _compact_event(
        EventRecord(1.25, "chat.complete", {"turn_id": "turn-1"}),
        source_index=54,
    )

    assert payload["source_event_range"] == [40, 60]
    assert "event_range" not in payload
    assert compact["source_index"] == 54
    assert compact["method"] == "chat.complete"


def test_live_product_finalizer_stops_product_when_screenshot_fails() -> None:
    import asyncio

    class Product:
        page = object()
        stopped = False

        async def screenshot(self, _name: str) -> Path:
            raise RuntimeError("renderer already closed")

        async def stop(self) -> None:
            self.stopped = True

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="live_product_finalize_") as temp:
            report_path = Path(temp) / "report.json"
            product = Product()
            report = {"paths": {}}
            await _finalize_product_run(product, report, report_path)  # type: ignore[arg-type]
            assert product.stopped is True
            assert report_path.is_file()

    asyncio.run(scenario())


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")


if __name__ == "__main__":
    _main()
