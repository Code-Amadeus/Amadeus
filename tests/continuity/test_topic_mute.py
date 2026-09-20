"""User-governed topic mutes: suppression hides proactive mention, never recall."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from core.continuity import (
    ContinuityService,
    MemoryCandidate,
    MemoryKind,
    MemoryResolver,
    RealityClock,
)
from core.continuity.memory_retriever import MemoryRetriever
from core.continuity.models import TurnEvidence
from server.handlers.continuity_handler import ContinuityHandler
from server.protocol import Method
from ._support import FakeClock


class FakeEventBus:
    def __init__(self) -> None:
        self.callbacks = defaultdict(list)

    def on(self, method, callback) -> None:
        self.callbacks[method].append(callback)

    def off(self, method, callback) -> None:
        if callback in self.callbacks[method]:
            self.callbacks[method].remove(callback)

    async def emit(self, method, payload) -> None:
        for callback in list(self.callbacks[method]):
            await callback(method, payload)


def _memory(store, *, key: str, summary: str, created_at: float = 100.0):
    records = store.apply_memory_candidates(
        TurnEvidence(
            session_id="s",
            turn_id=f"t-{key}",
            user_text=summary,
            observed_at=created_at,
        ),
        (MemoryCandidate(memory_key=key, kind=MemoryKind.EPISODIC, summary=summary, importance=0.6),),
        resolver=MemoryResolver(),
        complete_turn=False,
    )
    assert len(records) == 1
    return records[0]


def _service(store, tmp_path, *, session_dir=None):
    fake = FakeClock(datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc), 0.0)
    return ContinuityService(
        store,
        clock=RealityClock(store, now_provider=fake.now, monotonic_provider=fake.monotonic),
        memory_enabled=True,
        relationship_enabled=False,
        life_enabled=False,
        archive_recall_enabled=True,
        session_dir=session_dir if session_dir is not None else tmp_path / "sessions",
    )


def test_store_mutes_are_idempotent_and_clearable(continuity_store) -> None:
    created = continuity_store.add_topic_mute("京都那家甜点店", scope="s", session_id="s", turn_id="t1")
    repeated = continuity_store.add_topic_mute("京都那家甜点店", scope="s", session_id="s", turn_id="t2")
    assert repeated.id == created.id
    assert [mute.topic for mute in continuity_store.list_topic_mutes()] == ["京都那家甜点店"]

    cleared = continuity_store.clear_topic_mute(created.id)
    assert cleared is not None and cleared.cleared_at is not None
    assert continuity_store.list_topic_mutes() == []
    assert continuity_store.list_topic_mutes(active_only=False)[0].cleared_at is not None


def test_muted_topic_is_hidden_but_a_user_raised_topic_still_recalls(continuity_store) -> None:
    _memory(continuity_store, key="user.episode.dessert", summary="京都那家甜点店的焙茶巴菲很好吃")
    retriever = MemoryRetriever(continuity_store)
    assert retriever.retrieve("京都那家甜点店怎么样", now=1000.0, scope="s")

    continuity_store.add_topic_mute("京都那家甜点店", scope="s")
    assert retriever.retrieve("那家店后来怎么样了", now=1000.0, scope="s") == []
    resumed = retriever.retrieve("还记得京都那家甜点店吗", now=1000.0, scope="s")
    assert [hit.memory.memory_key for hit in resumed] == ["user.episode.dessert"]


def test_grounding_note_lists_muted_topics_without_injecting_them(continuity_store, tmp_path) -> None:
    _memory(continuity_store, key="user.episode.dessert", summary="京都那家甜点店的焙茶巴菲很好吃")
    continuity_store.add_topic_mute("京都那家甜点店", scope="s")
    service = _service(continuity_store, tmp_path)

    ordinary = service.grounding_for_turn("晚饭吃什么好", session_id="s", turn_id="now")
    assert "焙茶巴菲" not in ordinary.text
    assert "Topics the user asked you not to raise" in ordinary.text
    assert "京都那家甜点店" in ordinary.text

    raised = service.grounding_for_turn("还记得京都那家甜点店吗", session_id="s", turn_id="asked")
    assert "焙茶巴菲" in raised.text
    assert "Topics the user asked you not to raise" not in raised.text
    trace = service.c8_retrieval_traces(limit=1)[0]
    assert trace["mute_count"] == 0 and trace["mute_exempted"] == 1


async def test_explicit_mute_directive_persists_and_does_not_become_memory(continuity_store, tmp_path) -> None:
    service = _service(continuity_store, tmp_path)
    bus = FakeEventBus()
    service.start()
    service.bind_event_bus(bus)

    await bus.emit(
        "chat.user",
        {"session_id": "session-mute", "turn_id": "mute-1", "text": "不要再提京都那家甜点店了"},
    )
    await service.drain()
    assert [mute.topic for mute in continuity_store.list_topic_mutes()] == ["京都那家甜点店"]

    await bus.emit(
        "chat.complete",
        {"session_id": "session-mute", "turn_id": "mute-1", "full_text": "わかった。"},
    )
    await service.drain()
    # A memory-control utterance is never stored as conversation memory.
    assert continuity_store.list_active_memories() == []
    await service.aclose(graceful=False)


async def test_ambiguous_mute_target_is_declined(continuity_store, tmp_path) -> None:
    service = _service(continuity_store, tmp_path)
    bus = FakeEventBus()
    service.start()
    service.bind_event_bus(bus)
    await bus.emit(
        "chat.user",
        {"session_id": "session-mute", "turn_id": "mute-2", "text": "别再提这个了"},
    )
    await service.drain()
    assert continuity_store.list_topic_mutes() == []
    await service.aclose(graceful=False)


async def test_c7_mute_controls_expose_host_only_api(continuity_store, tmp_path) -> None:
    _memory(continuity_store, key="user.episode.dessert", summary="京都那家甜点店的焙茶巴菲很好吃")
    service = _service(continuity_store, tmp_path)
    mute = continuity_store.add_topic_mute("京都那家甜点店")
    handler = ContinuityHandler(service)

    listed = await handler.handle(Method.CONTINUITY_MUTE_LIST, {})
    assert listed and listed["ok"] is True
    assert [item["id"] for item in listed["mutes"]] == [mute.id]

    cleared = await handler.handle(Method.CONTINUITY_MUTE_CLEAR, {"mute_id": mute.id})
    assert cleared and cleared["ok"] is True
    assert cleared["mute"]["topic"] == "京都那家甜点店"
    assert continuity_store.list_topic_mutes() == []

    try:
        await handler.handle(Method.CONTINUITY_MUTE_CLEAR, {"mute_id": "missing"})
    except ValueError as exc:
        assert "not found" in str(exc)
    else:  # pragma: no cover - the handler must fail closed
        raise AssertionError("clearing an unknown mute must fail")
