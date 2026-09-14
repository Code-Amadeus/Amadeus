"""Explicit real-Handler ingress assembly; no production canary or live models."""

import asyncio
import ast
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent_host.work_ledger_store import WorkLedgerStore
from core import session_manager as sm
import core.turn_coordinator as tc
from core.turn_coordinator import TurnAuthorityError
from server.control_decision import ControlDecision, ControlDecisionEntry
from server.control_ledger import ControlLedgerConflict, ControlLedgerStore
from server.focus_control import FocusControl
from server.handlers.chat_handler import ChatHandler
from server.interrupt_flow import MainTurnInterruptFlow
from server.reference_catalog import TypedReferenceCandidate
from server.turn_admission import capture_turn_admission
from server.work_destination_service import WorkDestinationService


def request(utterance="u1", *, turn=None, session="A", text="Choose destination"):
    return {"text": text, "session_id": session, "utterance_id": utterance, "turn_id": turn or "turn-" + utterance}


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    for sid in ("A", "B"):
        sm.create_session(sid)
        sm.conversation_history.add_user("HISTORY_" + sid)
        assert sm.save_session(sid)
    assert sm.load_session("A")[0]
    turns = tc.TurnCoordinator()
    monkeypatch.setattr(tc, "coordinator", turns)
    monkeypatch.setattr("server.interrupt_flow.interrupt_flow", MainTurnInterruptFlow())
    emit = AsyncMock()
    monkeypatch.setattr("server.handlers.chat_handler.bus.emit", emit)
    path = tmp_path / "ledger.sqlite3"
    root = tmp_path / "project"
    root.mkdir()
    with WorkLedgerStore(path) as store, closing(ControlLedgerStore(path)) as ledger:
        project = store.create_or_get_project(root)
        destination = WorkDestinationService(store, registry_check=lambda _: True)
        control = FocusControl(ledger, destination, fence_scope="foreground")
        chosen = ControlDecision(status="ok", entries=(ControlDecisionEntry(
            proposal_index=0, control={"intent": "focus", "provider": "codex", "subject": "project"},
            reference_candidates=(TypedReferenceCandidate("project", project.project_id, project.name, "persistent"),),
            session_context="bind", reference_kind="project", workspace_effect="none",
        ),))

        def make(*, mode="turn_decision", runner=None):
            legacy = AsyncMock(return_value="legacy reply")
            direct = AsyncMock(return_value=None)
            handler = ChatHandler()
            handler.configure(stream_llm_query=legacy, pending_sentence_items=None, interaction_branch_router=direct)
            handler.configure_control_ingress(ledger, fence_scope="foreground", authority_mode=mode, turn_runner=runner)
            monkeypatch.setattr(handler, "_prepare_visual_context", AsyncMock(return_value=None))
            return handler, legacy, direct

        yield SimpleNamespace(ledger=ledger, store=store, turns=turns, emit=emit, make=make,
            control=control, decision=chosen, project=project, destination=destination)


def test_exact_grant_drives_one_real_focus_without_old_router_or_issuer(context, monkeypatch):
    async def run():
        seen = []
        async def runner(text, **kwargs):
            admission = kwargs["turn_admission"]
            seen.append(admission)
            assert kwargs["history_snapshot"].dialog == [{"role": "user", "content": "HISTORY_A"}]
            context.control.admit(admission)
            sealed = context.control.seal(admission, context.decision)
            receipt = context.control.apply(sealed["effect_id"])
            assert receipt["receipt"] == context.ledger.get_receipt(sealed["effect_id"])
            return "Focus accepted"
        handler, legacy, direct = context.make(runner=runner)
        monkeypatch.setattr(context.turns, "_issue_epoch", Mock(side_effect=AssertionError("no second issuer")))
        result = await handler._handle_send(request())
        assert result["status"] == "ok"
        await handler._stream_task
        stored = context.ledger.find_admission("chat:A", "u1")
        assert stored["chat_epoch"] == handler._chat_epoch == context.turns.snapshot()["epochs"]["chat"] == seen[0].chat_epoch == 1
        assert stored["root_id"] == seen[0].root_id
        assert stored["authority_mode"] == "turn_decision"
        assert context.destination.session_project("A") == context.project.project_id
        assert context.store.list_work_items() == []
        legacy.assert_not_called()
        direct.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("concurrent", [False, True])
