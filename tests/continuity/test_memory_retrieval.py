from __future__ import annotations

from dataclasses import replace

from core.continuity import (
    MemoryCandidate,
    MemoryKind,
    MemoryPriorityClass,
    MemoryResolver,
    MemoryRetriever,
    TurnEvidence,
)
from core.continuity.context_renderer import render_continuity_grounding
from core.continuity.retrieval_policy import ContinuityRetrievalPolicy


def _write(store, *, turn: str, key: str, summary: str, object_text: str = "", kind=MemoryKind.USER_FACT, importance=0.6, observed_at=1000.0):
    evidence = TurnEvidence(
        session_id="session-r",
        turn_id=turn,
        user_text=summary,
        assistant_text="ack",
        observed_at=observed_at,
    )
    candidate = MemoryCandidate(
        memory_key=key,
        kind=kind,
        summary=summary,
        predicate=key.rsplit(".", 1)[-1],
        object_text=object_text,
        importance=importance,
        confidence=1.0,
        priority_class=MemoryPriorityClass.P2,
    )
    store.apply_memory_candidates(evidence, (candidate,), resolver=MemoryResolver(), complete_turn=True)
    record = store.get_active_memory(key, scope="session-r")
    assert record is not None
    return record


def test_structured_slot_recall_works_for_short_chinese_query(continuity_store) -> None:
    _write(
        continuity_store,
        turn="t1",
        key="user.fact.birth_date",
        summary="我的生日是1月2日",
        object_text="1月2日",
    )
    retriever = MemoryRetriever(continuity_store)
    hits = retriever.retrieve("生日？", now=1100.0, scope="session-r")
    assert hits
    assert hits[0].memory.memory_key == "user.fact.birth_date"
    assert "structured" in hits[0].reasons


def test_trigram_fts_recalls_chinese_phrase_without_semantic_dependencies(continuity_store) -> None:
    _write(
        continuity_store,
        turn="t1",
        key="user.note.paper",
        summary="记住我们下次继续讨论记忆巩固论文",
        kind=MemoryKind.TOPIC,
    )
    retriever = MemoryRetriever(continuity_store)
    hits = retriever.retrieve("上次说的记忆巩固论文呢", now=1100.0, scope="session-r")
    assert any(hit.memory.memory_key == "user.note.paper" for hit in hits)
    assert any("lexical" in hit.reasons for hit in hits)


def test_retrieval_excludes_current_turn_and_expired_memory(continuity_store) -> None:
    current = _write(
        continuity_store,
        turn="current",
        key="user.fact.birth_date",
        summary="我的生日是1月2日",
        object_text="1月2日",
        observed_at=1000.0,
    )
    expired = _write(
        continuity_store,
        turn="old",
        key="user.note.old",
        summary="记忆巩固论文旧笔记",
        kind=MemoryKind.TOPIC,
        observed_at=900.0,
    )
    continuity_store._connection.execute(
        "UPDATE memory_items SET expires_at = ? WHERE id = ?",
        (950.0, expired.id),
    )
    retriever = MemoryRetriever(continuity_store)
    hits = retriever.retrieve("我的生日和记忆巩固论文", now=1100.0, exclude_turn_id="current", scope="session-r")
    ids = {hit.memory.id for hit in hits}
    assert current.id not in ids
    assert expired.id not in ids


def test_semantic_search_is_optional_and_combines_with_host_scoring(continuity_store) -> None:
    first = _write(
        continuity_store,
        turn="t1",
        key="user.note.alpha",
        summary="完全无共同字面的第一条",
        kind=MemoryKind.TOPIC,
        importance=0.4,
    )
    second = _write(
        continuity_store,
        turn="t2",
        key="user.note.beta",
        summary="另一条完全无共同字面的内容",
        kind=MemoryKind.TOPIC,
        importance=0.4,
    )

    class FakeSemantic:
        def search(self, query, records, *, top_k):
            assert query == "conceptual cue"
            return {first.id: 0.95, second.id: 0.2}

    retriever = MemoryRetriever(continuity_store, semantic_searcher=FakeSemantic())
    hits = retriever.retrieve("conceptual cue", now=1100.0, scope="session-r")
    assert hits and hits[0].memory.id == first.id
    assert hits[0].semantic_score == 0.95
    assert "semantic" in hits[0].reasons


def test_mmr_and_context_budget_avoid_duplicate_memory_dump(continuity_store) -> None:
    records = []
    for index, text in enumerate((
        "我喜欢在下雨天待在家看电影",
        "下雨天我喜欢待在家里看电影",
        "我喜欢雨天在家看电影和休息",
        "我喜欢咖啡",
    )):
        records.append(_write(
            continuity_store,
            turn=f"t{index}",
            key=f"user.note.{index}",
            summary=text,
            kind=MemoryKind.TOPIC,
            importance=0.7,
            observed_at=1000.0 + index,
        ))
    policy = replace(
        ContinuityRetrievalPolicy(),
        max_items=3,
        mmr_lambda=0.55,
        max_context_chars=900,
    )
    retriever = MemoryRetriever(continuity_store, policy=policy)
    hits = retriever.retrieve("下雨天在家看电影", now=1200.0, scope="session-r")
    assert len(hits) <= 3
    rendered = render_continuity_grounding(hits, max_chars=policy.max_context_chars)
    assert len(rendered.text) <= policy.max_context_chars
    assert "score" not in rendered.text.lower()
    assert "memory_id" not in rendered.text.lower()


