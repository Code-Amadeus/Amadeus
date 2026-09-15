"""A resolved single Work plan reuses the existing cooperative acceptance owners."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_host.provider_types import ProviderInputDelivery
from server.compound_control import (
    CompoundControlOperation,
    CompoundControlPlan,
    SourceClause,
)
from server.control_decision import CONTROL_REFERENCE_CANDIDATES_ATTR
from server.attention_request import AttentionRequestCoordinator
from server.cooperative_provider_loop import ChildConversation, ContextBinding
from server.handlers.work_ledger_handler import WorkLedgerHandler
from test_auip_launch import _seed_app
from test_cooperative_pending_turn import pending_host as pending_host


def planned(provider, source, clause, intent, candidate=None, **extra):
    start = source.index(clause) if clause in source else 0
    action = {"provider":provider, "intent":intent, "task":clause,
        "_host_workspace_access":"write", **extra}
    if candidate is not None:
        action.update(subject="work_item",
            **{CONTROL_REFERENCE_CANDIDATES_ATTR:(candidate,)})
    return CompoundControlPlan(status="ok",
        operations=(CompoundControlOperation(0, clause, action),),
        clauses=(SourceClause(clause, start, start + len(clause)),))


async def install_plans(context, plans):
    ingress = await context.manager._ingress_for(context.session_id)
    context.manager.query = AsyncMock(
        side_effect=AssertionError("resolved Work plan must not issue a reference query"))

    async def work_request(actual_ingress, turn_id, receipt, admission):
        return await context.manager.handle_planned_work_action(
            actual_ingress, turn_id, receipt, admission, plans[turn_id])

    ingress.work_request = work_request
    return ingress


def configure_professional_planner(context, planner, *, work_texts):
    context.manager.work_planner = planner

    async def coarse_query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] != "user":
            return "Ready."
        action = ({"op":"work"}
            if frame["current"]["text"] in set(work_texts) else None)
        return json.dumps({"say":"Ready.", "action":action})

    context.manager.query = coarse_query


async def send(context, text, turn_id):
    accepted = await context.handler.send_text(text, session_id=context.session_id,
        turn_id=turn_id)
    assert accepted["status"] == "ok"
    # Receipt creation includes real Host intake and disk IO. This bound catches
    # hangs; these tests assert control semantics, not a three-second latency SLA.
    await asyncio.wait_for(context.handler._stream_task, 10)
    return context.manager.ingresses[context.session_id].receipts[turn_id]


async def test_coarse_work_proposal_calls_async_planner_once_with_full_source(
        pending_host):
    context = pending_host
    text = "这步你来，帮我另做个清单页吧。"
    clause = "帮我另做个清单页吧。"
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def planner(ingress, turn_id, receipt, admission):
        calls.append((ingress, turn_id, receipt, admission))
        assert receipt["state"] == "work_plan_required"
        assert receipt["text"] == text
        assert not ingress.loop._foreground.locked()
        started.set()
        await release.wait()
        return planned(context.manager.provider, text, clause, "execute", one_off=True)

    configure_professional_planner(context, planner, work_texts={text})
    accepted = await context.handler.send_text(text, session_id=context.session_id,
        turn_id="coarse-planned")
    assert accepted["status"] == "ok"
    await asyncio.wait_for(started.wait(), 3)
    assert len(calls) == 1
    release.set()
    await asyncio.wait_for(context.handler._stream_task, 3)
    result = context.manager.ingresses[context.session_id].receipts["coarse-planned"]
    await context.finish()
    assert result["state"] == "work_started"
    request = context.host.adapter.requests[0]["request"]
    assert request.task == clause and request.metadata["source_user_text"] == text
    replay = await context.handler.send_text(text, session_id=context.session_id,
        turn_id="coarse-planned")
    assert replay["status"] == "replayed"
    assert len(calls) == context.host.adapter.calls == 1


async def test_coarse_ordinary_chat_never_calls_professional_planner(pending_host):
    context = pending_host
    planner = AsyncMock(side_effect=AssertionError("ordinary Chat has no Work proposal"))
    text = "今天有点累。"
    configure_professional_planner(context, planner, work_texts=set())
    result = await send(context, text, "coarse-chat")
    assert result["state"] == "no_action"
    planner.assert_not_awaited()
    assert context.host.adapter.calls == 0
    replay = await context.handler.send_text(text, session_id=context.session_id,
        turn_id="coarse-chat")
    assert replay["status"] == "replayed"
    planner.assert_not_awaited()


async def test_new_turn_expires_waiting_professional_plan_without_execution(pending_host):
    context = pending_host
    old_text = "Build the obsolete page."
    new_text = "Just chatting now."
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def planner(ingress, _turn_id, receipt, _admission):
        calls.append(dict(receipt))
        assert not ingress.loop._foreground.locked()
        started.set()
        await release.wait()
        return planned(context.manager.provider, old_text, old_text,
            "execute", one_off=True)

    configure_professional_planner(context, planner, work_texts={old_text})
    original_query = context.manager.query
    async def role_send(messages, **kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame.get("source_kind") == "user" and frame["current"]["text"] == old_text:
            return '{"action":{"op":"send"},"say":"確認して渡すわ。"}'
        return await original_query(messages, **kwargs)
    context.manager.query = role_send
    await context.handler.send_text(old_text, session_id=context.session_id,
        turn_id="coarse-old")
    old_stream = context.handler._stream_task
    await asyncio.wait_for(started.wait(), 3)
    await context.handler.send_text(new_text, session_id=context.session_id,
        turn_id="coarse-new")
    await asyncio.wait_for(context.handler._stream_task, 3)
    release.set()
    await asyncio.gather(old_stream, return_exceptions=True)
    assert len(calls) == 1
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []
    assert context.manager.ingresses[context.session_id].receipts["coarse-new"]["state"] == "no_action"


async def test_role_send_new_goal_uses_professional_execute_and_replays_once(
        pending_host):
    context = pending_host
    text = "量子誤り訂正の比較メモを新しく作って。"
    planner_calls = []

    async def planner(_ingress, _turn, receipt, _admission):
        planner_calls.append(dict(receipt))
        return planned(context.manager.provider, text, text, "execute", one_off=True,
            _host_display_title="量子誤り訂正の比較メモ")

    configure_professional_planner(context, planner, work_texts={text})
    original_query = context.manager.query
    async def role_send(messages, **kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame.get("source_kind") == "user":
            return '{"action":{"op":"send"},"say":"調べてまとめるわ。"}'
        return await original_query(messages, **kwargs)
    context.manager.query = role_send

    result = await send(context, text, "role-send-new-work")
    await context.finish()
    assert result["state"] == "work_started"
    assert len(planner_calls) == context.host.adapter.calls == 1
    assert planner_calls[0]["provider_message_action"] == {"op":"send"}
    item = context.host.work.get_work_item(result["work_item_id"])
    assert item.title == "量子誤り訂正の比較メモ"
    assert item.goal == text
    assert context.host.work.get_project(item.project_id).metadata["scratch"] is True
    request = context.host.adapter.requests[0]["request"]
    assert request.task == text and request.metadata["source_user_text"] == text
    replay = await context.handler.send_text(text, session_id=context.session_id,
        turn_id="role-send-new-work")
    assert replay["status"] == "replayed"
    assert len(planner_calls) == context.host.adapter.calls == 1


async def test_failed_professional_plan_never_falls_back_to_role_send(pending_host):
    context = pending_host
    text = "新しい調査を始めて。"
    failed = CompoundControlPlan(status="invalid", reason="controlled failure")
    configure_professional_planner(context, lambda *_args:failed, work_texts={text})
    original_query = context.manager.query
    async def role_send(messages, **kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame.get("source_kind") == "user":
            return '{"action":{"op":"send"},"say":"調べるわ。"}'
        return await original_query(messages, **kwargs)
    context.manager.query = role_send

    result = await send(context, text, "failed-role-send-plan")
    assert result["state"] == "rejected"
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []
    assert not context.manager.ingresses[context.session_id].loop.children


async def test_zero_work_provider_send_to_ambiguity_uses_existing_attention_owner(
        pending_host, tmp_path):
    context = pending_host
    context.manager.attention = AttentionRequestCoordinator()
    text = "別の調査担当に確認して。"
    def message_plan(_ingress, _turn, receipt, _admission):
        return planned(context.manager.provider, receipt["text"], receipt["text"],
            "message", _host_workspace_access="none")
    configure_professional_planner(context, message_plan, work_texts={text})
    original_query = context.manager.query
    async def role_send_to(messages, **kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame.get("source_kind") == "user":
            return json.dumps({"action":{"op":"send_to",
                "provider":context.manager.provider}, "say":"確認先を選ぶわ。"})
        return await original_query(messages, **kwargs)
    context.manager.query = role_send_to
    ingress = await context.manager._ingress_for(context.session_id)
    loop = ingress.loop
    def allocate(_label, child_id):
        workspace = tmp_path / child_id
        workspace.mkdir()
        return workspace
    loop.allocate = allocate
    current = loop._create_context("Current", context.manager.provider)
    loop.bind_context(current.child_id)
    loop._create_context("Candidate A", context.manager.provider)
    loop._create_context("Candidate B", context.manager.provider)

    try:
        result = await send(context, text, "planned-send-to-ambiguity")
        assert result["state"] == "address_selection_required"
        assert result["attention_request_id"]
        request, = context.manager.attention.list_pending(context.session_id)
        assert request["id"] == result["attention_request_id"]
        assert len(request["options"]) == 2
        assert context.host.adapter.calls == 0
    finally:
        context.manager.attention.reset_for_tests()


async def test_zero_work_rejected_provider_message_gets_existing_fact_presentation(
        pending_host):
    context = pending_host
    text = "未登録の担当にも聞いて。"
    def message_plan(_ingress, _turn, receipt, _admission):
        return planned(context.manager.provider, receipt["text"], receipt["text"],
            "message", _host_workspace_access="none")
    configure_professional_planner(context, message_plan, work_texts={text})

    async def role(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame.get("source_kind") == "user":
            return '{"action":{"op":"delegate","provider":"missing"},"say":"渡すわ。"}'
        return "その実行先は使えないわ。"
    context.manager.query = role

    result = await send(context, text, "planned-provider-rejected")
    assert result["state"] == "rejected"
    assert result["reason"] == "delegate_provider_unavailable"
    assert context.host.adapter.calls == 0
    assert [row["text"] for row in context.publications
        if row["cause"] == "planned-provider-rejected"] == [
            "渡すわ。", "その実行先は使えないわ。"]


async def test_zero_work_unknown_provider_message_gets_existing_fact_presentation(
        pending_host, monkeypatch):
    context = pending_host
    text = "実行中の担当に追加で確認して。"
    def message_plan(_ingress, _turn, receipt, _admission):
        return planned(context.manager.provider, receipt["text"], receipt["text"],
            "message", _host_workspace_access="none")
    configure_professional_planner(context, message_plan, work_texts={text})

    async def role(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame.get("source_kind") == "user":
            return '{"action":{"op":"send"},"say":"確認を送るわ。"}'
        return "届いたか確認できないわ。"
    context.manager.query = role
    ingress = await context.manager._ingress_for(context.session_id)
    continuation = AsyncMock(return_value={"state":"unknown",
        "reason":"controlled_unknown"})
    monkeypatch.setattr(ingress.loop, "continue_provider_message", continuation)

    result = await send(context, text, "planned-provider-unknown")
    assert result["state"] == "unknown", result
    assert result["reason"] == "controlled_unknown"
    continuation.assert_awaited_once()
    assert [row["text"] for row in context.publications
        if row["cause"] == "planned-provider-unknown"] == [
            "確認を送るわ。", "届いたか確認できないわ。"]


async def test_true_empty_work_plan_stays_no_action_without_provider_delivery(
        pending_host):
    context = pending_host
    text = "今回は作業を増やさないで。"
    empty = CompoundControlPlan(status="ok", operations=(), clauses=())
    configure_professional_planner(context, lambda *_args:empty, work_texts={text})

    result = await send(context, text, "true-empty-work-plan")
    assert result["state"] == "no_action"
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []
    assert not context.manager.ingresses[context.session_id].loop.children


async def test_planned_execute_creates_independent_work_and_replays_once(pending_host, tmp_path):
    context = pending_host
    old, old_attempt, _ = _seed_app(context.host.work, context.host.project,
        tmp_path / "drafts", title="Existing Memo", turn_id="old", goal="Existing Memo")
    context.host.work.update_attempt(old_attempt.attempt_id,
        metadata={"session_id":context.session_id})
    text = "这步你来，帮我另做个清单页吧。"
    clause = "帮我另做个清单页吧。"
    plans = {"planned-execute":planned(context.manager.provider, text, clause,
        "execute", one_off=True)}
    ingress = await install_plans(context, plans)
    binding = (ingress.loop.bound_context_id, ingress.loop._binding.token)
    result = await send(context, text, "planned-execute")
    await context.finish()
    assert result["state"] == "work_started"
    assert result["work_item_id"] != old.work_item_id
    assert len(context.host.work.list_attempts(old.work_item_id)) == 1
    assert len(context.host.work.list_work_items()) == 2
    request = context.host.adapter.requests[0]["request"]
    assert request.task == clause
    assert request.metadata["source_user_text"] == text
    assert request.metadata["source_user_operation_text"] == clause
    assert Path(context.host.adapter.requests[0]["request"].cwd).resolve() != Path(
        old.workspace_path).resolve()
    assert (ingress.loop.bound_context_id, ingress.loop._binding.token) == binding
    calls = context.host.adapter.calls
    replay = await context.handler.send_text(text, session_id=context.session_id,
        turn_id="planned-execute")
    assert replay["status"] == "replayed"
    assert context.host.adapter.calls == calls


async def test_planned_empty_operation_accepts_conversation_only_without_effect(pending_host):
    context = pending_host
    text = "今天有点累。"
    plans = {"planned-chat":CompoundControlPlan(status="ok")}
    ingress = await install_plans(context, plans)
    result = await send(context, text, "planned-chat")
    assert result["state"] == "no_action"
    assert ingress.receipts["planned-chat"] == result
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []
    context.manager.query.assert_not_awaited()
    replay = await context.handler.send_text(text, session_id=context.session_id,
        turn_id="planned-chat")
    assert replay["status"] == "replayed"
    assert context.host.adapter.calls == 0


@pytest.mark.parametrize("access", ["none", "read", "write"])
async def test_independent_goal_uses_own_draft_and_preserves_workspace_access(
        pending_host, tmp_path, access):
    context = pending_host
    ingress = await context.manager._ingress_for(context.session_id)
    loop = ingress.loop
    workspace = tmp_path / "old-game"
    workspace.mkdir()
    child = ChildConversation("old-game", "Old game", str(workspace),
        context.manager.provider, context.manager.context_requirements[context.manager.provider],
        workspace_route={})
    loop._state.register(child, initial_binding_token=loop._binding.token)
    child.run_status = "done"
    loop._state.checkpoint(child)
    loop.children[child.child_id] = child
    loop._binding = ContextBinding(child.child_id, loop._binding.token)
    binding = loop._binding
    text = "帮我查一下共识算法的原始论文。"
    plan = planned(context.manager.provider, text, text, "execute",
        _host_workspace_access=access)
    await install_plans(context, {"independent-goal":plan})
    result = await send(context, text, "independent-goal")
    await context.finish()
    assert result["state"] == "work_started" and result["child_id"] == ""
    request = context.host.adapter.requests[0]["request"]
    assert request.task == text and request.session is None
    assert request.requirements.workspace_access == access
    assert request.metadata["write_intent"] is (access == "write")
    assert Path(request.cwd).parent == tmp_path / "scratch"
    assert Path(request.cwd) != workspace
    item = context.host.work.get_work_item(result["work_item_id"])
    assert context.host.work.get_project(item.project_id).metadata["scratch"] is True
    await context.host.coordinator.dispose_work_item(item.work_item_id,
        action="accept", rationale="User accepted the result.")
    assert context.host.work.get_project_by_path(item.workspace_path) is None
    assert all(row["workspacePath"] != item.workspace_path
        for row in context.host.coordinator.workspace_routing_context()["candidates"])
    promoted = context.host.coordinator.promote_work_item_to_project(item.work_item_id)
    assert promoted["workItemId"] == item.work_item_id
    assert context.host.work.get_project_by_path(item.workspace_path).project_id == promoted["projectId"]
    assert loop._binding == binding and child.work_item_id == ""
    context.manager.query.assert_not_awaited()


async def test_planned_report_reads_selected_completed_work_once_and_replays(
        pending_host, tmp_path):
    context = pending_host
    item, attempt, _ = _seed_app(context.host.work, context.host.project,
        tmp_path / "drafts", title="Memo", turn_id="memo-report", goal="Create Memo")
    context.host.work.update_attempt(attempt.attempt_id,
        metadata={"session_id":context.session_id})
    await context.manager._ingress_for(context.session_id)
    candidate = next(row for row in context.manager.work_candidates_for_context(
        context.session_id, "")[0] if row.entity_id == item.work_item_id)
    report_calls = []

    async def report(source, attrs, *, publish=None):
        report_calls.append((source, dict(attrs)))
        await publish("Memo is complete.")
        return "canonical report"

    context.manager.configure_work(context.host.control, context.host.executor,
        report_request=report)
    text = "现在帮我看看，Memo 做完了吗？"
    clause = "Memo 做完了吗？"
    plans = {"planned-report":planned(context.manager.provider, text, clause,
        "report", candidate, _host_workspace_access="none")}
    ingress = await install_plans(context, plans)
    result = await send(context, text, "planned-report")
    assert result["state"] == "work_reported"
    assert result["report_work_item_id"] == item.work_item_id
    assert result["report_result"] == "canonical report"
    assert len(report_calls) == 1 and report_calls[0][0] == clause
    assert report_calls[0][1]["workspace_ref"] == item.work_item_id
    assert any(row.get("cause") == "planned-report" and row.get("text") == "Memo is complete."
        for row in ingress.loop.history)
    assert context.host.adapter.calls == 0
    assert len(context.host.work.list_attempts(item.work_item_id)) == 1
    replay = await context.handler.send_text(text, session_id=context.session_id,
        turn_id="planned-report")
    assert replay["status"] == "replayed" and len(report_calls) == 1
    context.manager.query.assert_not_awaited()


async def test_planned_one_off_bypasses_bound_native_context_without_rebinding(
        pending_host, tmp_path):
    context = pending_host
    ingress = await context.manager._ingress_for(context.session_id)
    loop = ingress.loop
    workspace = tmp_path / "bound-context"
    workspace.mkdir()
    child = ChildConversation("bound-context", "Existing context", str(workspace),
        context.manager.provider, context.manager.context_requirements[context.manager.provider],
        workspace_route={"projectId":context.host.project.project_id})
    loop._state.register(child, initial_binding_token=loop._binding.token)
    child.run_status = "done"
    loop._state.checkpoint(child)
    loop.children[child.child_id] = child
    loop._binding = ContextBinding(child.child_id, loop._binding.token)
    binding = loop._binding
    text = "Build an independent calendar."
    plans = {"planned-bound-one-off":planned(context.manager.provider,
        text, text, "execute", one_off=True)}
    await install_plans(context, plans)
    result = await send(context, text, "planned-bound-one-off")
    await context.finish()
    assert result["state"] == "work_started" and result["child_id"] == ""
    request = context.host.adapter.requests[0]["request"]
    assert Path(request.cwd).resolve() != workspace.resolve()
    assert Path(request.cwd).resolve().parent == (tmp_path / "scratch").resolve()
    assert loop._binding == binding and child.work_item_id == ""


async def test_planned_one_off_rejects_workspace_pin_instead_of_writing_there(
        pending_host, tmp_path):
    context = pending_host
    pinned, attempt, _ = _seed_app(context.host.work, context.host.project,
        tmp_path / "drafts", title="Pinned", turn_id="pinned", goal="Pinned")
    context.host.work.update_attempt(attempt.attempt_id,
        metadata={"session_id":context.session_id})
    context.host.coordinator.set_focus(mode="pinned", work_item_id=pinned.work_item_id)
    text = "Build an independent calendar."
    plans = {"planned-pinned-one-off":planned(context.manager.provider,
        text, text, "execute", one_off=True)}
    await install_plans(context, plans)
    result = await send(context, text, "planned-pinned-one-off")
    assert result["state"] == "rejected"
    assert result["reason"] == "work_one_off_destination_unavailable"
    assert context.host.adapter.calls == 0
    assert len(context.host.work.list_work_items()) == 1


async def test_planned_amend_keeps_completed_work_and_workspace(pending_host, tmp_path):
    context = pending_host
    item, attempt, _ = _seed_app(context.host.work, context.host.project,
        tmp_path / "drafts", title="Memo", turn_id="memo", goal="Create Memo")
    context.host.work.update_attempt(attempt.attempt_id,
        metadata={"session_id":context.session_id})
    ingress = await context.manager._ingress_for(context.session_id)
    candidates, complete, _ = context.manager.work_candidates_for_context(
        context.session_id, "")
    candidate = next(row for row in candidates if row.entity_id == item.work_item_id)
    assert complete
    text = "Add a heading to Memo."
    plans = {"planned-amend":planned(context.manager.provider, text, text,
        "amend", candidate, _host_display_title="Wrong replacement title")}
    await install_plans(context, plans)
    result = await send(context, text, "planned-amend")
    await context.finish()
    assert result["state"] == "work_started"
    assert result["work_item_id"] == item.work_item_id
    assert len(context.host.work.list_work_items()) == 1
    assert len(context.host.work.list_attempts(item.work_item_id)) == 2
    persisted = context.host.work.get_work_item(item.work_item_id)
    assert persisted.title == "Memo"
    assert persisted.goal == "Create Memo"
    assert Path(context.host.adapter.requests[0]["request"].cwd).resolve() == Path(
        item.workspace_path).resolve()
    context.manager.query.assert_not_awaited()
    assert ingress.receipts["planned-amend"] == result


async def test_planned_amend_rejects_workspace_ref_conflicting_with_typed_candidate(
        pending_host, tmp_path):
    context = pending_host
    first, first_attempt, _ = _seed_app(context.host.work, context.host.project,
        tmp_path / "drafts", title="First", turn_id="first", goal="First")
    second, second_attempt, _ = _seed_app(context.host.work, context.host.project,
        tmp_path / "drafts", title="Second", turn_id="second", goal="Second")
    for attempt in (first_attempt, second_attempt):
        context.host.work.update_attempt(attempt.attempt_id,
            metadata={"session_id":context.session_id})
    await context.manager._ingress_for(context.session_id)
    candidates, complete, _ = context.manager.work_candidates_for_context(
        context.session_id, "")
    candidate = next(row for row in candidates if row.entity_id == first.work_item_id)
    assert complete
    text = "Amend the first page."
    plans = {"planned-conflicting-ref":planned(context.manager.provider,
        text, text, "amend", candidate, workspace_ref=second.work_item_id)}
    await install_plans(context, plans)
    result = await send(context, text, "planned-conflicting-ref")
    assert result["state"] == "rejected"
    assert result["reason"] == "planned_work_target_unsupported"
    assert context.host.adapter.calls == 0
    assert len(context.host.work.list_attempts(first.work_item_id)) == 1
    assert len(context.host.work.list_attempts(second.work_item_id)) == 1


async def test_planned_amend_running_work_reuses_input_attempt_and_replay(pending_host):
    context = pending_host
    context.host.adapter.manifest = replace(context.host.adapter.manifest,
        capabilities=replace(context.host.adapter.manifest.capabilities, append_input=True))
    inputs = []

    async def append_input(run_id, message):
        inputs.append((run_id, message))
        return ProviderInputDelivery("delivered")

    context.host.adapter.append_input = append_input
    context.host.runtime.register(context.host.adapter)
    work_handler = WorkLedgerHandler(context.host.coordinator,
        provider_input=context.host.runtime.append_input)
    context.manager.configure_work(context.host.control, context.host.executor,
        input_request=work_handler.submit_input)
    create = "Build a live notes page."
    amend = "Add a clear button to the live notes page."
    plans = {"planned-live-create":planned(context.manager.provider, create, create, "execute")}
    await install_plans(context, plans)
    context.host.adapter.release.clear()
    context.host.adapter.started.clear()
    try:
        first = await send(context, create, "planned-live-create")
        await asyncio.wait_for(context.host.adapter.started.wait(), 3)
        await context.host.coordinator.drain_provider_facts()
        candidates, complete, _ = context.manager.work_candidates_for_context(
            context.session_id, "")
        candidate = next(row for row in candidates
            if row.entity_id == first["work_item_id"])
        assert complete and candidate.execution == "running"
        plans["planned-live-amend"] = planned(context.manager.provider,
            amend, amend, "amend", candidate)
        result = await send(context, amend, "planned-live-amend")
        await work_handler.drain_inputs()
        assert result["state"] == "work_input_accepted"
        assert result["work_item_id"] == first["work_item_id"]
        assert result["attempt_id"] == first["attempt_id"]
        assert inputs == [(first["run_id"], amend)]
        replay = await context.handler.send_text(amend, session_id=context.session_id,
            turn_id="planned-live-amend")
        assert replay["status"] == "replayed"
        await work_handler.drain_inputs()
        assert inputs == [(first["run_id"], amend)]
        assert len(context.host.work.list_attempts(first["work_item_id"])) == 1
        context.manager.query.assert_not_awaited()
    finally:
        context.host.adapter.release.set()
        await context.finish()
        await work_handler.drain_inputs()


async def test_planned_retract_stops_selected_running_work_only_and_replays(
        pending_host, tmp_path):
    context = pending_host
    other, other_attempt, _ = _seed_app(context.host.work, context.host.project,
        tmp_path / "drafts", title="Other", turn_id="other", goal="Other")
    context.host.work.update_attempt(other_attempt.attempt_id,
        metadata={"session_id":context.session_id})
    create = "Build the stoppable page."
    stop = "Stop the stoppable page."
    plans = {"planned-stop-create":planned(context.manager.provider,
        create, create, "execute", one_off=True)}
    await install_plans(context, plans)
    context.host.adapter.release.clear()
    context.host.adapter.started.clear()
    original_cancel = context.host.runtime.cancel
    context.host.runtime.cancel = AsyncMock(side_effect=original_cancel)
    try:
        first = await send(context, create, "planned-stop-create")
        await asyncio.wait_for(context.host.adapter.started.wait(), 3)
        await context.host.coordinator.drain_provider_facts()
        candidate = next(row for row in context.manager.work_candidates_for_context(
            context.session_id, "")[0] if row.entity_id == first["work_item_id"])
        plans["planned-stop"] = planned(context.manager.provider, stop, stop,
            "retract", candidate, _host_workspace_access="none")
        result = await send(context, stop, "planned-stop")
        assert result["state"] == "stopped"
        assert result["work_item_id"] == first["work_item_id"]
        assert context.host.runtime.get_run(first["run_id"]).status in {"done", "cancelled"}
        assert context.host.work.get_attempt(other_attempt.attempt_id).execution_status == "succeeded"
        assert len(context.host.work.list_attempts(other.work_item_id)) == 1
        context.host.runtime.cancel.assert_awaited_once_with(first["run_id"])
        replay = await context.handler.send_text(stop, session_id=context.session_id,
            turn_id="planned-stop")
        assert replay["status"] == "replayed"
        context.host.runtime.cancel.assert_awaited_once()
        context.manager.query.assert_not_awaited()
    finally:
        context.host.adapter.release.set()
        await context.finish()


async def test_planned_retract_terminal_work_reports_state_without_new_attempt(
        pending_host, tmp_path):
    context = pending_host
    item, attempt, _ = _seed_app(context.host.work, context.host.project,
        tmp_path / "drafts", title="Terminal", turn_id="terminal", goal="Terminal")
    context.host.work.update_attempt(attempt.attempt_id,
        metadata={"session_id":context.session_id})
    await context.manager._ingress_for(context.session_id)
    candidate = next(row for row in context.manager.work_candidates_for_context(
        context.session_id, "")[0] if row.entity_id == item.work_item_id)
    text = "Stop the terminal page."
    plans = {"planned-terminal-stop":planned(context.manager.provider,
        text, text, "retract", candidate, _host_workspace_access="none")}
    await install_plans(context, plans)
    original_cancel = context.host.runtime.cancel
    context.host.runtime.cancel = AsyncMock(side_effect=original_cancel)
    result = await send(context, text, "planned-terminal-stop")
    assert result["state"] == "not_active"
    assert result["status"] == "succeeded"
    assert result["work_item_id"] == item.work_item_id
    assert len(context.host.work.list_attempts(item.work_item_id)) == 1
    context.host.runtime.cancel.assert_not_awaited()
    replay = await context.handler.send_text(text, session_id=context.session_id,
        turn_id="planned-terminal-stop")
    assert replay["status"] == "replayed"
    context.host.runtime.cancel.assert_not_awaited()
    assert len(context.host.work.list_attempts(item.work_item_id)) == 1
    context.manager.query.assert_not_awaited()


@pytest.mark.parametrize(("variant", "reason"), [
    ("source", "planned_work_source_invalid"),
    ("invalid_access", "planned_work_semantics_unsupported"),
    ("focus", "planned_work_semantics_unsupported"),
])
async def test_planned_work_rejects_tampered_source_and_unsupported_semantics(
        pending_host, variant, reason):
    context = pending_host
    actual = "Build the approved page."
    plan_source = "Build a different page." if variant == "source" else actual
    extra = ({"_host_workspace_access":"admin"} if variant == "invalid_access" else
        {"focus":"set"} if variant == "focus" else {})
    plans = {"planned-rejected":planned(context.manager.provider,
        plan_source, plan_source, "execute", **extra)}
    await install_plans(context, plans)
    result = await send(context, actual, "planned-rejected")
    assert result["state"] == "rejected" and result["reason"] == reason
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []
    context.manager.query.assert_not_awaited()