def test_active_transport_replay_never_cancels_or_duplicates_pending_plan(context, concurrent):
    async def run():
        ready, release = asyncio.Event(), asyncio.Event()
        seen = []
        async def runner(text, **kwargs):
            admission = kwargs["turn_admission"]
            sealed = context.control.seal(admission, context.decision)
            seen.append(sealed)
            ready.set()
            await release.wait()
            context.control.apply(sealed["effect_id"])
            return "accepted"
        handler, legacy, direct = context.make(runner=runner)
        if concurrent:
            first, second = await asyncio.gather(handler._handle_send(request()), handler._handle_send(request(turn="alias")))
        else:
            first = await handler._handle_send(request())
            await ready.wait()
            second = await handler._handle_send(request(turn="alias"))
        await ready.wait()
        task = handler._stream_task
        assert first["status"] == "ok" and second["status"] == "replayed"
        assert len(seen) == 1 and not task.done()
        assert context.ledger.get_effect(seen[0]["effect_id"])["state"] == "pending"
        assert context.ledger.get_epoch_fence("foreground")["chat_epoch"] == handler._chat_epoch == 1
        release.set()
        await task
        assert context.ledger.get_receipt(seen[0]["effect_id"]) is not None
        legacy.assert_not_called()
        direct.assert_not_called()
    asyncio.run(run())


def test_changed_replay_is_refused_before_interrupting_original(context):
    async def run():
        release = asyncio.Event()
        async def waiting(*args, **kwargs):
            await release.wait()
            return "done"
        handler, _, _ = context.make(runner=waiting)
        await handler._handle_send(request())
        task = handler._stream_task
        with pytest.raises(TurnAuthorityError, match="changed"):
            await handler._handle_send(request(text="different words"))
        assert handler._stream_task is task and not task.done()
        assert handler._chat_epoch == 1
        release.set()
        await task
    asyncio.run(run())


def test_distinct_utterance_and_stale_replay_keep_the_newer_owner(context):
    async def run():
        gates = [asyncio.Event(), asyncio.Event()]
        calls = []
        async def runner(text, **kwargs):
            index = len(calls)
            calls.append(kwargs["turn_admission"])
            await gates[index].wait()
            return "done"
        handler, _, _ = context.make(runner=runner)
        await handler._handle_send(request())
        first = handler._stream_task
        await asyncio.sleep(0)
        await handler._handle_send(request("u2"))
        second = handler._stream_task
        await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await first
        assert len(calls) == 2 and calls[0].root_id != calls[1].root_id
        assert calls[1].chat_epoch == 3  # one invalidation, then one new admission
        replay = await handler._handle_send(request(turn="old-alias"))
        assert replay["status"] == "replayed" and replay["admission_lifecycle"] != "current"
        assert handler._stream_task is second and not second.done()
        before = context.ledger.get_epoch_fence("foreground")
        assert (await handler._handle_abort({"turn_id": "turn-u1"}))["status"] == "stale"
        assert context.ledger.get_epoch_fence("foreground") == before
        gates[1].set()
        await second
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["pending", "missing_runner", "missing_session", "unprepared_session"])
def test_unsupported_ingress_never_falls_back_or_admits(context, kind):
    async def run():
        runner = None if kind == "missing_runner" else AsyncMock(return_value="new")
        handler, legacy, direct = context.make(runner=runner)
        params = request()
        if kind == "pending":
            params["pending"] = True
        if kind == "missing_session":
            sm.set_current_session_id(None)
            params["session_id"] = ""
        if kind == "unprepared_session":
            params["session_id"] = "unprepared"
        with pytest.raises(TurnAuthorityError):
            await handler._handle_send(params)
        assert context.ledger.get_epoch_fence("foreground") is None
        assert handler._stream_task is None
        legacy.assert_not_called()
        direct.assert_not_called()
    asyncio.run(run())


def test_host_mode_is_not_taken_from_request_metadata(context):
    async def run():
        handler, legacy, direct = context.make(mode="legacy", runner=AsyncMock())
        await handler._handle_send({**request(), "authority_mode": "turn_decision", "chat_epoch": 999})
        await handler._stream_task
        assert context.ledger.find_admission("chat:A", "u1")["authority_mode"] == "legacy"
        assert handler._chat_epoch == 1
        legacy.assert_awaited_once()
        direct.assert_awaited_once()
        handler._control_turn_runner.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["lookup", "open", "adopt"])
