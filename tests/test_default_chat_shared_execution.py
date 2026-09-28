"""Default Chat controls reach the same real Work intake and Provider Runtime."""

import asyncio
import json

import pytest

from core.chat_runtime import ChatRuntime, _TurnState
from server import host_action_dispatcher
from server.app import _handle_delegate
from server.control_adjudication import RuntimeControlDecisionResolver
from server.work_planner import RuntimeWorkPlanner
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_work import configure_professional_planner, send
from test_work_effect_executor import _RuntimeAdapter


@pytest.mark.parametrize("strategy", ["default", "professional"])
async def test_chat_strategies_reach_shared_real_work_and_provider_runtime(
        pending_host, monkeypatch, strategy):
    context = pending_host
    adapter = _RuntimeAdapter("codex", context.host.work)
    context.host.runtime.register(adapter)
    monkeypatch.setattr("agent_host.provider_runtime.runtime", context.host.runtime)
    monkeypatch.setattr("server.work_ledger_coordinator.get_work_ledger_coordinator",
        lambda: context.host.coordinator)
    monkeypatch.setattr("server.interaction_branch.get_interaction_branch_coordinator",
        lambda: None)
    monkeypatch.setattr(host_action_dispatcher, "_delegate_handler", _handle_delegate)
    context.manager.context_requirements[adapter.provider_id] = (
        context.manager.context_requirements[context.manager.provider])
    source = "帮我做个清单页吧。"
    queries = []
    evidence = []

    async def query(messages):
        queries.append(messages)
        if strategy == "default" and len(queries) == 1:
            return json.dumps({"clauses": [source]}, ensure_ascii=False)
        return json.dumps({"decisions": [{"proposal_index": 0,
            "provider": adapter.provider_id, "intent": "execute",
            "work_placement": "draft", "session_context": "unchanged",
            "workspace_effect": "write", "payload_continuity": "current_turn",
            "reference_mode": "none",
            **({"source_clause": source, "references": None}
                if strategy == "professional" else {})}]})

    if strategy == "default":
        runtime = ChatRuntime()
        resolver = RuntimeControlDecisionResolver(coordinator=context.host.coordinator,
            query=query, compound_enabled=True, compound_sink=evidence.append)
        runtime.configure(control_proposal_observer=resolver,
            control_proposal_authority=True, compound_control_authority=True)
        state = _TurnState(gui_callback=None, turn_id="default-create",
            question=source, session_id=context.session_id,
            interaction_branch_routing_lease={})
        runtime._consume_stream_chunk(state,
            'いいわ、作るわね。[DELEGATE provider="codex" intent="execute" '
            'task="帮我做个清单页吧。"]')
        await runtime._wait_for_control_authority(state)
        assert state.control_effective_actions, [row.reason for row in evidence]
    else:
        planner = RuntimeWorkPlanner(coordinator=context.host.coordinator,
            query=query, provider=context.manager.provider)
        configure_professional_planner(context, planner, work_texts={source})
        result = await send(context, source, "professional-create")
        assert result["state"] == "work_started", result
    await asyncio.gather(*tuple(host_action_dispatcher.dispatch_tasks))
    await context.finish()
    for record in context.host.runtime._runs.values():
        if record.task_handle is not None:
            await record.task_handle
    await context.host.coordinator.drain_provider_facts()
    assert adapter.calls == 1, context.host.runtime.list_runs()
    assert context.host.adapter.calls == 0
    assert len(context.host.work.list_work_items()) == 1
    item = context.host.work.list_work_items()[0]
    attempts = context.host.work.list_attempts(item.work_item_id)
    assert len(attempts) == 1
    request = adapter.requests[0]["request"]
    assert request.metadata["work"]["work_item_id"] == item.work_item_id
    assert request.metadata["work"]["attempt_id"] == attempts[0].attempt_id
    assert request.task == source
    assert len(queries) == (2 if strategy == "default" else 1)
