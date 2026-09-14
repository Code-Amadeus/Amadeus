"""Questions retain Work identity without turning into Work amendments."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path

import pytest

from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_context_recovery import CooperativeWorkFixture
from test_cooperative_planned_work import planned
from server.handlers.work_ledger_handler import WorkLedgerHandler


@pytest.fixture
async def work_conversation_host(pending_host):
    context = pending_host
    native = CooperativeWorkFixture()
    native.provider_id = context.manager.provider
    native.manifest = replace(native.manifest, provider_id=native.provider_id,
        capabilities=replace(native.manifest.capabilities,
            task_kinds=("general", "workspace_mutation")))
    native.release.set()
    context.host.runtime.register(native)
    original = context.host.coordinator.prepare_request

    def prepare(request, run_id, intake_authority=None):
        if getattr(intake_authority, "kind", "") == "cooperative_provider_effect":
            return context.manager.prepare_runtime_request(request, run_id, intake_authority)
        return original(request, run_id, intake_authority)

    context.host.runtime.set_request_preparer(prepare)
    context.host.runtime.set_native_session_checkpoint(context.manager.checkpoint_native_session)
    input_owner = WorkLedgerHandler(context.host.coordinator,
        provider_input=context.host.runtime.append_input)
    context.manager.work_input = input_owner.submit_input
    try:
        yield context, native
    finally:
        native.release.set()
        await input_owner.drain_inputs()


async def test_terminal_work_questions_reuse_native_context_without_new_attempts(work_conversation_host):
    context, native = work_conversation_host
    second_id = ""
    frames = []

    async def query(messages, **_kwargs):
        if "typed reference-set resolver" in messages[0]["content"]:
            return json.dumps({"references":["work_item:" + second_id]})
        frame = json.loads(messages[-1]["content"])
        frames.append(frame)
        if frame["source_kind"] != "user":
            return "確認したわ。"
        text = frame["current"]["text"]
        action = ({"op":"work", "intent":"execute"} if text in {"做第一份报告。", "再做第二份报告。"}
            else {"op":"send_to", "target":"第二份报告"} if text.startswith("第二份")
            else {"op":"send"})
        return json.dumps({"action":action, "say":"確認するわ。"}, ensure_ascii=False)

    context.manager.query = query
    context.manager.work_planner = lambda _ingress, _turn, receipt, _admission: (
        planned(context.manager.provider, receipt["text"], receipt["text"],
            "message", _host_workspace_access="none")
        if receipt.get("provider_message_action") else planned(
            context.manager.provider, receipt["text"], receipt["text"],
            "execute", one_off=True))

    async def send(text, turn):
        await context.handler.send_text(text, session_id=context.session_id, turn_id=turn)
        await asyncio.wait_for(context.handler._stream_task, 4)
        await context.finish()
        loop = context.manager.ingresses[context.session_id].loop
        await loop.wait()
        return context.manager.ingresses[context.session_id].receipts[turn]

    first = await send("做第一份报告。", "first")
    loop = context.manager.ingresses[context.session_id].loop
    assert first["state"] == "work_started"
    assert not loop.children and len(native.requests) == 1
    item = context.host.work.get_work_item(first["work_item_id"])
    before = (item.state, len(context.host.work.list_attempts(item.work_item_id)))
    followup = await send("这份报告用了什么方法？", "question-one")
    assert followup["state"] == "started"
    request = native.requests[-1]
    assert request.session == native.handles[first["run_id"]]
    assert Path(request.cwd) == Path(item.workspace_path)
    assert request.requirements.workspace_access == "read"
    assert request.metadata["cooperative_work_item_id"] == item.work_item_id
    assert native.work_runs == 1
    assert len(context.host.work.list_work_items()) == 1
    assert (context.host.work.get_work_item(item.work_item_id).state,
        len(context.host.work.list_attempts(item.work_item_id))) == before
    assert context.host.work.get_project_by_path(item.workspace_path) is None

    binding = loop._binding
    second = await send("再做第二份报告。", "second")
    second_id = second["work_item_id"]
    assert second_id != item.work_item_id
    assert loop._binding == binding
    answer = await send("第二份报告的结论解释一下。", "question-two")
    assert answer["state"] == "started"
    second_context = answer["child_id"]
    assert loop._binding == binding
    assert native.requests[-1].session == native.handles[second["run_id"]]
    assert native.requests[-1].metadata["cooperative_work_item_id"] == second_id
    assert native.work_runs == 2
    assert len(context.host.work.list_attempts(second_id)) == 1
    assert len(context.host.work.list_work_items()) == 2

    # Restore the same idle conversation through the existing cold context store.
    loop.children.pop(second_context, None)
    loop._live_children.pop(second_context, None)
    again = await send("第二份报告里的例子再解释一下。", "question-two-again")
    assert again["child_id"] == second_context
    assert native.requests[-1].session == native.handles[second["run_id"]]
    assert len(context.host.work.list_attempts(second_id)) == 1
    current = [row for row in frames if row["source_kind"] == "user"][-1]
    assert {row["token"] for row in current["work_tasks"]} == {
        "work_item:" + item.work_item_id, "work_item:" + second_id}


@pytest.mark.parametrize("addressed", [False, True])
async def test_query_after_amendment_still_uses_the_same_work_input_owner(work_conversation_host, addressed):
    context, native = work_conversation_host
    work_id = ""

    async def query(messages, **_kwargs):
        if "typed reference-set resolver" in messages[0]["content"]:
            return json.dumps({"references":["work_item:" + work_id]})
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] != "user":
            return "確認したわ。"
        text = frame["current"]["text"]
        action = ({"op":"work"} if text in {"做份报告。", "给报告加个标题。"}
            else {"op":"send_to", "target":"刚才的报告"} if addressed and text == "现在还差什么？"
            else {"op":"send"})
        return json.dumps({"say":"確認するわ。", "action":action})

    def planner(ingress, _turn, receipt, _admission):
        text = receipt["text"]
        if receipt.get("provider_message_action"):
            return planned(context.manager.provider, text, text,
                "message", _host_workspace_access="none")
        candidate = (next(row for row in context.manager.work_candidates_for_context(
            context.session_id, ingress.loop.bound_context_id)[0]
            if row.entity_id == work_id) if work_id else None)
        return planned(context.manager.provider, text, text,
            "amend" if work_id else "execute", candidate,
            **({} if work_id else {"one_off":True}))

    context.manager.query, context.manager.work_planner = query, planner
    async def submit(text, turn):
        await context.handler.send_text(text, session_id=context.session_id, turn_id=turn)
        await asyncio.wait_for(context.handler._stream_task, 4)
        return context.manager.ingresses[context.session_id].receipts[turn]

    first = await submit("做份报告。", "make")
    work_id = first["work_item_id"]
    await context.finish()
    await submit("报告采用了什么方法？", "question")
    loop = context.manager.ingresses[context.session_id].loop
    await loop.wait()
    native.release.clear()
    native.started.clear()
    try:
        amendment = await submit("给报告加个标题。", "amend")
        assert amendment["work_item_id"] == work_id
        await asyncio.wait_for(native.started.wait(), 2)
        original_input = context.manager.work_input
        async def input_without_foreground(params, **kwargs):
            assert not loop._foreground.locked()
            return await original_input(params, **kwargs)
        context.manager.work_input = input_without_foreground
        result = await submit("现在还差什么？", "question-while-running")
        assert result["state"] == "work_input_accepted"
        assert native.inputs == [(amendment["run_id"], "现在还差什么？")]
        assert native.work_runs == 2 and len(native.requests) == 3
        assert len(context.host.work.list_attempts(work_id)) == 2
        assert len(context.host.work.list_work_items()) == 1
    finally:
        native.release.set()
        await context.finish()
