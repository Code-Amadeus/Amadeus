from __future__ import annotations

from datetime import datetime, timezone

from core.continuity import (
    ContinuityService,
    ContinuityStore,
    MemoryCandidate,
    MemoryKind,
    MemoryResolver,
    RelationshipPolicy,
    RelationshipProposal,
    RelationshipStateClass,
    TurnEvidence,
)
from core.continuity.clock import RealityClock
from core.continuity.memory_extractor import stable_freeform_memory_key
from core.continuity.relationship import DeterministicRelationshipExtractor
from ._support import FakeClock


def _proposal(
    dimension: str,
    delta: float,
    *,
    state_class: RelationshipStateClass = RelationshipStateClass.RELATIONSHIP,
    event_type: str = "test_event",
    confidence: float = 1.0,
    source_key: str = "",
    occurred_at: float | None = None,
) -> RelationshipProposal:
    return RelationshipProposal(
        event_type=event_type,
        state_class=state_class,
        dimension=dimension,
        delta=delta,
        confidence=confidence,
        source_memory_key=source_key,
        occurred_at=occurred_at,
    )


def _relationship_value(store: ContinuityStore, dimension: str, *, now: float) -> float:
    return store.get_relationship_snapshot(now=now).relationship_value(dimension)


def _affect_value(store: ContinuityStore, dimension: str, *, now: float) -> float:
    return store.get_relationship_snapshot(now=now).affect_value(dimension)


async def test_default_extractor_uses_user_evidence_not_assistant_friendliness() -> None:
    extractor = DeterministicRelationshipExtractor()
    evidence = TurnEvidence(
        session_id="s",
        turn_id="t",
        user_text="你好",
        assistant_text="我真的很喜欢你，也非常信任你。",
        observed_at=1000.0,
    )
    proposals = tuple(await extractor.extract(evidence))
    assert proposals == ()


async def test_direct_user_trust_emits_small_bounded_relationship_events() -> None:
    extractor = DeterministicRelationshipExtractor()
    proposals = tuple(
        await extractor.extract(
            TurnEvidence(session_id="s", turn_id="t", user_text="我信任你", observed_at=1000.0)
        )
    )
    pairs = {(p.event_type, p.dimension, p.delta) for p in proposals}
    assert ("user_expressed_trust", "trust", 0.04) in pairs
    assert ("user_expressed_trust", "closeness", 0.015) in pairs
    assert all(p.source_memory_key for p in proposals)


def test_relationship_event_is_idempotent_and_per_event_delta_is_capped(continuity_store) -> None:
    evidence = TurnEvidence(session_id="s", turn_id="t", user_text="trust", observed_at=1000.0)
    proposal = _proposal("trust", 0.90, event_type="trust", occurred_at=1000.0)
    first = continuity_store.apply_relationship_proposals(evidence, (proposal,), as_of=1000.0)
    second = continuity_store.apply_relationship_proposals(evidence, (proposal,), as_of=1000.0)

    assert len(first) == 1
    assert second == ()
    assert first[0].proposed_delta == 0.90
    assert first[0].bounded_delta == 0.05
    assert _relationship_value(continuity_store, "trust", now=1000.0) == 0.55
    assert len(continuity_store.list_relationship_events()) == 1


def test_window_cap_is_deterministic_even_when_events_arrive_out_of_order(tmp_path) -> None:
    policy = RelationshipPolicy()

    def build(path, order):
        store = ContinuityStore(path)
        for index in order:
            when = 1000.0 + index * 60.0
            evidence = TurnEvidence(
                session_id="s",
                turn_id=f"t{index}",
                user_text=f"e{index}",
                observed_at=when,
            )
            store.apply_relationship_proposals(
                evidence,
                (_proposal("trust", 0.05, event_type="trust", occurred_at=when),),
                policy=policy,
                as_of=2000.0,
            )
        return store

    chronological = build(tmp_path / "chronological.sqlite3", [0, 1, 2, 3])
    reverse = build(tmp_path / "reverse.sqlite3", [3, 2, 1, 0])
    try:
        assert _relationship_value(chronological, "trust", now=2000.0) == 0.62
        assert _relationship_value(reverse, "trust", now=2000.0) == 0.62
        assert [round(e.bounded_delta, 6) for e in chronological.list_relationship_events()] == [
            round(e.bounded_delta, 6) for e in reverse.list_relationship_events()
        ] == [0.05, 0.05, 0.02, 0.0]
    finally:
        chronological.close()
        reverse.close()


