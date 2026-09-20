from __future__ import annotations

from datetime import datetime, timezone

from core.continuity import (
    CharacterLifeProfile,
    CharacterLifeRuntime,
    ContinuityService,
    ContinuityStore,
    LifePolicy,
    LifeScheduleStatus,
    MemoryCandidate,
    MemoryKind,
    MemoryResolver,
    TurnEvidence,
)
from core.continuity.clock import RealityClock
from ._support import FakeClock


def _policy_profile():
    policy = LifePolicy.load("config/continuity_policy.json")
    profile = CharacterLifeProfile.load("character_continuity/kurisu.json")
    return policy, profile


def _runtime(store: ContinuityStore) -> CharacterLifeRuntime:
    policy, profile = _policy_profile()
    return CharacterLifeRuntime(store, profile=profile, policy=policy)


def _item_shape(snapshot):
    return [
        (item.ordinal, item.category, item.title, item.starts_at, item.ends_at, item.thread_key)
        for item in snapshot.items
    ]


def test_daily_schedule_is_deterministic_shared_and_restart_stable(tmp_path) -> None:
    db_path = tmp_path / "continuity.sqlite3"
    now = datetime(2026, 9, 18, 7, 0, tzinfo=timezone.utc)

    store = ContinuityStore(db_path)
    first_runtime = _runtime(store)
    first = first_runtime.ensure_today(now)
    second_runtime = _runtime(store)
    second = second_runtime.ensure_today(now)
    assert first.schedule is not None and second.schedule is not None
    assert first.schedule.schedule_id == second.schedule.schedule_id
    assert first.schedule.seed_hash == second.schedule.seed_hash
    assert _item_shape(first) == _item_shape(second)
    store.close()

    reopened = ContinuityStore(db_path)
    try:
        third = _runtime(reopened).ensure_today(now)
        assert third.schedule is not None
        assert third.schedule.schedule_id == first.schedule.schedule_id
        assert _item_shape(third) == _item_shape(first)
    finally:
        reopened.close()


def test_midnight_rollover_creates_exactly_one_new_local_day_plan(continuity_store) -> None:
    runtime = _runtime(continuity_store)
    day_one = datetime(2026, 9, 18, 23, 50, tzinfo=timezone.utc)
    day_two = datetime(2026, 9, 19, 0, 5, tzinfo=timezone.utc)
    first = runtime.ensure_today(day_one)
    second = runtime.ensure_today(day_two)
    again = runtime.ensure_today(day_two)
    assert first.local_date == "2026-09-18"
    assert second.local_date == again.local_date == "2026-09-19"
    assert second.schedule is not None and again.schedule is not None
    assert second.schedule.schedule_id == again.schedule.schedule_id
    assert continuity_store.life_diagnostics("kurisu")["schedule_count"] == 2


def test_late_start_generates_full_plan_but_bounds_past_outcomes(continuity_store) -> None:
    runtime = _runtime(continuity_store)
    snapshot = runtime.ensure_today(datetime(2026, 9, 18, 23, 0, tzinfo=timezone.utc))
    assert len(snapshot.items) == 5
    events = continuity_store.list_life_events("kurisu", local_date="2026-09-18")
    outcomes = [event for event in events if event.event_type == "activity_outcome"]
    assert len(outcomes) == runtime.policy.max_today_outcomes == 4
    assert sum(item.status == LifeScheduleStatus.SKIPPED for item in snapshot.items) == 1


def test_multi_day_offline_catchup_uses_coarse_summaries_not_fabricated_schedules(continuity_store) -> None:
    runtime = _runtime(continuity_store)
    runtime.ensure_today(datetime(2026, 9, 10, 7, 0, tzinfo=timezone.utc))
    snapshot = runtime.ensure_today(datetime(2026, 9, 12, 7, 0, tzinfo=timezone.utc))
    assert snapshot.local_date == "2026-09-12"
    # There is no generated 2026-09-11 schedule; only a coarse day marker.
    assert continuity_store.get_life_schedule("kurisu", "2026-09-11") is None
    events = continuity_store.list_life_events("kurisu", include_invalidated=False)
    summaries = [event for event in events if event.event_type == "catchup_day_summary"]
    assert [event.local_date for event in summaries] == ["2026-09-11"]
    assert continuity_store.life_diagnostics("kurisu")["schedule_count"] == 2


def test_long_offline_gap_creates_one_gap_marker_only(continuity_store) -> None:
    runtime = _runtime(continuity_store)
    runtime.ensure_today(datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc))
    runtime.ensure_today(datetime(2026, 9, 10, 7, 0, tzinfo=timezone.utc))
    events = continuity_store.list_life_events("kurisu")
    assert sum(event.event_type == "catchup_gap_summary" for event in events) == 1
    assert not [event for event in events if event.event_type == "catchup_day_summary"]
    assert continuity_store.life_diagnostics("kurisu")["schedule_count"] == 2



def test_ongoing_life_thread_carries_across_days_with_same_thread_key(continuity_store) -> None:
    runtime = _runtime(continuity_store)
    first = runtime.ensure_today(datetime(2026, 9, 18, 7, 0, tzinfo=timezone.utc))
    first_keys = [item.thread_key for item in first.items if item.thread_key]
    assert len(first_keys) == 1
    second = runtime.ensure_today(datetime(2026, 9, 19, 7, 0, tzinfo=timezone.utc))
    second_keys = [item.thread_key for item in second.items if item.thread_key]
    assert second_keys == first_keys
    threads = continuity_store.list_life_threads("kurisu", status="active")
    assert any(thread.thread_key == first_keys[0] for thread in threads)

