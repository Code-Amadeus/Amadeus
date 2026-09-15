"""Chat-owned setup/cancellation/shutdown; real files and SQLite, no models."""

import asyncio
import ast
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.turn_coordinator import TurnAuthorityError
from server.handlers.chat_handler import ChatHandler
from server.interrupt_flow import MainTurnInterruptFlow
from server.protocol import Method
from server.speculative_turn import SpeculativeTurnLauncher
from server.ws_handler import ConnectionManager
from test_chat_control_ingress import context as context, request


@pytest.mark.parametrize("targeted", [False, True])
@pytest.mark.parametrize("managed", [False, True])
def test_abort_stops_pre_admission_setup_without_cancelling_its_caller(context, targeted, managed):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def interruption():
            entered.set()
            await release.wait()
        runner = AsyncMock(return_value="response")
        if managed:
            handler, _, _ = context.make(runner=runner)
        else:
            handler = ChatHandler()
            handler.configure(stream_llm_query=runner, pending_sentence_items=None)
        handler._presentation_interrupt = interruption
        caller = asyncio.create_task(handler._handle_send(request()))
        await entered.wait()
        assert handler.is_busy()
        result = await handler._handle_abort({"turn_id": "turn-u1"} if targeted else {})
        assert result["status"] == ("cancelled_before_admission" if targeted else "aborted")
        sent = await caller  # no need to release the interruption
        assert sent["status"] == "cancelled_before_admission"
        assert not caller.cancelled() and not handler.is_busy()
        assert context.ledger.find_admission("chat:A", "u1") is None
        assert context.turns.snapshot()["counters"]["turns_started"] == 0
        runner.assert_not_called()
    asyncio.run(run())


def test_targeted_abort_cancels_all_queued_aliases_without_touching_active_owner(context):
    async def run():
        ready, release = asyncio.Event(), asyncio.Event()
        async def runner(*args, **kwargs):
            ready.set()
            await release.wait()
            return "original"
        handler, _, _ = context.make(runner=runner)
        await handler._handle_send(request("active"))
        await ready.wait()
        original = handler._stream_task
        fence = context.ledger.get_epoch_fence("foreground")
        await handler._control_ingress_lock.acquire()
        queued = [asyncio.create_task(handler._handle_send(request("queued", turn="same"))) for _ in range(2)]
        await asyncio.sleep(0)
        result = await handler._handle_abort({"turn_id": "same"})
        assert result["status"] == "cancelled_before_admission"
        results = await asyncio.gather(*queued)
        handler._control_ingress_lock.release()
        assert all(result["status"] == "cancelled_before_admission" for result in results)
        assert context.ledger.get_epoch_fence("foreground") == fence
        assert not original.done() and handler._stream_task is original
        assert context.ledger.find_admission("chat:A", "queued") is None
        release.set()
        await original
    asyncio.run(run())


def test_global_abort_cancels_only_inputs_already_arrived_not_later_input(context):
    async def run():
        runner = AsyncMock(return_value="new")
        handler, _, _ = context.make(runner=runner)
        await handler._control_ingress_lock.acquire()
        queued = [asyncio.create_task(handler._handle_send(request(str(i)))) for i in range(3)]
        await asyncio.sleep(0)
        await handler._handle_abort({})
        results = await asyncio.gather(*queued)
        handler._control_ingress_lock.release()
        assert all(result["status"] == "cancelled_before_admission" for result in results)
        assert context.ledger.get_epoch_fence("foreground") is None
        await handler._handle_send(request("later"))
        await handler._stream_task
        runner.assert_awaited_once()
        assert context.ledger.find_admission("chat:A", "later")["lifecycle"] == "current"
    asyncio.run(run())


