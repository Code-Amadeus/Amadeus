from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from core.continuity import ContinuityService, ContinuityStore, RealityClock
from core.continuity.models import ConsolidationStatus
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


def _service(store, tmp_path, *, fake=None):
    fake = fake or FakeClock(datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc), 0.0)
    return ContinuityService(
        store,
        clock=RealityClock(
            store,
            now_provider=fake.now,
            monotonic_provider=fake.monotonic,
        ),
        session_dir=tmp_path / "sessions",
    )


async def test_normal_completed_turn_is_consolidated_after_chat_complete(continuity_store, tmp_path) -> None:
    service = _service(continuity_store, tmp_path)
    bus = FakeEventBus()
    service.start()
    service.bind_event_bus(bus)

    await bus.emit(
        "chat.user",
        {"session_id": "session-a", "turn_id": "t1", "text": "我的生日是1月2日"},
    )
    # Passive memory must not be written on the pre-LLM user event.
    assert continuity_store.get_active_memory("user.fact.birth_date") is None

    await bus.emit(
        "chat.complete",
        {"session_id": "session-a", "turn_id": "t1", "full_text": "知道了。"},
    )
    await service.drain()

    memory = continuity_store.get_active_memory("user.fact.birth_date")
    assert memory is not None
    assert memory.object_text == "1月2日"
    state = continuity_store.get_consolidation_turn("session-a", "t1")
    assert state is not None and state.status is ConsolidationStatus.COMPLETED


async def test_explicit_remember_and_forget_use_fast_durable_path(continuity_store, tmp_path) -> None:
    service = _service(continuity_store, tmp_path)
    service.start()

    await service._on_chat_user(
        "chat.user",
        {"session_id": "session-a", "turn_id": "remember", "text": "记住我的生日是1月2日"},
    )
    remembered = continuity_store.get_active_memory("user.fact.birth_date")
    assert remembered is not None
    assert remembered.pinned is True

    await service._on_chat_user(
        "chat.user",
        {"session_id": "session-a", "turn_id": "forget", "text": "忘记我的生日"},
    )
    assert continuity_store.get_active_memory("user.fact.birth_date") is None
    assert continuity_store.get_active_tombstone("user.fact.birth_date") is not None


async def test_assistant_generated_fact_is_not_saved_by_default(continuity_store, tmp_path) -> None:
    service = _service(continuity_store, tmp_path)
    service.start()
    await service._on_chat_user(
        "chat.user",
        {"session_id": "session-a", "turn_id": "t-assistant", "text": "你好"},
    )
    await service._on_chat_complete(
        "chat.complete",
        {
            "session_id": "session-a",
            "turn_id": "t-assistant",
            "full_text": "你的生日是1月2日。",
        },
    )
    await service.drain()
    assert continuity_store.list_active_memories() == []


async def test_registered_completed_turn_recovers_after_restart(tmp_path) -> None:
    db_path = tmp_path / "runtime" / "continuity.sqlite3"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)

    store1 = ContinuityStore(db_path)
    fake1 = FakeClock(datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc), 0.0)
    service1 = ContinuityService(
        store1,
        clock=RealityClock(store1, now_provider=fake1.now, monotonic_provider=fake1.monotonic),
        session_dir=session_dir,
    )
    service1.start()
    await service1._on_chat_user(
        "chat.user",
        {"session_id": "session-recover", "turn_id": "recover-1", "text": "我的职业是研究员"},
    )
    # Ensure the observed turn marker is durable, then simulate the process
    # dying after Session history is saved but before chat.complete consolidation.
    await service1.drain()
    Path(session_dir, "session-recover.json").write_text(
        json.dumps(
            {
                "session_id": "session-recover",
                "dialog": [
                    {
                        "role": "user",
                        "content": "我的职业是研究员",
                        "turn_id": "recover-1",
                        "created_at": "2026-09-12T12:00:00+00:00",
                    },
                    {
                        "role": "assistant",
                        "content": "嗯。",
                        "turn_id": "recover-1",
                        "created_at": "2026-09-12T12:00:03+00:00",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    await service1.aclose(graceful=False)

    store2 = ContinuityStore(db_path)
    fake2 = FakeClock(datetime(2026, 9, 12, 12, 5, tzinfo=timezone.utc), 300.0)
    service2 = ContinuityService(
        store2,
        clock=RealityClock(store2, now_provider=fake2.now, monotonic_provider=fake2.monotonic),
        session_dir=session_dir,
    )
    service2.start()
    await service2.drain()
    memory = store2.get_active_memory("user.fact.occupation")
    assert memory is not None and memory.object_text == "研究员"
    state = store2.get_consolidation_turn("session-recover", "recover-1")
    assert state is not None and state.status is ConsolidationStatus.COMPLETED
    await service2.aclose(graceful=False)


async def test_unregistered_old_session_is_not_auto_imported(tmp_path) -> None:
    db_path = tmp_path / "runtime" / "continuity.sqlite3"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    Path(session_dir, "old-session.json").write_text(
        json.dumps(
            {
                "session_id": "old-session",
                "dialog": [
                    {"role": "user", "content": "我的名字叫旧用户", "turn_id": "old-1"},
                    {"role": "assistant", "content": "知道了", "turn_id": "old-1"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = ContinuityStore(db_path)
    service = ContinuityService(store, session_dir=session_dir)
    service.start()
    await service.drain()
    assert store.list_active_memories() == []
    await service.aclose(graceful=False)


async def test_chat_complete_without_in_memory_user_event_reloads_exact_session_turn(continuity_store, tmp_path) -> None:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    Path(session_dir, "session-rebind.json").write_text(
        json.dumps(
            {
                "session_id": "session-rebind",
                "dialog": [
                    {
                        "role": "user",
                        "content": "我的名字叫真由理",
                        "turn_id": "rebind-1",
                        "created_at": "2026-09-12T12:00:00+00:00",
                    },
                    {
                        "role": "assistant",
                        "content": "嗯。",
                        "turn_id": "rebind-1",
                        "created_at": "2026-09-12T12:00:02+00:00",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service = ContinuityService(continuity_store, session_dir=session_dir)
    service.start()
    await service._on_chat_complete(
        "chat.complete",
        {
            "session_id": "session-rebind",
            "turn_id": "rebind-1",
            "full_text": "嗯。",
        },
    )
    await service.drain()
    memory = continuity_store.get_active_memory("user.fact.name")
    assert memory is not None and memory.object_text == "真由理"
