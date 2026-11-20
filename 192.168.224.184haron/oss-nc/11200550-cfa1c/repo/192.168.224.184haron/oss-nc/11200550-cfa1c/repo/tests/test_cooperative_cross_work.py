"""The speaking context does not relocate an explicitly addressed Work."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from server.cooperative_provider_loop import ChildConversation, ContextBinding
from agent_host.provider_types import ProviderInputDelivery
from server.handlers.work_ledger_handler import WorkLedgerHandler
from test_auip_launch import _seed_app
from test_cooperative_pending_turn import pending_host as pending_host


async def test_settled_amend_resolves_user_named_work_over_role_known_alias(pending_host, tmp_path):
    context = pending_host
    memo, memo_attempt, _ = _seed_app(context.host.work, context.host.project,
        tmp_path / "drafts", title="Memo", turn_id="memo", goal="写一个简单便签")
    timer, timer_attempt, _ = _seed_app(context.host.work, context.host.project,
        tmp_path / "drafts", title="Timer", turn_id="timer", goal="做一个 Timer 页面")
    for attempt in (memo_attempt, timer_attempt):
        context.host.work.update_attempt(attempt.attempt_id,
            metadata={"session_id":context.session_id})
    text = "给 Timer 加个按钮。"
    reference_queries = []

    async def query(messages, **_kwargs):
        try:
            frame = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            reference_queries.append(messages)
            return json.dumps({"references":["work_item:" + timer.work_item_id]})
        if frame["source_kind"] == "user":
            return json.dumps({"action":{"op":"work", "intent":"amend",
                "target":"Memo"}, "say":"Timer に反映するわ。"})
        return "受領状態を確認したわ。"

    context.manager.query = query
    ingress = await context.manager._ingress_for(context.session_id)
    await context.handler.send_text(text, session_id=context.session_id,
        turn_id="amend-timer")
    await context.finish()
    result = ingress.receipts["amend-timer"]
    assert result["state"] == "work_started", result
    assert result["work_item_id"] == timer.work_item_id
    assert len(context.host.work.list_attempts(memo.work_item_id)) == 1
    assert len(context.host.work.list_attempts(timer.work_item_id)) == 2
    request = context.host.adapter.requests[-1]["request"]
    assert Path(request.cwd).resolve() == Path(timer.workspace_path).resolve()
    raw = json.loads(context.host.control_store.get_effect(result["effect_id"])["payload_json"])
    assert raw["version"] == 4 and raw["work_item_id"] == timer.work_item_id
    assert raw["task"] == text and raw["source_user_text"] == text
    assert len(reference_queries) == 1


@pytest.mark.parametrize(("operation", "scope"), [(operation, scope)
    for operation in ("amend", "execute") for scope in ("settled", "running", "read_only", "projectless")
    if (operation, scope) != ("execute", "settled")] + [("amend", scope) for scope in
    ("live_target", "other_project", "missing_target", "same_read_only", "same_running")])
async def test_existing_work_keeps_its_workspace_outside_speaking_context(pending_host, tmp_path, scope, operation):
    context = pending_host
    item, previous, _ = _seed_app(context.host.work, context.host.project,
        tmp_path / "drafts", title="Memo", turn_id="memo", goal="写一个简单便签")
    context.host.work.update_attempt(previous.attempt_id, metadata={"session_id":context.session_id})
    text = "把之前那个便签加个标题。" if operation == "amend" else "就在这个目录里新做个便签。"
    reference_queries = []

    async def query(messages, **_kwargs):
        try:
            frame = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            reference_queries.append(messages)
            return json.dumps({"references":[] if scope == "missing_target" else ["work_item:" + item.work_item_id]})
        if frame["source_kind"] == "user":
            return json.dumps({"action":{"op":"work", "intent":operation,
                **({"target":"之前那个便签"} if operation == "amend" else {})}, "say":"確認して反映するわ。"})
        return "受領状態を確認したわ。"

    context.manager.query = query
    ingress = await context.manager._ingress_for(context.session_id)
    loop = ingress.loop
    inputs = []
    if scope == "live_target":
        context.host.adapter.manifest = replace(context.host.adapter.manifest,
            capabilities=replace(context.host.adapter.manifest.capabilities, append_input=True))
        async def append_input(run_id, message):
            inputs.append((run_id, message))
            return ProviderInputDelivery("delivered")
        context.host.adapter.append_input = append_input
        context.host.runtime.register(context.host.adapter)
        work_handler = WorkLedgerHandler(context.host.coordinator, provider_input=context.host.runtime.append_input)
        context.manager.work_input = work_handler.submit_input
        context.host.adapter.release.clear()
        await context.handler.send_text(text, session_id=context.session_id, turn_id="start-existing")
        await asyncio.wait_for(context.handler._stream_task, 3)
        await asyncio.wait_for(context.host.adapter.started.wait(), 3)
        started = ingress.receipts["start-existing"]
        assert started["state"] == "work_started"
    source = Path(item.workspace_path) if scope.startswith("same_") else tmp_path / "speaking-context"
    source.mkdir(exist_ok=True)
    marker = source / "untouched.txt"
    marker.write_text("source context", encoding="utf8")
    original_files = sorted(p.name for p in source.iterdir())
    policy = context.manager.context_requirements[context.manager.provider]
    if scope in {"read_only", "same_read_only"}:
        policy = replace(policy, workspace_access="read")
    source_project = (context.host.work.create_or_get_project(source)
        if scope == "other_project" else context.host.project)
    child = ChildConversation("source-context", "目录讨论", str(source), context.manager.provider,
        policy, workspace_route={} if scope == "projectless" else {"projectId":source_project.project_id})
    loop._state.register(child, initial_binding_token=loop._binding.token)
    loop._binding = ContextBinding(child.child_id, loop._binding.token)
    child.run_status = "running" if scope in {"running", "same_running"} else "done"
    loop._state.checkpoint(child)
    loop.children[child.child_id] = child
    original_binding = loop._binding
    await context.handler.send_text(text, session_id=context.session_id, turn_id="cross-work")
    if scope == "live_target":
        try:
            await asyncio.wait_for(context.handler._stream_task, 3)
            await work_handler.drain_inputs()
            result = ingress.receipts["cross-work"]
            assert result["state"] == "work_input_accepted", result
            assert inputs == [(started["run_id"], text)]
            assert context.host.adapter.calls == 1
            assert len(context.host.work.list_attempts(item.work_item_id)) == 2
            assert context.manager.active_work_for_recipient(context.session_id, child.child_id) is None
        finally:
            context.host.adapter.release.set()
            await context.finish()
        return
    await context.finish()
    result = ingress.receipts["cross-work"]
    if scope in {"missing_target", "same_running"}:
        assert result["state"] == "rejected", result
        assert result["reason"] == ("work_amend_target_none" if scope == "missing_target" else "work_context_not_settled")
        assert context.host.adapter.calls == 0
        assert len(context.host.work.list_attempts(item.work_item_id)) == 1
    elif operation == "amend":
        assert result["state"] == "work_started", result
        assert result["work_item_id"] == item.work_item_id
        assert len(context.host.work.list_attempts(item.work_item_id)) == 2
        request = context.host.adapter.requests[-1]["request"]
        assert Path(request.cwd).resolve() == Path(item.workspace_path).resolve()
        assert request.session is None
        raw = json.loads(context.host.control_store.get_effect(result["effect_id"])["payload_json"])
        assert raw["version"] == 4 and raw["work_item_id"] == item.work_item_id
        assert len(reference_queries) == 1
    else:
        # This fixture has no reusable native attachment. New work in this
        # context must not silently relocate to a default Draft workspace.
        assert result["state"] in {"rejected", "unknown"}, result
        assert context.host.adapter.calls == 0
        assert len(context.host.work.list_attempts(item.work_item_id)) == 1
    assert loop._binding == original_binding and child.work_item_id == ""
    assert marker.read_text(encoding="utf8") == "source context"
    assert sorted(p.name for p in source.iterdir()) == (
        sorted([*original_files, "accepted-c2.txt"])
        if scope == "same_read_only" else original_files)