def test_low_confidence_proposal_is_not_durable(continuity_store) -> None:
    evidence = TurnEvidence(session_id="s", turn_id="t", user_text="maybe", observed_at=1000.0)
    result = continuity_store.apply_relationship_proposals(
        evidence,
        (_proposal("warmth", 0.04, confidence=0.2),),
        as_of=1000.0,
    )
    assert result == ()
    assert continuity_store.list_relationship_events() == []
    assert _relationship_value(continuity_store, "warmth", now=1000.0) == 0.5


def test_affect_decays_from_snapshot_without_scanning_or_writing(continuity_store) -> None:
    evidence = TurnEvidence(session_id="s", turn_id="t", user_text="angry", observed_at=1000.0)
    continuity_store.apply_relationship_proposals(
        evidence,
        (
            _proposal(
                "irritation",
                0.20,
                state_class=RelationshipStateClass.AFFECT,
                event_type="anger",
                occurred_at=1000.0,
            ),
        ),
        as_of=1000.0,
    )
    assert round(_affect_value(continuity_store, "irritation", now=1000.0), 6) == 0.2
    assert round(_affect_value(continuity_store, "irritation", now=1000.0 + 2 * 3600), 6) == 0.1
    assert round(_affect_value(continuity_store, "irritation", now=1000.0 + 4 * 3600), 6) == 0.05


def test_forget_closure_invalidates_relationship_effect_even_without_memory_row(continuity_store) -> None:
    text = "我信任你"
    key = stable_freeform_memory_key(text)
    evidence = TurnEvidence(session_id="s", turn_id="t", user_text=text, observed_at=1000.0)
    continuity_store.apply_relationship_proposals(
        evidence,
        (_proposal("trust", 0.04, event_type="trust", source_key=key, occurred_at=1000.0),),
        as_of=1000.0,
    )
    assert _relationship_value(continuity_store, "trust", now=1000.0) == 0.54

    # There is no memory row, but the opaque source key still closes the derived effect.
    assert continuity_store.forget_memory(key, observed_at=1100.0) == 0
    assert _relationship_value(continuity_store, "trust", now=1100.0) == 0.5
    events = continuity_store.list_relationship_events(include_invalidated=True)
    assert len(events) == 1 and events[0].invalidated_at == 1100.0
    assert text not in repr(continuity_store.relationship_diagnostics(now=1100.0))


def test_forget_closure_follows_linked_memory_id(continuity_store) -> None:
    evidence = TurnEvidence(session_id="s", turn_id="fact", user_text="我的名字叫真由理", observed_at=1000.0)
    candidate = MemoryCandidate(
        memory_key="user.fact.name",
        kind=MemoryKind.USER_FACT,
        summary="我的名字叫真由理",
        predicate="name",
        object_text="真由理",
    )
    records = continuity_store.apply_memory_candidates(
        evidence,
        (candidate,),
        resolver=MemoryResolver(),
        complete_turn=False,
    )
    assert len(records) == 1
    continuity_store.apply_relationship_proposals(
        evidence,
        (
            _proposal(
                "warmth",
                0.03,
                event_type="memory_link_test",
                source_key="user.fact.name",
                occurred_at=1000.0,
            ),
        ),
        as_of=1000.0,
    )
    event = continuity_store.list_relationship_events()[0]
    assert event.source_memory_id == records[0].id
    assert continuity_store.forget_memory("user.fact.name", observed_at=1100.0) == 1
    assert _relationship_value(continuity_store, "warmth", now=1100.0) == 0.5
    assert continuity_store.list_relationship_events() == []


