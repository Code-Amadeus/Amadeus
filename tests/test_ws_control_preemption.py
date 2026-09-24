"""Explicit control preemption with ordinary FIFO and transport ownership."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.websockets import WebSocketDisconnect
import websockets

from core import session_manager as sm
from core.chat_runtime import get_chat_runtime
from core.turn_coordinator import TurnAuthorityError
from server.handlers.session_handler import SessionHandler
from server.protocol import Method
from server.ws_handler import ConnectionManager
from test_chat_control_ingress import context as context, request


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.seen = {}
        self.fail_id = ""
        self.write_attempts = []

    async def iter_text(self):
        while (value := await self.incoming.get()) is not None:
            yield value

    def put(self, req_id, method, params=None):
        self.incoming.put_nowait(json.dumps({"type": "req", "id": req_id, "method": method, "params": params or {}}))

    async def send_json(self, payload):
        self.write_attempts.append(payload["id"])
        if payload["id"] == self.fail_id:
            raise WebSocketDisconnect()
        self.outgoing.put_nowait(payload)

    async def response(self, req_id):
        async def receive():
            while req_id not in self.seen:
                message = await self.outgoing.get()
                self.seen[message["id"]] = message["params"]
            return self.seen[req_id]
        return await asyncio.wait_for(receive(), 2)

    async def finish(self, reader):
        self.incoming.put_nowait(None)
        await asyncio.wait_for(reader, 2)


def register_blocker(manager, *, method="fixture.block"):
    entered, release = asyncio.Event(), asyncio.Event()
    trace = []
    async def handle(_method, params):
        trace.append("started")
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            trace.append("cancelled")
            raise
        trace.append("completed")
        return {"ok": True}
    manager.register_handler(SimpleNamespace(methods=[method], handle=handle))
    return entered, release, trace


def test_real_loopback_same_socket_abort_preempts_actual_chat_setup(context):
    async def run():
        get_chat_runtime()  # initialized by live bootstrap before WS readiness
        entered, release, aborted, replied = (asyncio.Event() for _ in range(4))
        async def interruption():
            entered.set()
            await release.wait()
        runner = AsyncMock(return_value="must not execute")
        handler, _, _ = context.make(runner=runner)
        handler._presentation_interrupt = interruption
        abort = handler._handle_abort
        async def observed_abort(params):
            if params.get("turn_id") == "turn-u1":
                aborted.set()
            return await abort(params)
        handler._handle_abort = observed_abort
        manager = ConnectionManager()
        manager.register_handler(handler)
        class Adapter:
            def __init__(self, connection):
                self.connection = connection
            async def accept(self):
                pass
            async def iter_text(self):
                async for value in self.connection:
                    yield value
            async def send_json(self, payload):
                await self.connection.send(json.dumps(payload))
        async def connected(connection):
            await manager.handle_connection(Adapter(connection))
        responses = {}
        async with websockets.serve(connected, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as client:
                async def receive():
                    async for value in client:
                        message = json.loads(value)
                        responses[message["id"]] = message["params"]
                        if {"send", "stop"} <= responses.keys():
                            replied.set()
                collector = asyncio.create_task(receive())
                try:
                    await client.send(json.dumps({"type": "req", "id": "send", "method": Method.CHAT_SEND, "params": request()}))
                    await asyncio.wait_for(entered.wait(), 2)
                    await client.send(json.dumps({"type": "req", "id": "stop", "method": Method.CHAT_ABORT, "params": {"turn_id": "turn-u1"}}))
                    await asyncio.wait_for(aborted.wait(), 1)
                    await asyncio.wait_for(replied.wait(), 2)
                finally:
                    release.set()
                    await handler.close()
                    await client.close()
                    await collector
        assert responses["stop"]["status"] == "cancelled_before_admission"
        assert responses["send"]["status"] == "cancelled_before_admission"
        assert context.ledger.find_admission("chat:A", "u1") is None
        runner.assert_not_called()
    asyncio.run(run())


def test_session_creation_and_complete_request_fifo_precede_chat(context):
    async def run():
        get_chat_runtime()
        manager, socket = ConnectionManager(), Socket()
        created, release = asyncio.Event(), asyncio.Event()
        session = SessionHandler()
        async def handle(method, params):
            result = await session.handle(method, params)
            created.set()
            await release.wait()  # installation does not finish the whole request
            return result
        manager.register_handler(SimpleNamespace(methods=[Method.SESSION_CREATE], handle=handle))
        runner = AsyncMock(return_value="hello")
        chat, _, _ = context.make(runner=runner)
        manager.register_handler(chat)
        reader = asyncio.create_task(manager._read_loop(socket, "fifo", socket.send_json))
        socket.put("create", Method.SESSION_CREATE, {"session_id": "created-ws"})
        socket.put("send", Method.CHAT_SEND, request(session="created-ws"))
        await asyncio.wait_for(created.wait(), 2)
        assert sm.get_current_session_id() == "created-ws"
        assert context.ledger.find_admission("chat:created-ws", "u1") is None
        runner.assert_not_called()
        release.set()
        assert (await socket.response("create"))["session"]["id"] == "created-ws"
        assert (await socket.response("send"))["status"] == "ok"
        await chat._stream_task
        assert runner.await_args.kwargs["turn_admission"].session_id == "created-ws"
        assert runner.await_args.kwargs["history_snapshot"].dialog == []
        await socket.finish(reader)
    asyncio.run(run())


@pytest.mark.parametrize("global_abort", [False, True])
def test_queued_aliases_are_cancelled_with_own_response_ids_and_future_input_survives(context, global_abort):
    async def run():
        manager, socket = ConnectionManager(), Socket()
        entered, release, trace = register_blocker(manager)
        runner = AsyncMock(return_value="future")
        chat, _, _ = context.make(runner=runner)
        manager.register_handler(chat)
        reader = asyncio.create_task(manager._read_loop(socket, "queued", socket.send_json))
        socket.put("block", "fixture.block")
        await entered.wait()
        for uid in ("one", "two"):
            socket.put(uid, Method.CHAT_SEND, request(uid, turn="same"))
        socket.put("abort", Method.CHAT_ABORT, {} if global_abort else {"turn_id": "same"})
        assert (await socket.response("abort"))["status"] == ("aborted" if global_abort else "cancelled_before_admission")
        assert trace == ["started"]
        socket.put("future", Method.CHAT_SEND, request("future"))
        release.set()
        for uid in ("one", "two"):
            assert (await socket.response(uid))["status"] == "cancelled_before_admission"
            assert context.ledger.find_admission("chat:A", uid) is None
        assert (await socket.response("future"))["status"] == "ok"
        await chat._stream_task
        runner.assert_awaited_once()
        assert trace == ["started", "completed"]
        await socket.finish(reader)
    asyncio.run(run())


def test_queue_cancellation_does_not_overwrite_a_handler_fence_error(context, monkeypatch):
    async def run():
        manager, socket = ConnectionManager(), Socket()
        entered, release, _ = register_blocker(manager)
        chat, _, _ = context.make(runner=AsyncMock())
        manager.register_handler(chat)
        monkeypatch.setattr(chat, "_advance_chat_epoch", Mock(side_effect=TurnAuthorityError("fence denied")))
        reader = asyncio.create_task(manager._read_loop(socket, "error", socket.send_json))
        socket.put("block", "fixture.block")
        await entered.wait()
        socket.put("send", Method.CHAT_SEND, request())
        socket.put("abort", Method.CHAT_ABORT)
        assert (await socket.response("abort"))["error"] == "fence denied"
        release.set()
        assert (await socket.response("send"))["status"] == "cancelled_before_admission"
        assert context.ledger.get_epoch_fence("foreground") is None
        await socket.finish(reader)
    asyncio.run(run())


def test_abort_on_another_connection_reaches_unstarted_matching_chat(context):
    async def run():
        manager = ConnectionManager()
        first, second = Socket(), Socket()
        entered, release, _ = register_blocker(manager)
        runner = AsyncMock()
        chat, _, _ = context.make(runner=runner)
        manager.register_handler(chat)
        readers = [asyncio.create_task(manager._read_loop(s, str(i), s.send_json)) for i, s in enumerate((first, second))]
        first.put("block", "fixture.block")
        await entered.wait()
        first.put("send", Method.CHAT_SEND, request())
        await asyncio.sleep(0)
        second.put("abort", Method.CHAT_ABORT, {"turn_id": "turn-u1"})
        assert (await second.response("abort"))["status"] == "cancelled_before_admission"
        release.set()
        assert (await first.response("send"))["status"] == "cancelled_before_admission"
        runner.assert_not_called()
        await first.finish(readers[0])
        await second.finish(readers[1])
        assert manager._queued_chats == set()
    asyncio.run(run())


def test_queue_full_refuses_normal_work_but_explicit_control_bypasses_capacity(context, monkeypatch):
    async def run():
        monkeypatch.setattr("server.ws_handler._QUEUED_REQUEST_LIMIT", 2)
        manager, socket = ConnectionManager(), Socket()
        entered, release, _ = register_blocker(manager)
        chat, _, _ = context.make(runner=AsyncMock())
        manager.register_handler(chat)
        reader = asyncio.create_task(manager._read_loop(socket, "full", socket.send_json))
        socket.put("block", "fixture.block")
        await entered.wait()
        for uid in ("one", "two", "overflow"):
            socket.put(uid, Method.CHAT_SEND, request(uid))
        socket.put("abort", Method.CHAT_ABORT)
        assert (await socket.response("overflow"))["error"] == "request queue is full"
        assert (await socket.response("abort"))["status"] == "aborted"
        release.set()
        for uid in ("one", "two"):
            assert (await socket.response(uid))["status"] == "cancelled_before_admission"
        assert context.ledger.get_epoch_fence("foreground") is None
        await socket.finish(reader)
    asyncio.run(run())


@pytest.mark.parametrize("urgent_send_fails", [False, True])
def test_connection_loss_drops_unstarted_queue_but_drains_started_domain_call(context, urgent_send_fails):
    async def run():
        manager, socket = ConnectionManager(), Socket()
        entered, release, trace = register_blocker(manager)
        runner = AsyncMock()
        chat, _, _ = context.make(runner=runner)
        manager.register_handler(chat)
        reader = asyncio.create_task(manager._read_loop(socket, "lost", socket.send_json))
        socket.put("block", "fixture.block")
        await entered.wait()
        socket.put("unstarted", Method.CHAT_SEND, request())
        if urgent_send_fails:
            socket.fail_id = "abort"
            socket.put("abort", Method.CHAT_ABORT, {"turn_id": "stale"})
        else:
            socket.incoming.put_nowait(None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not reader.done() and trace == ["started"]
        release.set()
        await asyncio.wait_for(reader, 2)
        assert trace == ["started", "completed"]
        assert manager._queued_chats == set()
        runner.assert_not_called()
        if urgent_send_fails:
            assert socket.write_attempts == ["abort"]
    asyncio.run(run())


@pytest.mark.parametrize("raw", ["null", "[]", '{"params":[]}', '{"method":{}}', "invalid-json"])
def test_bad_frame_is_refused_without_cancelling_prior_started_request(context, raw):
    async def run():
        manager, socket = ConnectionManager(), Socket()
        entered, release, trace = register_blocker(manager)
        reader = asyncio.create_task(manager._read_loop(socket, "bad", socket.send_json))
        socket.put("block", "fixture.block")
        await entered.wait()
        socket.incoming.put_nowait(raw)
        assert "error" in await socket.response("?")
        assert trace == ["started"] and not reader.done()
        release.set()
        assert (await socket.response("block"))["ok"]
        await socket.finish(reader)
    asyncio.run(run())


def test_explicit_audio_interrupt_keeps_existing_domain_semantics(context):
    async def run():
        manager, socket = ConnectionManager(), Socket()
        entered, release, trace = register_blocker(manager)
        tts = SimpleNamespace(methods=[Method.TTS_INTERRUPT], handle=AsyncMock(return_value={"status": "interrupted"}))
        manager.register_handler(tts)
        reader = asyncio.create_task(manager._read_loop(socket, "tts", socket.send_json))
        socket.put("block", "fixture.block")
        await entered.wait()
        params = {"turn_id": "annotation-only", "annotate_history": False}
        socket.put("tts", Method.TTS_INTERRUPT, params)
        assert (await socket.response("tts"))["status"] == "interrupted"
        tts.handle.assert_awaited_once_with(Method.TTS_INTERRUPT, params)
        assert trace == ["started"]
        release.set()
        await socket.response("block")
        await socket.finish(reader)
    asyncio.run(run())


@pytest.mark.parametrize("control", [Method.VN_LAUNCH_STOP, Method.VN_INPUT_SET, Method.VN_MODE_SET])
@pytest.mark.parametrize("urgent", [Method.TTS_INTERRUPT, Method.CHAT_ABORT])
def test_vn_control_wait_does_not_block_interrupt_and_drains_on_close(context, control, urgent):
    async def run():
        manager, socket = ConnectionManager(), Socket()
        entered, release, finished = (asyncio.Event() for _ in range(3))

        async def handle(method, _params):
            if method == control:
                entered.set()
                await release.wait()
                finished.set()
                return {"status": "done"}
            return {"status": "interrupted"}

        manager.register_handler(SimpleNamespace(methods=[control, urgent], handle=handle))
        reader = asyncio.create_task(manager._read_loop(socket, "vn-control", socket.send_json))
        socket.put("control", control)
        await asyncio.wait_for(entered.wait(), 2)
        socket.put("urgent", urgent)
        assert (await socket.response("urgent"))["status"] == "interrupted"
        socket.incoming.put_nowait(None)
        await asyncio.sleep(0)
        assert not reader.done() and not finished.is_set()
        release.set()
        await asyncio.wait_for(reader, 2)
        assert finished.is_set()

    asyncio.run(run())


def test_handler_self_cancellation_cannot_leave_a_live_receiver_without_worker(context):
    async def run():
        manager, socket = ConnectionManager(), Socket()
        async def handle(method, params):
            raise asyncio.CancelledError()
        manager.register_handler(SimpleNamespace(methods=["fixture.cancel"], handle=handle))
        reader = asyncio.create_task(manager._read_loop(socket, "cancel", socket.send_json))
        socket.put("cancel", "fixture.cancel")
        with pytest.raises(ExceptionGroup) as error:
            await asyncio.wait_for(reader, 2)
        assert "request handler cancelled" in str(error.value.exceptions[0])
        assert manager._queued_chats == set()
    asyncio.run(run())


def test_worker_and_control_responses_share_the_actual_connection_send_lock(context):
    async def run():
        manager = ConnectionManager()
        normal_entered, control_entered, release = (asyncio.Event() for _ in range(3))
        async def handle(method, params):
            (control_entered if method == Method.TTS_INTERRUPT else normal_entered).set()
            await release.wait()
            return {"ok": True}
        manager.register_handler(SimpleNamespace(methods=["fixture.normal", Method.TTS_INTERRUPT], handle=handle))
        class GuardedSocket(Socket):
            sending = False
            async def accept(self):
                pass
            async def send_json(self, payload):
                assert not self.sending, "concurrent transport writes"
                self.sending = True
                try:
                    await asyncio.sleep(0.01)
                    await super().send_json(payload)
                finally:
                    self.sending = False
        socket = GuardedSocket()
        reader = asyncio.create_task(manager.handle_connection(socket))
        socket.put("normal", "fixture.normal")
        await normal_entered.wait()
        socket.put("control", Method.TTS_INTERRUPT)
        await control_entered.wait()
        release.set()
        assert (await socket.response("normal"))["ok"]
        assert (await socket.response("control"))["ok"]
        await socket.finish(reader)
    asyncio.run(run())


def test_external_connection_task_cancellation_still_cancels_its_worker(context):
    async def run():
        manager, socket = ConnectionManager(), Socket()
        entered, release, trace = register_blocker(manager)
        reader = asyncio.create_task(manager._read_loop(socket, "external-cancel", socket.send_json))
        socket.put("block", "fixture.block")
        await entered.wait()
        socket.put("queued", "fixture.block")
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert trace == ["started", "cancelled"]
        assert manager._queued_chats == set()
    asyncio.run(run())
