"""Stop a named task without borrowing the default receiving context's run."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from server.cooperative_chat_ingress import CooperativeChatManager
from server.attention_request import AttentionRequestCoordinator
from server.cooperative_provider_effect import CooperativeProviderEffectLedger
from server.cooperative_provider_loop import LoopConflict
from core import session_manager as sm
from test_cooperative_context_recovery import host_factory as host_factory
from test_cooperative_provider_loop import loop_host as loop_host
from test_work_effect_executor import _host, _admission, _payload


async def test_work_first_named_stop_never_falls_through_to_current_work(loop_host):
    loop, adapter, controls, _, delivered = loop_host
    loop.active_work = lambda _context:{"work_item_id":"work-B", "run_id":"run-B",
        "effect_id":"effect-B", "attempt_id":"attempt-B", "runtime_attached":True}
    text = "停止先前那个查资料的任务，B继续。"
    controls[text] = {"op":"interrupt", "target":"以前の調査"}
    receipt = await loop.submit(text)
    assert receipt["state"] == "task_stop_resolution_required"
    assert receipt["text"] == text and receipt["target"] == "以前の調査"
    assert "run_id" not in receipt and "work_item_id" not in receipt
    assert not loop.children and not adapter.requests and not adapter.cancelled
    assert delivered == []


@pytest.mark.parametrize("scenario", ["other_context", "old_same_context", "unknown_stop",
    "unknown_target", "ambiguous", "missing_history", "session_history", "bare_interrupt",
    "unrelated_finishes", "target_finishes", "ambiguous_unrelated_finishes", "ambiguous_target_finishes"])
async def test_named_provider_stop_keeps_exact_execution_and_default_binding(host_factory, scenario, monkeypatch):
    host = host_factory()
    manager = object.__new__(CooperativeChatManager)
    manager.ledger, manager.runtime = host.ledger, host.runtime
    manager.work_executor = None
    manager.attention = AttentionRequestCoordinator()
    host.ingress.work_request = manager.handle_work_action
    stop_text = "停止之前查资料的任务，另一个继续。"
    source_a, source_b = "调查主题A。", "调查主题B。"
    reference_queries = []
    expressions = []
    publications = []
    host.loop.publish = lambda event:publications.append(event) or True

    async def query(messages, **_kwargs):
        try:
            frame = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            reference_queries.append(messages)
            candidates, _, _, _ = manager.task_stop_candidates(host.ingress)
            choices = [candidate for candidate in candidates if candidate.label == source_a]
            if scenario.startswith("ambiguous"):
                choices = list(candidates)
            elif scenario == "unknown_target":
                choices = []
            if scenario in {"unrelated_finishes", "target_finishes"}:
                await original_cancel(second["run_id"] if scenario == "unrelated_finishes" else first["run_id"])
            return json.dumps({"references":[candidate.token for candidate in choices]})
        if frame["source_kind"] != "user":
            expressions.append(frame)
            return "対象の実行状態を確認したわ。"
        action = {"op":"send"}
        if frame["current"]["text"] == stop_text:
            action = {"op":"interrupt"}
            if scenario != "bare_interrupt":
                action["target"] = "之前查资料的任务"
        return json.dumps({"say":"確認するわ。", "action":action})

    manager.query = host.loop.query = query
    original_cancel = host.runtime.cancel
    host.runtime.cancel = AsyncMock(wraps=original_cancel)
    try:
        if scenario != "old_same_context":
            host.adapter.release.clear()
        first = await host.send(source_a, "task-a")
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        if scenario == "old_same_context":
            await host.loop.wait()
        else:
            child_b = host.loop._create_context("Another context", host.adapter.provider_id)
            host.loop.bind_context(child_b.child_id)
        host.adapter.release.clear()
        host.adapter.started.clear()
        second = await host.send(source_b, "task-b")
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        binding = (host.loop.bound_context_id, host.loop._binding.token)
        if scenario == "unknown_stop":
            host.adapter.cancel = AsyncMock(return_value={"confirmed":False,
                "cancelled":False, "reason":"native_unconfirmed"})
        if scenario in {"missing_history", "session_history"}:
            # A retained execution cannot borrow a context label when its source is unavailable.
            host.runtime._runs.pop(first["run_id"])
            host.loop.history[:] = [item for item in host.loop.history
                if item.get("input_id") != "task-a"]
            if scenario == "missing_history":
                history, changed = sm._read_session_history(host.ingress.session_id)
                history.dialog[:] = [item for item in history.dialog if item.get("turn_id") != "task-a"]
                monkeypatch.setattr(sm, "_read_session_history", lambda _session:(history, changed))
        result = await host.send(stop_text, "named-stop")
        if scenario in {"other_context", "unknown_stop", "bare_interrupt", "unrelated_finishes"}:
            expected_run = first["run_id"]
            assert [call.args[0] for call in host.runtime.cancel.await_args_list] == [expected_run]
            assert result["state"] == ("unknown" if scenario == "unknown_stop" else "stopped")
            replay = await host.send(stop_text, "named-stop")
            assert replay["status"] == "replayed"
            assert host.runtime.cancel.await_count == 1
        elif scenario in {"old_same_context", "session_history", "target_finishes"}:
            assert result["state"] == ("unknown" if scenario == "session_history" else "not_active")
            host.runtime.cancel.assert_not_awaited()
        elif scenario.startswith("ambiguous"):
            assert result["state"] == "task_stop_selection_required"
            host.runtime.cancel.assert_not_awaited()
            request = manager.attention.list_pending(host.ingress.session_id)[0]
            option = next(option for option in request["options"] if option["label"] == source_a)
            if scenario != "ambiguous":
                await original_cancel(second["run_id"] if scenario == "ambiguous_unrelated_finishes" else first["run_id"])
            selected = await manager.attention.resolve(session_id=host.ingress.session_id,
                request_id=request["id"], option_id=option["id"])
            assert selected["ok"]
            assert [call.args[0] for call in host.runtime.cancel.await_args_list] == (
                [] if scenario == "ambiguous_target_finishes" else [first["run_id"]])
            assert host.ingress.receipts["named-stop"]["state"] == (
                "not_active" if scenario == "ambiguous_target_finishes" else "stopped")
        else:
            assert result["state"] == "rejected"
            assert result["reason"] == ("task_stop_target_incomplete"
                if scenario == "missing_history" else "task_stop_target_none")
            host.runtime.cancel.assert_not_awaited()
        assert (host.loop.bound_context_id, host.loop._binding.token) == binding
        if scenario not in {"unrelated_finishes", "ambiguous_unrelated_finishes"}:
            assert host.runtime.get_run(second["run_id"]).status == "running"
        assert len(host.adapter.requests) == 2
        assert len(reference_queries) == (0 if scenario == "missing_history" else 1)
        if scenario in {"other_context", "unknown_stop"}:
            rows = host.ledger._db.execute("SELECT payload_json FROM control_effect_outbox WHERE kind='provider'").fetchall()
            stops = [json.loads(row[0]) for row in rows if json.loads(row[0])["operation"] == "interrupt"]
            assert len(stops) == 1
            assert stops[0]["run_id"] == first["run_id"]
            assert stops[0]["source_binding_context_id"] == second["child_id"]
            assert stops[0]["source_utterance_id"] == "named-stop"
            assert len([event for event in publications if event["cause"] == "named-stop"]) == 1
        host.adapter.release.set()
        await host.loop.wait()
        if scenario in {"other_context", "bare_interrupt"}:
            assert not [event for event in publications if event["cause"] == first["run_id"]]
            assert not [frame for frame in expressions
                if frame["current"].get("run_id") == first["run_id"]]
            assert next(event for event in publications if event["cause"] == "named-stop")["text"] == "確認するわ。"
            assert result["provider_status"] == "cancelled"
        elif scenario == "target_finishes":
            # A native/externally observed cancellation without a settled Host
            # stop still needs its own result publication.
            assert any(event["cause"] == first["run_id"] for event in publications)
        if scenario == "old_same_context":
            receipt_frame = next(frame for frame in expressions
                if frame["source_kind"] == "host_receipt" and frame["current"].get("state") == "not_active")
            assert receipt_frame["current"]["question"] == stop_text
            assert receipt_frame["current"]["action"] == "interrupt"
    finally:
        manager.attention.reset_for_tests()
        host.adapter.release.set()
        await host.close()


async def test_stop_of_old_task_after_reconstruction_never_borrows_new_run(host_factory):
    original = host_factory()
    await original.send("只读检查A目录。", "old-a")
    await original.loop.wait()
    await original.send("只读检查B目录。", "old-b")
    await original.loop.wait()
    context_id = original.loop.bound_context_id
    await original.close()
    resumed = host_factory(allow_allocate=False)
    manager = object.__new__(CooperativeChatManager)
    manager.ledger, manager.runtime = resumed.ledger, resumed.runtime
    manager.work_executor = None
    manager.attention = AttentionRequestCoordinator()
    resumed.ingress.work_request = manager.handle_work_action
    with patch.object(resumed.loop, "get_context", side_effect=AssertionError("catalog must stay cold")):
        candidates, complete, _, _ = manager.task_stop_candidates(resumed.ingress)
        assert complete and {candidate.label for candidate in candidates} == {"只读检查A目录。", "只读检查B目录。"}

    async def query(messages, **_kwargs):
        try:
            frame = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            candidates, complete, _, _ = manager.task_stop_candidates(resumed.ingress)
            assert complete
            return json.dumps({"references":[candidate.token for candidate in candidates
                if candidate.label == "只读检查A目录。"]})
        if frame["source_kind"] != "user":
            return "以前の実行は終了しているわ。"
        return json.dumps({"say":"確認するわ。", "action":{
            "op":"interrupt", "target":"之前检查A的任务"}})

    try:
        resumed.adapter.release.clear()
        active = await resumed.send("继续检查C目录。", "new-c")
        await asyncio.wait_for(resumed.adapter.started.wait(), 2)
        resumed.runtime.cancel = AsyncMock(wraps=resumed.runtime.cancel)
        manager.query = resumed.loop.query = query
        result = await resumed.send("停止之前检查A的任务。", "stop-old")
        assert result["state"] == "not_active"
        resumed.runtime.cancel.assert_not_awaited()
        assert resumed.runtime.get_run(active["run_id"]).status == "running"
        assert resumed.loop.bound_context_id == context_id
        assert len(resumed.adapter.requests) == 1
    finally:
        manager.attention.reset_for_tests()
        resumed.adapter.release.set()
        await resumed.close()


@pytest.mark.parametrize("scenario", ["active", "settled", "unattached", "binding_changed",
    "candidate_changed", "source_changed", "session_changed"])
async def test_named_work_stop_uses_its_attempt_not_current_work(tmp_path, monkeypatch, scenario):
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", "session-c2")
    async with _host(tmp_path, block=scenario != "settled", source="创建A报告。", task="创建A报告。") as host:
        first = await host.executor.dispatch(host.effect_id)
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        if scenario == "settled":
            await host.executor.finish(first)
        other_workspace = tmp_path / "other-work"
        other_workspace.mkdir()
        other_project = host.work.create_or_get_project(other_workspace)
        second_admission = _admission(suffix="work-b", epoch=2, text="创建B报告。")
        host.control.admit(second_admission, fence_scope="foreground-chat")
        second_effect = host.control.seal(second_admission, _payload(other_project.project_id,
            host.adapter.provider_id, suffix="work-b", source="创建B报告。", task="创建B报告。"))
        host.adapter.release.clear()
        host.adapter.started.clear()
        second = await host.executor.dispatch(second_effect["effect_id"])
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        await host.coordinator.drain_provider_facts()
        # This fake adapter confirms only the addressed run; it never releases B.
        host.adapter.cancel = AsyncMock(return_value={"confirmed":True, "cancelled":True})
        stop_text = "停止之前创建A的任务，B继续。"
        admission = _admission(suffix="stop-work", epoch=3, text=stop_text)
        host.control.admit(admission, fence_scope="foreground-chat")
        loop = SimpleNamespace(_foreground=asyncio.Lock(),
            _binding=SimpleNamespace(child_id="", token="session-token"),
            children={}, context_catalog=lambda:[], history=[], prior_messages=lambda _turn:[],
            trace=[], _express_and_deliver=AsyncMock(),
            _effects=CooperativeProviderEffectLedger(host.control_store))
        ingress = SimpleNamespace(session_id="session-c2", loop=loop, receipts={})
        manager = object.__new__(CooperativeChatManager)
        manager.ledger, manager.runtime, manager.work_executor = host.control_store, host.runtime, host.executor
        manager.attention = AttentionRequestCoordinator()
        manager._work_dispatches = {"session:session-c2":second}
        manager.work_control = host.control

        async def query(*_args, **_kwargs):
            candidates, complete, _, _ = manager.task_stop_candidates(ingress)
            assert complete
            if scenario == "binding_changed":
                loop._binding.token = "replacement-token"
            elif scenario == "candidate_changed":
                await host.runtime.cancel(first.record.run_id)
                await host.coordinator.drain_provider_facts()
                host.adapter.cancel.reset_mock()
            elif scenario == "session_changed":
                monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", "another-session")
            return json.dumps({"references":[candidate.token for candidate in candidates
                if candidate.entity_id == first.binding["work_item_id"]]})

        manager.query = query
        if scenario == "unattached":
            get_run = host.runtime.get_run
            monkeypatch.setattr(host.runtime, "get_run", lambda run_id:
                None if run_id == first.record.run_id else get_run(run_id))
        receipt = {"state":"task_stop_resolution_required", "target":"之前创建A的任务",
            "text":stop_text + ("篡改" if scenario == "source_changed" else ""),
            "child_id":"", "source_binding_token":"session-token"}
        if scenario in {"binding_changed", "source_changed", "session_changed"}:
            with pytest.raises(ValueError if scenario == "source_changed" else LoopConflict):
                await manager.handle_work_action(ingress, admission.turn_id, receipt, admission)
            host.adapter.cancel.assert_not_awaited()
        else:
            result = await manager.handle_work_action(ingress, admission.turn_id, receipt, admission)
            assert result["work_item_id"] == first.binding["work_item_id"]
            assert result["state"] == {"active":"stopped", "settled":"not_active", "candidate_changed":"not_active",
                "unattached":"unknown"}[scenario]
            if scenario == "active":
                host.adapter.cancel.assert_awaited_once_with(first.record.run_id)
                assert result["status"] == result["provider_status"] == "cancelled"
                current = loop._express_and_deliver.await_args.args[0]
                assert current["source"] == "host_receipt"
                assert current["state"] == "stopped"
                assert current["status"] == current["provider_status"] == "cancelled"
            else:
                host.adapter.cancel.assert_not_awaited()
        assert host.runtime.get_run(second.record.run_id).status == "running"
        assert loop._binding.child_id == ""
        assert len(host.work.list_work_items()) == 2
        assert all(len(host.work.list_attempts(item.work_item_id)) == 1
            for item in host.work.list_work_items())
        manager.attention.reset_for_tests()


@pytest.mark.parametrize("provider_status, expected_state", [
    ("cancelled", "stopped"),
    ("done", "stopped"),
    ("error", "stopped"),
    (None, "unknown"),
])
async def test_cancel_work_run_projects_one_current_provider_status(
        provider_status, expected_state):
    active = {"run_id":"run-a", "status":"running", "work_item_id":"work-a",
        "source_user_text":"停止A。"}
    runtime = SimpleNamespace(
        cancel=AsyncMock(return_value={"confirmed":True, "cancelled":True,
            "reason":"user_cancelled"}),
        get_run=lambda _run_id: (SimpleNamespace(status=provider_status)
            if provider_status is not None else None),
    )
    manager = object.__new__(CooperativeChatManager)
    manager.runtime = runtime

    result = await manager._cancel_work_run(active)

    runtime.cancel.assert_awaited_once_with("run-a")
    assert active == {"run_id":"run-a", "status":"running", "work_item_id":"work-a",
        "source_user_text":"停止A。"}
    assert result == {**active, "state":expected_state, "reason":"user_cancelled",
        "status":provider_status or "unknown", "provider_status":provider_status or "unknown"}