def test_restart_and_explicit_rebuild_are_deterministic(tmp_path) -> None:
    db_path = tmp_path / "continuity.sqlite3"
    store = ContinuityStore(db_path)
    evidence = TurnEvidence(session_id="s", turn_id="t", user_text="thanks", observed_at=1000.0)
    store.apply_relationship_proposals(
        evidence,
        (
            _proposal("warmth", 0.04, event_type="thanks", occurred_at=1000.0),
            _proposal("playfulness", 0.20, state_class=RelationshipStateClass.AFFECT, event_type="play", occurred_at=1000.0),
        ),
        as_of=1000.0,
    )
    before = store.relationship_diagnostics(now=1300.0)
    store.close()

    reopened = ContinuityStore(db_path)
    try:
        after = reopened.relationship_diagnostics(now=1300.0)
        assert before == after
        rebuilt = reopened.rebuild_relationship_state(now=1300.0)
        assert round(rebuilt.relationship_value("warmth"), 6) == round(before["relationship"]["warmth"], 6)
        assert round(rebuilt.affect_value("playfulness"), 6) == round(before["affect"]["playfulness"], 6)
    finally:
        reopened.close()


async def test_service_shadow_updates_state_without_changing_grounding(continuity_store) -> None:
    fake = FakeClock(datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc), 0.0)
    service = ContinuityService(
        continuity_store,
        clock=RealityClock(continuity_store, now_provider=fake.now, monotonic_provider=fake.monotonic),
        relationship_enabled=True,
        relationship_live_enabled=False,
    )
    service.start()
    await service._on_chat_user(
        "chat.user",
        {"session_id": "s", "turn_id": "t", "text": "我信任你"},
    )
    await service._on_chat_complete(
        "chat.complete",
        {"session_id": "s", "turn_id": "t", "full_text": "嗯。"},
    )
    await service.drain()

    assert _relationship_value(continuity_store, "trust", now=fake.current.timestamp()) > 0.5
    grounding = service.grounding_for_turn("你好", session_id="s", turn_id="next")
    assert "Relationship context:" not in grounding.text
    await service.aclose(graceful=False)


async def test_service_live_adds_only_qualitative_bounded_projection(continuity_store) -> None:
    fake = FakeClock(datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc), 0.0)
    service = ContinuityService(
        continuity_store,
        clock=RealityClock(continuity_store, now_provider=fake.now, monotonic_provider=fake.monotonic),
        relationship_enabled=True,
        relationship_live_enabled=True,
    )
    service.start()
    await service._on_chat_user(
        "chat.user",
        {"session_id": "s", "turn_id": "t", "text": "我信任你，谢谢你"},
    )
    await service._on_chat_complete(
        "chat.complete",
        {"session_id": "s", "turn_id": "t", "full_text": "不用谢。"},
    )
    await service.drain()

    grounding = service.grounding_for_turn("继续吧", session_id="s", turn_id="next")
    assert "Relationship context:" in grounding.text
    assert "Trust is" in grounding.text
    assert "0.54" not in grounding.text
    assert "event_id" not in grounding.text
    assert "source_hash" not in grounding.text
    assert "permissions" in grounding.text
    await service.aclose(graceful=False)


async def test_relationship_disable_prevents_relationship_writes(continuity_store) -> None:
    fake = FakeClock(datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc), 0.0)
    service = ContinuityService(
        continuity_store,
        clock=RealityClock(continuity_store, now_provider=fake.now, monotonic_provider=fake.monotonic),
        relationship_enabled=False,
        relationship_live_enabled=True,
    )
    service.start()
    await service._on_chat_user("chat.user", {"session_id": "s", "turn_id": "t", "text": "我信任你"})
    await service._on_chat_complete("chat.complete", {"session_id": "s", "turn_id": "t", "full_text": "嗯"})
    await service.drain()
    assert continuity_store.list_relationship_events() == []
    assert "Relationship context:" not in service.grounding_for_turn("继续").text
    await service.aclose(graceful=False)