def test_fts_tracks_supersede_and_forget_without_becoming_second_truth(continuity_store) -> None:
    old = _write(
        continuity_store,
        turn="t1",
        key="user.fact.favorite_food",
        summary="我的喜欢的食物是寿司",
        object_text="寿司",
    )
    newer = TurnEvidence("session-r", "t2", "我的喜欢的食物是拉面", "ack", observed_at=1100.0)
    candidate = MemoryCandidate(
        memory_key="user.fact.favorite_food",
        kind=MemoryKind.USER_FACT,
        summary="我的喜欢的食物是拉面",
        predicate="favorite_food",
        object_text="拉面",
    )
    continuity_store.apply_memory_candidates(newer, (candidate,), resolver=MemoryResolver(), complete_turn=True)
    assert not any(record.id == old.id for record, _ in continuity_store.search_memory_fts('"寿司"', scope="session-r"))
    assert any(record.object_text == "拉面" for record, _ in continuity_store.search_memory_fts('"喜欢的食物"', scope="session-r"))

    continuity_store.forget_memory("user.fact.favorite_food", scope="session-r", session_id="session-r", turn_id="forget")
    assert continuity_store.search_memory_fts('"喜欢的食物"', scope="session-r") == []
    assert continuity_store.rebuild_fts() == 0


def test_recall_accounting_updates_only_selected_rendered_memory(continuity_store, fake_clock) -> None:
    from core.continuity.clock import RealityClock
    from core.continuity.service import ContinuityService

    record = _write(
        continuity_store,
        turn="old",
        key="user.fact.birth_date",
        summary="我的生日是1月2日",
        object_text="1月2日",
        observed_at=fake_clock.current.timestamp() - 86400,
    )
    clock = RealityClock(
        continuity_store,
        now_provider=fake_clock.now,
        monotonic_provider=fake_clock.monotonic,
    )
    service = ContinuityService(continuity_store, clock=clock)
    grounding = service.grounding_for_turn("你还记得我的生日吗？", session_id="session-r", turn_id="new")
    assert grounding.memory_count == 1
    assert "1月2日" in grounding.text
    updated = continuity_store.get_active_memory("user.fact.birth_date", scope="session-r")
    assert updated is not None
    assert updated.recall_count == record.recall_count + 1
    assert updated.last_recalled_at is not None


def test_structured_slot_recall_is_not_lost_behind_large_generic_candidate_window(continuity_store) -> None:
    target = _write(
        continuity_store,
        turn="birthday-old",
        key="user.fact.birth_date",
        summary="我的生日是1月2日",
        object_text="1月2日",
        importance=0.1,
        observed_at=1.0,
    )
    for index in range(150):
        _write(
            continuity_store,
            turn=f"decoy-{index}",
            key=f"user.fact.decoy_{index}",
            summary=f"测试事实 {index}",
            object_text=str(index),
            importance=1.0,
            observed_at=100.0 + index,
        )

    hits = MemoryRetriever(continuity_store).retrieve("生日？", now=1000.0, scope="session-r")
    assert hits
    assert hits[0].memory.id == target.id
    assert "structured" in hits[0].reasons


def test_semantic_search_scans_beyond_fast_generic_candidate_window(continuity_store) -> None:
    target = _write(
        continuity_store,
        turn="semantic-old",
        key="user.note.old_semantic_target",
        summary="一条很早的概念记忆",
        kind=MemoryKind.TOPIC,
        importance=0.1,
        observed_at=1.0,
    )
    for index in range(150):
        _write(
            continuity_store,
            turn=f"semantic-decoy-{index}",
            key=f"user.note.semantic_decoy_{index}",
            summary=f"近期高优先级内容 {index}",
            kind=MemoryKind.TOPIC,
            importance=1.0,
            observed_at=100.0 + index,
        )

    class FakeSemantic:
        def search(self, query, records, *, top_k):
            assert query == "conceptual cue"
            assert any(record.id == target.id for record in records)
            return {target.id: 0.99}

    hits = MemoryRetriever(
        continuity_store,
        semantic_searcher=FakeSemantic(),
    ).retrieve("conceptual cue", now=1000.0, scope="session-r")
    assert hits and hits[0].memory.id == target.id
    assert hits[0].semantic_score == 0.99


def test_recall_accounting_does_not_rebuild_fts_row(continuity_store) -> None:
    record = _write(
        continuity_store,
        turn="fts-stable",
        key="user.note.fts_stable",
        summary="记忆巩固论文索引稳定性",
        kind=MemoryKind.TOPIC,
    )
    before = continuity_store._connection.execute(
        "SELECT rowid FROM memory_fts WHERE memory_id = ?", (record.id,)
    ).fetchone()
    assert before is not None
    continuity_store.mark_memories_recalled((record.id,), recalled_at=1200.0)
    after = continuity_store._connection.execute(
        "SELECT rowid FROM memory_fts WHERE memory_id = ?", (record.id,)
    ).fetchone()
    assert after is not None
    assert after[0] == before[0]