def test_activity_outcome_is_saved_once_and_thread_progress_is_idempotent(continuity_store) -> None:
    runtime = _runtime(continuity_store)
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    runtime.ensure_today(now)
    events_once = continuity_store.list_life_events("kurisu")
    threads_once = continuity_store.list_life_threads("kurisu")
    runtime.ensure_today(now)
    events_twice = continuity_store.list_life_events("kurisu")
    threads_twice = continuity_store.list_life_threads("kurisu")
    assert [(event.event_id, event.summary) for event in events_once] == [
        (event.event_id, event.summary) for event in events_twice
    ]
    assert [(thread.thread_id, thread.progress) for thread in threads_once] == [
        (thread.thread_id, thread.progress) for thread in threads_twice
    ]


def test_character_life_state_is_explicitly_simulated_and_does_not_create_memory_facts(continuity_store) -> None:
    runtime = _runtime(continuity_store)
    snapshot = runtime.ensure_today(datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc))
    assert snapshot.schedule is not None
    assert snapshot.schedule.source_class.value == "simulated_life"
    assert all(thread.source_class.value == "simulated_life" for thread in snapshot.active_threads)
    assert all(event.source_class.value == "simulated_life" for event in snapshot.recent_events)
    assert continuity_store.list_active_memories() == []


def test_life_event_provenance_closes_when_linked_memory_is_forgotten(continuity_store) -> None:
    evidence = TurnEvidence(session_id="s", turn_id="fact", user_text="我喜欢红茶", observed_at=1000.0)
    records = continuity_store.apply_memory_candidates(
        evidence,
        (MemoryCandidate(memory_key="user.preference.tea", kind=MemoryKind.PREFERENCE, summary="我喜欢红茶"),),
        resolver=MemoryResolver(),
        complete_turn=False,
    )
    assert len(records) == 1
    policy, _ = _policy_profile()
    continuity_store.record_life_event(
        character_id="kurisu",
        local_date="2026-09-18",
        event_type="source_link_test",
        summary="Opaque provenance test marker.",
        occurred_at=1010.0,
        policy=policy,
        fingerprint_material="source-link-test",
        source_memory_key="user.preference.tea",
    )
    assert len(continuity_store.list_life_events("kurisu")) == 1
    assert continuity_store.forget_memory("user.preference.tea", observed_at=1100.0) == 1
    assert continuity_store.list_life_events("kurisu") == []
    historical = continuity_store.list_life_events("kurisu", include_invalidated=True)
    assert len(historical) == 1 and historical[0].invalidated_at == 1100.0


async def test_chat_interrupt_pauses_and_resumes_current_simulated_activity(continuity_store) -> None:
    fake = FakeClock(datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc), 0.0)
    policy, profile = _policy_profile()
    runtime = CharacterLifeRuntime(continuity_store, profile=profile, policy=policy)
    service = ContinuityService(
        continuity_store,
        clock=RealityClock(continuity_store, now_provider=fake.now, monotonic_provider=fake.monotonic),
        memory_enabled=False,
        relationship_enabled=False,
        life_runtime=runtime,
        life_policy=policy,
        life_enabled=True,
        life_live_enabled=False,
    )
    service.start()
    await service._on_chat_user("chat.user", {"session_id": "s", "turn_id": "t", "text": "hello"})
    await service.drain()
    paused = runtime.snapshot(fake.current)
    assert paused.current_item is not None and paused.current_item.status == LifeScheduleStatus.PAUSED

    fake.advance(minutes=5)
    await service._on_chat_complete("chat.complete", {"session_id": "s", "turn_id": "t", "full_text": "hi"})
    await service.drain()
    resumed = runtime.snapshot(fake.current)
    assert resumed.current_item is not None and resumed.current_item.status == LifeScheduleStatus.RUNNING
    event_types = [event.event_type for event in continuity_store.list_life_events("kurisu")]
    assert "chat_interrupted" in event_types and "chat_resumed" in event_types
    await service.aclose(graceful=False)


async def test_life_shadow_updates_state_without_main_chat_projection(continuity_store) -> None:
    fake = FakeClock(datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc), 0.0)
    policy, profile = _policy_profile()
    runtime = CharacterLifeRuntime(continuity_store, profile=profile, policy=policy)
    service = ContinuityService(
        continuity_store,
        clock=RealityClock(continuity_store, now_provider=fake.now, monotonic_provider=fake.monotonic),
        memory_enabled=False,
        relationship_enabled=False,
        life_runtime=runtime,
        life_policy=policy,
        life_enabled=True,
        life_live_enabled=False,
    )
    service.start()
    assert continuity_store.get_life_schedule("kurisu", "2026-09-18") is not None
    grounding = service.grounding_for_turn("hello")
    assert "Character Life context:" not in grounding.text
    await service.aclose(graceful=False)


async def test_life_live_projection_is_bounded_qualitative_and_non_authoritative(continuity_store) -> None:
    fake = FakeClock(datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc), 0.0)
    policy, profile = _policy_profile()
    runtime = CharacterLifeRuntime(continuity_store, profile=profile, policy=policy)
    service = ContinuityService(
        continuity_store,
        clock=RealityClock(continuity_store, now_provider=fake.now, monotonic_provider=fake.monotonic),
        memory_enabled=False,
        relationship_enabled=False,
        life_runtime=runtime,
        life_policy=policy,
        life_enabled=True,
        life_live_enabled=True,
    )
    service.start()
    grounding = service.grounding_for_turn("hello")
    assert "Character Life context:" in grounding.text
    assert "SIMULATED_LIFE" in grounding.text
    assert "not Canon" in grounding.text
    assert "permission" in grounding.text and "Work state" in grounding.text
    assert "event_id" not in grounding.text
    assert "source_hash" not in grounding.text
    assert "thread_id" not in grounding.text
    assert len(grounding.text) <= service.retrieval_policy.max_context_chars
    await service.aclose(graceful=False)