async def test_explicit_forget_command_does_not_recreate_semantic_relationship_signal() -> None:
    extractor = DeterministicRelationshipExtractor()
    proposals = tuple(
        await extractor.extract(
            TurnEvidence(
                session_id="s",
                turn_id="forget",
                user_text="忘记我信任你",
                observed_at=1000.0,
            )
        )
    )
    assert proposals == ()


async def test_service_explicit_forget_closes_prior_relationship_contribution(continuity_store) -> None:
    fake = FakeClock(datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc), 0.0)
    service = ContinuityService(
        continuity_store,
        clock=RealityClock(continuity_store, now_provider=fake.now, monotonic_provider=fake.monotonic),
        relationship_enabled=True,
        relationship_live_enabled=False,
    )
    service.start()
    await service._on_chat_user("chat.user", {"session_id": "s", "turn_id": "trust", "text": "我信任你"})
    await service._on_chat_complete("chat.complete", {"session_id": "s", "turn_id": "trust", "full_text": "嗯"})
    await service.drain()
    assert _relationship_value(continuity_store, "trust", now=fake.current.timestamp()) == 0.54

    fake.advance(seconds=5)
    await service._on_chat_user("chat.user", {"session_id": "s", "turn_id": "forget", "text": "忘记我信任你"})
    await service._on_chat_complete("chat.complete", {"session_id": "s", "turn_id": "forget", "full_text": "好"})
    await service.drain()

    assert _relationship_value(continuity_store, "trust", now=fake.current.timestamp()) == 0.5
    semantic_active = [
        event for event in continuity_store.list_relationship_events()
        if event.dimension in {"trust", "closeness"}
    ]
    assert semantic_active == []
    await service.aclose(graceful=False)


def test_project_relationship_policy_loads_all_bounded_dimensions() -> None:
    from pathlib import Path

    policy = RelationshipPolicy.load(Path(__file__).resolve().parents[2] / "config" / "continuity_policy.json")
    assert policy.policy_version == "c5-v1"
    assert set(policy.event_delta_caps) == {
        "familiarity", "trust", "warmth", "respect", "closeness",
        "irritation", "embarrassment", "tension", "playfulness",
    }
    assert policy.event_cap("trust") == 0.05
    assert policy.window_cap("trust") == 0.12
    assert policy.affect_half_life("irritation") == 2.0
async def test_generic_reassurance_is_not_durable_relationship_repair() -> None:
    extractor = DeterministicRelationshipExtractor()
    evidence = TurnEvidence(
        session_id="s",
        turn_id="t-generic-reassurance",
        user_text="没关系了，我们继续这个项目。",
        observed_at=1000.0,
    )
    assert tuple(await extractor.extract(evidence)) == ()


def test_expired_affect_only_history_does_not_create_neutral_relationship_projection(continuity_store) -> None:
    from core.continuity.relationship import render_relationship_projection

    evidence = TurnEvidence(session_id="s", turn_id="affect-only", user_text="angry", observed_at=1000.0)
    continuity_store.apply_relationship_proposals(
        evidence,
        (
            _proposal(
                "irritation",
                0.20,
                state_class=RelationshipStateClass.AFFECT,
                event_type="anger",
                occurred_at=1000.0,
            ),
        ),
        as_of=1000.0,
    )
    # After many half-lives, the transient affect is below the Live projection
    # threshold. It must not manufacture a neutral long-term relationship line.
    snapshot = continuity_store.get_relationship_snapshot(now=1000.0 + 48 * 3600)
    assert render_relationship_projection(snapshot) == ""

