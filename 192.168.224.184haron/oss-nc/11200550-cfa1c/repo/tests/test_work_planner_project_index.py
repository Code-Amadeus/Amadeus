"""An existing Project is a lookup scope, never a replacement Work identity."""
import asyncio
import json
from unittest.mock import Mock

from test_cooperative_pending_turn import pending_host as pending_host
from test_auip_launch import _seed_app
from server.work_planner import RuntimeWorkPlanner


async def test_old_project_work_is_indexed_and_amended_without_a_new_work(pending_host, tmp_path, monkeypatch):
    context = pending_host
    work, attempt, _ = _seed_app(context.host.work, context.host.project,
        tmp_path / "old-game", title="Earlier Gomoku", turn_id="old-game", goal="Build Gomoku")
    context.host.work.update_attempt(attempt.attempt_id, metadata={"session_id":"earlier-session"})
    source = "给之前那个五子棋加一个悔棋功能。"
    indexed = Mock(wraps=context.host.coordinator.project_work_items_for_resolution)
    monkeypatch.setattr(context.host.coordinator, "project_work_items_for_resolution", indexed)
    planning = []

    async def query(messages):
        if "[Independent candidate verdict - FINAL]" in messages[0]["content"]:
            return json.dumps({"evidence":"exact" if "work_item:"+work.work_item_id
                in messages[-1]["content"] else "none"})
        planning.append(messages)
        content = "\n".join(row["content"] for row in messages)
        token = ("work_item:"+work.work_item_id if "work_item:"+work.work_item_id in content
            else "project:"+context.host.project.project_id)
        return json.dumps({"decisions":[{"proposal_index":0,
            "source_clause":source, "provider":context.manager.provider,
            "intent":"amend", "subject":"work_item", "work_placement":"not_applicable",
            "session_context":"unchanged", "workspace_effect":"write",
            "payload_continuity":"current_turn", "reference_mode":"candidates", "references":[token]}]})

    context.manager.work_planner = RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=query, provider=context.manager.provider)
    async def role(messages, **_kwargs):
        return '{"action":{"op":"work"},"say":"直すわ。"}' if json.loads(
            messages[-1]["content"])["source_kind"] == "user" else "確認したわ。"
    context.manager.query = role
    await context.handler.send_text(source, session_id=context.session_id, turn_id="old-work-amend")
    await asyncio.wait_for(context.handler._stream_task, 4)
    await context.finish()
    result = context.manager.ingresses[context.session_id].receipts["old-work-amend"]
    assert result["state"] == "work_started" and result["work_item_id"] == work.work_item_id
    assert len(context.host.work.list_work_items()) == 1
    assert len(context.host.work.list_attempts(work.work_item_id)) == 2
    assert len(planning) == 2
    assert "work_item:"+work.work_item_id not in "\n".join(row["content"] for row in planning[0])
    assert "work_item:"+work.work_item_id in "\n".join(row["content"] for row in planning[1])
    assert indexed.called
