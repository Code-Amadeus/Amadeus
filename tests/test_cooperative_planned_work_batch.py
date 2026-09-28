"""A complete professional Work plan is accepted once before domain dispatch."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

from agent_host.provider_types import ProviderInputDelivery
from server.attention_request import AttentionRequestCoordinator
from server.compound_control import (
    CompoundControlOperation,
    CompoundControlPlan,
    SourceClause,
)
from server.control_decision import CONTROL_REFERENCE_CANDIDATES_ATTR
from server.handlers.work_ledger_handler import WorkLedgerHandler
from server.whole_turn_control import parse_whole_turn_reply
from test_auip_launch import _seed_app
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_targets import add_project, catalog_candidate
from test_cooperative_planned_work import install_plans, send


def multi_plan(provider, source, specs):
    operations = []
    clauses = []
    for index, spec in enumerate(specs):
        clause = spec["clause"]
        start = source.index(clause)
        candidates = spec.get("candidates")
        action = {
            "provider": provider,
            "intent": spec["intent"],
            "task": clause,
            "_host_workspace_access": (
                "write" if spec["intent"] in {"execute", "amend"} else "none"
            ),
            **dict(spec.get("extra") or {}),
        }
        if candidates is not None:
            candidates = tuple(candidates)
            kinds = {candidate.kind for candidate in candidates}
            action["subject"] = next(iter(kinds)) if len(kinds) == 1 else "open"
            action[CONTROL_REFERENCE_CANDIDATES_ATTR] = candidates
        operations.append(CompoundControlOperation(index, clause, action))
        clauses.append(SourceClause(clause, start, start + len(clause)))
    return CompoundControlPlan(
        status="ok", operations=tuple(operations), clauses=tuple(clauses)
    )


async def test_two_new_project_work_effects_accept_and_replay_as_one_plan(
    pending_host, tmp_path
):
    context = pending_host
    first = add_project(context, tmp_path / "first-project", "First Project")
    second = add_project(context, tmp_path / "second-project", "Second Project")
    first_ref = catalog_candidate(context, "project", first.project_id)
    second_ref = catalog_candidate(context, "project", second.project_id)
    first_clause = "在第一个项目做课程表。"
    second_clause = "在第二个项目做购物页。"
    text = first_clause + second_clause
    plan = multi_plan(context.manager.provider, text, (
        {"clause": first_clause, "intent": "execute", "candidates": (first_ref,)},
        {"clause": second_clause, "intent": "execute", "candidates": (second_ref,)},
    ))
    await install_plans(context, {"two-new": plan})

    result = await send(context, text, "two-new")
    await context.finish()

    assert result["state"] == "planned_work_batch_applied"
    assert [row["state"] for row in result["operations"]] == [
        "work_started", "work_started"
    ]
    items = [context.host.work.get_work_item(row["work_item_id"])
        for row in result["operations"]]
    assert [item.project_id for item in items] == [first.project_id, second.project_id]
    assert [row["request"].task for row in context.host.adapter.requests] == [
        first_clause, second_clause
    ]
    assert all(row["request"].metadata["source_user_text"] == text
        for row in context.host.adapter.requests)
    before = context.host.adapter.calls
    replay = await context.handler.send_text(
        text, session_id=context.session_id, turn_id="two-new"
    )
    assert replay["status"] == "replayed"
    assert context.host.adapter.calls == before == 2
    admission = context.host.control_store.find_admission(
        "chat:" + context.session_id, "two-new"
    )
    assert len(json.loads(admission["plan_json"])["effects"]) == 2


async def test_write_and_report_share_acceptance_and_keep_distinct_sources(
    pending_host, tmp_path
):
    context = pending_host
    item, attempt, _ = _seed_app(
        context.host.work, context.host.project, tmp_path / "existing",
        title="Existing Report", turn_id="existing-report", goal="Existing Report"
    )
    context.host.work.update_attempt(
        attempt.attempt_id, metadata={"session_id": context.session_id}
    )
    project = add_project(context, tmp_path / "new-project", "New Project")
    project_ref = catalog_candidate(context, "project", project.project_id)
    report_ref = catalog_candidate(context, "work_item", item.work_item_id)
    write_clause = "在新项目做一个索引页。"
    report_clause = "再报告旧报告的状态。"
    text = write_clause + report_clause
    reports = []

    async def report(source, attrs, *, publish=None):
        reports.append((source, dict(attrs)))
        return "Existing Report is complete."

    context.manager.configure_work(
        context.host.control, context.host.executor, report_request=report
    )
    plan = multi_plan(context.manager.provider, text, (
        {"clause": write_clause, "intent": "execute", "candidates": (project_ref,)},
        {"clause": report_clause, "intent": "report", "candidates": (report_ref,)},
    ))
    await install_plans(context, {"write-report": plan})

    result = await send(context, text, "write-report")
    await context.finish()

    assert result["state"] == "planned_work_batch_applied"
    assert result["operations"][0]["state"] == "work_started"
    assert result["operations"][1]["state"] == "work_reported"
    assert reports == [(report_clause, {
        "intent": "report", "subject": "work_item",
        "lookup_session_id": context.session_id,
        "workspace_ref": item.work_item_id,
    })]
    created = context.host.work.get_work_item(
        result["operations"][0]["work_item_id"]
    )
    assert created.project_id == project.project_id
    assert len(context.host.work.list_attempts(item.work_item_id)) == 1


async def test_global_project_report_preserved_by_parser_and_reads_without_work(
    pending_host,
):
    context = pending_host
    text = "概览一下我的项目。"
    raw = json.dumps({"decisions": [{
        "proposal_index": 0,
        "source_clause": text,
        "provider": context.manager.provider,
        "intent": "report",
        "subject": "project",
        "work_placement": "not_applicable",
        "session_context": "unchanged",
        "workspace_effect": "none",
        "payload_continuity": "current_turn",
        "reference_mode": "none",
        "references": None,
    }]})
    candidate_catalog = tuple(
        catalog_candidate(context, "project", project.project_id)
        for project in context.host.work.list_projects()
        if Path(project.canonical_path).is_dir()
    )
    plan = parse_whole_turn_reply(
        raw, source=text, candidates=candidate_catalog,
        provider_ids=(context.manager.provider,)
    )
    assert plan.status == "ok", plan.reason
    assert plan.operations[0].action[CONTROL_REFERENCE_CANDIDATES_ATTR] is None
    reports = []

    async def report(source, attrs, *, publish=None):
        reports.append((source, dict(attrs)))
        return "Project overview"

    context.manager.configure_work(
        context.host.control, context.host.executor, report_request=report
    )
    await install_plans(context, {"project-overview": plan})

    result = await send(context, text, "project-overview")

    assert result["state"] == "work_reported"
    assert result["report_project_id"] == ""
    assert reports == [(text, {"intent": "report", "subject": "project",
        "lookup_session_id": context.session_id, "project_id": ""})]
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []


async def test_input_and_stop_different_active_work_use_distinct_operation_identity(
    pending_host, tmp_path
):
    context = pending_host
    context.host.adapter.manifest = context.host.adapter.manifest.__class__(
        provider_id=context.host.adapter.manifest.provider_id,
        display_name=context.host.adapter.manifest.display_name,
        capabilities=context.host.adapter.manifest.capabilities.__class__(
            **{**context.host.adapter.manifest.capabilities.to_dict(),
               "append_input": True}
        ),
    )
    inputs = []

    async def append_input(run_id, message):
        inputs.append((run_id, message))
        return ProviderInputDelivery("delivered")

    context.host.adapter.append_input = append_input
    context.host.runtime.register(context.host.adapter)
    input_owner = WorkLedgerHandler(
        context.host.coordinator, provider_input=context.host.runtime.append_input
    )
    context.manager.configure_work(
        context.host.control, context.host.executor,
        input_request=input_owner.submit_input
    )
    first = add_project(context, tmp_path / "live-one", "Live One")
    second = add_project(context, tmp_path / "live-two", "Live Two")
    first_ref = catalog_candidate(context, "project", first.project_id)
    second_ref = catalog_candidate(context, "project", second.project_id)
    create_one, create_two = "创建第一个实时页面。", "创建第二个实时页面。"
    create_text = create_one + create_two
    plans = {"two-live": multi_plan(context.manager.provider, create_text, (
        {"clause": create_one, "intent": "execute", "candidates": (first_ref,)},
        {"clause": create_two, "intent": "execute", "candidates": (second_ref,)},
    ))}
    await install_plans(context, plans)
    context.host.adapter.release.clear()
    original_cancel = context.host.runtime.cancel
    context.host.runtime.cancel = AsyncMock(side_effect=original_cancel)
    try:
        started = await send(context, create_text, "two-live")
        for _ in range(100):
            if context.host.adapter.calls == 2:
                break
            await asyncio.sleep(0.01)
        assert context.host.adapter.calls == 2
        await context.host.coordinator.drain_provider_facts()
        first_work, second_work = (
            started["operations"][0], started["operations"][1]
        )
        first_work_ref = catalog_candidate(
            context, "work_item", first_work["work_item_id"]
        )
        second_work_ref = catalog_candidate(
            context, "work_item", second_work["work_item_id"]
        )
        first_input_clause = "给第一个页面加标题。"
        second_input_clause = "给第二个页面加页脚。"
        both_input_text = first_input_clause + second_input_clause
        plans["two-inputs"] = multi_plan(context.manager.provider, both_input_text, (
            {"clause": first_input_clause, "intent": "amend",
                "candidates": (first_work_ref,)},
            {"clause": second_input_clause, "intent": "amend",
                "candidates": (second_work_ref,)},
        ))
        both_inputs = await send(context, both_input_text, "two-inputs")
        await input_owner.drain_inputs()
        assert [row["state"] for row in both_inputs["operations"]] == [
            "work_input_accepted", "work_input_accepted"
        ]
        input_ids = [row["input"]["input_id"] for row in both_inputs["operations"]]
        assert len(set(input_ids)) == 2
        assert all(value.startswith("planned-input-") for value in input_ids)
        assert inputs == [(first_work["run_id"], first_input_clause),
            (second_work["run_id"], second_input_clause)]
        input_clause = "给第一个页面补一行。"
        stop_clause = "停止第二个页面。"
        text = input_clause + stop_clause
        plans["input-stop"] = multi_plan(context.manager.provider, text, (
            {"clause": input_clause, "intent": "amend",
                "candidates": (first_work_ref,)},
            {"clause": stop_clause, "intent": "retract",
                "candidates": (second_work_ref,)},
        ))

        result = await send(context, text, "input-stop")
        await input_owner.drain_inputs()

        assert result["state"] == "planned_work_batch_applied"
        assert result["operations"][0]["state"] == "work_input_accepted"
        assert result["operations"][1]["state"] == "stopped"
        accepted_input = result["operations"][0]["input"]
        assert accepted_input["input_id"].startswith("planned-input-")
        assert accepted_input["input_id"] != "input-stop"
        assert inputs[-1] == (first_work["run_id"], input_clause)
        assert len(inputs) == 3
        context.host.runtime.cancel.assert_awaited_once_with(second_work["run_id"])
        assert len(context.host.work.list_attempts(first_work["work_item_id"])) == 1
        assert len(context.host.work.list_attempts(second_work["work_item_id"])) == 1
        accepted = context.host.control_store.find_admission(
            "chat:" + context.session_id, "input-stop"
        )
        evidence = json.loads(accepted["plan_json"])["evidence"]
        assert len(evidence["compound_work_plan"]["operations"]) == 2
    finally:
        context.host.adapter.release.set()
        await context.finish()
        await input_owner.drain_inputs()


async def test_invalid_second_operation_accepts_no_effect_and_runs_no_prefix(
    pending_host, tmp_path
):
    context = pending_host
    project = add_project(context, tmp_path / "atomic-project", "Atomic Project")
    candidate = catalog_candidate(context, "project", project.project_id)
    first_clause = "先创建有效页面。"
    second_clause = "再创建第二个页面。"
    text = first_clause + second_clause
    plan = multi_plan(context.manager.provider, text, (
        {"clause": first_clause, "intent": "execute", "candidates": (candidate,)},
        {"clause": second_clause, "intent": "execute", "candidates": (candidate,),
            "extra": {"_host_workspace_access": "admin"}},
    ))
    await install_plans(context, {"invalid-second": plan})

    result = await send(context, text, "invalid-second")

    assert result["state"] == "rejected"
    assert result["reason"] == "planned_work_semantics_unsupported"
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []
    admission = context.host.control_store.find_admission(
        "chat:" + context.session_id, "invalid-second"
    )
    assert json.loads(admission["plan_json"])["effects"] == []


async def test_duplicate_existing_work_mutation_rejects_the_whole_plan(
    pending_host, tmp_path
):
    context = pending_host
    item, attempt, _ = _seed_app(
        context.host.work, context.host.project, tmp_path / "duplicate",
        title="One Work", turn_id="one-work", goal="One Work"
    )
    context.host.work.update_attempt(
        attempt.attempt_id, metadata={"session_id": context.session_id}
    )
    candidate = catalog_candidate(context, "work_item", item.work_item_id)
    first_clause = "先给页面加标题。"
    second_clause = "再给同一页面加页脚。"
    text = first_clause + second_clause
    plan = multi_plan(context.manager.provider, text, (
        {"clause": first_clause, "intent": "amend", "candidates": (candidate,)},
        {"clause": second_clause, "intent": "amend", "candidates": (candidate,)},
    ))
    await install_plans(context, {"duplicate-mutation": plan})

    result = await send(context, text, "duplicate-mutation")

    assert result["state"] == "rejected"
    assert result["reason"] == "planned_work_duplicate_mutation"
    assert context.host.adapter.calls == 0
    assert len(context.host.work.list_attempts(item.work_item_id)) == 1
    admission = context.host.control_store.find_admission(
        "chat:" + context.session_id, "duplicate-mutation"
    )
    assert json.loads(admission["plan_json"])["effects"] == []


async def test_batch_ambiguity_selects_before_any_operation_is_accepted(
    pending_host, tmp_path
):
    context = pending_host
    first = add_project(context, tmp_path / "ambiguous-one", "Ambiguous One")
    second = add_project(context, tmp_path / "ambiguous-two", "Ambiguous Two")
    fixed = add_project(context, tmp_path / "fixed", "Fixed")
    ambiguous = (
        catalog_candidate(context, "project", first.project_id),
        catalog_candidate(context, "project", second.project_id),
    )
    fixed_ref = catalog_candidate(context, "project", fixed.project_id)
    context.manager.attention = AttentionRequestCoordinator()
    first_clause, second_clause = "先做固定页。", "再在选中的项目做概览。"
    text = first_clause + second_clause
    plan = multi_plan(context.manager.provider, text, (
        {"clause": first_clause, "intent": "execute", "candidates": (fixed_ref,)},
        {"clause": second_clause, "intent": "execute", "candidates": ambiguous},
    ))
    await install_plans(context, {"batch-attention": plan})

    pending = await send(context, text, "batch-attention")

    assert pending["state"] == "planned_work_selection_required"
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []
    admission = context.host.control_store.find_admission(
        "chat:" + context.session_id, "batch-attention"
    )
    assert admission["plan_id"] is None
    request = context.manager.attention.list_pending(context.session_id)[0]
    option = next(row for row in request["options"]
        if "Ambiguous Two" in row["label"])
    resolved = await context.manager.attention.resolve(
        session_id=context.session_id, request_id=request["id"],
        option_id=option["id"]
    )
    await context.finish()

    assert resolved["ok"] is True
    assert resolved["outcome"]["state"] == "planned_work_batch_applied"
    assert context.host.adapter.calls == 2
    assert len(context.host.work.list_work_items()) == 2
