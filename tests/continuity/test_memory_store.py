from __future__ import annotations

from core.continuity import (
    ContinuityStore,
    MemoryCandidate,
    MemoryKind,
    MemoryPriorityClass,
    MemoryResolver,
    TurnEvidence,
)


def _evidence(turn_id: str, text: str, *, session_id: str = "session-a") -> TurnEvidence:
    return TurnEvidence(
        session_id=session_id,
        turn_id=turn_id,
        user_text=text,
        assistant_text="ack",
        observed_at=100.0 + int(turn_id.lstrip("t") or "0"),
    )


def _candidate(value: str, *, explicit: bool = False) -> MemoryCandidate:
    return MemoryCandidate(
        memory_key="user.fact.favorite_color",
        kind=MemoryKind.USER_FACT,
        summary=f"我的最喜欢的颜色是{value}",
        predicate="favorite_color",
        object_text=value,
        importance=0.7,
        confidence=0.99,
        priority_class=MemoryPriorityClass.P0 if explicit else MemoryPriorityClass.P2,
        pinned=explicit,
        explicit_keep=explicit,
    )


def test_duplicate_reinforces_once_per_turn_and_survives_restart(tmp_path) -> None:
    db_path = tmp_path / "continuity.sqlite3"
    resolver = MemoryResolver()
    store = ContinuityStore(db_path)
    try:
        first = _evidence("t1", "我的最喜欢的颜色是蓝色")
        store.register_turn_observed(first)
        store.apply_memory_candidates(first, (_candidate("蓝色"),), resolver=resolver, complete_turn=True)

        same_turn = store.apply_memory_candidates(
            first,
            (_candidate("蓝色", explicit=True),),
            resolver=resolver,
            complete_turn=False,
        )
        assert same_turn == [] or same_turn[0].mention_count == 1
        active = store.get_active_memory("user.fact.favorite_color")
        assert active is not None
        assert active.mention_count == 1
        assert active.pinned is True

        second = _evidence("t2", "我的最喜欢的颜色是蓝色")
        store.register_turn_observed(second)
        store.apply_memory_candidates(second, (_candidate("蓝色"),), resolver=resolver, complete_turn=True)
        active = store.get_active_memory("user.fact.favorite_color")
        assert active is not None and active.mention_count == 2
    finally:
        store.close()

    reopened = ContinuityStore(db_path)
    try:
        active = reopened.get_active_memory("user.fact.favorite_color")
        assert active is not None
        assert active.object_text == "蓝色"
        assert active.mention_count == 2
    finally:
        reopened.close()


def test_changed_value_supersedes_without_destroying_history(continuity_store) -> None:
    resolver = MemoryResolver()
    first = _evidence("t1", "我的最喜欢的颜色是蓝色")
    second = _evidence("t2", "我的最喜欢的颜色是红色")
    continuity_store.apply_memory_candidates(first, (_candidate("蓝色"),), resolver=resolver, complete_turn=True)
    continuity_store.apply_memory_candidates(second, (_candidate("红色"),), resolver=resolver, complete_turn=True)

    active = continuity_store.get_active_memory("user.fact.favorite_color")
    assert active is not None and active.object_text == "红色"
    history = continuity_store.get_memory_history("user.fact.favorite_color")
    assert len(history) == 2
    assert history[0].state.value == "superseded"
    assert history[0].valid_to is not None
    assert history[1].state.value == "active"


def test_explicit_forget_hard_deletes_history_and_blocks_passive_reimport(continuity_store) -> None:
    resolver = MemoryResolver()
    first = _evidence("t1", "我的最喜欢的颜色是蓝色")
    continuity_store.apply_memory_candidates(first, (_candidate("蓝色"),), resolver=resolver, complete_turn=True)

    deleted = continuity_store.forget_memory(
        "user.fact.favorite_color",
        kind=MemoryKind.USER_FACT,
        session_id="session-a",
        turn_id="forget-1",
        observed_at=200.0,
    )
    assert deleted == 1
    assert continuity_store.get_memory_history("user.fact.favorite_color") == []
    tombstone = continuity_store.get_active_tombstone("user.fact.favorite_color")
    assert tombstone is not None
    assert not hasattr(tombstone, "summary")
    assert not hasattr(tombstone, "object_text")

    passive = _evidence("t3", "我的最喜欢的颜色是蓝色")
    continuity_store.apply_memory_candidates(passive, (_candidate("蓝色"),), resolver=resolver, complete_turn=True)
    assert continuity_store.get_active_memory("user.fact.favorite_color") is None

    explicit = TurnEvidence(
        session_id="session-a",
        turn_id="t4",
        user_text="记住我的最喜欢的颜色是蓝色",
        assistant_text="ack",
        observed_at=300.0,
    )
    continuity_store.apply_memory_candidates(
        explicit,
        (_candidate("蓝色", explicit=True),),
        resolver=resolver,
        complete_turn=True,
    )
    restored = continuity_store.get_active_memory("user.fact.favorite_color")
    assert restored is not None and restored.pinned is True
    assert continuity_store.get_active_tombstone("user.fact.favorite_color") is None


