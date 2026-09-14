"""Cooperative VTS worker ownership; real queues/threads, no devices or models."""

import ast
import asyncio
from pathlib import Path
from queue import Queue
import sys
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from vts import action
from server.protocol import Method


class ObservedStop(Event):
    def __init__(self):
        super().__init__()
        self.waiting = Event()

    def wait(self, timeout=None):
        self.waiting.set()
        return super().wait(timeout)


def start_worker(worker, stop):
    errors = []
    finished = Event()

    def run():
        try:
            worker(stop)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = Thread(target=run, daemon=True)
    thread.start()
    return thread, finished, errors


@pytest.fixture
def isolated_action(monkeypatch):
    manager = SimpleNamespace(
        connected=True,
        trigger_hotkey=Mock(),
        activate_expression=Mock(),
        send_parameters=Mock(),
        send_ping=Mock(),
        send_heartbeat=Mock(),
        _start_background_reconnect=Mock(),
    )
    monkeypatch.setattr(action, "_vts_manager", manager)
    monkeypatch.setattr(action, "_pending_actions", Queue())
    monkeypatch.setattr(action, "_paused", False)
    return manager


@pytest.mark.parametrize("state", ["unconfigured", "idle", "paused"])
def test_action_worker_wakes_and_exits_when_stopped(isolated_action, monkeypatch, state):
    if state == "unconfigured":
        monkeypatch.setattr(action, "_pending_actions", None)
    elif state == "paused":
        monkeypatch.setattr(action, "_paused", True)
        action._pending_actions.put({"type": "HOTKEY", "attrs": {"name": "discard"}})
    stop = ObservedStop()
    thread, finished, errors = start_worker(action.action_worker, stop)
    try:
        assert stop.waiting.wait(1), errors
        stop.set()
        assert finished.wait(1), "worker did not acknowledge stop"
        assert not errors
        isolated_action.trigger_hotkey.assert_not_called()
        if state == "paused":
            assert action._pending_actions.empty()
    finally:
        stop.set()
        thread.join(1)
    assert not thread.is_alive()


def test_paused_drain_stops_even_if_producers_keep_queue_nonempty(isolated_action, monkeypatch):
    stop = Event()
    consumed = []

    def consume():
        consumed.append(True)
        if len(consumed) == 3:
            stop.set()
        return {"type": "HOTKEY"}

    monkeypatch.setattr(action, "_paused", True)
    monkeypatch.setattr(action, "_pending_actions", SimpleNamespace(
        empty=lambda: False, get_nowait=consume))
    thread, finished, errors = start_worker(action.action_worker, stop)
    try:
        assert finished.wait(1)
        assert not errors
        assert len(consumed) == 3
        isolated_action.trigger_hotkey.assert_not_called()
    finally:
        stop.set()
        thread.join(1)


@pytest.mark.parametrize("worker", [action.action_worker, action.heartbeat_worker])
def test_pre_stopped_worker_does_not_act(isolated_action, worker):
    stop = Event()
    stop.set()
    action._pending_actions.put({"type": "HOTKEY", "attrs": {"name": "queued"}})
    thread, finished, errors = start_worker(worker, stop)
    assert finished.wait(1)
    thread.join(1)
    assert not errors
    assert action._pending_actions.qsize() == 1
    isolated_action.trigger_hotkey.assert_not_called()
    isolated_action.send_ping.assert_not_called()
    isolated_action._start_background_reconnect.assert_not_called()


def test_long_parameter_fade_stops_without_finishing_or_consuming_next_action(isolated_action):
    stop = ObservedStop()
    isolated_action.send_parameters.side_effect = lambda _: stop.set()
    action._pending_actions.put({"type": "PARAM", "attrs": {
        "id": "ParamAngleX", "value": "30", "fade": "60"}})
    action._pending_actions.put({"type": "HOTKEY", "attrs": {"name": "next"}})
    thread, finished, errors = start_worker(action.action_worker, stop)
    try:
        assert finished.wait(1), "stop waited for the 60-second fade"
        assert not errors
        isolated_action.send_parameters.assert_called_once()
        isolated_action.trigger_hotkey.assert_not_called()
        assert action._pending_actions.qsize() == 1
    finally:
        stop.set()
        thread.join(1)


def test_action_worker_dispatches_normally_before_stop(isolated_action):
    stop = Event()
    isolated_action.trigger_hotkey.side_effect = lambda _: stop.set()
    action._pending_actions.put({"type": "HOTKEY", "attrs": {"name": "hello"}})
    thread, finished, errors = start_worker(action.action_worker, stop)
    try:
        assert finished.wait(1)
        assert not errors
        isolated_action.trigger_hotkey.assert_called_once_with("hello")
    finally:
        stop.set()
        thread.join(1)


