"""A legal workspace-less history row must not block later local Work.

Screenshot regression (2026-09-13): the role planned Codex/execute/desktop,
then the Host raised `path is required` before the adapter started. Workspace-less
history must remain a conversation fact without supplying a local directory.
"""
import asyncio

import pytest

from agent_host.work_ledger_store import WorkLedgerStore
from server.work_destination_service import WorkDestinationService
from server.protocol import Method
from core import session_manager as sm
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_openclaw_work import _install_openclaw
from test_cooperative_planned_focus_export import parse_plan, decision
from test_cooperative_planned_work import configure_professional_planner, send


@pytest.mark.parametrize("history_scope", ["none", "same_project", "other_project"])
def test_valid_local_destination_ignores_unrelated_workspace_less_history(tmp_path, history_scope):
    workspace = tmp_path / "local-project"
    workspace.mkdir()
    with WorkLedgerStore(tmp_path / "work.sqlite3") as store:
        project = store.create_or_get_project(workspace)
        if history_scope != "none":
            parent = project
            if history_scope == "other_project":
                other = tmp_path / "other-project"
                other.mkdir()
                parent = store.create_or_get_project(other)
            store.create_work_item(parent.project_id, title="Completed external research",
                goal="Look up a website", workspace_mode="none", workspace_path="")
        destination = WorkDestinationService(store, registry_check=lambda path: True)
        route = destination.resolve_workspace_route({"project_id": project.project_id, "cwd": str(workspace)})
        assert route["status"] == "resolved" and route["projectId"] == project.project_id
        foreign = tmp_path / "unrelated-directory"
        foreign.mkdir()
        refused = destination.resolve_workspace_route({"project_id": project.project_id, "cwd": str(foreign)})
        assert refused["status"] == "invalid" and refused["reason"] == "workspace_project_mismatch"


@pytest.mark.parametrize("history", ["clean", "same_session", "new_session"])
@pytest.mark.parametrize("request_kind", ["chat", "local", "desktop"])
async def test_new_input_remains_usable_after_workspace_less_openclaw_work(pending_host, tmp_path, history, request_kind):
    context = pending_host
    if history != "clean":
        source = "帮我找到并打开 bilibili。"
        adapter, _ = _install_openclaw(context, source=source, workspace_effect="none")
        first = await send(context, source, "external-first")
        await context.finish()
        assert first["state"] == "work_started" and adapter.calls == 1
        item = context.host.work.get_work_item(first["work_item_id"])
        assert item.workspace_mode == "none" and item.workspace_path == ""
        if history == "new_session":
            sm.create_session("next-local-session")
            context.session_id = "next-local-session"

    source = {"desktop": "你能在桌面上做一个关于你自己的网站吗？",
        "local": "你能做一个关于你自己的网站吗？", "chat": "谢谢，我们先聊聊音乐。"}[request_kind]
    target = tmp_path / "desktop"
    target.mkdir()
    context.host.coordinator.export_service.desktop_path = target
    plan = parse_plan(context, source, [decision(context, source,
        intent="execute", references=None, placement="draft", workspace_effect="write",
        target="desktop" if request_kind == "desktop" else "")])

    async def planner(*args):
        return plan

    configure_professional_planner(context, planner,
        work_texts=set() if request_kind == "chat" else {source})
    ingress = context.manager.ingresses.get(context.session_id)
    if ingress is not None:
        # A real manager installs its query before the first ingress. This test
        # changes only the scripted semantic answer between two user turns.
        ingress.loop.query = context.manager.query
    accepted = await context.handler.send_text(source, session_id=context.session_id, turn_id="local-next")
    assert accepted["status"] == "ok"
    await asyncio.wait_for(context.handler._stream_task, 5)
    await context.finish()
    errors = [event for method, event in context.visible if method == Method.CHAT_ERROR]
    assert errors == [], errors
    receipt = context.manager.ingresses[context.session_id].receipts.get("local-next", {})
    assert receipt.get("state") == ("no_action" if request_kind == "chat" else "work_started"), receipt
    assert context.host.adapter.calls == (0 if request_kind == "chat" else 1)
    assert list(target.iterdir()) == []


async def test_explicit_workspace_less_subject_keeps_independent_project_destination(pending_host):
    context = pending_host
    source = "查找网站，不创建本地文件。"
    _install_openclaw(context, source=source, workspace_effect="none")
    first = await send(context, source, "external-first")
    await context.finish()
    destination = context.manager.destination
    destination.set_session_project(context.session_id, context.host.project.project_id)
    destination.bind_session_context(context.session_id, "", work_item_id=first["work_item_id"])
    ingress = context.manager.ingresses[context.session_id]
    route = ingress.loop.initial_destination(context.manager.provider,
        ingress.loop.context_requirements[context.manager.provider])
    assert route["workspace"] == context.host.project.canonical_path
    assert route["workspace_route"]["projectId"] == context.host.project.project_id
    selected = context.host.work.get_session_work_context(context.session_id)
    assert selected.active_work_item_id == first["work_item_id"]
    assert context.host.work.get_work_item(first["work_item_id"]).workspace_path == ""