def test_consolidation_writes_and_completion_marker_are_atomic(continuity_store) -> None:
    class ExplodingResolver(MemoryResolver):
        def __init__(self) -> None:
            self.calls = 0

        def resolve(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("injected resolver failure")
            return super().resolve(**kwargs)

    first = MemoryCandidate(
        memory_key="user.fact.one",
        kind=MemoryKind.USER_FACT,
        summary="我的项目是一号",
        predicate="one",
        object_text="一号",
    )
    second = MemoryCandidate(
        memory_key="user.fact.two",
        kind=MemoryKind.USER_FACT,
        summary="我的项目是二号",
        predicate="two",
        object_text="二号",
    )
    evidence = _evidence("t9", "两个候选")
    continuity_store.register_turn_observed(evidence)

    import pytest

    with pytest.raises(RuntimeError, match="injected resolver failure"):
        continuity_store.apply_memory_candidates(
            evidence,
            (first, second),
            resolver=ExplodingResolver(),
            complete_turn=True,
        )

    assert continuity_store.get_active_memory("user.fact.one") is None
    assert continuity_store.get_active_memory("user.fact.two") is None
    state = continuity_store.get_consolidation_turn("session-a", "t9")
    assert state is not None and state.status.value == "observed"


def test_tombstone_table_does_not_copy_forgotten_plaintext(tmp_path) -> None:
    import sqlite3

    db_path = tmp_path / "continuity.sqlite3"
    store = ContinuityStore(db_path)
    try:
        resolver = MemoryResolver()
        evidence = _evidence("t1", "我的最喜欢的颜色是极光蓝")
        store.apply_memory_candidates(
            evidence,
            (_candidate("极光蓝"),),
            resolver=resolver,
            complete_turn=True,
        )
        store.forget_memory(
            "user.fact.favorite_color",
            kind=MemoryKind.USER_FACT,
            session_id="session-a",
            turn_id="forget-raw",
        )
    finally:
        store.close()

    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT memory_key, kind, reason FROM memory_tombstones WHERE cleared_at IS NULL"
        ).fetchone()
        item_count = connection.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
    finally:
        connection.close()
    assert row == ("user.fact.favorite_color", "user_fact", "explicit_forget")
    assert item_count == 0
    assert "极光蓝" not in "|".join(str(value or "") for value in row)


def test_stale_explicit_remember_cannot_resurrect_after_newer_forget(continuity_store) -> None:
    resolver = MemoryResolver()
    old_remember = TurnEvidence(
        session_id="session-a",
        turn_id="old-remember",
        user_text="记住我的最喜欢的颜色是蓝色",
        assistant_text="ack",
        observed_at=100.0,
    )
    continuity_store.apply_memory_candidates(
        old_remember,
        (_candidate("蓝色", explicit=True),),
        resolver=resolver,
        complete_turn=True,
    )
    continuity_store.forget_memory(
        "user.fact.favorite_color",
        kind=MemoryKind.USER_FACT,
        session_id="session-a",
        turn_id="forget-newer",
        observed_at=200.0,
    )

    replayed_old = TurnEvidence(
        session_id="session-a",
        turn_id="replayed-old",
        user_text="记住我的最喜欢的颜色是蓝色",
        assistant_text="ack",
        observed_at=100.0,
    )
    continuity_store.apply_memory_candidates(
        replayed_old,
        (_candidate("蓝色", explicit=True),),
        resolver=resolver,
        complete_turn=True,
    )
    assert continuity_store.get_active_memory("user.fact.favorite_color") is None
    assert continuity_store.get_active_tombstone("user.fact.favorite_color") is not None


def test_stale_conflicting_recovery_cannot_override_newer_active_value(continuity_store) -> None:
    resolver = MemoryResolver()
    newer = TurnEvidence(
        session_id="session-a",
        turn_id="newer",
        user_text="我的最喜欢的颜色是红色",
        assistant_text="ack",
        observed_at=200.0,
    )
    continuity_store.apply_memory_candidates(
        newer,
        (_candidate("红色"),),
        resolver=resolver,
        complete_turn=True,
    )

    stale = TurnEvidence(
        session_id="session-old",
        turn_id="stale",
        user_text="我的最喜欢的颜色是蓝色",
        assistant_text="ack",
        observed_at=100.0,
    )
    continuity_store.apply_memory_candidates(
        stale,
        (_candidate("蓝色"),),
        resolver=resolver,
        complete_turn=True,
    )
    active = continuity_store.get_active_memory("user.fact.favorite_color")
    assert active is not None and active.object_text == "红色"
    assert len(continuity_store.get_memory_history("user.fact.favorite_color")) == 1
