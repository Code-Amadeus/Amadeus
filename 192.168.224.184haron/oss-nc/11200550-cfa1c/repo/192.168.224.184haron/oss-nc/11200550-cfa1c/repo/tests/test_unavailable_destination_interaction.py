"""Unavailable selected directories must not gate unrelated user input."""
import asyncio
import json
from pathlib import Path

import pytest

from server.protocol import Method
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_work import configure_professional_planner, planned
from test_cooperative_planned_targets import catalog_candidate
from test_cooperative_provider_loop import loop_host as loop_host


@pytest.mark.parametrize("loss", ["available", "moved", "deleted", "untrusted"])
@pytest.mark.parametrize("request_kind", ["chat", "independent", "amend"])
@pytest.mark.parametrize("registry", ["fixture", "production"])
async def test_selected_directory_loss_is_local_to_its_execution(pending_host, tmp_path, monkeypatch, loss, request_kind, registry):
    context = pending_host
    # Production shares one destination owner between Chat and Work intake.
    context.host.coordinator.destination = context.manager.destination
    workspace = tmp_path / "selected 真实目录"
    workspace.mkdir()
    if registry == "production":
        from server.project_registry import cwd_in_project_registry
        monkeypatch.setattr("config.settings.WORK_PROJECT_ALLOWLIST", str(workspace))
        monkeypatch.setattr("server.work_ledger_coordinator.cwd_in_project_registry", cwd_in_project_registry)
        context.manager.destination._registry_check = cwd_in_project_registry
        assert cwd_in_project_registry(str(workspace))
    project = context.host.work.create_or_get_project(workspace)
    item = context.host.work.create_work_item(project.project_id, title="Selected page",
        goal="Build selected page", workspace_path=str(workspace))
    attempt = context.host.work.create_attempt(item.work_item_id, provider=context.manager.provider,
        task="Build selected page", metadata={"session_id": context.session_id})
    context.host.work.update_attempt(attempt.attempt_id, execution_status="succeeded")
    context.manager.destination.bind_session_context(context.session_id, project.project_id,
        work_item_id=item.work_item_id)
    target = catalog_candidate(context, "work_item", item.work_item_id)
    if loss == "moved":
        moved = tmp_path / "moved 真实目录"
        assert workspace.resolve().is_relative_to(tmp_path.resolve())
        assert moved.resolve().is_relative_to(tmp_path.resolve())
        workspace.rename(moved)
    elif loss == "deleted":
        workspace.rmdir()  # Empty directory created by this test only.
    elif loss == "untrusted":
        if registry == "production":
            monkeypatch.setattr("config.settings.WORK_PROJECT_ALLOWLIST", "")
            assert not cwd_in_project_registry(str(workspace))
        else:
            context.manager.destination._registry_check = lambda path: Path(path).resolve() != workspace.resolve()

    source = {"chat": "我们先聊聊音乐。", "independent": "另外在一个独立草稿中创建清单。",
        "amend": "修改刚才那个页面。"}[request_kind]
    plan = planned(context.manager.provider, source, source,
        "amend" if request_kind == "amend" else "execute",
        target if request_kind == "amend" else None,
        **({"one_off": True} if request_kind == "independent" else {}))
    async def planner(*args):
        return plan
    configure_professional_planner(context, planner,
        work_texts=set() if request_kind == "chat" else {source})
    accepted = await context.handler.send_text(source, session_id=context.session_id, turn_id="after-loss")
    assert accepted["status"] == "ok"
    await asyncio.wait_for(context.handler._stream_task, 4)
    await context.finish()
    receipt = context.manager.ingresses[context.session_id].receipts.get("after-loss", {})
    errors = [event for method, event in context.visible if method == Method.CHAT_ERROR]
    if request_kind == "amend" and loss != "available":
        assert receipt.get("state") == "rejected" or errors
        assert context.host.adapter.calls == 0
        configure_professional_planner(context, planner, work_texts=set())
        ingress = context.manager.ingresses[context.session_id]
        ingress.loop.query = context.manager.query
        await context.handler.send_text("被拒绝后继续聊天。", session_id=context.session_id,
            turn_id="after-rejection-chat")
        await asyncio.wait_for(context.handler._stream_task, 4)
        assert ingress.receipts["after-rejection-chat"]["state"] == "no_action"
        assert context.host.adapter.calls == 0
    else:
        assert not errors, errors
        assert receipt.get("state") == ("no_action" if request_kind == "chat" else "work_started"), receipt
        assert context.host.adapter.calls == int(request_kind != "chat")
    assert context.host.work.get_work_item(item.work_item_id).workspace_path == str(workspace.resolve())
    assert workspace.exists() is (loss in {"available", "untrusted"})


async def test_destination_changes_during_ordinary_chat_do_not_cancel_chat(loop_host):
    loop, adapter, _, _, _ = loop_host
    selected = {"workspace": "first"}
    loop.initial_destination = lambda provider, requirements: {
        "requirements": requirements, "workspace": selected["workspace"],
        "workspace_route": {"status": "resolved"}}
    async def query(messages):
        selected["workspace"] = "second"
        return json.dumps({"action": None, "say": "继续聊。"})
    loop.query = query
    assert (await loop.submit("聊点别的。"))["state"] == "no_action"
    assert not loop.children and not adapter.requests


async def test_unavailable_initial_send_rejects_without_allocating_fallback(loop_host):
    loop, adapter, controls, _, _ = loop_host
    loop.initial_destination = lambda provider, requirements: {
        "requirements": requirements, "workspace": "", "workspace_route": {
            "status": "invalid", "reason": "selected directory was removed"}}
    controls["检查选中的目录。"] = {"op": "send"}
    rejected = await loop.submit("检查选中的目录。")
    assert rejected["state"] == "rejected"
    assert rejected["reason"] == "workspace_destination_unavailable"
    assert not loop.children and not adapter.requests
    assert (await loop.submit("聊点别的。"))["state"] == "no_action"
