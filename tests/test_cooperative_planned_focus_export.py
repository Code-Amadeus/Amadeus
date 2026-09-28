"""Professional plans reuse the Project focus and Desktop export owners."""

from dataclasses import replace
import json
from pathlib import Path

from agent_host.provider_types import ProviderSessionHandle
from core import session_manager as sm
from server.compound_control import (
    CompoundControlOperation,
    CompoundControlPlan,
    SourceClause,
)
from server.control_decision import CONTROL_REFERENCE_CANDIDATES_ATTR
from server.cooperative_provider_loop import ChildConversation, ContextBinding
from server.focus_policy import finalize_work_focus_modifiers
from server.reference_catalog import candidate_catalog_from_coordinator
from server.whole_turn_control import parse_whole_turn_reply
from test_auip_launch import _seed_app
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_targets import (
    add_project,
    catalog_candidate,
    target_plan,
)
from test_cooperative_planned_work import install_plans, planned, send


def parse_plan(context, source, rows):
    candidates, complete, reason = candidate_catalog_from_coordinator(
        context.host.coordinator, context.session_id
    )
    assert complete, reason
    plan = parse_whole_turn_reply(
        json.dumps({"decisions": rows}), source=source, candidates=candidates,
        provider_ids=(context.manager.provider,)
    )
    assert plan.status == "ok", plan.reason
    return plan


def decision(context, source, *, index=0, intent, references=None,
             subject="", placement="not_applicable", session_context="unchanged",
             workspace_effect="none", target=""):
    return {
        "proposal_index": index,
        "source_clause": source,
        "provider": context.manager.provider,
        "intent": intent,
        **({"subject": subject} if subject else {}),
        **({"target": target} if target else {}),
        "work_placement": placement,
        "session_context": session_context,
        "workspace_effect": workspace_effect,
        "payload_continuity": "current_turn",
        "reference_mode": "none" if references is None else "candidates",
        "references": None if references is None else list(references),
    }


def configure_focus_owner(context):
    calls = []

    async def focus(attrs, *, session_id):
        calls.append((dict(attrs), session_id))
        if sm.get_current_session_id() != session_id:
            return {"ok": False, "authority_blocked": True,
                "message": "originating Session changed"}
        project_id = str(attrs.get("project_id") or "")
        if project_id:
            context.manager.destination.available_project(project_id)
            chosen = context.manager.destination.set_session_project(
                session_id, project_id
            )
            return {"ok": True, "message": "focused", **chosen}
        context.manager.destination.clear_session_project(session_id)
        return {"ok": True, "message": "cleared"}

    context.manager.configure_work(
        context.host.control, context.host.executor, focus_request=focus
    )
    return calls


async def finalize_modifier(plan, monkeypatch, verdict):
    monkeypatch.setattr("llm.client.remote_llm_query",
        lambda *_args, **_kwargs: verdict)
    actions = [{"type": "DELEGATE", "attrs": dict(operation.action)}
        for operation in plan.operations]
    for operation, action in zip(plan.operations, actions):
        action["attrs"]["_host_source_user_text"] = operation.source_clause
    await finalize_work_focus_modifiers(actions)
    for action in actions:
        if action["attrs"].get("one_off") == "true":
            action["attrs"]["one_off"] = True
    return replace(plan, operations=tuple(replace(operation,
        action=action["attrs"]) for operation, action in zip(
            plan.operations, actions)))


async def test_natural_project_focus_set_and_clear_use_owner_and_replay(
    pending_host, tmp_path
):
    context = pending_host
    project = add_project(context, tmp_path / "focus-project", "Focus Project")
    candidate = catalog_candidate(context, "project", project.project_id)
    calls = configure_focus_owner(context)
    set_text = "接下来切到 Focus Project。"
    set_plan = parse_plan(context, set_text, [decision(
        context, set_text, intent="focus", subject="project",
        references=(candidate.token,), session_context="bind"
    )])
    assert "task" not in set_plan.operations[0].action
    clear_text = "然后回到本会话的草稿。"
    clear_plan = parse_plan(context, clear_text, [decision(
        context, clear_text, intent="focus", subject="open",
        references=None, session_context="clear"
    )])
    plans = {"focus-set": set_plan, "focus-clear": clear_plan}
    await install_plans(context, plans)

    focused = await send(context, set_text, "focus-set")

    assert focused["state"] == "work_focus_changed"
    assert context.manager.destination.session_project(
        context.session_id
    ) == project.project_id
    replay = await context.handler.send_text(
        set_text, session_id=context.session_id, turn_id="focus-set"
    )
    assert replay["status"] == "replayed" and len(calls) == 1
    cleared = await send(context, clear_text, "focus-clear")
    assert cleared["state"] == "work_focus_changed"
    assert context.manager.destination.session_project(context.session_id) == ""
    assert calls == [
        ({"intent": "focus", "project_id": project.project_id}, context.session_id),
        ({"intent": "focus"}, context.session_id),
    ]
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []


