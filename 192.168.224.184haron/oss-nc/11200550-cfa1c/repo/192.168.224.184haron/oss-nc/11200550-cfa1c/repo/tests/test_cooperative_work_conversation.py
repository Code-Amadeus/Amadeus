"""Questions retain Work identity without turning into Work amendments."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path

import pytest

from agent_host.provider_catalog import OPENCLAW_MANIFEST
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_types import ProviderEvent, ProviderRunResult, ProviderSessionHandle
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_context_recovery import CooperativeWorkFixture
from test_cooperative_planned_work import planned, send
from server.handlers.work_ledger_handler import WorkLedgerHandler
from server.protocol import Method
from server.control_ledger import ControlLedgerConflict


class WorkspaceLessWorkFixture(CooperativeWorkFixture):
    async def run(self, request, run_id, emit):
        self.requests.append(request)
        assert request.cwd is None
        handle = request.session or ProviderSessionHandle(
            provider=self.provider_id, session_id="native-" + run_id,
            scope="work_item" if request.metadata.get("work") else "interaction")
        self.handles[run_id] = handle
        await emit(ProviderEvent(provider=self.provider_id, run_id=run_id,
            type="session.opened", session=handle))
        if request.metadata.get("source") == "control_work_effect":
            self.work_runs += 1
        self.started.set()
        await self.release.wait()
        return ProviderRunResult(status="done", result="查询已完成。", session=handle)


@pytest.fixture
async def work_conversation_host(pending_host, request):
    context = pending_host
    mode = getattr(request, "param", "local")
    native = CooperativeWorkFixture() if mode == "local" else WorkspaceLessWorkFixture()
    if mode != "local":
        context.manager.provider = mode
        context.manager.context_requirements[mode] = ProviderRequirements(
            task_kind="general", workspace_access="none", workspace_ownership="none",
            resume="attach")
        native.manifest = replace(OPENCLAW_MANIFEST, provider_id=mode)
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


@pytest.mark.parametrize("work_conversation_host", ["local", "openclaw", "workspace-less"], indirect=True)
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
            else {"op":"send_to", "target":"第一份报告"})
        return json.dumps({"action":action, "say":"確認するわ。"}, ensure_ascii=False)

    context.manager.query = query
    context.manager.work_planner = lambda _ingress, _turn, receipt, _admission: (
        planned(context.manager.provider, receipt["text"], receipt["text"],
            "message", _host_workspace_access="none")
        if receipt.get("provider_message_action") else planned(
            context.manager.provider, receipt["text"], receipt["text"],
            "execute", one_off=True,
            _host_workspace_access=native.manifest.capabilities.workspace_access))

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
    second_id = item.work_item_id
    before = (item.state, len(context.host.work.list_attempts(item.work_item_id)))
    followup = await send("这份报告用了什么方法？", "question-one")
    assert followup["state"] == "started"
    request = native.requests[-1]
    assert request.session == native.handles[first["run_id"]]
    if native.manifest.capabilities.workspace_access == "none":
        assert request.cwd is None and item.workspace_path == ""
        assert item.workspace_mode == "none"
        assert request.requirements.workspace_access == "none"
    else:
        assert Path(request.cwd) == Path(item.workspace_path)
        assert request.requirements.workspace_access == "read"
    assert request.metadata["cooperative_work_item_id"] == item.work_item_id
    assert native.work_runs == 1
    assert len(context.host.work.list_work_items()) == 1
    assert (context.host.work.get_work_item(item.work_item_id).state,
        len(context.host.work.list_attempts(item.work_item_id))) == before
    if item.workspace_path:
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

    if request.session.scope == "work_item":
        # Both tasks legitimately have no cwd; that does not make their native
        # sessions interchangeable or permit a task-scoped context to rebind.
        first_context = loop.get_context(followup["child_id"])
        with pytest.raises(ControlLedgerConflict, match="confined"):
            loop.bind_work_item(second_id, context_id=first_context.child_id)
        assert first_context.work_item_id == item.work_item_id

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
    assert not any(method == Method.CHAT_ERROR for method, _ in context.visible)
    assert {params["turn_id"] for method, params in context.visible
        if method == Method.CHAT_COMPLETE} >= {
            "question-one", "question-two", "question-two-again"}


@pytest.mark.parametrize("work_conversation_host,failure", [
    ("local", "missing_workspace"),
    ("local", "foreign_session"),
    ("openclaw", "foreign_session"),
], indirect=["work_conversation_host"])
async def test_unavailable_work_conversation_preserves_chat(
        work_conversation_host, failure, tmp_path):
    context, native = work_conversation_host
    work_id = ""

    async def query(messages, **_kwargs):
        if "typed reference-set resolver" in messages[0]["content"]:
            return json.dumps({"references":["work_item:" + work_id]})
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] != "user":
            return "今はそのタスクに接続できないけど、話は続けられるわ。"
        text = frame["current"]["text"]
        action = ({"op":"work"} if text == "做份报告。" else None
            if text == "谢谢。" else {"op":"send_to", "target":"刚才的报告"})
        return json.dumps({"say":"確認するわ。", "action":action})

    context.manager.query = query
    context.manager.work_planner = lambda _ingress, _turn, receipt, _admission: (
        planned(context.manager.provider, receipt["text"], receipt["text"],
            "message", _host_workspace_access="none")
        if receipt.get("provider_message_action") else planned(
            context.manager.provider, receipt["text"], receipt["text"],
            "execute", one_off=True,
            _host_workspace_access=native.manifest.capabilities.workspace_access))
    first = await send(context, "做份报告。", "make")
    assert first["state"] == "work_started"
    await context.finish()
    work_id = first["work_item_id"]
    item = context.host.work.get_work_item(work_id)
    attempt, = context.host.work.list_attempts(work_id)
    if failure == "missing_workspace":
        workspace = Path(item.workspace_path).resolve()
        moved = workspace.with_name(workspace.name + "-moved")
        assert workspace.is_relative_to(tmp_path.resolve())
        assert moved.is_relative_to(tmp_path.resolve())
        workspace.rename(moved)
    else:
        context.host.work.update_attempt(attempt.attempt_id, metadata={
            "provider_session":ProviderSessionHandle(provider="other-provider",
                session_id="unrelated-session", scope="interaction").to_dict()})

    rejected = await send(context, "解释一下刚才的报告。", "question")
    loop = context.manager.ingresses[context.session_id].loop
    await loop.wait()
    assert rejected["state"] == "rejected"
    assert rejected["reason"] == "addressed_context_unavailable"
    assert len(native.requests) == 1
    assert len(context.host.work.list_attempts(work_id)) == 1
    assert context.host.work.get_work_item(work_id).state == item.state
    assert not loop.children
    diagnostic = next(row for row in loop.trace
        if row["kind"] == "work_conversation_unavailable")
    assert diagnostic["work_item_id"] == work_id and diagnostic["error"]
    assert (await send(context, "谢谢。", "thanks"))["state"] == "no_action"
    assert not any(method == Method.CHAT_ERROR for method, _ in context.visible)
    assert {params["turn_id"] for method, params in context.visible
        if method == Method.CHAT_COMPLETE} >= {"question", "thanks"}


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