def test_ingress_failure_never_starts_a_runner_or_leaves_a_failed_grant_current(context, monkeypatch, boundary):
    async def run():
        runner = AsyncMock(return_value="new")
        handler, legacy, direct = context.make(runner=runner)
        if boundary == "adopt":
            monkeypatch.setattr(context.turns, "open_turn", Mock(side_effect=TurnAuthorityError("adoption failed")))
        else:
            monkeypatch.setattr(context.ledger, "find_admission" if boundary == "lookup" else "open_admission", Mock(side_effect=RuntimeError("store unavailable")))
        with pytest.raises(TurnAuthorityError):
            await handler._handle_send(request())
        runner.assert_not_called()
        legacy.assert_not_called()
        direct.assert_not_called()
        assert handler._stream_task is None
        assert handler._chat_epoch == 0
        if boundary == "adopt":
            stored = context.ledger.find_admission("chat:A", "u1")
            assert stored["lifecycle"] == "discarded"
        else:
            assert context.ledger.get_epoch_fence("foreground") is None
    asyncio.run(run())


@pytest.mark.parametrize("has_runner", [False, True])
def test_reconstructed_handler_restores_watermark_and_reads_replay_without_runner(context, has_runner):
    async def run():
        recorded = capture_turn_admission(utterance_id="u1", turn_id="old", session_id="A", transcript="Choose destination", authority_mode="turn_decision")
        context.ledger.open_admission(root_id=recorded.root_id, source_scope=recorded.dialogue_source_scope,
            fence_scope="foreground", utterance_id=recorded.utterance_id, authority_mode=recorded.authority_mode,
            transcript_hash=recorded.transcript_hash, minimum_epoch=20)
        runner = AsyncMock(return_value="new") if has_runner else None
        handler, _, _ = context.make(runner=runner)
        replay = await handler._handle_send(request())
        assert replay["status"] == "replayed" and replay["plan_id"] is None
        assert handler._stream_task is None
        assert handler._chat_epoch == context.turns.snapshot()["epochs"]["chat"] == 20
        if has_runner:
            runner.assert_not_called()
            await handler._handle_send(request("u2"))
            await handler._stream_task
            assert handler._chat_epoch == 21
    asyncio.run(run())


@pytest.mark.parametrize("fence_fails", [False, True])
def test_wired_session_load_retires_or_reports_failure_before_context_switch(context, monkeypatch, fence_fails):
    async def run():
        ready, release = asyncio.Event(), asyncio.Event()
        effects = []
        async def runner(text, **kwargs):
            effects.append(context.control.seal(kwargs["turn_admission"], context.decision)["effect_id"])
            ready.set()
            await release.wait()
            return "should not finish"
        handler, _, _ = context.make(runner=runner)
        sm.configure_activation_guard(handler.invalidate_session_context)
        await handler._handle_send(request())
        await ready.wait()
        task = handler._stream_task
        if fence_fails:
            monkeypatch.setattr(context.ledger, "advance_epoch", Mock(side_effect=ControlLedgerConflict("cannot fence")))
        loaded = sm.load_session("B")
        assert loaded[0] is (not fence_fails)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sm.get_current_session_id() == ("A" if fence_fails else "B")
        assert handler._chat_epoch == (1 if fence_fails else 2)
        assert context.ledger.get_effect(effects[0])["state"] == "cancelled"
        assert not any(call.args[0] == "chat.complete" for call in context.emit.await_args_list)
    asyncio.run(run())


@pytest.mark.parametrize("reload_same", [False, True])
def test_context_change_while_waiting_for_quiescence_cannot_be_undone_by_old_input(context, reload_same):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def presentation_interrupt():
            entered.set()
            await release.wait()
        runner = AsyncMock(return_value="new")
        handler, _, _ = context.make(runner=runner)
        handler._presentation_interrupt = presentation_interrupt
        pending = asyncio.create_task(handler._handle_send(request()))
        await entered.wait()
        target = "A" if reload_same else "B"
        assert sm.load_session(target)[0]
        release.set()
        with pytest.raises(TurnAuthorityError, match="context changed"):
            await pending
        assert sm.get_current_session_id() == target
        assert context.ledger.get_epoch_fence("foreground") is None
        runner.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("reload_same", [False, True])
@pytest.mark.parametrize("wired_guard", [False, True])
def test_session_selection_invalidates_inputs_already_waiting_for_ingress_lock(context, reload_same, wired_guard):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        interrupt_calls = []
        async def presentation_interrupt():
            interrupt_calls.append(sm.get_current_session_id())
            entered.set()
            await release.wait()
        runner = AsyncMock(return_value="new")
        handler, _, _ = context.make(runner=runner)
        handler._presentation_interrupt = presentation_interrupt
        if wired_guard:
            sm.configure_activation_guard(handler.invalidate_session_context)
        first = asyncio.create_task(handler._handle_send(request("one")))
        await entered.wait()
        second = asyncio.create_task(handler._handle_send(request("two")))
        await asyncio.sleep(0)  # second has arrived, but the first owns the lock
        target = "A" if reload_same else "B"
        assert sm.load_session(target)[0]
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, TurnAuthorityError) for result in results)
        assert sm.get_current_session_id() == target
        assert context.ledger.get_epoch_fence("foreground") is None
        assert interrupt_calls == ["A"]  # stale queued input cannot interrupt the new context
        runner.assert_not_called()
    asyncio.run(run())


