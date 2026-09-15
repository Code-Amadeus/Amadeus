"""A typed AUIP after-Work decision joins the formal Work planner once."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_host.provider_authoring import auip_authoring_outcome_requirement
from server.auip_control_decision import (
    AuipControlDecision,
    AuipControlDecisionResolver,
)
from server.auip_launch import AuipLaunchCoordinator
from server.auip_bundle_validation import finalize_staged_auip_web_bundle
from server.auip_app_source import discover_launchable_auip_app
from server.compound_control import (
    CompoundControlOperation,
    CompoundControlPlan,
    SourceClause,
)
from server.control_decision import CONTROL_REFERENCE_CANDIDATES_ATTR
from server.attention_request import AttentionRequestCoordinator
from server.protocol import Method
from server.reference_catalog import candidate_catalog_from_coordinator
from server.work_planner import RuntimeWorkPlanner
from test_auip_bundle_validation import _bundle
from test_auip_control_decision import _Runtime
from test_auip_launch import _seed_app
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_work import (
    configure_professional_planner,
    planned,
    send,
)
from test_cooperative_planned_targets import add_project, catalog_candidate
from test_cooperative_planned_work_batch import multi_plan
from test_runtime_work_planner import decision as planner_decision


async def install_after_work(context, tmp_path, *, route=None,
                             active_decision=None, relation="independent"):
    events = []

    async def emit(method, payload):
        events.append((method, payload))

    launch = AuipLaunchCoordinator(
        artifacts=context.host.work,
        work_roster=context.host.coordinator,
        attention=context.manager.attention,
        emit=emit,
    )
    source_calls = []

    if active_decision is None:
        async def source_query(_messages):
            source_calls.append(True)
            return json.dumps({"action":"engage", "timing":"after_work",
                "mode":"collaborate", "target":"",
                **({"work_relation":relation} if relation is not None else {})})

        context.manager.auip_decider = AuipControlDecisionResolver(
            query=source_query, app_runtime=_Runtime(), launch_catalog=launch,
            has_active_work=lambda _session:()
        )
        context.manager.auip_entry_context = lambda session: (
            launch.render_prompt_context(
                session, language="ja", include_control_contract=False
            )
        )
    else:
        class ActiveDecision:
            def capture(self, **_kwargs):
                source_calls.append(True)
                return active_decision

        context.manager.auip_decider = ActiveDecision()
    async def real_route(attrs, **kwargs):
        kwargs.pop("user_text", None)
        return await launch.route_control(attrs, **kwargs)

    context.manager.auip_router = AsyncMock(
        side_effect=route or real_route
    )
    context.manager.auip_cancel_deferred = launch.cancel_deferred
    return launch, events, source_calls


@pytest.mark.parametrize("relation", [None, "subsumed", "independent"])
@pytest.mark.parametrize("project_history", [True, False])
async def test_real_planned_work_reserves_and_launches_once(
    pending_host, tmp_path, monkeypatch, relation, project_history
):
    context = pending_host
    if not project_history:
        context.host.work.set_project_state(context.host.project.project_id, "retired")
    launch, events, source_calls = await install_after_work(
        context, tmp_path, relation=relation
    )
    assert launch.has_project_history() is project_history
    text = "创建一个计数器应用；完成后打开它一起试试。"
    clause = "创建一个计数器应用"

    monkeypatch.setattr("llm.prompts.registered_provider_ids",
        lambda:(context.manager.provider,))
    planner_requests = []

    async def planning_query(messages):
        planner_requests.append(messages)
        assert "after_work" in json.dumps(messages, ensure_ascii=False)
        reply = json.loads(planner_decision(clause, context.manager.provider, "execute"))
        # Observed model spelling: the Draft axis already establishes a new
        # destination; this category does not identify an existing Project.
        reply["decisions"][0]["subject"] = "project"
        return json.dumps(reply)

    planner = RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=planning_query, provider=context.manager.provider)

    configure_professional_planner(context, planner, work_texts={text})
    original_run = context.host.adapter.run

    async def author_app(request, run_id, emit):
        result = await original_run(request, run_id, emit)
        root = Path(request.cwd)
        assets = _bundle(root)
        manifest = Path(__file__).resolve().parents[1] / (
            "examples/auip-2048/auip.manifest.json"
        )
        (root / "auip.manifest.json").write_bytes(manifest.read_bytes())
        finalize_staged_auip_web_bundle(root, materialized_files=tuple(assets))
        return result

    context.host.adapter.run = author_app

    await context.handler.send_text(
        text, session_id=context.session_id, turn_id="planned-after-work"
    )
    await asyncio.wait_for(context.handler._stream_task, 5)
    await context.finish()
    receipt = context.manager.ingresses[context.session_id].receipts[
        "planned-after-work"
    ]

    assert receipt["state"] == "work_auip_batch_started"
    assert receipt["work"]["state"] == "work_started"
    assert len(source_calls) == len(planner_requests) == 1
    assert context.host.adapter.calls == 1


    assert context.manager.auip_router.await_count == 1
    attrs = context.manager.auip_router.await_args.args[0]
    assert attrs["_host_work_binding"] == "turn"
    assert attrs["mode"] == "collaborate"
    admission = context.host.control_store.find_admission(
        "chat:" + context.session_id, "planned-after-work"
    )
    evidence = json.loads(admission["plan_json"])["evidence"]
    rows = evidence["cooperative_batch"]["actions"]
    assert rows[0]["source_start"] == 0
    assert rows[0]["source_end"] == len(clause)
    assert rows[1]["source_start"] == 0
    assert rows[1]["source_end"] == len(text)
    assert context.host.adapter.requests[0]["request"].task != text
    assert context.host.adapter.requests[0]["request"].metadata[
        "host_outcome_requirement"] == auip_authoring_outcome_requirement(mode="collaborate")
    await launch.on_work_updated(Method.WORK_UPDATED, {"reason":"provider.result"})
    opened = [payload for method, payload in events
        if method == Method.AUIP_LAUNCH_REQUESTED]
    assert len(opened) == 1
    assert opened[0]["work_item_id"] == receipt["work"]["work_item_id"]
    replay = await context.handler.send_text(
        text, session_id=context.session_id, turn_id="planned-after-work"
    )
    assert replay["status"] == "replayed"
    assert context.manager.auip_router.await_count == 1
    assert context.host.adapter.calls == 1


@pytest.mark.parametrize("work_proposal", [False, True])
async def test_empty_history_auip_query_requires_a_work_proposal(
        pending_host, tmp_path, work_proposal):
    context = pending_host
    context.host.work.set_project_state(context.host.project.project_id, "retired")
    launch, _events, _source_calls = await install_after_work(context, tmp_path)
    assert not launch.has_project_history()
    app_query = AsyncMock(return_value='{"action":"none"}')
    context.manager.auip_decider._query = app_query
    text = "做个便签页吧。" if work_proposal else "今天有点累。"
    planner = AsyncMock(return_value=planned(context.manager.provider,
        text, text, "execute", one_off=True))
    configure_professional_planner(context, planner,
        work_texts={text} if work_proposal else set())
    result = await send(context, text, "fresh-turn")
    await context.finish()
    assert result["state"] == ("work_started" if work_proposal else "no_action")
    assert app_query.await_count == planner.await_count == int(work_proposal)
    assert context.host.adapter.calls == int(work_proposal)
    assert not launch._deferred


async def test_completed_work_amendment_reuses_identity_then_launches_once(
    pending_host, tmp_path
):
    context = pending_host
    item, attempt, artifact = _seed_app(
        context.host.work, context.host.project, tmp_path / "counter-app",
        title="Counter", turn_id="original-counter", goal="Build Counter",
        with_manifest=False
    )
    context.host.work.update_attempt(attempt.attempt_id,
        metadata={"session_id":context.session_id})
    context.host.work.register_artifact(item.work_item_id,
        attempt_id=artifact.attempt_id, kind="business.file",
        title=artifact.title, path=artifact.path, status="registered",
        sha256=artifact.sha256,
        metadata={"relative_path":"index.html",
            "attribution":"workspace_window"})
    candidate = next(row for row in candidate_catalog_from_coordinator(
        context.host.coordinator, context.session_id)[0]
        if row.kind == "work_item" and row.entity_id == item.work_item_id)
    launch, events, source_calls = await install_after_work(context, tmp_path)
    text = "把刚才那个计数器改成深色，改好再打开咱们试试。"
    clause = "把刚才那个计数器改成深色"

    async def planner(*_args):
        return planned(context.manager.provider, text, clause, "amend", candidate)

    configure_professional_planner(context, planner, work_texts={text})
    original_run = context.host.adapter.run

    async def amend_app(request, run_id, emit):
        result = await original_run(request, run_id, emit)
        root = Path(request.cwd)
        example = tmp_path / "provider-bundle-template"
        example.mkdir(exist_ok=True)
        _bundle(example)
        manifest_path = example / "auip.manifest.json"
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        original_manifest = json.dumps(original, ensure_ascii=False, indent=2)
        manifest = json.loads((Path(__file__).resolve().parents[1]
            / "examples/auip-2048/auip.manifest.json").read_text(encoding="utf-8"))
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8")
        entry_path = example / "index.html"
        entry_path.write_text(entry_path.read_text(encoding="utf-8").replace(
            original_manifest, json.dumps(manifest, ensure_ascii=False, indent=2)),
            encoding="utf-8")
        for filename in ("index.html", "auip.manifest.json"):
            (root / filename).write_bytes((example / filename).read_bytes())
        finalize_staged_auip_web_bundle(root, materialized_files=tuple(
            request.metadata["auip_host_materialized_files"]))
        return result

    context.host.adapter.run = amend_app
    await context.handler.send_text(
        text, session_id=context.session_id, turn_id="amend-after-work"
    )
    await asyncio.wait_for(context.handler._stream_task, 5)
    await context.finish()
    receipt = context.manager.ingresses[context.session_id].receipts[
        "amend-after-work"
    ]

    assert receipt["state"] == "work_auip_batch_started"
    assert receipt["work"]["work_item_id"] == item.work_item_id
    assert receipt["work"]["attempt_id"] != attempt.attempt_id
    assert len(context.host.work.list_work_items()) == 1
    assert len(context.host.work.list_attempts(item.work_item_id)) == 2
    request = context.host.adapter.requests[0]["request"]
    assert request.cwd == item.workspace_path
    assert request.metadata["work"]["work_item_id"] == item.work_item_id
    assert request.metadata["host_outcome_requirement"]["facet"] == "auip.application"
    assert len(source_calls) == 1 and context.manager.auip_router.await_count == 1
    attrs = context.manager.auip_router.await_args.args[0]
    assert attrs["_host_work_binding"] == "turn"
    assert attrs["_host_work_item_id"] == item.work_item_id
    app = discover_launchable_auip_app(context.host.work, item.work_item_id)
    latest = context.host.work.get_attempt(receipt["work"]["attempt_id"])
    assessment = context.host.work.latest_completion(item.work_item_id)
    assert app is not None, (assessment.completeness, assessment.attention,
        assessment.rationale, assessment.evidence)
    candidates = launch.candidates(context.session_id)
    assert candidates, (latest.attempt_id, app)
    assert latest.attempt_id in candidates[0].contributing_attempt_ids, candidates[0]
    assert launch._deferred, (receipt["auip"], latest.metadata)
    matched = launch._attempts_for_turn(context.session_id, "amend-after-work")
    assert [row.attempt_id for row in matched] == [latest.attempt_id], latest.metadata
    assert latest.execution_status == "succeeded"
    await launch.on_work_updated(Method.WORK_UPDATED, {"reason":"provider.result"})
    assert not launch._deferred, launch._deferred
    opened = [payload for method, payload in events
        if method == Method.AUIP_LAUNCH_REQUESTED]
    assert len(opened) == 1 and opened[0]["work_item_id"] == item.work_item_id, events
    replay = await context.handler.send_text(
        text, session_id=context.session_id, turn_id="amend-after-work"
    )
    assert replay["status"] == "replayed"
    assert context.host.adapter.calls == 1
    assert context.manager.auip_router.await_count == 1


async def test_reservation_failure_accepts_no_work(pending_host, tmp_path):
    context = pending_host
    item, attempt, _ = _seed_app(context.host.work, context.host.project,
        tmp_path / "existing-reservation", title="Existing Counter",
        turn_id="existing-reservation", goal="Existing Counter",
        with_manifest=False)
    context.host.work.update_attempt(attempt.attempt_id,
        metadata={"session_id":context.session_id})
    candidate = next(row for row in candidate_catalog_from_coordinator(
        context.host.coordinator, context.session_id)[0]
        if row.kind == "work_item" and row.entity_id == item.work_item_id)

    async def reject_route(_attrs, **_kwargs):
        return {"ok":False, "error":"reservation refused"}

    launch, _events, _calls = await install_after_work(
        context, tmp_path, route=reject_route
    )
    text = "修改现有计数器；完成后打开它。"
    clause = "修改现有计数器"

    async def planner(*_args):
        return planned(context.manager.provider, text, clause,
            "amend", candidate)

    configure_professional_planner(context, planner, work_texts={text})
    await context.handler.send_text(
        text, session_id=context.session_id, turn_id="reservation-failed"
    )
    await asyncio.wait_for(context.handler._stream_task, 5)
    result = context.manager.ingresses[context.session_id].receipts[
        "reservation-failed"
    ]

    assert result["state"] == "rejected"
    assert result["reason"] == "work_auip_batch_reservation_rejected"
    assert context.host.adapter.calls == 0
    assert len(context.host.work.list_work_items()) == 1
    assert len(context.host.work.list_attempts(item.work_item_id)) == 1
    assert not launch._deferred


async def test_one_amend_with_report_binds_only_write_and_delivers_report_facts(
    pending_host, tmp_path
):
    context = pending_host
    counter, counter_attempt, counter_artifact = _seed_app(
        context.host.work, context.host.project, tmp_path / "mixed-counter",
        title="Counter", turn_id="mixed-counter", goal="Counter",
        with_manifest=False)
    memo, memo_attempt, _ = _seed_app(
        context.host.work, context.host.project, tmp_path / "mixed-memo",
        title="Memo", turn_id="mixed-memo", goal="Memo", with_manifest=False)
    for attempt in (counter_attempt, memo_attempt):
        context.host.work.update_attempt(attempt.attempt_id,
            metadata={"session_id":context.session_id})
    context.host.work.register_artifact(counter.work_item_id,
        attempt_id=counter_artifact.attempt_id, kind="business.file",
        title=counter_artifact.title, path=counter_artifact.path,
        status="registered", sha256=counter_artifact.sha256,
        metadata={"relative_path":"index.html",
            "attribution":"workspace_window"})
    catalog = candidate_catalog_from_coordinator(
        context.host.coordinator, context.session_id)[0]
    counter_ref = next(row for row in catalog
        if row.kind == "work_item" and row.entity_id == counter.work_item_id)
    memo_ref = next(row for row in catalog
        if row.kind == "work_item" and row.entity_id == memo.work_item_id)
    launch, events, _calls = await install_after_work(context, tmp_path)
    amend_clause = "把计数器改成深色"
    report_clause = "顺便告诉我便签做完没"
    text = amend_clause + "，改好再打开；" + report_clause + "。"
    plan = multi_plan(context.manager.provider, text, (
        {"clause":amend_clause, "intent":"amend", "candidates":(counter_ref,)},
        {"clause":report_clause, "intent":"report", "candidates":(memo_ref,)},
    ))

    async def planner(*_args):
        return plan

    reports = []

    async def report(source, attrs, *, publish=None):
        reports.append((source, dict(attrs)))
        await publish("Memo is complete.")
        return "canonical memo report"

    configure_professional_planner(context, planner, work_texts={text})
    context.manager.configure_work(context.host.control, context.host.executor,
        report_request=report)
    original_run = context.host.adapter.run

    async def amend_app(request, run_id, emit):
        result = await original_run(request, run_id, emit)
        root = Path(request.cwd)
        template = tmp_path / "mixed-template"
        template.mkdir(exist_ok=True)
        _bundle(template)
        manifest_path = template / "auip.manifest.json"
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        embedded = json.dumps(original, ensure_ascii=False, indent=2)
        manifest = json.loads((Path(__file__).resolve().parents[1]
            / "examples/auip-2048/auip.manifest.json").read_text(encoding="utf-8"))
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8")
        entry = template / "index.html"
        entry.write_text(entry.read_text(encoding="utf-8").replace(
            embedded, json.dumps(manifest, ensure_ascii=False, indent=2)),
            encoding="utf-8")
        for filename in ("index.html", "auip.manifest.json"):
            (root / filename).write_bytes((template / filename).read_bytes())
        finalize_staged_auip_web_bundle(root, materialized_files=tuple(
            request.metadata["auip_host_materialized_files"]))
        return result

    context.host.adapter.run = amend_app
    await context.handler.send_text(
        text, session_id=context.session_id, turn_id="amend-report-after"
    )
    await asyncio.wait_for(context.handler._stream_task, 5)
    await context.finish()
    result = context.manager.ingresses[context.session_id].receipts[
        "amend-report-after"]

    assert result["state"] == "work_auip_batch_started", result
    batch = result["work"]
    assert batch["state"] == "planned_work_batch_applied"
    assert batch["operations"][0]["work_item_id"] == counter.work_item_id
    assert batch["operations"][1]["report_work_item_id"] == memo.work_item_id
    assert reports[0][0] == report_clause and len(reports) == 1
    attrs = context.manager.auip_router.await_args.args[0]
    assert attrs["_host_work_item_id"] == counter.work_item_id
    assert len(context.host.work.list_attempts(counter.work_item_id)) == 2
    assert len(context.host.work.list_attempts(memo.work_item_id)) == 1
    await launch.on_work_updated(Method.WORK_UPDATED, {"reason":"provider.result"})
    opened = [payload for method, payload in events
        if method == Method.AUIP_LAUNCH_REQUESTED]
    assert len(opened) == 1 and opened[0]["work_item_id"] == counter.work_item_id


async def test_work_failure_cancels_exact_reservation(
    pending_host, tmp_path, monkeypatch
):
    context = pending_host
    launch, _events, _calls = await install_after_work(context, tmp_path)
    text = "创建一个应用；完成后打开它。"
    clause = "创建一个应用"

    async def planner(*_args):
        return planned(context.manager.provider, text, clause,
            "execute", one_off=True)

    configure_professional_planner(context, planner, work_texts={text})
    monkeypatch.setattr(context.manager.destination, "resolve_workspace_route",
        lambda *_args, **_kwargs:{"status":"invalid", "source":"scratch_default"})
    await context.handler.send_text(
        text, session_id=context.session_id, turn_id="work-failed"
    )
    await asyncio.wait_for(context.handler._stream_task, 5)
    result = context.manager.ingresses[context.session_id].receipts["work-failed"]

    assert result["state"] == "rejected"
    assert result["reason"] == "work_destination_unavailable"
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []
    assert not launch._deferred


@pytest.mark.parametrize("relation", [None, "subsumed", "independent"])
async def test_zero_work_plan_retains_existing_after_work_request(pending_host, tmp_path, relation):
    context = pending_host
    item, attempt, _ = _seed_app(context.host.work, context.host.project,
        tmp_path / "existing-app", title="计数器", turn_id="existing-counter",
        terminal=False)
    context.host.work.update_attempt(attempt.attempt_id, execution_status="running",
        metadata={"session_id":context.session_id})
    launch, events, source_calls = await install_after_work(context, tmp_path, relation=relation)
    text = "那个计数器做好了就打开吧。"
    plans, references = [], []

    async def planner(*_args):
        plans.append(True)
        return CompoundControlPlan(status="ok")

    async def query(messages, **_kwargs):
        if messages[-1]["content"].startswith("[Current user message]"):
            references.append(messages)
            return json.dumps({"references":["work_item:" + item.work_item_id]})
        frame = json.loads(messages[-1]["content"])
        return (json.dumps({"action":{"op":"work"}, "say":"できたら開くわ。"})
            if frame["source_kind"] == "user" else "完成を待って開くわ。")

    context.manager.query = query
    context.manager.work_planner = planner
    await context.handler.send_text(text, session_id=context.session_id, turn_id="existing-entry")
    await asyncio.wait_for(context.handler._stream_task, 5)
    receipt = context.manager.ingresses[context.session_id].receipts["existing-entry"]
    assert len(plans) == len(source_calls) == len(references) == 1
    assert receipt["state"] == "auip_after_work_deferred", receipt
    assert receipt["work_item_id"] == item.work_item_id
    assert context.manager.auip_router.call_args.args[0]["_host_work_binding"] == "active"
    pending = launch._deferred[(context.session_id, "existing-entry")]
    assert pending.work_item_id == item.work_item_id and pending.operation_id == attempt.operation_id
    assert len(context.host.work.list_work_items()) == 1
    assert len(context.host.work.list_attempts(item.work_item_id)) == 1
    assert context.host.adapter.calls == 0
    assert not any(method == Method.AUIP_LAUNCH_REQUESTED for method, _ in events)
    replay = await context.handler.send_text(text, session_id=context.session_id,
        turn_id="existing-entry")
    assert replay["status"] == "replayed"
    assert len(plans) == len(source_calls) == len(references) == 1
    assert context.manager.auip_router.await_count == 1


async def test_bound_work_uncertainty_is_not_presented_as_a_refusal(
        pending_host, tmp_path, monkeypatch):
    context = pending_host
    launch, events, _calls = await install_after_work(context, tmp_path)
    text = "做个小游戏，做好再打开。"
    clause = "做个小游戏"

    async def planner(*_args):
        return planned(context.manager.provider, text, clause, "execute", one_off=True)

    configure_professional_planner(context, planner, work_texts={text})
    query = context.manager.query
    facts = []

    async def observed_query(messages, **kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] == "host_receipt":
            facts.append(frame["current"])
        return await query(messages, **kwargs)

    context.manager.query = observed_query
    dispatch = context.host.executor.dispatch
    accepted = []
    finished = False
    context.host.adapter.release.clear()
    run = context.host.adapter.run

    async def produce_app(request, run_id, emit):
        result = await run(request, run_id, emit)
        root = Path(request.cwd)
        assets = _bundle(root)
        manifest = Path(__file__).resolve().parents[1] / "examples/auip-2048/auip.manifest.json"
        (root / "auip.manifest.json").write_bytes(manifest.read_bytes())
        finalize_staged_auip_web_bundle(root, materialized_files=tuple(assets))
        return result

    context.host.adapter.run = produce_app

    async def lose_runtime_observation(effect_id):
        original = await dispatch(effect_id)
        accepted.append(original)
        await asyncio.wait_for(context.host.adapter.started.wait(), 3)
        real_get_run = context.host.runtime.get_run
        with monkeypatch.context() as lost:
            lost.setattr(context.host.runtime, "get_run", lambda run_id:
                None if run_id == original.record.run_id else real_get_run(run_id))
            return await dispatch(effect_id)

    monkeypatch.setattr(context.host.executor, "dispatch", lose_runtime_observation)
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="unknown-work-entry")
        await asyncio.wait_for(context.handler._stream_task, 5)
        result = context.manager.ingresses[context.session_id].receipts["unknown-work-entry"]
        assert result["state"] == "unknown"
        assert facts[-1]["state"] == "unknown", facts
        assert accepted[0].record.status == "running"
        assert (context.session_id, "unknown-work-entry") in launch._deferred
        assert not any(method == Method.AUIP_LAUNCH_REQUESTED for method, _ in events)
        replay = await context.handler.send_text(text, session_id=context.session_id,
            turn_id="unknown-work-entry")
        assert replay["status"] == "replayed"
        assert len(accepted) == context.host.adapter.calls == 1
        context.host.adapter.release.set()
        await context.host.executor.finish(accepted[0])
        finished = True
        await launch.on_work_updated(Method.WORK_UPDATED, {"reason":"provider.result"})
        await launch.on_work_updated(Method.WORK_UPDATED, {"reason":"repeated observation"})
        opened = [payload for method, payload in events if method == Method.AUIP_LAUNCH_REQUESTED]
        assert len(opened) == 1 and opened[0]["work_item_id"] == result["work_item_id"]
    finally:
        context.host.adapter.release.set()
        if accepted and not finished:
            await context.host.executor.finish(accepted[0])


async def test_ambiguous_project_is_selected_before_one_reservation_and_launch(
    pending_host, tmp_path
):
    context = pending_host
    context.manager.attention = AttentionRequestCoordinator()
    launch, events, _calls = await install_after_work(context, tmp_path)
    first = add_project(context, tmp_path / "first-app", "First App Project")
    second = add_project(context, tmp_path / "second-app", "Second App Project")
    candidates = (catalog_candidate(context, "project", first.project_id),
        catalog_candidate(context, "project", second.project_id))
    text = "在那个项目创建应用；完成后打开它。"
    clause = "在那个项目创建应用"
    start = text.index(clause)
    action = {"provider":context.manager.provider, "intent":"execute",
        "task":clause, "subject":"project", "_host_workspace_access":"write",
        CONTROL_REFERENCE_CANDIDATES_ATTR:candidates}
    plan = CompoundControlPlan(status="ok",
        operations=(CompoundControlOperation(0, clause, action),),
        clauses=(SourceClause(clause, start, start + len(clause)),))

    async def planner(*_args):
        return plan

    configure_professional_planner(context, planner, work_texts={text})
    original_run = context.host.adapter.run

    async def author_app(request, run_id, emit):
        result = await original_run(request, run_id, emit)
        root = Path(request.cwd)
        assets = _bundle(root)
        manifest = Path(__file__).resolve().parents[1] / (
            "examples/auip-2048/auip.manifest.json"
        )
        (root / "auip.manifest.json").write_bytes(manifest.read_bytes())
        finalize_staged_auip_web_bundle(root, materialized_files=tuple(assets))
        return result

    context.host.adapter.run = author_app
    await context.handler.send_text(
        text, session_id=context.session_id, turn_id="ambiguous-after-work"
    )
    await asyncio.wait_for(context.handler._stream_task, 5)
    pending = context.manager.ingresses[context.session_id].receipts[
        "ambiguous-after-work"
    ]

    assert pending["state"] == "planned_work_selection_required"
    assert context.manager.auip_router.await_count == 0
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []
    request = context.manager.attention.list_pending(context.session_id)[0]
    option = next(row for row in request["options"]
        if "Second App Project" in row["label"])
    selected = await context.manager.attention.resolve(
        session_id=context.session_id, request_id=request["id"],
        option_id=option["id"]
    )
    await context.finish()

    assert selected["ok"] is True, selected
    outcome = selected["outcome"]
    assert outcome["state"] == "work_auip_batch_started"
    assert context.manager.auip_router.await_count == 1
    assert context.host.adapter.calls == 1
    item = context.host.work.get_work_item(outcome["work"]["work_item_id"])
    assert item.project_id == second.project_id
    await launch.on_work_updated(Method.WORK_UPDATED, {"reason":"provider.result"})
    opened = [payload for method, payload in events
        if method == Method.AUIP_LAUNCH_REQUESTED]
    assert len(opened) == 1 and opened[0]["work_item_id"] == item.work_item_id


@pytest.mark.parametrize("entry_action", ["launch", "engage"])
async def test_active_replacement_keeps_typed_app_identity_without_recapture(
    pending_host, tmp_path, entry_action
):
    context = pending_host
    decision = AuipControlDecision(status="ok", action=entry_action,
        timing="after_work", mode="observe", target="Current App",
        work_relation="independent", app_session_id="app-session-current")
    async def reserve(_attrs, **_kwargs):
        return {"ok":True, "deferred":True}

    _launch, _events, source_calls = await install_after_work(
        context, tmp_path, active_decision=decision, route=reserve)
    text = ("重新做一个应用，完成后替换当前这个。" if entry_action == "launch"
        else "重新做一个应用，完成后打开一起试试。")
    clause = "重新做一个应用"

    async def planner(_ingress, _turn_id, receipt, _admission):
        assert receipt["auip_context"]["app_session_id"] == "app-session-current"
        return planned(context.manager.provider, text, clause,
            "execute", one_off=True)

    configure_professional_planner(context, planner, work_texts={text})
    await context.handler.send_text(
        text, session_id=context.session_id, turn_id="active-replacement"
    )
    await asyncio.wait_for(context.handler._stream_task, 5)
    await context.finish()
    result = context.manager.ingresses[context.session_id].receipts[
        "active-replacement"
    ]
    assert context.manager.auip_router.await_args is not None, result
    attrs = context.manager.auip_router.await_args.args[0]

    assert len(source_calls) == 1
    assert attrs["_host_app_session_id"] == "app-session-current"
    assert attrs["_host_work_binding"] == "turn"
    assert context.host.adapter.calls == 1, result
