from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.continuity import (
    CharacterLifeProfile,
    CharacterLifeRuntime,
    ContinuityService,
    LifePolicy,
    MemoryCandidate,
    MemoryKind,
    MemoryResolver,
    TurnEvidence,
)
from core.continuity.clock import RealityClock
from core.continuity.relationship import RelationshipPolicy
from ._support import FakeClock


def _insert_memory(store, *, key: str = "user.preference.tea", summary: str = "我喜欢红茶"):
    evidence = TurnEvidence(session_id="s", turn_id="t", user_text=summary, observed_at=1000.0)
    records = store.apply_memory_candidates(
        evidence,
        (MemoryCandidate(memory_key=key, kind=MemoryKind.PREFERENCE, summary=summary),),
        resolver=MemoryResolver(),
        complete_turn=False,
    )
    assert len(records) == 1
    return records[0]


def test_c7_memory_list_pin_and_forget_use_host_store_controls(continuity_store) -> None:
    record = _insert_memory(continuity_store)
    service = ContinuityService(continuity_store, memory_enabled=False, relationship_enabled=False)

    listed = service.c7_list_memories()
    assert listed[0]["id"] == record.id
    assert listed[0]["summary"] == "我喜欢红茶"
    assert "source_hash" not in listed[0]

    pinned = service.c7_set_memory_pinned(record.id, True)
    assert pinned["pinned"] is True
    assert pinned["retention_tier"] == "hot"
    assert continuity_store.get_memories_by_ids((record.id,))[0].pinned is True

    result = service.c7_forget_memory(record.id)
    assert result["deleted"] == 1
    assert service.c7_list_memories() == []
    assert continuity_store.get_active_tombstone("user.preference.tea") is not None


@pytest.mark.asyncio
async def test_c7_rebuild_indexes_only_rebuilds_derived_state(continuity_store) -> None:
    record = _insert_memory(continuity_store)
    continuity_store.upsert_memory_embedding(
        record.id,
        model_id="test-model",
        dimension=2,
        vector_blob=b"12345678",
        content_hash="stale",
    )
    service = ContinuityService(continuity_store, memory_enabled=False, relationship_enabled=False)
    result = await service.c7_rebuild_indexes()
    assert result == {
        "fts_row_count": 1,
        "cleared_embedding_rows": 1,
        "semantic_rebuild": "lazy",
    }
    assert continuity_store.get_active_memory("user.preference.tea") is not None


def test_c7_status_and_schedule_preserve_simulated_life_classification(continuity_store) -> None:
    fake = FakeClock(datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc), 0.0)
    life_policy = LifePolicy.load("config/continuity_policy.json")
    profile = CharacterLifeProfile.load("character_continuity/kurisu.json")
    life_runtime = CharacterLifeRuntime(continuity_store, profile=profile, policy=life_policy)
    life_runtime.ensure_today(fake.current)
    service = ContinuityService(
        continuity_store,
        clock=RealityClock(continuity_store, now_provider=fake.now, monotonic_provider=fake.monotonic),
        memory_enabled=False,
        relationship_enabled=True,
        relationship_policy=RelationshipPolicy.load("config/continuity_policy.json"),
        life_runtime=life_runtime,
        life_policy=life_policy,
        life_enabled=True,
    )
    status = service.c7_status()
    assert status["schema_version"] == 7
    assert status["life"]["source_class"] == "simulated_life"
    assert "source_hash" not in repr(status["relationship"])

    schedule = service.c7_schedule_inspector()
    assert schedule["source_class"] == "simulated_life"
    assert schedule["schedule_present"] is True
    assert schedule["items"]
    assert all("summary" not in item and "source_hash" not in item for item in schedule["items"])