def test_older_input_internal_session_install_does_not_refuse_newer_queued_target(context):
    async def run():
        assert sm.load_session("B")[0]
        entered, release = asyncio.Event(), asyncio.Event()
        async def presentation_interrupt():
            entered.set()
            await release.wait()
        runner = AsyncMock(return_value="new")
        handler, _, _ = context.make(runner=runner)
        sm.configure_activation_guard(handler.invalidate_session_context)
        handler._presentation_interrupt = presentation_interrupt
        first = asyncio.create_task(handler._handle_send(request("one", session="A")))
        await entered.wait()
        second = asyncio.create_task(handler._handle_send(request("two", session="B")))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
        assert [result["status"] for result in results] == ["ok", "ok"]
        await handler._stream_task
        assert sm.get_current_session_id() == "B"
        assert runner.await_args.kwargs["turn_admission"].session_id == "B"
        assert runner.await_args.kwargs["history_snapshot"].dialog == [{"role": "user", "content": "HISTORY_B"}]
        assert context.ledger.find_admission("chat:B", "two")["lifecycle"] == "current"
    asyncio.run(run())


def test_queued_input_detaches_transport_fields_at_arrival(context):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def presentation_interrupt():
            entered.set()
            await release.wait()
        runner = AsyncMock(return_value="new")
        handler, _, _ = context.make(runner=runner)
        handler._presentation_interrupt = presentation_interrupt
        first = asyncio.create_task(handler._handle_send(request("one")))
        await entered.wait()
        params = request("two", session="", text="original")
        params["source_evidence"] = {"marker": "original"}
        second = asyncio.create_task(handler._handle_send(params))
        await asyncio.sleep(0)
        params.update(session_id="B", text="mutated", utterance_id="mutated")
        params["source_evidence"]["marker"] = "mutated"
        release.set()
        await asyncio.gather(first, second)
        await handler._stream_task
        assert sm.get_current_session_id() == "A"
        assert runner.await_args.args[0] == "original"
        assert context.ledger.find_admission("chat:A", "two") is not None
        assert context.ledger.find_admission("chat:B", "mutated") is None
    asyncio.run(run())


def test_task_cancelled_before_first_instruction_retires_its_exact_admission(context, monkeypatch):
    async def run():
        runner = AsyncMock(return_value="new")
        handler, _, _ = context.make(runner=runner)
        retired = asyncio.Event()
        retire = handler._retire_control_turn
        def observe_retirement(admission):
            retire(admission)
            retired.set()
        monkeypatch.setattr(handler, "_retire_control_turn", observe_retirement)
        create_task = asyncio.create_task
        def cancel_before_start(coro, *args, **kwargs):
            task = create_task(coro, *args, **kwargs)
            if coro.cr_code.co_name == "_run_stream":
                task.cancel()
            return task
        monkeypatch.setattr(asyncio, "create_task", cancel_before_start)
        await handler._handle_send(request())
        task = handler._stream_task
        with pytest.raises(asyncio.CancelledError):
            await task
        # A raw Task cancellation is not a durable fencing receipt; wait for
        # the owned done callback rather than assuming callback scheduling.
        await asyncio.wait_for(retired.wait(), timeout=1)
        assert context.ledger.find_admission("chat:A", "u1")["lifecycle"] == "discarded"
        runner.assert_not_called()
    asyncio.run(run())


def test_deleted_inactive_source_session_is_not_recreated_by_queued_input(context):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def presentation_interrupt():
            entered.set()
            await release.wait()
        runner = AsyncMock(return_value="new")
        handler, _, _ = context.make(runner=runner)
        sm.configure_activation_guard(handler.invalidate_session_context)
        handler._presentation_interrupt = presentation_interrupt
        pending = asyncio.create_task(handler._handle_send(request(session="B")))
        await entered.wait()
        assert sm.delete_session("B")
        release.set()
        with pytest.raises(TurnAuthorityError, match="no longer exists"):
            await pending
        assert sm.get_current_session_id() == "A"
        assert "B" not in sm.list_sessions()
        assert context.ledger.get_epoch_fence("foreground") is None
        runner.assert_not_called()
    asyncio.run(run())