def test_close_cancels_ready_queued_setup_before_scheduling_its_drain(context):
    async def run():
        runner = AsyncMock(return_value="must not start")
        handler, _, _ = context.make(runner=runner)
        await handler._control_ingress_lock.acquire()
        caller = asyncio.create_task(handler._handle_send(request()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        handler._control_ingress_lock.release()
        await handler.close()
        assert (await caller)["status"] == "cancelled_before_admission"
        assert context.ledger.get_epoch_fence("foreground") is None
        runner.assert_not_called()
    asyncio.run(run())


def test_internal_compound_supersede_does_not_cancel_its_new_setup(context, monkeypatch):
    async def run():
        ready = asyncio.Event()
        async def runner(text, **kwargs):
            if kwargs["turn_admission"].utterance_id == "old":
                ready.set()
                await asyncio.Event().wait()
            return "new"
        handler, _, _ = context.make(runner=runner)
        tts = SimpleNamespace(handle=AsyncMock())
        flow = MainTurnInterruptFlow()
        flow.configure(chat_handler=handler, tts_handler=tts)
        monkeypatch.setattr("server.interrupt_flow.interrupt_flow", flow)
        await handler._handle_send(request("old"))
        old_task = handler._stream_task
        await ready.wait()
        result = await handler._handle_send(request("new"))
        assert result["status"] == "ok"
        await handler._stream_task
        assert old_task.cancelled()
        tts.handle.assert_awaited_once()
        assert tts.handle.await_args.args[1]["turn_id"] == "turn-old"
        assert context.ledger.find_admission("chat:A", "new")["lifecycle"] == "current"
    asyncio.run(run())


@pytest.mark.parametrize("setup_exists", [False, True])
def test_stale_or_setup_only_targeted_flow_cannot_discard_unrelated_pending_or_audio(context, setup_exists):
    async def run():
        handler, _, _ = context.make(runner=AsyncMock(return_value="unused"))
        context.turns.open_turn(turn_id="replacement", local_next_epoch=1, pending=True)
        handler._active_turn_id = "replacement"
        tts = SimpleNamespace(handle=AsyncMock())
        flow = MainTurnInterruptFlow()
        flow.configure(chat_handler=handler, tts_handler=tts)
        caller = None
        if setup_exists:
            await handler._control_ingress_lock.acquire()
            caller = asyncio.create_task(handler._handle_send(request("target")))
            await asyncio.sleep(0)
        before = context.turns.snapshot()
        result = await flow.interrupt(source="test", turn_id="turn-target")
        assert result["status"] == ("cancelled_before_admission" if setup_exists else "stale")
        assert context.turns.snapshot() == before
        tts.handle.assert_not_called()
        if caller is not None:
            assert (await caller)["status"] == "cancelled_before_admission"
            handler._control_ingress_lock.release()
    asyncio.run(run())


def test_external_abort_keeps_actual_ws_read_loop_available_for_next_request(context):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        complete = asyncio.Event()
        async def interruption():
            entered.set()
            await release.wait()
        runner = AsyncMock(return_value="next response")
        handler, _, _ = context.make(runner=runner)
        handler._presentation_interrupt = interruption
        manager = ConnectionManager()
        manager.register_handler(handler)
        class Socket:
            async def iter_text(self):
                for uid in ("first", "next"):
                    yield json.dumps({"id": uid, "method": Method.CHAT_SEND, "params": request(uid)})
                await complete.wait()
        responses = []
        async def send_json(payload):
            responses.append(payload)
            if payload.get("id") == "next":
                complete.set()
        reader = asyncio.create_task(manager._read_loop(Socket(), "connection", send_json))
        await entered.wait()
        await handler._handle_abort({"turn_id": "turn-first"})
        release.set()
        await reader
        await handler._stream_task
        assert [row["id"] for row in responses] == ["first", "next"]
        assert responses[0]["params"]["status"] == "cancelled_before_admission"
        assert responses[1]["params"]["status"] == "ok"
        runner.assert_awaited_once()
    asyncio.run(run())


def test_original_caller_cancellation_still_propagates(context):
    async def run():
        entered = asyncio.Event()
        async def interruption():
            entered.set()
            await asyncio.Event().wait()
        handler, _, _ = context.make(runner=AsyncMock())
        handler._presentation_interrupt = interruption
        caller = asyncio.create_task(handler._handle_send(request()))
        await entered.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert not handler.is_busy()
        assert context.ledger.get_epoch_fence("foreground") is None
    asyncio.run(run())


@pytest.mark.parametrize("before_admission", [False, True])
def test_close_drains_owned_tasks_before_returning_and_rejects_later_send(context, before_admission):
    async def run():
        entered = asyncio.Event()
        effects = []
        async def wait_forever(*args, **kwargs):
            if not before_admission:
                effects.append(context.control.seal(kwargs["turn_admission"], context.decision)["effect_id"])
            entered.set()
            await asyncio.Event().wait()
        handler, _, _ = context.make(runner=wait_forever)
        if before_admission:
            handler._presentation_interrupt = wait_forever
        caller = asyncio.create_task(handler._handle_send(request()))
        await entered.wait()
        await handler.close()
        result = await caller
        assert result["status"] == ("cancelled_before_admission" if before_admission else "ok")
        assert not handler.is_busy()
        with pytest.raises(TurnAuthorityError, match="closed"):
            await handler._handle_send(request("later"))
        if effects:
            assert context.ledger.get_effect(effects[0])["state"] == "cancelled"
        else:
            assert context.ledger.get_epoch_fence("foreground") is None
        await handler.close()
    asyncio.run(run())


@pytest.mark.parametrize("discard_fails", [False, True])
def test_failed_close_fence_is_not_success_but_local_producer_is_drained(context, monkeypatch, discard_fails):
    async def run():
        entered = asyncio.Event()
        async def runner(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        handler, _, _ = context.make(runner=runner)
        await handler._handle_send(request())
        await entered.wait()
        monkeypatch.setattr(context.ledger, "advance_epoch", Mock(side_effect=RuntimeError("fence unavailable")))
        if discard_fails:
            monkeypatch.setattr(context.ledger, "discard", Mock(side_effect=RuntimeError("store unavailable")))
        with pytest.raises(TurnAuthorityError):
            await handler.close()
        assert handler._stream_task.cancelled() and not handler.is_busy()
        assert handler._chat_epoch == 1
        assert context.ledger.find_admission("chat:A", "u1")["lifecycle"] == ("current" if discard_fails else "discarded")
        with pytest.raises(TurnAuthorityError, match="closed"):
            await handler._handle_send(request("later"))
    asyncio.run(run())


def test_cancelling_close_waiter_does_not_cancel_owned_cleanup(context):
    async def run():
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def runner(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        handler, _, _ = context.make(runner=runner)
        await handler._handle_send(request())
        await started.wait()
        caller = asyncio.create_task(handler.close())
        await cleaning.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert not handler._close_task.done()
        release.set()
        await handler.close()
        assert not handler.is_busy() and handler._stream_task.cancelled()
        assert context.ledger.get_epoch_fence("foreground")["chat_epoch"] == 2
    asyncio.run(run())


def test_close_also_drains_superseded_stream_still_running_its_finally(context):
    async def run():
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def runner(*args, **kwargs):
            if kwargs["turn_admission"].utterance_id == "old":
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
            return "new response"
        handler, _, _ = context.make(runner=runner)
        await handler._handle_send(request("old"))
        old_task = handler._stream_task
        await started.wait()
        await handler._handle_send(request("new"))
        await cleaning.wait()
        closing = asyncio.create_task(handler.close())
        returned_before_old_cleanup = False
        try:
            await asyncio.wait_for(asyncio.shield(closing), timeout=0.1)
            returned_before_old_cleanup = True
        except asyncio.TimeoutError:
            pass
        finally:
            release.set()
            await closing
            await asyncio.gather(old_task, return_exceptions=True)
        assert not returned_before_old_cleanup
    asyncio.run(run())


def test_abort_then_close_waits_for_cleanup_instead_of_cancelling_it_again(context):
    async def run():
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        cleaned = []
        async def runner(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.append(True)
        handler, _, _ = context.make(runner=runner)
        await handler._handle_send(request())
        await started.wait()
        await handler._handle_abort({})
        await cleaning.wait()
        closing = asyncio.create_task(handler.close())
        returned_early = False
        try:
            await asyncio.wait_for(asyncio.shield(closing), timeout=0.1)
            returned_early = True
        except asyncio.TimeoutError:
            pass
        finally:
            release.set()
            await closing
        assert not returned_early and cleaned == [True]
        assert handler._stream_task.cancelling() == 1
    asyncio.run(run())


def test_close_cancels_all_owned_streams_even_when_transport_alias_is_reused(context):
    async def run():
        release = asyncio.Event()
        async def runner(*args, **kwargs):
            await release.wait()
            return "done"
        handler, _, _ = context.make(runner=runner)
        await handler._handle_send(request("old", turn="same"))
        old = handler._stream_task
        await handler._handle_send(request("new", turn="same"))
        current = handler._stream_task
        assert old is not current and not old.done()
        timed_out = False
        try:
            await asyncio.wait_for(handler.close(), timeout=0.3)
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            release.set()
            await handler.close()
            await asyncio.gather(old, current, return_exceptions=True)
        assert not timed_out
        assert old.cancelled() and current.cancelled()
    asyncio.run(run())


@pytest.mark.parametrize("managed", [False, True])
def test_ingress_uses_bounded_source_projection_not_whole_request_clone(context, managed):
    class Evidence(dict):
        def __deepcopy__(self, memo):
            raise AssertionError("unbounded clone")
        def keys(self):
            raise AssertionError("unbounded enumeration")
    async def run():
        runner = AsyncMock(return_value="ok")
        if managed:
            handler, _, _ = context.make(runner=runner)
        else:
            handler = ChatHandler()
            handler.configure(stream_llm_query=runner, pending_sentence_items=None)
        source = Evidence(asr_confidence=0.4, n_best_hashes=["original"])
        params = {**request(), "source_evidence": source, "unused": Evidence()}
        await handler._handle_send(params)
        source["n_best_hashes"].append("late mutation")
        await handler._stream_task
        evidence = runner.await_args.kwargs["turn_admission"].source_evidence
        assert evidence["asr_confidence"] == 0.4 and evidence["n_best_hashes"] == ["original"]
    asyncio.run(run())


def test_cancelled_speculative_setup_does_not_publish_a_phantom_pending_slot(context, monkeypatch):
    async def run():
        entered = asyncio.Event()
        handler = ChatHandler()
        handler.configure(stream_llm_query=AsyncMock(), pending_sentence_items=None)
        original_setup = handler._handle_send_owned
        async def setup(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            return await original_setup(*args, **kwargs)
        # Hold actual pre-admission setup. Speculative input must not invoke
        # the confirmed-turn background interruption merely to reach this seam.
        monkeypatch.setattr(handler, "_handle_send_owned", setup)
        launcher = SpeculativeTurnLauncher()
        launcher.configure(send_pending=lambda text, **kw: handler.send_text(text, pending=True, **kw),
            confirm=handler.confirm_pending_turn, discard=handler.discard_pending_turn)
        monkeypatch.setattr(launcher, "_policy_blocked_reason", lambda _: "")
        launching = asyncio.create_task(launcher.launch("speculative input"))
        await asyncio.wait_for(entered.wait(), 1)
        await handler._handle_abort({})
        assert await launching is False
        assert not launcher.has_pending
        assert context.turns.snapshot()["counters"]["turns_started"] == 0
    asyncio.run(run())


@pytest.mark.parametrize("cancel_count", [0, 1, 2])
@pytest.mark.parametrize("fence_fails", [False, True])
def test_actual_app_finally_drains_chat_before_provider_and_continues_after_fence_failure(context, monkeypatch, fence_fails, cancel_count):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        cleaning, release_cleanup = asyncio.Event(), asyncio.Event()
        async def visual(**kwargs):
            entered.set()
            try:
                await release.wait()
            finally:
                if cancel_count:
                    cleaning.set()
                    await release_cleanup.wait()
        runner = AsyncMock(return_value="late response")
        handler, _, _ = context.make(runner=runner)
        monkeypatch.setattr(handler, "_prepare_visual_context", visual)
        await handler._handle_send(request())
        await entered.wait()
        if fence_fails:
            monkeypatch.setattr(context.ledger, "advance_epoch", Mock(side_effect=RuntimeError("fence failed")))
        order = []
        class CooperativeClose:
            async def begin_close(self):
                assert not order
                order.append("cooperative_begin")

            async def finish_close(self):
                assert order == ["cooperative_begin", "provider"]
                order.append("cooperative_finish")

        class CooperativeLedger:
            def close(self):
                assert order == ["cooperative_begin", "provider", "cooperative_finish"]
                order.append("cooperative_ledger")

        vts_worker_stop = threading.Event()
        loop = asyncio.get_running_loop()
        vts_workers = [loop.run_in_executor(None, vts_worker_stop.wait) for _ in range(2)]
        async def provider_close():
            assert handler._stream_task.done() and not handler.is_busy()
            assert vts_worker_stop.is_set() and all(worker.done() for worker in vts_workers)
            assert not any(worker.cancelled() for worker in vts_workers)
            assert order == ["cooperative_begin"]
            order.append("provider")
            release.set()
            await asyncio.sleep(0)
        async def drain_inputs():
            assert order == ["cooperative_begin", "provider", "cooperative_finish", "cooperative_ledger"]
            order.append("inputs")
        path = Path(__file__).resolve().parents[1] / "server/app.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bootstrap = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "bootstrap")
        cleanup = next(n for n in bootstrap.body if isinstance(n, ast.Try) and any(
            isinstance(part, ast.Attribute) and part.attr == "drain_provider_facts"
            for statement in n.finalbody for part in ast.walk(statement)))
        function = ast.AsyncFunctionDef(name="run_cleanup",
            args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=cleanup.finalbody, decorator_list=[])
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        monkeypatch.setitem(sys.modules, "asr.mic_input_service", SimpleNamespace(close_mic_input_service=Mock()))
        monkeypatch.setitem(sys.modules, "llm.llama_server", SimpleNamespace(stop_llama_server=Mock()))
        logger = Mock()
        namespace = dict(asyncio=asyncio, chat_h=handler, logger=logger, bus=SimpleNamespace(off=Mock()),
            vts_worker_stop=vts_worker_stop, vts_workers=vts_workers,
            Method=Method, auip_launch_callback=None, work_preview_auip_callback=None,
            auip_result_entry_callback=None,
            set_auip_launch_coordinator=Mock(), auip_presentation_callback=object(),
            auip_engagement_callback=object(), auip_engagement=SimpleNamespace(close=AsyncMock()),
            auip_narration_callback=object(), auip_narration=SimpleNamespace(close=AsyncMock()),
            provider_runtime=SimpleNamespace(set_request_preparer=Mock(),
                set_native_session_checkpoint=Mock(), set_start_admission_validator=Mock(),
                close=provider_close),
            cooperative_chat=CooperativeClose(), chat_role_delivery=None,
            cooperative_ledger=CooperativeLedger(),
            openclaw_gateway_start_task=None,
            provider_activity_h=SimpleNamespace(close=AsyncMock()),
            work_h=SimpleNamespace(drain_inputs=drain_inputs),
            work_ledger=SimpleNamespace(drain_provider_facts=AsyncMock(), close=lambda: order.append("ledger")),
            work_observer=SimpleNamespace(close=AsyncMock()), work_preview=SimpleNamespace(close_all=AsyncMock()),
            _stop_gui_render_runtime=Mock(), wake_service=None, asr_manager=None)
        exec(compile(module, str(path), "exec"), namespace)
        caller = asyncio.create_task(namespace["run_cleanup"]())
        if cancel_count:
            await cleaning.wait()
            for _ in range(cancel_count):
                caller.cancel()
                await asyncio.sleep(0)
            release_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await caller
            await asyncio.gather(handler._close_task, return_exceptions=True)
        else:
            await caller
        runner.assert_not_called()
        assert order == ["cooperative_begin", "provider", "cooperative_finish",
            "cooperative_ledger", "inputs", "ledger"]
        assert logger.exception.call_count == int(fence_fails)
    asyncio.run(run())
