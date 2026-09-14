"""Host approval lifecycle against the actual SDK's single-reader transport."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from queue import Queue
import threading
from types import SimpleNamespace

import pytest
from openai_codex import CodexConfig
from openai_codex.api import AsyncTurnHandle

from agent_host.adapters.codex_app_server import (
    CodexAppServerAdapter,
    _ApprovalAwareAsyncCodexClient,
)
from agent_host.provider_identity import PARENT_CONTEXT_DELIVERED_EVENT
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderPermissionResponse, ProviderSteerRequest
from server.event_bus import bus
from server.protocol import Method
from test_codex_app_server_adapter import _FakeCodex, _FakeThread, _FakeTurn, _request, _turn_completed


class Peer:
    """Only the native peer is fake; SDK framing/reader/router/handle are real."""

    def __init__(self, *, terminal_reply=True):
        self.inbound = Queue()
        self.writes = []
        self.interrupt_written = threading.Event()
        self.approval_replied = threading.Event()
        self.terminal_reply = terminal_reply
        self.interrupt_reply = True
        self.approval_committing = threading.Event()
        self.approval_write_gate = None

    def readline(self):
        return self.inbound.get()

    def send(self, message):
        self.inbound.put(json.dumps(message) + "\n")

    def write(self, line):
        message = json.loads(line)
        if message.get("id") == "approval-wire":
            self.approval_committing.set()
            if self.approval_write_gate is not None:
                assert self.approval_write_gate.wait(3), "fixture approval wire not released"
        self.writes.append(message)
        if message.get("method") == "turn/interrupt":
            self.interrupt_written.set()
            # Both frames are available even when the SDK approval callback
            # prevents its sole reader from consuming them.
            if self.interrupt_reply:
                self.send({"id": message["id"], "result": {}})
                if self.terminal_reply:
                    self.complete(thread_id=message["params"]["threadId"], turn_id=message["params"]["turnId"])
        else:
            assert "method" not in message, message
            if message.get("id") == "approval-wire":
                self.approval_replied.set()

    def flush(self):
        pass

    def close(self):
        pass

    def complete(self, status="interrupted", *, thread_id="thread-wire", turn_id="turn-wire"):
        self.send({"method": "turn/completed", "params": {
            "threadId": thread_id, "turn": {
                "id": turn_id, "status": status, "items": [], "itemsView": "full", "error": None,
            },
        }})


@asynccontextmanager
async def active_wire(root, *, turn_timeout=30, terminal_reply=True, approval_method="item/commandExecution/requestApproval", managed=False, owned_sdk=False):
    peer = Peer(terminal_reply=terminal_reply)
    codex = _FakeCodex([])
    adapter = CodexAppServerAdapter(
        codex=None if owned_sdk else codex,
        codex_factory=(lambda config: codex) if owned_sdk else None,
        approval_timeout_s=60, turn_timeout_s=turn_timeout,
        cancel_confirm_timeout_s=1, sync_desktop_provider=False,
    )
    client = _ApprovalAwareAsyncCodexClient(CodexConfig(), adapter._handle_sdk_approval)
    process_closed = threading.Event()
    def terminate():
        process_closed.set()
        peer.inbound.put("")
    client._sync._proc = SimpleNamespace(stdin=peer, stdout=peer, terminate=terminate, kill=terminate, wait=lambda timeout: 0)
    codex.close = client.close
    async def initialized():
        pass
    sdk = SimpleNamespace(_client=client, _ensure_initialized=initialized)
    handle = AsyncTurnHandle(sdk, "thread-wire", "turn-wire")
    codex.threads.append(_FakeThread("thread-wire", handle))
    events = []
    active_ready, permission_ready, expired = (asyncio.Event() for _ in range(3))
    async def emit(event):
        events.append(event)
        if event.type == PARENT_CONTEXT_DELIVERED_EVENT:
            active_ready.set()
        elif event.type == "permission.requested":
            permission_ready.set()
        elif event.type == "permission.expired":
            expired.set()
    runtime, record = None, None
    if managed:
        original_run = adapter.run
        async def observed_run(request, run_id, runtime_emit):
            async def observed_emit(event):
                await emit(event)
                await runtime_emit(event)
            return await original_run(request, run_id, observed_emit)
        adapter.run = observed_run
        runtime = ProviderRuntime()
        runtime.register(adapter)
        record = await runtime.start(_request(root, "Verify fixture only", write=True))
        run = record.task_handle
        assert run is not None
    else:
        run = asyncio.create_task(adapter.run(_request(root, "Verify fixture only", write=True), "run-wire", emit))
    controls = []
    try:
        await asyncio.wait_for(active_ready.wait(), 2)
        client._sync._start_reader_thread()
        peer.send({"id": "approval-wire", "method": approval_method, "params": {
            "threadId": "thread-wire", "turnId": "turn-wire", "itemId": "command-wire",
            "cwd": str(root), "command": "fixture-verification-no-command-is-executed",
        }})
        await asyncio.wait_for(permission_ready.wait(), 2)
        yield SimpleNamespace(adapter=adapter, peer=peer, run=run, controls=controls,
                              events=events, expired=expired, client=client,
                              runtime=runtime, record=record, codex=codex, sdk=sdk, process_closed=process_closed)
    finally:
        # Always release the real blocking callback, even for the pre-fix failure.
        await adapter.close()
        if peer.approval_write_gate is not None:
            peer.approval_write_gate.set()
        peer.complete()
        await asyncio.wait_for(asyncio.gather(run, *controls, return_exceptions=True), 3)
        if runtime is not None:
            await asyncio.wait_for(runtime.close(), 2)
        peer.inbound.put("")
        if client._sync._reader_thread is not None:
            await asyncio.to_thread(client._sync._reader_thread.join, 1)
            assert not client._sync._reader_thread.is_alive()
        client._sync._proc = None


@pytest.mark.parametrize("method,denied", [
    ("item/commandExecution/requestApproval", {"decision": "decline"}),
    ("item/fileChange/requestApproval", {"decision": "decline"}),
    ("item/permissions/requestApproval", {"permissions": {}, "scope": "turn"}),
])
def test_cancel_releases_approval_before_waiting_for_actual_sdk_interrupt_reply(tmp_path, method, denied):
    async def scenario():
        async with active_wire(tmp_path, approval_method=method) as state:
            cancelled = asyncio.create_task(state.adapter.cancel("run-wire"))
            state.controls.append(cancelled)
            try:
                outcome = await asyncio.wait_for(asyncio.shield(cancelled), 0.5)
            except TimeoutError:
                print({"interrupt_written": state.peer.interrupt_written.is_set(),
                       "queued_peer_frames": state.peer.inbound.qsize(),
                       "approval_replied": any(row.get("id") == "approval-wire" for row in state.peer.writes)})
                raise
            assert outcome["confirmed"] and outcome["cancelled"]
            assert (await state.run).status == "cancelled"
            await asyncio.wait_for(state.expired.wait(), 1)
            assert next(row["result"] for row in state.peer.writes if row.get("id") == "approval-wire") == denied
            interrupts = [row for row in state.peer.writes if row.get("method") == "turn/interrupt"]
            assert len(interrupts) == 1
            assert interrupts[0]["params"] == {"threadId": "thread-wire", "turnId": "turn-wire"}
    asyncio.run(scenario())


def test_turn_deadline_also_releases_approval_before_native_interrupt(tmp_path):
    async def scenario():
        async with active_wire(tmp_path, turn_timeout=1) as state:
            result = await asyncio.wait_for(asyncio.shield(state.run), 2)
            assert result.status == "error" and "timed out" in result.error
            assert state.peer.interrupt_written.is_set()
            assert next(row["result"] for row in state.peer.writes if row.get("id") == "approval-wire") == {"decision": "decline"}
    asyncio.run(scenario())


def test_late_allow_cannot_overwrite_cancel_even_before_callback_removes_request(tmp_path):
    async def scenario():
        async with active_wire(tmp_path) as state:
            request = next(event.payload["permissionRequest"] for event in state.events if event.type == "permission.requested")
            # Hold the callback at its condition reacquisition. The async cancel
            # path on this loop thread can still enter the same reentrant lock.
            with state.adapter._approval_condition:
                cancelled = asyncio.create_task(state.adapter.cancel("run-wire"))
                state.controls.append(cancelled)
                assert await asyncio.to_thread(state.peer.interrupt_written.wait, 1)
                assert request["request_id"] in state.adapter._pending_approvals
                response = await state.adapter.resolve_permission("run-wire", ProviderPermissionResponse(
                    request_id=request["request_id"], allow=True,
                ))
                assert response == {"accepted": False, "reason": "permission_request_not_pending"}
            assert (await asyncio.wait_for(cancelled, 2))["confirmed"]
            assert next(row["result"] for row in state.peer.writes if row.get("id") == "approval-wire") == {"decision": "decline"}
    asyncio.run(scenario())


def test_native_approval_already_returned_is_not_rewritten_by_later_cancel(tmp_path):
    async def scenario():
        async with active_wire(tmp_path) as state:
            request = next(event.payload["permissionRequest"] for event in state.events if event.type == "permission.requested")
            assert await state.adapter.resolve_permission("run-wire", ProviderPermissionResponse(
                request_id=request["request_id"], allow=True,
            )) == {"accepted": True}
            assert await asyncio.to_thread(state.peer.approval_replied.wait, 1)
            cancelled = asyncio.create_task(state.adapter.cancel("run-wire"))
            state.controls.append(cancelled)
            assert (await asyncio.wait_for(cancelled, 2))["confirmed"]
            assert next(row["result"] for row in state.peer.writes if row.get("id") == "approval-wire") == {"decision": "accept"}
            assert not any(event.type == "permission.expired" for event in state.events)
    asyncio.run(scenario())


def test_native_allow_committed_under_lock_can_reach_wire_after_cancel_begins(tmp_path):
    async def scenario():
        async with active_wire(tmp_path) as state:
            state.peer.approval_write_gate = threading.Event()
            request = next(event.payload["permissionRequest"] for event in state.events if event.type == "permission.requested")
            assert await state.adapter.resolve_permission("run-wire", ProviderPermissionResponse(
                request_id=request["request_id"], allow=True,
            )) == {"accepted": True}
            assert await asyncio.to_thread(state.peer.approval_committing.wait, 1)
            assert request["request_id"] not in state.adapter._pending_approvals
            cancelled = asyncio.create_task(state.adapter.cancel("run-wire"))
            state.controls.append(cancelled)
            await asyncio.sleep(0)
            assert state.adapter._active["run-wire"].cancel_requested
            assert not state.peer.approval_replied.is_set()
            state.peer.approval_write_gate.set()
            assert (await asyncio.wait_for(cancelled, 2))["confirmed"]
            assert next(row["result"] for row in state.peer.writes if row.get("id") == "approval-wire") == {"decision": "accept"}
    asyncio.run(scenario())


def test_withdrawn_permission_is_not_native_terminal_and_new_approval_stays_denied(tmp_path):
    async def scenario():
        async with active_wire(tmp_path, terminal_reply=False) as state:
            cancelled = asyncio.create_task(state.adapter.cancel("run-wire"))
            state.controls.append(cancelled)
            outcome = await asyncio.wait_for(cancelled, 2)
            assert outcome == {"confirmed": False, "cancelled": False, "reason": "interrupt_not_terminal"}
            assert not state.run.done()
            state.peer.send({"id": "approval-late", "method": "item/commandExecution/requestApproval", "params": {
                "threadId": "thread-wire", "turnId": "turn-wire", "itemId": "command-late",
                "cwd": str(tmp_path), "command": "must-not-run",
            }})
            # A following SDK RPC proves the reader has processed that callback;
            # this request also receives no invented terminal notification.
            await asyncio.wait_for(state.client.turn_interrupt("thread-wire", "turn-wire"), 1)
            assert next(row["result"] for row in state.peer.writes if row.get("id") == "approval-late") == {"decision": "decline"}
            assert len([event for event in state.events if event.type == "permission.requested"]) == 1
    asyncio.run(scenario())


def test_cancelling_one_run_preserves_other_runs_pending_approval(tmp_path):
    async def scenario():
        turns = [_FakeTurn(f"turn-{number}", blocked=True) for number in (1, 2)]
        adapter = CodexAppServerAdapter(codex=_FakeCodex([
            _FakeThread(f"thread-{number}", turn) for number, turn in enumerate(turns, 1)
        ]), approval_timeout_s=60)
        events = []
        requested = asyncio.Event()
        async def emit(event):
            events.append(event)
            if len([item for item in events if item.type == "permission.requested"]) == 2:
                requested.set()
        runs, approvals = [], []
        try:
            for number, turn in enumerate(turns, 1):
                runs.append(asyncio.create_task(adapter.run(_request(tmp_path, "fixture", write=True), f"run-{number}", emit)))
                await asyncio.wait_for(turn.started.wait(), 1)
                approvals.append(asyncio.create_task(asyncio.to_thread(adapter._handle_sdk_approval,
                    "item/commandExecution/requestApproval", {"threadId": f"thread-{number}",
                    "turnId": turn.id, "itemId": "command", "cwd": str(tmp_path)})))
            await asyncio.wait_for(requested.wait(), 1)
            assert (await asyncio.wait_for(adapter.cancel("run-1"), 1))["confirmed"]
            assert await approvals[0] == {"decision": "decline"}
            assert not approvals[1].done()
            other = next(event.payload["permissionRequest"] for event in events if event.type == "permission.requested" and event.run_id == "run-2")
            assert await adapter.resolve_permission("run-2", ProviderPermissionResponse(
                request_id=other["request_id"], allow=True,
            )) == {"accepted": True}
            assert await approvals[1] == {"decision": "accept"}
        finally:
            await adapter.close()
            for turn in turns:
                await turn.control_events.put(_turn_completed(turn.id, "completed"))
            await asyncio.wait_for(asyncio.gather(*runs, *approvals, return_exceptions=True), 2)
    asyncio.run(scenario())


def test_other_runs_approval_bounds_cancel_without_revoking_it_and_late_receipt_arrives(tmp_path):
    async def scenario():
        async with active_wire(tmp_path) as state:
            other_ready = asyncio.Event()
            other = AsyncTurnHandle(state.sdk, "thread-other", "turn-other")
            state.codex.threads.append(_FakeThread("thread-other", other))
            async def emit(event):
                if event.type == PARENT_CONTEXT_DELIVERED_EVENT:
                    other_ready.set()
            run = asyncio.create_task(state.adapter.run(_request(tmp_path, "other existing turn", write=True), "run-other", emit))
            state.controls.append(run)
            await asyncio.wait_for(other_ready.wait(), 1)
            outcome = await asyncio.wait_for(state.adapter.cancel("run-other"), 2)
            assert outcome == {"confirmed": False, "cancelled": False, "reason": "interrupt_not_terminal"}
            assert state.peer.interrupt_written.is_set() and state.peer.inbound.qsize() == 2
            permission = next(event.payload["permissionRequest"] for event in state.events if event.type == "permission.requested")
            pending = state.adapter._pending_approvals[permission["request_id"]]
            assert pending.run_id == "run-wire" and not pending.resolved.is_set()
            assert not state.adapter._active["run-wire"].cancel_requested
            assert await state.adapter.resolve_permission("run-wire", ProviderPermissionResponse(
                request_id=permission["request_id"], allow=True,
            )) == {"accepted": True}
            assert (await asyncio.wait_for(run, 2)).status == "cancelled"
            assert next(row["result"] for row in state.peer.writes if row.get("id") == "approval-wire") == {"decision": "accept"}
    asyncio.run(scenario())


def test_deadline_remains_bounded_when_interrupt_rpc_never_replies(tmp_path):
    async def scenario():
        async with active_wire(tmp_path, turn_timeout=1) as state:
            state.peer.interrupt_reply = False
            result = await asyncio.wait_for(asyncio.shield(state.run), 3)
            assert result.status == "error" and "timed out" in result.error
            assert state.peer.interrupt_written.is_set()
            assert next(row["result"] for row in state.peer.writes if row.get("id") == "approval-wire") == {"decision": "decline"}
            active = state.adapter._active["run-wire"]
            assert active.cancel_requested and not active.stream_task.done()
            writes = list(state.peer.writes)
            assert await state.adapter.steer("run-wire", ProviderSteerRequest(task="must not reopen", revision=1)) == {
                "accepted": False, "reason": "active_turn_not_found",
            }
            assert state.peer.writes == writes
            state.peer.complete()
            await asyncio.wait_for(asyncio.shield(active.stream_task), 1)
            assert "run-wire" not in state.adapter._active
    asyncio.run(scenario())


def test_owned_sdk_close_drains_timed_out_stream_via_actual_router_eof(tmp_path):
    async def scenario():
        async with active_wire(tmp_path, turn_timeout=1, owned_sdk=True) as state:
            state.peer.interrupt_reply = False
            result = await asyncio.wait_for(asyncio.shield(state.run), 3)
            assert result.status == "error" and "timed out" in result.error
            active = state.adapter._active["run-wire"]
            assert not active.stream_task.done()
            await asyncio.wait_for(state.adapter.close(), 2)
            assert state.process_closed.is_set()
            assert active.stream_task.done()
            assert not state.adapter._active
            assert not state.adapter._pending_approvals
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_caller", [False, True])
def test_runtime_close_drains_real_sdk_approval_cancel_and_publishes_once(tmp_path, cancel_caller):
    async def scenario():
        async with active_wire(tmp_path, terminal_reply=False, managed=True) as state:
            results = []
            async def capture_result(method, params):
                if params.get("run_id") == state.record.run_id:
                    results.append(params)
            bus.on(Method.PROVIDER_RESULT, capture_result)
            try:
                caller = asyncio.create_task(state.runtime.cancel(state.record.run_id))
                state.controls.append(caller)
                assert await asyncio.to_thread(state.peer.interrupt_written.wait, 1)
                await asyncio.wait_for(state.expired.wait(), 1)
                if cancel_caller:
                    caller.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await caller
                closing = asyncio.create_task(state.runtime.close())
                state.controls.append(closing)
                await asyncio.sleep(0)
                assert not closing.done()
                state.peer.complete()
                await asyncio.wait_for(closing, 2)
                assert state.record.status == "cancelled"
                assert [row["type"] for row in state.record.events].count("run.cancelled") == 1
                assert len(results) == 1 and results[0]["status"] == "cancelled"
                assert len([row for row in state.peer.writes if row.get("method") == "turn/interrupt"]) == 1
            finally:
                bus.off(Method.PROVIDER_RESULT, capture_result)
    asyncio.run(scenario())
