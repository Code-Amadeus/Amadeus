"""Explicit professional conversation decisions reuse the existing message owners."""

import asyncio
from dataclasses import replace
import json

import pytest

from server.compound_control import CompoundControlPlan
from server.control_decision import (
    CONTROL_PAYLOAD_GROUNDING_ATTR,
    CONTROL_REFERENCE_CANDIDATES_ATTR,
    ControlPayloadGrounding,
)
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_work import planned, send
from test_cooperative_work_conversation import work_conversation_host as work_conversation_host


@pytest.mark.parametrize("role_op", ["send", "work"])
@pytest.mark.parametrize("active", [False, True])
async def test_explicit_message_to_work_does_not_add_deliverable_requirements(
        work_conversation_host, role_op, active):
    context, native = work_conversation_host
    create = "做一份算法讲解笔记。"
    question = "让它接着讲吧。"
    work_id = ""
    queries = []

    async def role(messages, **_kwargs):
        # A professional typed target must not be interpreted a second time.
        frame = json.loads(messages[-1]["content"])
        queries.append(frame)
        if frame.get("source_kind") != "user":
            return "確認したわ。"
        action = {"op":"work" if frame["current"]["text"] == create else role_op}
        return json.dumps({"say":"続きを聞いてみるわ。", "action":action})

    async def planner(ingress, _turn, receipt, _admission):
        source = receipt["text"]
        if source == create:
            return planned(context.manager.provider, source, source, "execute", one_off=True)
        candidate = next(row for row in context.manager.work_candidates_for_context(
            context.session_id, ingress.loop.bound_context_id)[0]
            if row.entity_id == work_id)
        return planned(context.manager.provider, source, source,
            "message", candidate, _host_workspace_access="none")

    context.manager.query = role
    context.manager.work_planner = planner
    if active:
        native.release.clear()
    first = await send(context, create, "create-note")
    work_id = first["work_item_id"]
    await asyncio.wait_for(native.started.wait(), 3)
    if not active:
        await context.finish()
    store = context.host.work
    before = (len(store.list_work_items()), len(store.list_operations(work_id)),
        len(store.list_attempts(work_id)))
    try:
        result = await send(context, question, "continue-explanation")
        assert result["state"] == ("work_input_accepted" if active else "started"), result
        assert (len(store.list_work_items()), len(store.list_operations(work_id)),
            len(store.list_attempts(work_id))) == before
        if active:
            await context.manager.work_input.__self__.drain_inputs()
            inputs = store.list_provider_inputs(work_id)
            assert len(inputs) == 1 and inputs[0]["state"] == "delivered"
            assert inputs[0]["provider_run_id"] == first["run_id"]
            assert native.inputs[0][0] == first["run_id"]
        else:
            await context.manager.ingresses[context.session_id].loop.wait()
            request = native.requests[-1]
            assert request.task == question
            assert request.session == native.handles[first["run_id"]]
            assert request.requirements.workspace_access == "read"
        assert all(frame.get("source_kind") in {"user", "host_receipt", "provider"}
            for frame in queries)
        assert sum(row.get("cause") == "continue-explanation"
            and row.get("text") == "続きを聞いてみるわ。"
            for row in context.publications) == 1
        replay = await context.handler.send_text(question, session_id=context.session_id,
            turn_id="continue-explanation")
        assert replay["status"] == "replayed"
    finally:
        native.release.set()


async def test_message_and_active_amendment_share_acceptance_without_losing_question(
        work_conversation_host):
    context, native = work_conversation_host
    create = "做个清单页面。"
    query_clause, amendment = "问问它为什么这样布局", "再加个导出按钮。"
    source = query_clause + "，" + amendment
    work_id = ""

    async def role(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        return (json.dumps({"say":"伝えておくわ。", "action":{"op":"work"}})
            if frame.get("source_kind") == "user" else "確認したわ。")

    async def planner(ingress, _turn, receipt, _admission):
        if receipt["text"] == create:
            return planned(context.manager.provider, create, create, "execute", one_off=True)
        candidate = next(row for row in context.manager.work_candidates_for_context(
            context.session_id, ingress.loop.bound_context_id)[0] if row.entity_id == work_id)
        message = planned(context.manager.provider, source, query_clause,
            "message", candidate, _host_workspace_access="none")
        amend = planned(context.manager.provider, source, amendment, "amend", candidate)
        return CompoundControlPlan(status="ok",
            operations=(message.operations[0], replace(amend.operations[0], operation_index=1)),
            clauses=(message.clauses[0], amend.clauses[0]))

    context.manager.query, context.manager.work_planner = role, planner
    native.release.clear()
    first = await send(context, create, "create-list")
    work_id = first["work_item_id"]
    await asyncio.wait_for(native.started.wait(), 3)
    try:
        result = await send(context, source, "message-and-amend")
        assert result["state"] == "planned_work_batch_applied", result
        assert [row["state"] for row in result["operations"]] == [
            "work_input_accepted", "work_input_accepted"]
        await context.manager.work_input.__self__.drain_inputs()
        store = context.host.work
        assert len(store.list_attempts(work_id)) == 1
        operations = store.list_operations(work_id)
        assert [operation.intent for operation in operations] == ["execute", "amend"]
        inputs = store.list_provider_inputs(work_id)
        assert len(inputs) == 2 and all(row["state"] == "delivered" for row in inputs)
        assert len(native.inputs) == 2
        assert query_clause in native.inputs[0][1] and amendment in native.inputs[1][1]
        assert all(row[0] == first["run_id"] for row in native.inputs)
    finally:
        native.release.set()


@pytest.mark.parametrize("invalid", ["write", "prior_payload"])
async def test_message_cannot_carry_write_or_confirmation_authority(work_conversation_host, invalid):
    context, _native = work_conversation_host
    source = "让它再解释一下。"

    async def role(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        return (json.dumps({"say":"確認するわ。", "action":{"op":"send"}})
            if frame.get("source_kind") == "user" else "今は実行できないわ。")

    context.manager.query = role
    # A malformed professional decision cannot make a communication writable.
    context.manager.work_planner = lambda *_args:planned(
        context.manager.provider, source, source, "message",
        **{CONTROL_REFERENCE_CANDIDATES_ATTR:None,
            "_host_workspace_access":"write" if invalid == "write" else "none",
            **({CONTROL_PAYLOAD_GROUNDING_ATTR:ControlPayloadGrounding("confirmed_prior_request")}
                if invalid == "prior_payload" else {})})
    result = await send(context, source, "invalid-write-message")
    assert result["state"] == "rejected"
    assert context.host.work.list_work_items() == []
    assert not context.manager.ingresses[context.session_id].loop.children


async def test_coarse_work_corrected_to_current_provider_conversation_without_work(
        work_conversation_host, tmp_path):
    context, native = work_conversation_host
    text = "让 Codex 说说它的看法。"

    def allocate(_label, child_id):
        directory = tmp_path / child_id
        directory.mkdir()
        return directory

    async def role(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        return (json.dumps({"say":"聞いてみるわ。", "action":{"op":"work"}})
            if frame.get("source_kind") == "user" else "確認したわ。")

    context.manager.allocate = allocate
    context.manager.query = role
    context.manager.work_planner = lambda *_args:planned(
        context.manager.provider, text, text, "message", _host_workspace_access="none")
    result = await send(context, text, "coarse-provider-question")
    assert result["state"] == "started", result
    await context.manager.ingresses[context.session_id].loop.wait()
    assert context.host.work.list_work_items() == []
    assert len(native.requests) == 1 and native.requests[0].task == text
    assert native.requests[0].requirements.workspace_access == "read"