async def test_focus_then_explicit_project_work_keeps_independent_placement(
    pending_host, tmp_path
):
    context = pending_host
    focused_project = add_project(
        context, tmp_path / "future-project", "Future Project"
    )
    work_project = add_project(
        context, tmp_path / "current-project", "Current Project"
    )
    focused_ref = catalog_candidate(
        context, "project", focused_project.project_id
    )
    work_ref = catalog_candidate(context, "project", work_project.project_id)
    calls = configure_focus_owner(context)
    focus_clause = "以后切到 Future Project。"
    work_clause = "现在在 Current Project 做个索引页。"
    text = focus_clause + work_clause
    plan = parse_plan(context, text, [
        decision(context, focus_clause, index=0, intent="focus",
            subject="project", references=(focused_ref.token,),
            session_context="bind"),
        decision(context, work_clause, index=1, intent="execute",
            subject="project", references=(work_ref.token,),
            placement="project", workspace_effect="write"),
    ])
    await install_plans(context, {"focus-and-work": plan})

    result = await send(context, text, "focus-and-work")
    await context.finish()

    assert result["state"] == "planned_work_batch_applied"
    assert result["operations"][0]["state"] == "work_focus_changed"
    assert result["operations"][1]["state"] == "work_started"
    item = context.host.work.get_work_item(
        result["operations"][1]["work_item_id"]
    )
    assert item.project_id == work_project.project_id
    assert Path(context.host.adapter.requests[0]["request"].cwd).resolve() == Path(
        work_project.canonical_path
    ).resolve()
    assert context.manager.destination.session_project(
        context.session_id
    ) == focused_project.project_id
    assert len(calls) == 1


async def test_audited_work_focus_modifier_reuses_focus_owner(
    pending_host, tmp_path, monkeypatch
):
    context = pending_host
    project = add_project(context, tmp_path / "audited-focus", "Audited Focus")
    candidate = catalog_candidate(context, "project", project.project_id)
    calls = configure_focus_owner(context)
    text = "切到 Audited Focus 并做一个目录页。"
    plan = parse_plan(context, text, [decision(
        context, text, intent="execute", subject="project",
        references=(candidate.token,), placement="project",
        session_context="bind", workspace_effect="write"
    )])
    assert plan.operations[0].action["focus"] == "set"
    plan = await finalize_modifier(plan, monkeypatch, "SET")
    await install_plans(context, {"audited-focus": plan})

    result = await send(context, text, "audited-focus")
    await context.finish()

    assert result["state"] == "work_started"
    assert result["focus"]["state"] == "work_focus_changed"
    item = context.host.work.get_work_item(result["work_item_id"])
    assert item.project_id == project.project_id
    assert len(calls) == 1


async def test_denied_work_focus_modifier_preserves_work_without_focus(
    pending_host, monkeypatch
):
    context = pending_host
    calls = configure_focus_owner(context)
    text = "做一个草稿页面。"
    plan = parse_plan(context, text, [decision(
        context, text, intent="execute", references=None, placement="draft",
        session_context="clear", workspace_effect="write"
    )])
    assert plan.operations[0].action["focus"] == "clear"
    plan = await finalize_modifier(plan, monkeypatch, "NONE")
    action = plan.operations[0].action
    assert "focus" not in action and action["one_off"] is True
    await install_plans(context, {"denied-focus": plan})

    result = await send(context, text, "denied-focus")
    await context.finish()

    assert result["state"] == "work_started"
    assert "focus" not in result
    assert len(calls) == 0
    assert context.host.adapter.calls == 1


