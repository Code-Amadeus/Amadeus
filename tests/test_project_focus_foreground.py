"""The shared focus owner can return facts to the current foreground owner."""
import ast
import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

from test_cooperative_pending_turn import pending_host as pending_host


def focus_owner(announce):
    path = Path(__file__).resolve().parents[1] / "server/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    definition = next(node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_declared_focus")
    scope = {"logger": Mock(), "_observer_display_language": lambda: "japanese",
        "_schedule_focus_confirmation": announce}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(path), "exec"), scope)
    return scope["_handle_declared_focus"]


async def test_shared_focus_set_clear_uses_existing_destination_without_second_speaker(
        pending_host, monkeypatch):
    context = pending_host
    coordinator = context.host.coordinator
    monkeypatch.setattr("server.work_ledger_coordinator.get_work_ledger_coordinator",
        lambda: coordinator)
    announce = Mock(side_effect=AssertionError("foreground owns its publication"))
    owner = focus_owner(announce)
    result = await owner({"project_id": context.host.project.project_id},
        announce_result=False, session_id=context.session_id)
    assert result["ok"] is True
    assert coordinator.destination.session_project(context.session_id) == context.host.project.project_id
    result = await owner({}, announce_result=False, session_id=context.session_id)
    assert result["ok"] is True
    assert coordinator.destination.session_project(context.session_id) == ""
    assert context.host.work.list_work_items() == []
    assert context.host.adapter.calls == 0
    announce.assert_not_called()


async def test_shared_focus_refuses_changed_session_and_keeps_default_announcement(
        pending_host, monkeypatch):
    context = pending_host
    coordinator = context.host.coordinator
    monkeypatch.setattr("server.work_ledger_coordinator.get_work_ledger_coordinator",
        lambda: coordinator)
    announce = Mock()
    owner = focus_owner(announce)
    before = coordinator.destination.session_project(context.session_id)
    blocked = await owner({"project_id": context.host.project.project_id},
        announce_result=False, session_id="an-older-session")
    assert blocked["ok"] is False and blocked["authority_blocked"] is True
    assert coordinator.destination.session_project(context.session_id) == before
    announce.assert_not_called()
    result = await owner({"project_id": context.host.project.project_id},
        session_id=context.session_id)
    assert result["ok"] is True
    announce.assert_called_once()
    assert announce.call_args.kwargs["session_id"] == context.session_id


async def test_runtime_planner_focus_and_work_reach_existing_domain_owners(
        pending_host, monkeypatch):
    from server.work_planner import RuntimeWorkPlanner

    context = pending_host
    project_id = context.host.project.project_id
    source = "那就用这个项目吧，顺便做个清单页。"
    monkeypatch.setattr("llm.prompts.registered_provider_ids",
        lambda: (context.manager.provider,))
    monkeypatch.setattr("server.work_ledger_coordinator.get_work_ledger_coordinator",
        lambda: context.host.coordinator)
    audit = Mock(return_value="SET")
    monkeypatch.setattr("llm.client.remote_llm_query", audit)
    announce = Mock(side_effect=AssertionError("foreground owns its publication"))
    owner = focus_owner(announce)
    context.manager.configure_work(context.host.control, context.host.executor,
        focus_request=lambda attrs, *, session_id: owner(
            attrs, announce_result=False, session_id=session_id))
    requests = []

    async def query(messages):
        requests.append(messages)
        if "[Independent candidate verdict - FINAL]" in messages[0]["content"]:
            return json.dumps({"evidence":"contextual"
                if "project:" + project_id in messages[-1]["content"] else "none"})
        return json.dumps({"decisions": [{"proposal_index": 0,
            "source_clause": source, "provider": context.manager.provider,
            "intent": "execute", "subject": "project", "work_placement": "project",
            "session_context": "bind", "workspace_effect": "write",
            "payload_continuity": "current_turn", "reference_mode": "candidates",
            "references": ["project:" + project_id]}]}, ensure_ascii=False)

    async def role(_messages, **_kwargs):
        return '{"action":{"op":"work"},"say":"切り替えて作り始めるわ。"}'

    context.manager.query = role
    context.manager.work_planner = RuntimeWorkPlanner(
        coordinator=context.host.coordinator, query=query,
        provider=context.manager.provider)
    await context.handler.send_text(source, session_id=context.session_id,
        turn_id="focus-work-runtime")
    await asyncio.wait_for(context.handler._stream_task, 5)
    await context.finish()
    result = context.manager.ingresses[context.session_id].receipts["focus-work-runtime"]
    assert result["state"] == "work_started", result
    assert result["focus"]["state"] == "work_focus_changed"
    assert context.host.coordinator.destination.session_project(context.session_id) == project_id
    assert context.host.work.get_work_item(result["work_item_id"]).project_id == project_id
    assert context.host.adapter.requests[0]["request"].task == source
    assert len(requests) == 2  # Intent plus the existing candidate-evidence phase.
    assert context.host.adapter.calls == audit.call_count == 1
    announce.assert_not_called()
    replay = await context.handler.send_text(source, session_id=context.session_id,
        turn_id="focus-work-runtime")
    assert replay["status"] == "replayed"
    assert len(requests) == 2
    assert context.host.adapter.calls == audit.call_count == 1
