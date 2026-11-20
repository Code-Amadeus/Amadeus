"""Concurrent lazy ASR starts share the existing listener lifecycle owner."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import core.turn_coordinator as tc
from server.event_bus import EventBus
from server.handlers.asr_handler import AsrHandler
from server.protocol import Method


@pytest.mark.parametrize(("first_source", "second_source"), [
    ("wake", "wake"), ("microphone", "wake"), ("wake", "microphone"),
])
async def test_two_starts_waiting_for_one_lazy_manager_create_only_one_listener(
        monkeypatch, first_source, second_source):
    monkeypatch.setattr("server.handlers.asr_handler.bus", EventBus())
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    release_factory = threading.Event()
    both_initializing = asyncio.Event()
    finish_listener = asyncio.Event()
    manager = SimpleNamespace(is_ready=True)

    def make_manager():
        assert release_factory.wait(5), "test did not release lazy manager construction"
        return manager

    factory = Mock(side_effect=make_manager)
    handler = AsrHandler()
    handler.configure(asr_manager_factory=factory)
    original_ensure = handler._ensure_asr_manager
    ensure_calls = 0

    async def observe_ensure():
        nonlocal ensure_calls
        ensure_calls += 1
        if ensure_calls == 2:
            both_initializing.set()
        return await original_ensure()

    listener_tasks = []

    async def listen():
        listener_tasks.append(asyncio.current_task())
        await finish_listener.wait()

    listener = AsyncMock(side_effect=listen)
    monkeypatch.setattr(handler, "_ensure_asr_manager", observe_ensure)
    monkeypatch.setattr(handler, "_listen_loop", listener)
    first = asyncio.create_task(handler.start_listening({
        "source":first_source, "awake_seconds":180, "wake":{"event":"first"}}))
    second = asyncio.create_task(handler.start_listening({
        "source":second_source, "awake_seconds":180, "wake":{"event":"second"}}))
    try:
        await asyncio.wait_for(both_initializing.wait(), 2)
        assert not handler._active and handler._listen_task is None
        release_factory.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 3)
        assert factory.call_count == 1
        assert listener.call_count == 1
        assert handler._active and handler._source == "wake"
        assert handler._listen_task is listener_tasks[0]
        assert results[0]["status"] in {"listening", "awake"}
        assert results[1]["status"] == ("awake" if second_source == "wake" else "already_listening")
        assert handler._wake_payload["event"] == ("second" if second_source == "wake" else "first")
        await handler.stop_listening()
        await asyncio.gather(*listener_tasks, return_exceptions=True)
        assert not handler._active and handler._listen_task is None
        await handler.start_listening({"source":"wake", "awake_seconds":180})
        restarted_listener = handler._listen_task
        assert listener.call_count == 2 and factory.call_count == 1
        await handler.stop_listening()
        await asyncio.gather(restarted_listener, return_exceptions=True)
    finally:
        release_factory.set()
        finish_listener.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await asyncio.gather(*listener_tasks, return_exceptions=True)
        if handler._unload_task is not None:
            handler._unload_task.cancel()
            await asyncio.gather(handler._unload_task, return_exceptions=True)


async def test_start_during_initial_status_publication_refreshes_one_reserved_listener(monkeypatch):
    bus = EventBus()
    monkeypatch.setattr("server.handlers.asr_handler.bus", bus)
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    publishing, release = asyncio.Event(), asyncio.Event()

    async def hold_first_status(_method, payload):
        if payload.get("status") == "awake" and payload.get("source_payload", {}).get("id") == "first":
            publishing.set()
            await release.wait()

    bus.on(Method.ASR_STATUS, hold_first_status)
    handler = AsrHandler()
    handler.configure(asr_manager=SimpleNamespace(is_ready=True))
    listener = AsyncMock()
    monkeypatch.setattr(handler, "_listen_loop", listener)
    monkeypatch.setattr(handler, "schedule_unload", Mock())
    first = asyncio.create_task(handler.start_listening({"source":"wake", "awake_seconds":180,
        "source_payload":{"id":"first"}}))
    try:
        await asyncio.wait_for(publishing.wait(), 2)
        assert handler._active and handler._listen_task is None
        second = await handler.start_listening({"source":"wake", "awake_seconds":240,
            "source_payload":{"id":"second"}})
        assert second["status"] == "awake" and handler._awake_seconds == 240
        assert listener.call_count == 0
        release.set()
        await first
        assert listener.call_count == 1
        await handler._listen_task
        await handler.stop_listening()
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize("failure", ["unavailable", "error"])
async def test_lazy_manager_failure_never_reserves_a_listener(monkeypatch, failure):
    monkeypatch.setattr("server.handlers.asr_handler.bus", EventBus())
    handler = AsrHandler()
    factory = Mock(return_value=None) if failure == "unavailable" else Mock(side_effect=RuntimeError("load failed"))
    handler.configure(asr_manager_factory=factory)
    listener = AsyncMock()
    monkeypatch.setattr(handler, "_listen_loop", listener)
    result = await handler.start_listening({"source":"wake", "awake_seconds":180})
    assert result["status"] == "error"
    assert not handler._active and handler._listen_task is None
    listener.assert_not_called()