async def test_focus_and_clear_override_an_old_bound_context_for_later_new_work(
    pending_host, tmp_path
):
    context = pending_host
    context.host.adapter.manifest = replace(context.host.adapter.manifest,
        capabilities=replace(context.host.adapter.manifest.capabilities,
            resume="attach"))
    context.host.runtime.register(context.host.adapter)
    context.host.control.cooperative_context_resolver = (
        context.manager.resolve_work_recipient
    )
    old_project = context.host.project
    old_work, old_attempt, _ = _seed_app(
        context.host.work, old_project, tmp_path / "old-work",
        title="Old Work", turn_id="old-work", goal="Old Work"
    )
    context.host.work.update_attempt(old_attempt.attempt_id,
        metadata={"session_id": context.session_id})
    next_project = add_project(context, tmp_path / "next-project", "Next Project")
    next_ref = catalog_candidate(context, "project", next_project.project_id)
    old_work_ref = catalog_candidate(context, "work_item", old_work.work_item_id)
    ingress = await context.manager._ingress_for(context.session_id)
    loop = ingress.loop
    context.manager.destination.set_session_project(
        context.session_id, old_project.project_id
    )
    child = ChildConversation(
        "old-native", "Old native context", old_project.canonical_path,
        context.manager.provider,
        context.manager.context_requirements[context.manager.provider],
        native_session=ProviderSessionHandle(provider=context.manager.provider,
            session_id="native-old", scope="interaction"),
        workspace_route={"projectId": old_project.project_id}
    )
    loop._state.register(child, initial_binding_token=loop._binding.token)
    child.run_status = "done"
    loop._state.checkpoint(child)
    loop.children[child.child_id] = child
    loop._binding = ContextBinding(child.child_id, loop._binding.token)
    binding = loop._binding
    configure_focus_owner(context)
    focus_text = "把后续工作切到 Next Project。"
    clear_text = "后续回到草稿。"
    project_work = "做一个项目页面。"
    old_amend = "给原来的 Old Work 加标题。"
    draft_work = "再做一个独立草稿页。"
    plans = {
        "focus-next": parse_plan(context, focus_text, [decision(
            context, focus_text, intent="focus", subject="project",
            references=(next_ref.token,), session_context="bind"
        )]),
        "new-in-project": planned(
            context.manager.provider, project_work, project_work, "execute"
        ),
        "amend-old": target_plan(
            context.manager.provider, old_amend, "amend", (old_work_ref,)
        ),
        "focus-clear": parse_plan(context, clear_text, [decision(
            context, clear_text, intent="focus", subject="open",
            references=None, session_context="clear"
        )]),
        "new-in-draft": planned(
            context.manager.provider, draft_work, draft_work, "execute"
        ),
    }
    await install_plans(context, plans)

    assert (await send(context, focus_text, "focus-next"))["state"] == (
        "work_focus_changed"
    )
    project_result = await send(context, project_work, "new-in-project")
    await context.finish()
    assert project_result["state"] == "work_started", project_result
    project_item = context.host.work.get_work_item(project_result["work_item_id"])
    assert project_result["child_id"] == ""
    assert project_item.project_id == next_project.project_id
    assert Path(context.host.adapter.requests[-1]["request"].cwd).resolve() == Path(
        next_project.canonical_path
    ).resolve()
    amended = await send(context, old_amend, "amend-old")
    await context.finish()
    assert amended["work_item_id"] == old_work.work_item_id
    assert Path(context.host.adapter.requests[-1]["request"].cwd).resolve() == Path(
        old_work.workspace_path
    ).resolve()
    assert (await send(context, clear_text, "focus-clear"))["state"] == (
        "work_focus_changed"
    )
    draft_result = await send(context, draft_work, "new-in-draft")
    await context.finish()
    draft_item = context.host.work.get_work_item(draft_result["work_item_id"])
    assert draft_result["child_id"] == ""
    assert draft_item.project_id not in {
        old_project.project_id, next_project.project_id
    }
    assert Path(context.host.adapter.requests[-1]["request"].cwd).resolve().parent == (
        tmp_path / "scratch"
    ).resolve()
    assert loop.bound_context_id == "" and loop._binding.token != binding.token
    durable_binding, _ = loop._state.load_catalog()
    assert durable_binding["context_id"] is None
    retained = loop.get_context(child.child_id)
    assert retained is child and not retained.closed and retained.native_session == (
        child.native_session
    )


async def test_allowed_clear_keeps_draft_placement_from_the_shared_normalizer(
    pending_host, monkeypatch
):
    context = pending_host
    context.manager.destination.set_session_project(
        context.session_id, context.host.project.project_id
    )
    calls = configure_focus_owner(context)
    text = "回到草稿并做一个独立页面。"
    plan = parse_plan(context, text, [decision(
        context, text, intent="execute", references=None, placement="draft",
        session_context="clear", workspace_effect="write"
    )])
    assert plan.operations[0].action["one_off"] is True
    plan = await finalize_modifier(plan, monkeypatch, "CLEAR")
    await install_plans(context, {"clear-draft": plan})

    result = await send(context, text, "clear-draft")
    await context.finish()

    item = context.host.work.get_work_item(result["work_item_id"])
    assert result["state"] == "work_started"
    assert result["focus"]["state"] == "work_focus_changed"
    assert item.project_id != context.host.project.project_id
    assert context.manager.destination.session_project(context.session_id) == ""
    assert len(calls) == 1


