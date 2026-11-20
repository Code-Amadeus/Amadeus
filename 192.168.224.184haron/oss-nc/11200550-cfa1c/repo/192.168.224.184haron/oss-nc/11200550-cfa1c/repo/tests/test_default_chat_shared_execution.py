"""Both Cooperative strategies use the same real Work intake and Provider Runtime."""
import json

import pytest

from server.work_planner import RuntimeWorkPlanner
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_work import configure_professional_planner, send
from test_work_effect_executor import _RuntimeAdapter


@pytest.mark.parametrize("strategy", ["basic", "professional"])
async def test_chat_strategies_reach_shared_real_work_and_provider_runtime(
        pending_host, monkeypatch, strategy):
    context = pending_host
    adapter = _RuntimeAdapter("codex", context.host.work)
    context.host.runtime.register(adapter)
    monkeypatch.setattr("agent_host.provider_runtime.runtime", context.host.runtime)
    context.manager.context_requirements[adapter.provider_id] = (
        context.manager.context_requirements[context.manager.provider])
    context.manager.provider = adapter.provider_id
    source = "帮我做个清单页吧。"
    queries = []

    async def query(messages):
        queries.append(messages)
        return json.dumps({"decisions": [{"proposal_index": 0,
            "provider": adapter.provider_id, "intent": "execute",
            "work_placement": "draft", "session_context": "unchanged",
            "workspace_effect": "write", "payload_continuity": "current_turn",
            "reference_mode": "none", "source_clause": source,
            "references": None}]})

    if strategy == "professional":
        planner = RuntimeWorkPlanner(coordinator=context.host.coordinator,
            query=query, provider=context.manager.provider)
        configure_professional_planner(context, planner, work_texts={source})
    result = await send(context, source, strategy + "-create")
    assert result["state"] == "work_started", result
    await context.finish()
    for record in context.host.runtime._runs.values():
        if record.task_handle is not None:
            await record.task_handle
    await context.host.coordinator.drain_provider_facts()
    assert adapter.calls == 1, context.host.runtime.list_runs()
    assert context.host.adapter.calls == 0
    items = context.host.work.list_work_items()
    assert len(items) == 1
    item = items[0]
    attempts = context.host.work.list_attempts(item.work_item_id)
    assert len(attempts) == 1
    request = adapter.requests[0]["request"]
    assert request.metadata["work"]["work_item_id"] == item.work_item_id
    assert request.metadata["work"]["attempt_id"] == attempts[0].attempt_id
    assert request.task == source
    assert len(queries) == (1 if strategy == "professional" else 0)