def test_heartbeat_sleep_is_interruptible(isolated_action):
    stop = ObservedStop()
    thread, finished, errors = start_worker(action.heartbeat_worker, stop)
    try:
        assert stop.waiting.wait(1), errors
        stop.set()
        assert finished.wait(1), "stop waited for the three-second heartbeat interval"
        assert not errors
        isolated_action.send_ping.assert_not_called()
    finally:
        stop.set()
        thread.join(1)


@pytest.mark.parametrize("state", ["connected", "disconnected", "paused", "unconfigured"])
def test_heartbeat_preserves_existing_connection_policy(isolated_action, monkeypatch, state):
    class OneBeat(Event):
        def __init__(self):
            super().__init__()
            self.intervals = []

        def wait(self, timeout=None):
            self.intervals.append(timeout)
            if len(self.intervals) == 2:
                self.set()
            return self.is_set()

    if state == "disconnected":
        isolated_action.connected = False
    elif state == "paused":
        monkeypatch.setattr(action, "_paused", True)
    elif state == "unconfigured":
        monkeypatch.setattr(action, "_vts_manager", None)
    stop = OneBeat()
    action.heartbeat_worker(stop)
    assert stop.intervals == [3, 3]
    assert isolated_action.send_ping.call_count == int(state == "connected")
    assert isolated_action.send_heartbeat.call_count == int(state == "connected")
    assert isolated_action._start_background_reconnect.call_count == int(state == "disconnected")


@pytest.mark.parametrize("heartbeat_start_fails", [False, True])
def test_actual_bootstrap_owns_workers_even_if_second_submit_fails(isolated_action, monkeypatch, heartbeat_start_fails):
    """Execute the real final startup/teardown scope without booting heavy devices."""
    async def run():
        path = Path(__file__).resolve().parents[1] / "server/app.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bootstrap = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "bootstrap")
        start = next(i for i, n in enumerate(bootstrap.body) if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "vts_worker_stop" for t in n.targets))
        function = ast.AsyncFunctionDef(name="run_tail",
            args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=bootstrap.body[start:], decorator_list=[])
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        loop = asyncio.get_running_loop()
        futures, stops, callbacks = [], [], []

        def submit(executor, worker, stop):
            if heartbeat_start_fails and futures:
                raise RuntimeError("second submit failed")
            stops.append(stop)
            future = loop.run_in_executor(executor, worker, stop)
            futures.append(future)
            return future

        async def provider_close():
            assert stops and all(stop.is_set() for stop in stops)
            assert all(future.done() and not future.cancelled() for future in futures)
            callbacks.append("provider")

        async def drain_inputs():
            assert callbacks == ["provider"]
            callbacks.append("inputs")

        def close_ledger():
            assert callbacks == ["provider", "inputs"]
            callbacks.append("ledger")

        server_task = loop.create_future()
        server_task.set_result(None)
        monkeypatch.setitem(sys.modules, "asr.mic_input_service", SimpleNamespace(close_mic_input_service=Mock()))
        monkeypatch.setitem(sys.modules, "llm.llama_server", SimpleNamespace(stop_llama_server=Mock()))
        namespace = dict(asyncio=asyncio, threading=SimpleNamespace(Event=Event),
            loop=SimpleNamespace(run_in_executor=submit), _vts_action_mod=action,
            vts_manager=isolated_action, VTS_HEARTBEAT_ENABLED=True, port=0,
                    server_task=server_task, chat_h=SimpleNamespace(close=AsyncMock()), logger=Mock(),
                    cooperative_chat=None,
                    chat_role_delivery=None, cooperative_ledger=None,
                    openclaw_gateway_start_task=None,
                    bus=SimpleNamespace(off=Mock()), Method=Method,
            auip_launch_callback=None, work_preview_auip_callback=None,
            auip_result_entry_callback=None,
            set_auip_launch_coordinator=Mock(), auip_presentation_callback=None,
            auip_engagement_callback=None, auip_engagement=None,
                auip_narration_callback=None, auip_narration=None,
                provider_runtime=SimpleNamespace(set_request_preparer=Mock(),
                    set_native_session_checkpoint=Mock(),
                    set_start_admission_validator=Mock(), close=provider_close),
            provider_activity_h=SimpleNamespace(close=AsyncMock()),
            work_h=SimpleNamespace(drain_inputs=drain_inputs),
            work_ledger=SimpleNamespace(drain_provider_facts=AsyncMock(), close=close_ledger),
            work_observer=SimpleNamespace(close=AsyncMock()), work_preview=SimpleNamespace(close_all=AsyncMock()),
            _stop_gui_render_runtime=Mock(), wake_service=None, asr_manager=None)
        exec(compile(module, str(path), "exec"), namespace)
        try:
            if heartbeat_start_fails:
                with pytest.raises(RuntimeError, match="second submit failed"):
                    await namespace["run_tail"]()
            else:
                await namespace["run_tail"]()
            assert len(futures) == (1 if heartbeat_start_fails else 2)
            assert callbacks == ["provider", "inputs", "ledger"]
        finally:
            for stop in stops:
                stop.set()
            await asyncio.gather(*futures)
    asyncio.run(run())
