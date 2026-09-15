"""Canonical Provider choice reaches the shared Work runtime without substitution."""

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_host.provider_types import ProviderSessionHandle
from server.cooperative_provider_loop import ChildConversation, ContextBinding
from server.work_planner import RuntimeWorkPlanner
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_targets import catalog_candidate
from test_cooperative_planned_work import (
    configure_professional_planner, install_plans, planned, send,
)
from test_cooperative_planned_work_batch import multi_plan
from test_runtime_work_planner import decision
from test_work_effect_executor import _RuntimeAdapter


def second_provider(context):
    adapter = _RuntimeAdapter("generic-beta", context.host.work)
    context.host.runtime.register(adapter)
    context.manager.context_requirements[adapter.provider_id] = (
        context.manager.context_requirements[context.manager.provider])
    return adapter


async def test_planned_provider_reaches_runtime_and_replays_once(pending_host, monkeypatch):
    context = pending_host
    other = second_provider(context)
    text = "帮我做个清单页吧。"
    monkeypatch.setattr("llm.prompts.registered_provider_ids",
        lambda: (context.manager.provider, other.provider_id))
    query = AsyncMock(return_value=decision(text, other.provider_id, "execute"))
    planner = RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=query, provider=context.manager.provider)
    configure_professional_planner(context, planner, work_texts={text})
    result = await send(context, text, "second")
    await context.finish()
    assert result["state"] == "work_started"
    assert context.host.adapter.calls == 0 and other.calls == 1
    request = other.requests[0]["request"]
    assert request.provider == other.provider_id
    assert request.requirements == context.manager.context_requirements[other.provider_id]
    assert request.task == text
    assert context.host.work.get_attempt(result["attempt_id"]).provider == other.provider_id
    replay = await context.handler.send_text(text, session_id=context.session_id,
        turn_id="second")
    assert replay["status"] == "replayed" and other.calls == 1
    query.assert_awaited_once()


async def test_cross_provider_amend_preserves_work_without_foreign_native_context(pending_host):
    context = pending_host
    other = second_provider(context)
    text = "做个便签页。"
    plans = {"create": planned(context.manager.provider, text, text,
        "execute", one_off=True)}
    ingress = await install_plans(context, plans)
    first = await send(context, text, "create")
    await context.finish()
    item = context.host.work.get_work_item(first["work_item_id"])
    child = ChildConversation("old-native", "Old context", item.workspace_path,
        context.manager.provider,
        context.manager.context_requirements[context.manager.provider],
        native_session=ProviderSessionHandle(provider=context.manager.provider,
            session_id="foreign-native", scope="interaction"),
        workspace_route={"projectId": item.project_id})
    loop = ingress.loop
    loop._state.register(child, initial_binding_token=loop._binding.token)
    child.run_status = "done"
    loop._state.checkpoint(child)
    loop.children[child.child_id] = child
    loop._binding = ContextBinding(child.child_id, loop._binding.token)
    candidate = catalog_candidate(context, "work_item", item.work_item_id)
    amend = "便签加个清空按钮吧。"
    plans["amend"] = planned(other.provider_id, amend, amend, "amend", candidate)
    result = await send(context, amend, "amend")
    await context.finish()
    assert result["state"] == "work_started"
    assert result["work_item_id"] == item.work_item_id
    assert len(context.host.work.list_attempts(item.work_item_id)) == 2
    assert context.host.adapter.calls == other.calls == 1
    request = other.requests[0]["request"]
    assert Path(request.cwd).resolve() == Path(item.workspace_path).resolve()
    assert not request.metadata.get("provider_session_attach")
    assert request.session is None
    assert loop.bound_context_id == child.child_id


@pytest.mark.parametrize("unavailable", ["unconfigured", "unregistered", "read_only"])
async def test_invalid_provider_rejects_whole_plan_without_default_prefix(
        pending_host, unavailable):
    context = pending_host
    provider = "generic-beta"
    if unavailable == "read_only":
        second_provider(context)
        context.manager.context_requirements[provider] = replace(
            context.manager.context_requirements[provider], workspace_access="read")
    elif unavailable == "unregistered":
        context.manager.context_requirements[provider] = (
            context.manager.context_requirements[context.manager.provider])
    first, second = "做个便签。", "再做个计时器。"
    text = first + second
    plan = multi_plan(context.manager.provider, text, (
        {"clause": first, "intent": "execute", "extra": {"one_off": True}},
        {"clause": second, "intent": "execute",
            "extra": {"provider": provider, "one_off": True}},
    ))
    await install_plans(context, {"invalid": plan})
    result = await send(context, text, "invalid")
    await context.finish()
    assert result["state"] == "rejected"
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []


async def test_mixed_provider_batch_shares_acceptance(pending_host):
    context = pending_host
    other = second_provider(context)
    first, second = "做个便签。", "再做个计时器。"
    text = first + second
    plan = multi_plan(context.manager.provider, text, (
        {"clause": first, "intent": "execute", "extra": {"one_off": True}},
        {"clause": second, "intent": "execute",
            "extra": {"provider": other.provider_id, "one_off": True}},
    ))
    await install_plans(context, {"mixed": plan})
    result = await send(context, text, "mixed")
    await context.finish()
    assert result["state"] == "planned_work_batch_applied"
    assert [row["state"] for row in result["operations"]] == ["work_started"] * 2
    assert context.host.adapter.calls == other.calls == 1
    assert context.host.adapter.requests[0]["request"].task == first
    assert other.requests[0]["request"].task == second
    assert len(context.host.work.list_work_items()) == 2


async def test_active_amend_cannot_silently_change_selected_provider(pending_host):
    context = pending_host
    other = second_provider(context)
    input_request = AsyncMock()
    context.manager.configure_work(context.host.control, context.host.executor,
        input_request=input_request)
    text = "做个便签页。"
    plans = {"create": planned(context.manager.provider, text, text,
        "execute", one_off=True)}
    await install_plans(context, plans)
    context.host.adapter.release.clear()
    try:
        first = await send(context, text, "create")
        await asyncio.wait_for(context.host.adapter.started.wait(), 3)
        await context.host.coordinator.drain_provider_facts()
        candidate = catalog_candidate(context, "work_item", first["work_item_id"])
        amend = "换个执行端接着加清空按钮。"
        plans["amend"] = planned(other.provider_id, amend, amend, "amend", candidate)
        result = await send(context, amend, "amend")
        assert result["state"] == "rejected"
        assert result["reason"] == "planned_work_active_provider_mismatch"
        input_request.assert_not_awaited()
        assert other.calls == 0
        assert len(context.host.work.list_attempts(first["work_item_id"])) == 1
    finally:
        context.host.adapter.release.set()
        await context.finish()