async def test_inherit_clear_dispatches_v6_before_unbinding_native_context(
    pending_host, tmp_path, monkeypatch
):
    context = pending_host
    context.host.adapter.manifest = replace(context.host.adapter.manifest,
        capabilities=replace(context.host.adapter.manifest.capabilities,
            resume="attach"))
    context.host.runtime.register(context.host.adapter)
    context.host.control.cooperative_context_resolver = (
        context.manager.resolve_work_recipient
    )
    ingress = await context.manager._ingress_for(context.session_id)
    loop = ingress.loop
    project = context.host.project
    context.manager.destination.set_session_project(
        context.session_id, project.project_id
    )
    native = ProviderSessionHandle(provider=context.manager.provider,
        session_id="native-v6", scope="interaction")
    child = ChildConversation("native-v6", "Native V6", project.canonical_path,
        context.manager.provider,
        context.manager.context_requirements[context.manager.provider],
        native_session=native, workspace_route={"projectId":project.project_id})
    loop._state.register(child, initial_binding_token=loop._binding.token)
    child.run_status = "done"
    loop._state.checkpoint(child)
    loop.children[child.child_id] = child
    loop._binding = ContextBinding(child.child_id, loop._binding.token)
    configure_focus_owner(context)
    text = "这次沿用当前目录，同时让后续回到草稿。"
    plan = parse_plan(context, text, [decision(
        context, text, intent="execute", references=None, placement="inherit",
        session_context="clear", workspace_effect="write"
    )])
    assert plan.operations[0].action.get("one_off") is not True
    plan = await finalize_modifier(plan, monkeypatch, "CLEAR")
    await install_plans(context, {"inherit-clear": plan})

    result = await send(context, text, "inherit-clear")
    await context.finish()

    request = context.host.adapter.requests[0]["request"]
    assert result["state"] == "work_started" and result["child_id"] == child.child_id
    assert request.cwd == project.canonical_path and request.session == native
    assert loop.bound_context_id == ""
    assert loop.get_context(child.child_id) is child and not child.closed


async def test_desktop_plan_uses_host_marker_and_existing_permission_owner(
    pending_host, tmp_path
):
    context = pending_host
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    context.host.coordinator.export_service.desktop_path = desktop
    original_run = context.host.adapter.run

    async def stage_export(request, run_id, emit):
        result = await original_run(request, run_id, emit)
        staging = Path(request.metadata["export_plan"]["staging_root"])
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "index.html").write_text("<h1>Export</h1>", encoding="utf-8")
        return result

    context.host.adapter.run = stage_export
    text = "做一个简单页面并放到桌面。"
    plan = parse_plan(context, text, [decision(
        context, text, intent="execute", references=None, placement="draft",
        workspace_effect="write", target="desktop"
    )])
    action = plan.operations[0].action
    assert action["_host_external_target_authorized"] == "desktop"
    assert action["target"] == "desktop"
    await install_plans(context, {"desktop-work": plan})

    result = await send(context, text, "desktop-work")
    await context.finish()

    assert result["state"] == "work_started"
    request = context.host.adapter.requests[0]["request"]
    assert request.metadata["external_export"] == {"target": "desktop"}
    pending = context.host.work.list_permission_requests(
        result["work_item_id"], status="pending"
    )
    assert len(pending) == 1
    assert list(desktop.iterdir()) == []
    denied = await context.host.coordinator.resolve_permission(
        pending[0].request_id, allow=False,
        work_item_id=result["work_item_id"], attempt_id=result["attempt_id"]
    )
    assert denied["permission"]["status"] == "denied"
    assert denied["exportedPaths"] == [] and list(desktop.iterdir()) == []
    replay = await context.handler.send_text(
        text, session_id=context.session_id, turn_id="desktop-work"
    )
    assert replay["status"] == "replayed"
    assert context.host.adapter.calls == 1


async def test_desktop_target_without_host_authority_rejects_without_work(
    pending_host,
):
    context = pending_host
    text = "做个普通页面。"
    action = {"provider": context.manager.provider, "intent": "execute",
        "task": text, "target": "desktop", "_host_workspace_access": "write",
        CONTROL_REFERENCE_CANDIDATES_ATTR: None}
    plan = CompoundControlPlan(status="ok",
        operations=(CompoundControlOperation(0, text, action),),
        clauses=(SourceClause(text, 0, len(text)),))
    await install_plans(context, {"fake-desktop": plan})

    result = await send(context, text, "fake-desktop")

    assert result["state"] == "rejected"
    assert result["reason"] == "planned_work_semantics_unsupported"
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []
    forged = decision(context, text, intent="execute", references=None,
        placement="draft", workspace_effect="write", target="desktop")
    forged["_host_external_target_authorized"] = "desktop"
    raw = json.dumps({"decisions": [forged]})
    parsed = parse_whole_turn_reply(raw, source=text, candidates=(),
        provider_ids=(context.manager.provider,))
    assert parsed.status == "invalid"
