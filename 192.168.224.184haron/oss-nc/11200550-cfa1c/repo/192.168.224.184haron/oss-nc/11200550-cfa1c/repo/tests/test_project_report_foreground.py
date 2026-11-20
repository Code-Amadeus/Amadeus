"""A foreground Project report uses its caller's existing delivery owner."""
import ast
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from test_cooperative_pending_turn import pending_host as pending_host


def report_owner(speak):
    path = Path(__file__).resolve().parents[1] / "server/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    definition = next(node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_answer_report_from_ledger")
    scope = {"_observer_display_language":lambda:"japanese",
        "_speak_task_lookup_answer":speak, "logger":Mock()}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(path), "exec"), scope)
    return scope["_answer_report_from_ledger"]


@pytest.mark.parametrize("accepted", [True, False])
async def test_project_report_honors_foreground_publisher_without_second_delivery(
        pending_host, monkeypatch, accepted):
    context = pending_host
    monkeypatch.setattr("server.work_ledger_coordinator.get_work_ledger_coordinator",
        lambda:context.host.coordinator)
    snapshot = Mock(wraps=context.host.coordinator.project_status_snapshot)
    monkeypatch.setattr(context.host.coordinator, "project_status_snapshot", snapshot)
    speak = AsyncMock(side_effect=AssertionError("foreground report must not wait on another speech owner"))
    publish = AsyncMock(return_value=accepted)
    result = await report_owner(speak)("这个项目现在进展怎么样？", {
        "intent":"report", "subject":"project", "project_id":context.host.project.project_id,
        "lookup_session_id":context.session_id}, publish=publish)
    snapshot.assert_called_once_with(context.host.project.project_id)
    publish.assert_awaited_once()
    assert context.host.project.name in publish.call_args.args[0]
    speak.assert_not_awaited()
    assert result == ("[report] answered project from the ledger" if accepted
        else "[report] project answer pass unavailable")
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []


async def test_project_report_without_foreground_callback_keeps_existing_delivery(
        pending_host, monkeypatch):
    context = pending_host
    monkeypatch.setattr("server.work_ledger_coordinator.get_work_ledger_coordinator",
        lambda:context.host.coordinator)
    speak = AsyncMock(return_value=True)
    result = await report_owner(speak)("这个项目进展怎么样？", {
        "intent":"report", "subject":"project", "project_id":context.host.project.project_id})
    speak.assert_awaited_once()
    assert speak.call_args.kwargs["history_marker"] == "PROJECT_STATUS"
    assert result == "[report] answered project from the ledger"


async def test_runtime_planner_project_report_reaches_existing_ledger_owner(
        pending_host, monkeypatch):
    from server.work_planner import RuntimeWorkPlanner

    context = pending_host
    monkeypatch.setattr("server.work_ledger_coordinator.get_work_ledger_coordinator",
        lambda:context.host.coordinator)
    monkeypatch.setattr("llm.prompts.registered_provider_ids",
        lambda:(context.manager.provider,))
    source = "这个项目现在进展怎么样？"
    snapshot = Mock(wraps=context.host.coordinator.project_status_snapshot)
    monkeypatch.setattr(context.host.coordinator, "project_status_snapshot", snapshot)
    speak = AsyncMock(side_effect=AssertionError("must retain foreground delivery"))
    owner = report_owner(speak)
    planning_queries = []

    async def planning_query(messages):
        planning_queries.append(messages)
        if "[Independent candidate verdict - FINAL]" in messages[0]["content"]:
            return json.dumps({"evidence":"contextual"
                if "project:" + context.host.project.project_id in messages[-1]["content"] else "none"})
        return json.dumps({"decisions":[{"proposal_index":0, "source_clause":source,
            "provider":context.manager.provider, "intent":"report", "subject":"project",
            "work_placement":"not_applicable", "session_context":"unchanged",
            "workspace_effect":"none", "payload_continuity":"current_turn",
            "reference_mode":"candidates",
            "references":["project:" + context.host.project.project_id]}]}, ensure_ascii=False)

    async def role_query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        assert frame["source_kind"] == "user", "report must use its existing fact owner"
        return '{"action":{"op":"work"},"say":"プロジェクトの状況を確認するわ。"}'

    context.manager.query = role_query
    context.manager.work_planner = RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=planning_query, provider=context.manager.provider)
    context.manager.configure_work(context.host.control, context.host.executor,
        report_request=owner)
    ingress = await context.manager._ingress_for(context.session_id)
    binding = (ingress.loop.bound_context_id, ingress.loop._binding.token)
    await context.handler.send_text(source, session_id=context.session_id,
        turn_id="project-report-runtime")
    await asyncio.wait_for(context.handler._stream_task, 5)
    result = ingress.receipts["project-report-runtime"]
    assert result["state"] == "work_reported", result
    assert result["report_project_id"] == context.host.project.project_id
    assert result["report_result"] == "[report] answered project from the ledger"
    assert len(planning_queries) == 2
    snapshot.assert_called_once_with(context.host.project.project_id)
    speak.assert_not_awaited()
    assert context.host.adapter.calls == 0 and context.host.work.list_work_items() == []
    assert (ingress.loop.bound_context_id, ingress.loop._binding.token) == binding
    assert len(context.spoken) == 2
    assert context.host.project.name in context.spoken[-1]["display_text"]
    replay = await context.handler.send_text(source, session_id=context.session_id,
        turn_id="project-report-runtime")
    assert replay["status"] == "replayed"
    assert len(planning_queries) == 2 and snapshot.call_count == 1 and len(context.spoken) == 2
