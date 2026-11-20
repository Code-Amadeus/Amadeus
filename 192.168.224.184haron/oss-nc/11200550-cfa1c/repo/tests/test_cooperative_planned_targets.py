"""Host targeting for resolved single-operation Work plans."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core import session_manager as sm
from server.attention_request import AttentionRequestCoordinator
from server.compound_control import (
    CompoundControlOperation,
    CompoundControlPlan,
    SourceClause,
)
from server.control_decision import CONTROL_REFERENCE_CANDIDATES_ATTR
from server.cooperative_provider_loop import ChildConversation, ContextBinding
from server.reference_catalog import candidate_catalog_from_coordinator
from test_auip_launch import _seed_app
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_work import install_plans, send


def target_plan(provider, source, intent, candidates, **extra):
    candidates = tuple(candidates)
    kinds = {candidate.kind for candidate in candidates}
    subject = next(iter(kinds)) if len(kinds) == 1 else "open"
    action = {
        "provider": provider,
        "intent": intent,
        "task": source,
        "subject": subject,
        "_host_workspace_access": (
            "write" if intent in {"execute", "amend"} else "none"
        ),
        CONTROL_REFERENCE_CANDIDATES_ATTR: candidates,
        **extra,
    }
    return CompoundControlPlan(
        status="ok",
        operations=(CompoundControlOperation(0, source, action),),
        clauses=(SourceClause(source, 0, len(source)),),
    )


def catalog_candidate(context, kind, entity_id):
    candidates, complete, reason = candidate_catalog_from_coordinator(
        context.host.coordinator, context.session_id
    )
    assert complete, reason
    return next(
        candidate
        for candidate in candidates
        if candidate.kind == kind and candidate.entity_id == entity_id
    )


def add_project(context, path: Path, name: str):
    path.mkdir()
    return context.host.work.create_or_get_project(path, name=name)


async def test_project_execute_uses_selected_project_without_rebinding_native_context(
    pending_host, tmp_path
):
    context = pending_host
    project = add_project(context, tmp_path / "selected-project", "Selected Project")
    candidate = catalog_candidate(context, "project", project.project_id)
    ingress = await context.manager._ingress_for(context.session_id)
    loop = ingress.loop
    native_workspace = tmp_path / "native-context"
    native_workspace.mkdir()
    child = ChildConversation(
        "native-context",
        "Existing native context",
        str(native_workspace),
        context.manager.provider,
        context.manager.context_requirements[context.manager.provider],
        workspace_route={"projectId": context.host.project.project_id},
    )
    loop._state.register(child, initial_binding_token=loop._binding.token)
    child.run_status = "done"
    loop._state.checkpoint(child)
    loop.children[child.child_id] = child
    loop._binding = ContextBinding(child.child_id, loop._binding.token)
    binding = loop._binding
    text = "在课程项目里做个新的索引页。"
    await install_plans(
        context,
        {"project-create": target_plan(
            context.manager.provider, text, "execute", (candidate,)
        )},
    )

    result = await send(context, text, "project-create")
    await context.finish()

    item = context.host.work.get_work_item(result["work_item_id"])
    assert result["state"] == "work_started" and result["child_id"] == ""
    assert item is not None and item.project_id == project.project_id
    request = context.host.adapter.requests[0]["request"]
    assert Path(request.cwd).resolve() == Path(project.canonical_path).resolve()
    assert request.task == text and request.metadata["source_user_text"] == text
    assert loop._binding == binding and child.work_item_id == ""
    context.manager.query.assert_not_awaited()


async def test_project_source_amend_creates_new_work_in_project(pending_host, tmp_path):
    context = pending_host
    old, old_attempt, _ = _seed_app(
        context.host.work,
        context.host.project,
        tmp_path / "old-draft",
        title="Old Project Page",
        turn_id="old-project-page",
        goal="Create the old Project page",
    )
    context.host.work.update_attempt(
        old_attempt.attempt_id, metadata={"session_id": context.session_id}
    )
    candidate = catalog_candidate(
        context, "project", context.host.project.project_id
    )
    text = "给这个项目新增一个状态面板。"
    await install_plans(
        context,
        {"project-amend": target_plan(
            context.manager.provider, text, "amend", (candidate,)
        )},
    )

    result = await send(context, text, "project-amend")
    await context.finish()

    created = context.host.work.get_work_item(result["work_item_id"])
    assert result["state"] == "work_started"
    assert created is not None and created.work_item_id != old.work_item_id
    assert created.project_id == context.host.project.project_id
    assert len(context.host.work.list_attempts(old.work_item_id)) == 1
    assert len(context.host.work.list_attempts(created.work_item_id)) == 1
    assert context.host.adapter.requests[0]["request"].task == text


async def test_selected_work_item_keeps_its_identity_and_workspace(pending_host, tmp_path):
    context = pending_host
    item, attempt, _ = _seed_app(
        context.host.work,
        context.host.project,
        tmp_path / "drafts",
        title="Existing Page",
        turn_id="existing-page",
        goal="Create Existing Page",
    )
    context.host.work.update_attempt(
        attempt.attempt_id, metadata={"session_id": context.session_id}
    )
    candidate = catalog_candidate(context, "work_item", item.work_item_id)
    text = "把现有页面的标题加粗。"
    await install_plans(
        context,
        {"work-amend": target_plan(
            context.manager.provider, text, "amend", (candidate,)
        )},
    )

    result = await send(context, text, "work-amend")
    await context.finish()

    assert result["state"] == "work_started"
    assert result["work_item_id"] == item.work_item_id
    assert len(context.host.work.list_work_items()) == 1
    assert len(context.host.work.list_attempts(item.work_item_id)) == 2
    assert Path(context.host.adapter.requests[0]["request"].cwd).resolve() == Path(
        item.workspace_path
    ).resolve()


@pytest.mark.parametrize("variant", ["read_only", "missing_workspace"])
async def test_project_write_rejects_readonly_or_unavailable_target(
    pending_host, tmp_path, variant
):
    context = pending_host
    project_path = tmp_path / "bounded-project"
    project = add_project(context, project_path, "Bounded Project")
    candidate = catalog_candidate(context, "project", project.project_id)
    text = "在项目里增加目录页。"
    if variant == "read_only":
        context.manager.context_requirements[context.manager.provider] = replace(
            context.manager.context_requirements[context.manager.provider], workspace_access="read")
    if variant == "missing_workspace":
        project_path.rmdir()
    await install_plans(
        context,
        {"project-rejected": target_plan(
            context.manager.provider, text, "amend", (candidate,)
        )},
    )

    result = await send(context, text, "project-rejected")

    assert result["state"] == "rejected"
    assert result["reason"] == (
        "work_provider_not_writable"
        if variant == "read_only"
        else "planned_work_target_unavailable"
    )
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []


async def test_empty_planned_target_never_falls_back_to_current_work(
    pending_host, tmp_path
):
    context = pending_host
    current, attempt, _ = _seed_app(
        context.host.work,
        context.host.project,
        tmp_path / "current-draft",
        title="Current Work",
        turn_id="current-work",
        goal="Current Work",
    )
    context.host.work.update_attempt(
        attempt.attempt_id, metadata={"session_id": context.session_id}
    )
    text = "修改那个不存在的项目。"
    await install_plans(
        context,
        {"missing-target": target_plan(
            context.manager.provider, text, "amend", (), subject="project"
        )},
    )

    result = await send(context, text, "missing-target")

    assert result["state"] == "rejected"
    assert result["reason"] == "planned_work_target_unavailable"
    assert len(context.host.work.list_attempts(current.work_item_id)) == 1
    assert context.host.adapter.calls == 0
    context.manager.query.assert_not_awaited()


async def test_ambiguous_projects_use_one_attention_choice_and_replay_once(
    pending_host, tmp_path
):
    context = pending_host
    first = add_project(context, tmp_path / "project-one", "First Project")
    second = add_project(context, tmp_path / "project-two", "Second Project")
    candidates = (
        catalog_candidate(context, "project", first.project_id),
        catalog_candidate(context, "project", second.project_id),
    )
    context.manager.attention = AttentionRequestCoordinator()
    text = "在选中的项目里新增一个概览。"
    await install_plans(
        context,
        {"project-choice": target_plan(
            context.manager.provider, text, "amend", candidates
        )},
    )

    pending = await send(context, text, "project-choice")
    requests = context.manager.attention.list_pending(context.session_id)
    assert pending["state"] == "planned_work_selection_required"
    assert len(requests) == 1 and context.host.adapter.calls == 0
    replay = await context.handler.send_text(
        text, session_id=context.session_id, turn_id="project-choice"
    )
    assert replay["status"] == "replayed"
    option = next(
        row for row in requests[0]["options"] if "Second Project" in row["label"]
    )
    chosen = await context.manager.attention.resolve(
        session_id=context.session_id,
        request_id=requests[0]["id"],
        option_id=option["id"],
    )
    await context.finish()

    assert chosen["ok"] is True
    outcome = chosen["outcome"]
    assert "work_item_id" in outcome, outcome
    item = context.host.work.get_work_item(outcome["work_item_id"])
    assert outcome["state"] == "work_started"
    assert item is not None and item.project_id == second.project_id
    assert len(context.host.work.list_work_items()) == 1
    assert context.host.adapter.calls == 1
    duplicate = await context.manager.attention.resolve(
        session_id=context.session_id,
        request_id=requests[0]["id"],
        option_id=option["id"],
    )
    assert duplicate == {"ok": False, "error": "attention_request_not_found"}
    context.manager.query.assert_not_awaited()


@pytest.mark.parametrize("changed", ["session", "binding"])
async def test_attention_choice_rechecks_session_and_binding(
    pending_host, tmp_path, monkeypatch, changed
):
    context = pending_host
    projects = (
        add_project(context, tmp_path / "race-one", "Race One"),
        add_project(context, tmp_path / "race-two", "Race Two"),
    )
    candidates = tuple(
        catalog_candidate(context, "project", project.project_id)
        for project in projects
    )
    context.manager.attention = AttentionRequestCoordinator()
    text = "在选中的项目里加一页。"
    ingress = await install_plans(
        context,
        {"project-race": target_plan(
            context.manager.provider, text, "execute", candidates
        )},
    )
    pending = await send(context, text, "project-race")
    request = context.manager.attention.list_pending(context.session_id)[0]
    if changed == "session":
        monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", "another-session")
    else:
        ingress.loop._binding = ContextBinding(
            ingress.loop._binding.child_id, "changed-binding-token"
        )

    resolved = await context.manager.attention.resolve(
        session_id=context.session_id,
        request_id=request["id"],
        option_id=request["options"][0]["id"],
    )

    assert pending["state"] == "planned_work_selection_required"
    assert resolved["ok"] is False
    assert resolved["error"] == "attention_continuation_failed"
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []


async def test_project_report_passes_selected_host_identity_to_existing_owner(
    pending_host, tmp_path
):
    context = pending_host
    project = add_project(context, tmp_path / "report-project", "Report Project")
    candidate = catalog_candidate(context, "project", project.project_id)
    report = AsyncMock(return_value="Project ledger report")
    context.manager.configure_work(
        context.host.control, context.host.executor, report_request=report
    )
    text = "报告这个项目的当前状态。"
    await install_plans(
        context,
        {"project-report": target_plan(
            context.manager.provider, text, "report", (candidate,)
        )},
    )

    result = await send(context, text, "project-report")

    assert result["state"] == "work_reported"
    assert result["report_project_id"] == project.project_id
    assert result["report_result"] == "Project ledger report"
    report.assert_awaited_once()
    source, attrs = report.await_args.args
    assert source == text
    assert attrs["subject"] == "project"
    assert attrs["project_id"] == project.project_id
    assert attrs["lookup_session_id"] == context.session_id
    assert report.await_args.kwargs["publish"]
    assert context.host.adapter.calls == 0
