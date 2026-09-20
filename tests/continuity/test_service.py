from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone

from core.continuity.clock import RealityClock
from core.continuity.service import ContinuityService
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


async def test_service_tracks_accepted_chat_events_and_unbinds(continuity_store) -> None:
    fake = FakeClock(datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc), 10.0)
    clock = RealityClock(
        continuity_store,
        now_provider=fake.now,
        monotonic_provider=fake.monotonic,
    )
    service = ContinuityService(continuity_store, clock=clock)
    bus = FakeEventBus()

    service.start()
    service.bind_event_bus(bus)
    await bus.emit("chat.user", {"session_id": "session-a"})
    fake.advance(seconds=9)
    await bus.emit("chat.complete", {"session_id": "session-a", "turn_id": "t1"})
    await service.drain()

    state = continuity_store.get_clock_state()
    assert state.last_session_id == "session-a"
    assert state.last_user_turn_at is not None
    assert state.last_successful_chat_at is not None
    assert state.last_successful_chat_at - state.last_user_turn_at == 9.0

    service.unbind_event_bus()
    assert bus.callbacks["chat.user"] == []
    assert bus.callbacks["chat.complete"] == []


async def test_event_callback_does_not_wait_for_sqlite_worker(continuity_store) -> None:
    import threading

    fake = FakeClock(datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc), 10.0)
    clock = RealityClock(
        continuity_store,
        now_provider=fake.now,
        monotonic_provider=fake.monotonic,
    )
    service = ContinuityService(continuity_store, clock=clock)
    started = threading.Event()
    release = threading.Event()

    original = clock.mark_user_turn

    def slow_mark_user_turn(**kwargs):
        started.set()
        release.wait(timeout=2)
        return original(**kwargs)

    clock.mark_user_turn = slow_mark_user_turn  # type: ignore[method-assign]
    service.start()

    # The event callback only queues persistence and therefore returns while the
    # worker is deliberately blocked.
    await service._on_chat_user("chat.user", {"session_id": "session-slow"})
    assert await asyncio.to_thread(started.wait, 0.5)
    assert service._clock_tail is not None and not service._clock_tail.done()

    release.set()
    await service.drain()
    assert continuity_store.get_clock_state().last_session_id == "session-slow"


async def test_graceful_close_is_persisted_for_next_process(tmp_path) -> None:
    from core.continuity import ContinuityStore

    db_path = tmp_path / "continuity.sqlite3"
    store = ContinuityStore(db_path)
    fake = FakeClock(datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc), 0.0)
    service = ContinuityService(
        store,
        clock=RealityClock(
            store,
            now_provider=fake.now,
            monotonic_provider=fake.monotonic,
        ),
    )
    service.start()
    fake.advance(minutes=3)
    await service.aclose(graceful=True)

    reopened = ContinuityStore(db_path)
    try:
        state = reopened.get_clock_state()
        assert state.last_graceful_shutdown_at == fake.current.timestamp()
    finally:
        reopened.close()


async def test_global_continuity_disable_prevents_clock_and_memory_writes(continuity_store) -> None:
    fake = FakeClock(datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc), 0.0)
    service = ContinuityService(
        continuity_store,
        clock=RealityClock(
            continuity_store,
            now_provider=fake.now,
            monotonic_provider=fake.monotonic,
        ),
        continuity_enabled=False,
    )
    bus = FakeEventBus()
    service.start()
    service.bind_event_bus(bus)
    await bus.emit(
        "chat.user",
        {"session_id": "disabled", "turn_id": "t1", "text": "记住：我的生日是1月2日"},
    )
    await bus.emit(
        "chat.complete",
        {"session_id": "disabled", "turn_id": "t1", "full_text": "好"},
    )
    await service.drain()

    state = continuity_store.get_clock_state()
    assert state.last_app_started_at is None
    assert state.last_user_turn_at is None
    assert continuity_store.list_active_memories() == []
    assert continuity_store.list_recoverable_turns() == []
    service.unbind_event_bus()


async def test_memory_disable_keeps_reality_clock_but_skips_memory_pipeline(continuity_store) -> None:
    fake = FakeClock(datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc), 0.0)
    service = ContinuityService(
        continuity_store,
        clock=RealityClock(
            continuity_store,
            now_provider=fake.now,
            monotonic_provider=fake.monotonic,
        ),
        memory_enabled=False,
    )
    bus = FakeEventBus()
    service.start()
    service.bind_event_bus(bus)
    await bus.emit(
        "chat.user",
        {"session_id": "clock-only", "turn_id": "t1", "text": "记住：我的生日是1月2日"},
    )
    fake.advance(seconds=5)
    await bus.emit(
        "chat.complete",
        {"session_id": "clock-only", "turn_id": "t1", "full_text": "好"},
    )
    await service.drain()

    state = continuity_store.get_clock_state()
    assert state.last_user_turn_at is not None
    assert state.last_successful_chat_at is not None
    assert continuity_store.list_active_memories() == []
    assert continuity_store.list_recoverable_turns() == []
    service.unbind_event_bus()
