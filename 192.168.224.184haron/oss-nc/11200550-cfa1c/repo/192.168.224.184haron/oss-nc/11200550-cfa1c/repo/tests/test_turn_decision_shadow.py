"""Contracts for the observe-only whole-turn convergence experiment."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server.turn_decision_shadow as shadow_module

from agent_host.provider_contract import (
    ProviderCapabilities,
    ProviderManifest,
    ProviderRequirements,
)
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderRunRequest, ProviderRunResult
from agent_host.work_ledger_store import WorkLedgerStore
from server.auip_control_decision import AuipControlDecision
from server.turn_decision_shadow import (
    TurnDecisionShadowObserver,
    compile_shadow_turn_decision,
    get_turn_decision_shadow_observer,
)
from server.work_ledger_coordinator import WorkLedgerCoordinator


def _admit(
    observer: TurnDecisionShadowObserver,
    *,
    utterance_id: str = "utterance-1",
    turn_id: str = "turn-1",
    text: str = "Please update the game.",
):
    admission = observer.admit_turn(
        utterance_id=utterance_id,
        turn_id=turn_id,
        session_id="session-1",
        transcript=text,
        dialogue_source_scope="chat:session-1",
        input_source="voice",
        chat_epoch=7,
    )
    assert admission is not None
    return admission


def test_acceptance_identity_uses_source_and_utterance_not_text_hash() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    first = _admit(observer, utterance_id="u-1", turn_id="t-1", text="再来一次")
    replay = _admit(observer, utterance_id="u-1", turn_id="t-retry", text="再来一次")
    repeated_words = _admit(
        observer,
        utterance_id="u-2",
        turn_id="t-2",
        text="再来一次",
    )

    assert replay.root_id == first.root_id
    assert repeated_words.root_id != first.root_id
    assert first.acceptance_key == ("chat:session-1", "u-1")
    observer.record_event(
        "t-retry",
        stage="transport_retry_joined",
        origin_kind="transport_utterance",
        origin_id="u-1",
    )
    snapshot = observer.snapshot()
    assert snapshot["counters"]["admitted"] == 2
    assert snapshot["counters"]["replayed_admission"] == 1
    first_root = next(
        row
        for row in snapshot["recent"]
        if row["admission"]["root_id"] == first.root_id
    )
    assert first_root["events"][-1]["stage"] == "transport_retry_joined"


def test_identity_collision_is_observed_without_creating_second_root() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    first = _admit(observer, text="first transcript")
    collision = _admit(observer, text="different transcript")

    assert collision.root_id == first.root_id
    snapshot = observer.snapshot()
    assert snapshot["counters"]["invariant_violations"] == 1
    assert snapshot["recent"][0]["invariant_violations"]


def test_disabled_proposal_observation_does_not_touch_payload() -> None:
    class ExplodingProposals:
        def __iter__(self):
            raise AssertionError("disabled observer iterated proposals")

    observer = TurnDecisionShadowObserver(enabled=False)
    observer.observe_proposal_batch(
        SimpleNamespace(
            turn_id="disabled-turn",
            proposals=ExplodingProposals(),
            commit_point="closed",
            transport="inline",
        )
    )

    assert observer.snapshot()["counters"]["events"] == 0


def test_chat_admission_does_not_enumerate_unbounded_source_evidence() -> None:
    from server.handlers.chat_handler import ChatHandler

    class ExplodingEvidence(dict):
        def keys(self):
            raise AssertionError("capture enumerated unbounded source evidence")

    with patch(
        "server.turn_decision_shadow.get_enabled_turn_decision_shadow_observer",
        return_value=None,
    ):
        captured = ChatHandler._capture_turn_admission(
            utterance_id="disabled",
            turn_id="disabled",
            session_id="session",
            text="hello",
            source="voice",
            chat_epoch=1,
            pending=False,
            source_evidence=ExplodingEvidence({"asr_confidence": 0.4}),
            utterance_identity_source="explicit_utterance_id",
        )
        assert captured.source_evidence["asr_confidence"] == 0.4
        ChatHandler._observe_turn_admission(captured)


def test_proposal_observation_exposes_bounded_truncation() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    _admit(observer)
    observer.observe_proposal_batch(
        SimpleNamespace(
            turn_id="turn-1",
            proposals=tuple(
                {"intent": "execute", "provider": "codex", "task": f"task-{index}"}
                for index in range(45)
            ),
            commit_point="closed",
            transport="inline",
        )
    )

    payload = observer.snapshot()["recent"][0]["events"][-1]["payload"]
    assert payload["proposal_count"] == 45
    assert payload["proposal_observed_count"] == 40
    assert payload["proposal_observation_truncated"] is True
    assert len(payload["proposals"]) == 40


def test_effect_truncation_fails_closed_without_relationship_inference() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    admission = _admit(observer)
    actions = tuple(
        {
            "type": "DELEGATE",
            "attrs": {"intent": "execute", "task": f"task-{index}"},
        }
        for index in range(50)
    )
    auip = AuipControlDecision(
        status="ok",
        action="launch",
        timing="after_work",
        target="delivery",
        work_relation="same_goal",
    )

    decision = compile_shadow_turn_decision(
        admission,
        effective_actions=actions,
        auip_decision=auip,
        auip_dispatched=True,
    )

    assert decision.status == "failed_closed"
    assert len(decision.effects) == 40
    assert decision.execution_dependencies == ()
    assert decision.semantic_constraints == ()
    assert all(effect.goal_group_id is None for effect in decision.effects)
    assert any("truncated" in note for note in decision.notes)


def test_retry_aliases_and_root_capacity_are_strictly_bounded() -> None:
    observer = TurnDecisionShadowObserver(enabled=True, root_cap=999)
    _admit(observer, utterance_id="stable", turn_id="canonical")
    for index in range(20):
        _admit(
            observer,
            utterance_id="stable",
            turn_id=f"retry-{index}",
        )

    snapshot = observer.snapshot()
    assert snapshot["limits"]["roots"] == 64
    assert snapshot["retained"]["turn_ids"] == 8
    assert snapshot["recent"][0]["turn_alias_count"] == 7
    assert observer.record_event(
        "canonical",
        stage="canonical-still-bound",
        origin_kind="test",
    )
    assert observer.record_event(
        "retry-19",
        stage="latest-alias-bound",
        origin_kind="test",
    )
    assert (
        observer.record_event(
            "retry-0",
            stage="old-alias-evicted",
            origin_kind="test",
        )
        is None
    )


def test_turn_id_cannot_be_rebound_to_a_different_provenance_root() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    first = _admit(observer, utterance_id="first", turn_id="shared-turn")
    second = _admit(observer, utterance_id="second", turn_id="shared-turn")

    assert first.root_id != second.root_id
    assert observer.record_event(
        "shared-turn",
        stage="still-first-root",
        origin_kind="test",
    )
    snapshot = observer.snapshot()
    assert snapshot["counters"]["invariant_violations"] == 1
    first_row = next(
        row
        for row in snapshot["recent"]
        if row["admission"]["root_id"] == first.root_id
    )
    second_row = next(
        row
        for row in snapshot["recent"]
        if row["admission"]["root_id"] == second.root_id
    )
    assert first_row["events"][-1]["stage"] == "still-first-root"
    assert second_row["events"][0]["payload"]["turn_id_bound"] is False


def test_root_eviction_removes_only_its_bounded_turn_aliases() -> None:
    observer = TurnDecisionShadowObserver(enabled=True, root_cap=2)
    _admit(observer, utterance_id="u-1", turn_id="t-1")
    _admit(observer, utterance_id="u-1", turn_id="t-1-retry")
    _admit(observer, utterance_id="u-2", turn_id="t-2")
    _admit(observer, utterance_id="u-3", turn_id="t-3")

    snapshot = observer.snapshot()
    assert snapshot["retained"]["roots"] == 2
    assert snapshot["retained"]["turn_ids"] == 2
    assert observer.record_event(
        "t-1-retry",
        stage="evicted-alias",
        origin_kind="test",
    ) is None
    assert observer.record_event(
        "t-3",
        stage="retained-root",
        origin_kind="test",
    )


def test_invariant_details_are_bounded_while_total_remains_exact() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    _admit(observer)
    observer.mark_lifecycle("turn-1", "superseded")
    for index in range(40):
        observer.record_event(
            "turn-1",
            stage="legacy_dispatch_accepted",
            origin_kind="test",
            origin_id=str(index),
        )

    snapshot = observer.snapshot()
    recent = snapshot["recent"][0]
    assert snapshot["counters"]["invariant_violations"] == 40
    assert recent["invariant_violation_count"] == 40
    assert len(recent["invariant_violations"]) == 32
    assert recent["invariant_details_truncated"] is True


def test_event_logging_runs_outside_observer_lock(monkeypatch) -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    lock_owned: list[bool] = []

    def inspect_lock(*_args, **_kwargs) -> None:
        lock_owned.append(observer._lock._is_owned())

    monkeypatch.setattr(shadow_module.logger, "info", inspect_lock)
    _admit(observer)
    observer.record_event(
        "turn-1",
        stage="outside-lock",
        origin_kind="test",
        payload={"nested": ["value"]},
    )

    assert lock_owned == [False, False]


def test_concurrent_events_keep_unique_in_memory_sequence(monkeypatch) -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    monkeypatch.setattr(shadow_module.logger, "info", lambda *_args, **_kwargs: None)
    _admit(observer)

    def record(index: int) -> None:
        assert observer.record_event(
            "turn-1",
            stage="concurrent",
            origin_kind="test",
            origin_id=str(index),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(record, range(64)))

    snapshot = observer.snapshot()
    assert snapshot["counters"]["events"] == 65
    assert snapshot["retained"]["events"] == 65
    sequences = [
        event["sequence"] for event in snapshot["recent"][0]["events"]
    ]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))


def test_nested_payload_projection_has_one_total_budget() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    _admit(observer)
    observer.record_event(
        "turn-1",
        stage="bounded-payload",
        origin_kind="test",
        payload={
            f"key-{outer}": ["x" * 500 for _ in range(40)]
            for outer in range(40)
        },
    )

    payload = observer.snapshot()["recent"][0]["events"][-1]["payload"]
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert len(rendered) <= 16_384
    assert _json_node_count(payload) <= 512
    assert payload["_shadow_projection_budget_exhausted"] is True


def _json_node_count(value) -> int:
    if isinstance(value, dict):
        return 1 + sum(_json_node_count(item) for item in value.values())
    if isinstance(value, list):
        return 1 + sum(_json_node_count(item) for item in value)
    return 1


def test_projection_budget_counts_nested_sets_and_json_escaping() -> None:
    nested_sets = {
        f"outer-{outer}": {
            f"value-{outer}-{inner}-{leaf}"
            for leaf in range(40)
        }
        for outer in range(40)
        for inner in range(2)
    }
    nul_payload = {f"key-{index}": "\x00" * 500 for index in range(40)}

    for source in (nested_sets, nul_payload):
        projected = shadow_module._bounded_json(source)
        rendered = json.dumps(
            projected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        assert _json_node_count(projected) <= 512
        assert len(rendered) <= 16_384
        assert projected["_shadow_projection_budget_exhausted"] is True


def test_projection_preserves_empty_keys_and_is_json_response_safe() -> None:
    from starlette.responses import JSONResponse

    empty_key = shadow_module._bounded_json({"": 1, "after": 2})
    assert empty_key == {"": 1, "after": 2}
    spoofed_marker = shadow_module._bounded_json(
        {
            "_shadow_projection_truncated": False,
            **{f"field-{index}": index for index in range(50)},
        }
    )
    assert spoofed_marker["_shadow_projection_truncated"] is True

    projected = shadow_module._bounded_json(
        {
            "nan": float("nan"),
            "positive_infinity": float("inf"),
            "negative_infinity": float("-inf"),
            "surrogate": "\ud800",
        }
    )
    response = JSONResponse(projected)
    response.body.decode("utf-8")
    assert projected["nan"] == "<non-finite-float:nan>"
    assert projected["positive_infinity"] == "<non-finite-float:inf>"
    assert projected["negative_infinity"] == "<non-finite-float:-inf>"
    assert projected["surrogate"] == "?"


def test_returned_records_cannot_mutate_observer_state() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    admission = observer.admit_turn(
        utterance_id="immutable-utterance",
        turn_id="immutable-turn",
        session_id="session-1",
        transcript="Build it and then launch it.",
        source_evidence={"n_best_hashes": ["original"]},
    )
    assert admission is not None
    event = observer.record_event(
        "immutable-turn",
        stage="immutable-event",
        origin_kind="test",
        payload={"nested": ["original"]},
    )
    decision = observer.observe_settlement(
        "immutable-turn",
        effective_actions=(
            {"type": "DELEGATE", "attrs": {"intent": "execute", "task": "build"}},
        ),
        auip_decision=AuipControlDecision(
            status="ok",
            action="launch",
            timing="after_work",
            target="delivery",
            work_relation="same_goal",
        ),
        auip_dispatched=True,
    )
    assert event is not None
    assert decision is not None and decision.execution_dependencies

    admission.source_evidence["n_best_hashes"].append("mutated")
    event.payload["nested"].append("mutated")
    decision.execution_dependencies[0].condition["predicate"] = "mutated"

    row = observer.snapshot()["recent"][0]
    assert row["admission"]["source_evidence"]["n_best_hashes"] == ["original"]
    immutable_event = next(
        item for item in row["events"] if item["stage"] == "immutable-event"
    )
    assert immutable_event["payload"]["nested"] == ["original"]
    assert row["decision"]["execution_dependencies"][0]["condition"] == {
        "predicate": "launchable_auip_delivery_exists"
    }


def test_admission_preserves_optional_voice_source_evidence() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    admission = observer.admit_turn(
        utterance_id="voice-1",
        turn_id="turn-voice-1",
        session_id="session-1",
        transcript="好的",
        dialogue_source_scope="chat:session-1",
        input_source="wake",
        source_evidence={
            "asr_confidence": 0.72,
            "n_best_hashes": ["candidate-a", "candidate-b"],
            "tts_overlap": True,
            "n_best": ["好的", "好啊"],
        },
    )

    assert admission is not None
    assert admission.source_evidence == {
        "asr_confidence": 0.72,
        "n_best_hashes": ["candidate-a", "candidate-b"],
        "tts_overlap": True,
    }


def test_no_accepted_effect_is_not_claimed_as_no_user_action() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    admission = _admit(observer)

    decision = compile_shadow_turn_decision(admission)

    assert decision.status == "observed_no_effect"
    assert decision.effects == ()
    assert decision.planner_source == "existing_source_specific_witnesses"


def test_work_only_proposal_compiles_one_observed_effect() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    admission = _admit(observer)
    actions = (
        {
            "type": "DELEGATE",
            "attrs": {
                "intent": "amend",
                "task": "Update the existing game",
                "workspace_ref": "work-123",
                "provider": "codex",
            },
        },
    )

    decision = compile_shadow_turn_decision(
        admission,
        effective_actions=actions,
    )

    assert decision.status == "observed_effects"
    assert len(decision.effects) == 1
    effect = decision.effects[0]
    assert effect.axis == "work"
    assert effect.operation == "amend"
    assert effect.target_kind == "work_item"
    assert effect.target_id == "work-123"
    assert effect.goal_group_id
    assert effect.admission_status == "legacy_effective_control"
    assert decision.execution_dependencies == ()


def test_work_then_auip_uses_verified_outcome_dependency_only() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    admission = _admit(
        observer,
        text="Build the game, then when it is ready let us play together.",
    )
    actions = (
        {
            "type": "DELEGATE",
            "attrs": {
                "intent": "execute",
                "task": "Build the playable game",
                "provider": "codex",
            },
        },
    )
    auip = AuipControlDecision(
        status="ok",
        action="launch",
        timing="after_work",
        target="delivery",
        work_relation="same_goal",
    )

    decision = compile_shadow_turn_decision(
        admission,
        effective_actions=actions,
        auip_decision=auip,
        auip_dispatched=True,
    )

    assert [effect.axis for effect in decision.effects] == ["work", "auip"]
    assert decision.effects[0].goal_group_id == decision.effects[1].goal_group_id
    assert decision.effects[1].admission_status == "legacy_dispatched"
    assert len(decision.execution_dependencies) == 1
    dependency = decision.execution_dependencies[0]
    assert dependency.parent_effect_id == decision.effects[0].effect_id
    assert dependency.child_effect_id == decision.effects[1].effect_id
    assert dependency.condition_kind == "requires_verified_outcome"
    assert dependency.condition == {
        "predicate": "launchable_auip_delivery_exists"
    }
    assert decision.semantic_constraints == ()


def test_independent_work_and_auip_have_no_execution_edge() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    admission = _admit(observer)
    actions = (
        {
            "type": "DELEGATE",
            "attrs": {"intent": "execute", "task": "Write a release note"},
        },
    )
    auip = AuipControlDecision(
        status="ok",
        action="step",
        instruction="play one move",
        app_session_id="app-1",
        work_relation="independent",
    )

    decision = compile_shadow_turn_decision(
        admission,
        effective_actions=actions,
        auip_decision=auip,
        auip_dispatched=False,
    )

    assert [effect.axis for effect in decision.effects] == ["work", "auip"]
    assert decision.execution_dependencies == ()
    assert decision.effects[0].goal_group_id
    assert decision.effects[1].goal_group_id
    assert decision.effects[0].goal_group_id != decision.effects[1].goal_group_id
    assert decision.effects[1].admission_status == "witness_only"


def test_multiple_work_effects_remain_goal_group_unresolved() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    admission = _admit(observer)
    actions = (
        {"type": "DELEGATE", "attrs": {"intent": "execute", "task": "A"}},
        {"type": "DELEGATE", "attrs": {"intent": "execute", "task": "B"}},
    )

    decision = compile_shadow_turn_decision(
        admission,
        effective_actions=actions,
    )

    assert len(decision.effects) == 2
    assert all(effect.goal_group_id is None for effect in decision.effects)
    assert any("goal-group unresolved" in note for note in decision.notes)


def test_direct_browser_branch_is_observed_without_becoming_work() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    _admit(observer)

    decision = observer.observe_direct_branch(
        "turn-1",
        {
            "handled": True,
            "provider": "browser",
            "branch_id": "branch-1",
            "run": {"run_id": "run-1"},
        },
    )

    assert decision is not None
    assert len(decision.effects) == 1
    assert decision.effects[0].axis == "browser"
    assert decision.effects[0].target_id == "branch-1"
    assert decision.effects[0].admission_status == "legacy_direct_branch"
    replay = observer.observe_direct_branch(
        "turn-1",
        {
            "handled": True,
            "provider": "browser",
            "branch_id": "branch-1",
            "run": {"run_id": "run-1"},
        },
    )
    assert replay is not None and replay.decision_id == decision.decision_id
    assert observer.snapshot()["counters"]["decision_replays"] == 1


def test_trace_keeps_origin_identity_separate_from_arrival_order() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    admission = _admit(observer)
    observer.record_event(
        "turn-1",
        stage="proposal",
        origin_kind="main_chat_role",
        origin_id="proposal-1",
    )
    observer.record_event(
        "turn-1",
        stage="receipt",
        origin_kind="provider_runtime",
        origin_id="run-9",
    )

    events = observer.snapshot()["recent"][0]["events"]
    sequences = [event["sequence"] for event in events]
    assert sequences == sorted(sequences)
    assert all(event["root_id"] == admission.root_id for event in events)
    assert events[-2]["origin_id"] == "proposal-1"
    assert events[-1]["origin_id"] == "run-9"


def test_stale_effect_application_after_supersede_is_visible() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    _admit(observer)
    observer.mark_lifecycle("turn-1", "superseded", reason="barge_in")
    observer.record_event(
        "turn-1",
        stage="legacy_dispatch_accepted",
        origin_kind="host_action_dispatcher",
        origin_id="turn-1",
        payload={"action_count": 1},
    )
    observer.observe_settlement(
        "turn-1",
        effective_actions=(
            {
                "type": "DELEGATE",
                "attrs": {"intent": "execute", "task": "stale work"},
            },
        ),
    )

    snapshot = observer.snapshot()
    assert snapshot["counters"]["stale_application_events"] == 2
    assert snapshot["counters"]["invariant_violations"] == 2
    recent = snapshot["recent"][0]
    assert len(recent["invariant_violations"]) == 2
    stale_events = [
        event
        for event in recent["events"]
        if event["payload"].get("stale_origin_lifecycle") == "superseded"
    ]
    assert [event["stage"] for event in stale_events] == [
        "legacy_dispatch_accepted",
        "shadow_turn_decision_observed",
    ]


def test_repeated_settlement_is_idempotent_and_shape_change_is_visible() -> None:
    observer = TurnDecisionShadowObserver(enabled=True)
    _admit(observer)
    actions = (
        {"type": "DELEGATE", "attrs": {"intent": "execute", "task": "A"}},
    )
    first = observer.observe_settlement("turn-1", effective_actions=actions)
    replay = observer.observe_settlement("turn-1", effective_actions=actions)
    changed = observer.observe_settlement(
        "turn-1",
        effective_actions=(
            *actions,
            {"type": "DELEGATE", "attrs": {"intent": "execute", "task": "B"}},
        ),
    )

    assert first is not None and replay is not None and changed is not None
    assert replay.decision_id == first.decision_id
    assert changed.decision_id != first.decision_id
    snapshot = observer.snapshot()
    assert snapshot["counters"]["decisions"] == 1
    assert snapshot["counters"]["decision_replays"] == 1
    assert snapshot["counters"]["decision_mutations"] == 1
    stages = [event["stage"] for event in snapshot["recent"][0]["events"]]
    assert stages[-3:] == [
        "shadow_turn_decision_observed",
        "shadow_turn_decision_replayed",
        "shadow_turn_decision_mutated",
    ]


def test_work_attempt_and_provider_run_join_back_to_origin_turn(tmp_path) -> None:
    class _Adapter:
        provider_id = "turn-shadow-test"
        manifest = ProviderManifest(
            provider_id=provider_id,
            display_name="Turn shadow test",
            capabilities=ProviderCapabilities(task_kinds=("general",)),
        )

        async def run(self, request, run_id, emit):
            return ProviderRunResult(status="done", result="ok")

        async def cancel(self, run_id):
            return {"confirmed": True, "cancelled": True}

    async def scenario() -> None:
        observer = get_turn_decision_shadow_observer()
        observer.clear()
        admission = observer.admit_turn(
            utterance_id="utterance-lineage",
            turn_id="turn-lineage",
            session_id="session-lineage",
            transcript="Inspect this disposable workspace.",
            dialogue_source_scope="chat:session-lineage",
            input_source="voice",
        )
        assert admission is not None

        store = WorkLedgerStore(tmp_path / "work.sqlite3")
        coordinator = WorkLedgerCoordinator(store)
        runtime = ProviderRuntime()
        runtime.register(_Adapter())
        runtime.set_request_preparer(coordinator.prepare_request)
        try:
            record = await runtime.start(
                ProviderRunRequest(
                    provider="turn-shadow-test",
                    task="Inspect this disposable workspace.",
                    cwd=str(tmp_path),
                    requirements=ProviderRequirements(task_kind="general"),
                    ownership="managed",
                    metadata={
                        "turn_id": "turn-lineage",
                        "session_id": "session-lineage",
                    },
                )
            )
            assert record.task_handle is not None
            await record.task_handle

            events = observer.snapshot()["recent"][0]["events"]
            by_stage = {event["stage"]: event for event in events}
            assert "work_attempt_admitted" in by_stage
            assert "provider_run_created" in by_stage
            assert "provider_run_terminal" in by_stage
            attempt_payload = by_stage["work_attempt_admitted"]["payload"]
            created_payload = by_stage["provider_run_created"]["payload"]
            terminal_payload = by_stage["provider_run_terminal"]["payload"]
            assert attempt_payload["work_item_id"] == created_payload["work_item_id"]
            assert attempt_payload["attempt_id"] == created_payload["attempt_id"]
            assert created_payload["run_id"] == terminal_payload["run_id"]
            assert terminal_payload["status"] == "done"
        finally:
            runtime.set_request_preparer(None)
            coordinator.close()
            observer.clear()

    asyncio.run(scenario())


def test_reconstructed_field_pvz_fixture_has_one_work_and_one_conditional_auip() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "turn_decision"
        / "pvz_attach_then_play_reconstructed_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["evidence_status"] == (
        "reconstructed_field_observation_not_runtime_trace"
    )
    turn = fixture["turn"]
    observer = TurnDecisionShadowObserver(enabled=True)
    admission = observer.admit_turn(
        utterance_id=turn["utterance_id"],
        turn_id=turn["turn_id"],
        session_id=turn["session_id"],
        transcript=turn["transcript"],
        dialogue_source_scope=turn["dialogue_source_scope"],
        input_source="voice",
    )
    assert admission is not None
    decision = compile_shadow_turn_decision(
        admission,
        effective_actions=fixture["effective_actions"],
        auip_decision=AuipControlDecision(**fixture["auip_decision"]),
    )

    expected = fixture["expected_shadow"]
    assert decision.status == expected["status"]
    assert [effect.axis for effect in decision.effects] == expected["effect_axes"]
    assert sum(effect.axis == "work" for effect in decision.effects) == expected[
        "work_effect_count"
    ]
    assert sum(effect.axis == "auip" for effect in decision.effects) == expected[
        "auip_effect_count"
    ]
    assert len(decision.execution_dependencies) == 1
    dependency = decision.execution_dependencies[0]
    assert dependency.condition_kind == expected["dependency_condition"]
    assert dependency.condition["predicate"] == expected["dependency_predicate"]