def test_deleted_session_source_replay_is_readable_without_reactivation(context):
    async def run():
        runner = AsyncMock(return_value="new")
        handler, _, _ = context.make(runner=runner)
        sm.configure_activation_guard(handler.invalidate_session_context)
        await handler._handle_send(request())
        await handler._stream_task
        assert sm.delete_session("A")
        before = context.ledger.get_epoch_fence("foreground")
        replay = await handler._handle_send(request(turn="alias"))
        assert replay["status"] == "replayed" and replay["admission_lifecycle"] == "discarded"
        assert context.ledger.get_epoch_fence("foreground") == before
        assert sm.get_current_session_id() is None
        assert "A" not in sm.list_sessions()
        runner.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("target", ["old", "current", ""])
def test_default_targeted_abort_only_interrupts_its_current_turn(context, target):
    async def run():
        handler = ChatHandler()  # no managed configuration
        handler._active_turn_id = "current"
        handler._active_accumulated_text = "partial"
        task = Mock()
        task.done.return_value = False
        task.cancelling.return_value = 0
        handler._stream_task = task
        result = await handler._handle_abort({"turn_id": target})
        if target == "old":
            assert result["status"] == "stale"
            assert handler._active_turn_id == "current" and handler._chat_epoch == 0
            task.cancel.assert_not_called()
        else:
            assert result["status"] == "aborted" and result["accumulated_text"] == "partial"
            assert handler._active_turn_id == "" and handler._chat_epoch == 1
            task.cancel.assert_called_once()
    asyncio.run(run())


@pytest.mark.parametrize("epoch", [True, -1, 2.5, "3", 0, 2])
def test_core_refuses_invalid_or_stale_adopted_grant_without_mutating_active_owner(context, epoch):
    context.turns.open_turn(turn_id="current", local_next_epoch=2)
    before = context.turns.snapshot()
    with pytest.raises(TurnAuthorityError):
        context.turns.open_turn(turn_id="rejected", local_next_epoch=3, granted_epoch=epoch)
    after = context.turns.snapshot()
    assert after["epochs"] == before["epochs"]
    assert after["active_turn_id"] == "current"


def test_core_watermark_restore_neither_opens_a_turn_nor_moves_backwards(context):
    assert context.turns.synchronize_chat_epoch(20) == 20
    assert context.turns.snapshot()["active_turn_id"] == ""
    assert context.turns.synchronize_chat_epoch(20) == 20
    with pytest.raises(TurnAuthorityError):
        context.turns.synchronize_chat_epoch(19)
    assert context.turns.snapshot()["epochs"]["chat"] == 20
    assert context.turns.snapshot()["active_turn_id"] == ""


@pytest.mark.parametrize("entry", ["runtime", "dispatcher", "browser", "b2", "auip_callback"])
def test_explicit_new_mode_cannot_enter_legacy_execution(entry):
    admission = capture_turn_admission(utterance_id="u", turn_id="t", session_id="A", transcript="Do it", chat_epoch=1, authority_mode="turn_decision")
    async def run():
        if entry == "runtime":
            from core.chat_runtime import ChatRuntime
            runtime = ChatRuntime()
            queue = asyncio.Queue()
            queue.put_nowait("old item")
            runtime.configure(pending_sentence_items=queue, playback_manager=None, provider="local")
            with pytest.raises(TurnAuthorityError):
                await runtime.stream_llm_query("Do it", turn_admission=admission)
            assert queue.get_nowait() == "old item"
        elif entry == "dispatcher":
            from server.host_action_dispatcher import record_actions
            sink = Mock()
            with pytest.raises(TurnAuthorityError):
                record_actions([{"type": "EMO", "value": "happy"}], expression_sink=sink, turn_admission=admission)
            sink.assert_not_called()
        elif entry in {"browser", "b2"}:
            from server.auip_b2 import AuipB2Coordinator
            from server.interaction_branch import InteractionBranchCoordinator
            cls = AuipB2Coordinator if entry == "b2" else InteractionBranchCoordinator
            with pytest.raises(TurnAuthorityError):
                await cls.try_route_user_message(object(), text="Do it", session_id="A", turn_id="t", turn_admission=admission)
        else:
            path = Path(__file__).resolve().parents[1] / "server" / "app.py"
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            bootstrap = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "bootstrap")
            callback = next(node for node in bootstrap.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_route_auip_control")
            namespace = {}
            exec(compile(ast.Module(body=[callback], type_ignores=[]), str(path), "exec"), namespace)
            with pytest.raises(TurnAuthorityError):
                await namespace["_route_auip_control"]({}, session_id="A", user_text="Do it", turn_id="t", turn_admission=admission)
    asyncio.run(run())
