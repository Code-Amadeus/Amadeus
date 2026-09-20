from core.continuity import (
    ContinuityService, ContinuityStore, MemoryCandidate, MemoryKind, MemoryPriorityClass,
    MemoryResolver, RetentionTier, TurnEvidence,
)


def _put(store, *, key="loop", when=0.0, work_item_id=""):
    evidence = TurnEvidence("s", f"t-{key}", "todo", observed_at=when)
    candidate = MemoryCandidate(
        memory_key=key, kind=MemoryKind.OPEN_LOOP, summary="unfinished task",
        importance=0.2, priority_class=MemoryPriorityClass.P2,
        work_item_id=work_item_id,
    )
    store.apply_memory_candidates(evidence, [candidate], resolver=MemoryResolver(), complete_turn=True)


def test_truth_state_and_retention_tier_are_independent():
    store = ContinuityStore(":memory:")
    _put(store, when=0.0)
    result = store.run_retention_maintenance(now=200 * 86400.0)
    assert result["archived"] == 1
    record = store.get_active_memory("loop")
    assert record is not None and record.state.value == "active"
    assert record.retention_tier is RetentionTier.ARCHIVE
    assert store.get_memory_history("loop")[0].state.value == "active"


def test_pinned_memory_stays_hot_and_maintenance_is_idempotent():
    store = ContinuityStore(":memory:")
    evidence = TurnEvidence("s", "t-pinned", "remember", observed_at=0.0)
    candidate = MemoryCandidate(memory_key="p", kind=MemoryKind.USER_FACT, summary="critical", pinned=True, priority_class=MemoryPriorityClass.P0)
    store.apply_memory_candidates(evidence, [candidate], resolver=MemoryResolver(), complete_turn=True)
    first = store.run_retention_maintenance(now=200 * 86400.0)
    second = store.run_retention_maintenance(now=200 * 86400.0)
    assert first["scanned"] == second["scanned"] == 1
    assert store.get_active_memory("p").retention_tier is RetentionTier.HOT


def test_work_update_is_idempotent_and_downgrades_terminal_open_loop():
    store = ContinuityStore(":memory:")
    _put(store, key="wloop", work_item_id="w-1")
    payload = {"event_id": "evt-1", "work": {"id": "w-1", "state": "completed"}}
    assert store.apply_work_update(payload)["updated"] == 1
    assert store.apply_work_update(payload)["applied"] is False
    record = store.get_active_memory("wloop")
    assert record is not None and record.retention_tier is RetentionTier.COLD


def test_forget_removes_memory_regardless_of_retention_tier():
    store = ContinuityStore(":memory:")
    _put(store, key="forget-me", when=0.0)
    store.run_retention_maintenance(now=200 * 86400.0)
    assert store.forget_memory("forget-me") == 1
    assert store.list_memories_by_tier(RetentionTier.ARCHIVE) == []
    assert store.get_active_tombstone("forget-me") is not None


def test_work_snapshot_shape_is_normalized_and_stale_update_is_ignored():
    store = ContinuityStore(":memory:")
    _put(store, key="real-work", work_item_id="w-real")
    terminal = {"event_id": "new", "work": {"id": "w-real", "status": "succeeded"}}
    stale = {"event_id": "old", "work": {"id": "w-real", "status": "running"}}
    assert store.apply_work_update(terminal, observed_at=200.0)["updated"] == 1
    result = store.apply_work_update(stale, observed_at=100.0)
    assert result["applied"] is False and result["stale"] is True
    assert store.get_active_memory("real-work").retention_tier is RetentionTier.COLD


def test_retention_maintenance_enforces_hot_working_set_cap_idempotently():
    store = ContinuityStore(":memory:")
    for index in range(3):
        _put(store, key=f"cap-{index}", when=0.0)
    store.run_retention_maintenance(now=0.0, max_hot_memories=2)
    assert len(store.list_memories_by_tier(RetentionTier.HOT)) == 2
    assert len(store.list_memories_by_tier(RetentionTier.COLD)) == 1
    second = store.run_retention_maintenance(now=0.0, max_hot_memories=2)
    assert second["promoted"] == second["demoted"] == second["archived"] == 0


def test_real_work_snapshot_is_normalized_per_item():
    payload = {
        "reason": "work.complete",
        "work": {
            "revision": "r1",
            "items": [
                {
                    "id": "w-snapshot",
                    "state": "review_ready",
                    "execution": "succeeded",
                    "updatedAtEpoch": 123.5,
                }
            ],
        },
    }
    events = ContinuityService._normalized_work_events(payload)
    assert len(events) == 1
    normalized, observed = events[0]
    assert normalized["work"] == {"id": "w-snapshot", "status": "succeeded"}
    assert observed == 123.5

    reopened, reopened_at = ContinuityService._normalized_work_events({
        "reason": "work.reopen",
        "work": {
            "items": [
                {
                    "id": "w-snapshot",
                    "state": "open",
                    "execution": "succeeded",
                    "updatedAtEpoch": 124.0,
                }
            ]
        },
    })[0]
    assert reopened["work"]["status"] == "reopened"
    assert reopened_at == 124.0
