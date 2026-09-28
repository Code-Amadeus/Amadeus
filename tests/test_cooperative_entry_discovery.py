"""Missing managed-app entry uses the professional Work owner without bypasses."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server.compound_control import CompoundControlPlan
from test_cooperative_auip_entry import entry_host as entry_host
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_work import planned


async def _send(context, text: str, turn_id: str):
    accepted = await context.handler.send_text(
        text, session_id=context.session_id, turn_id=turn_id)
    assert accepted["status"] == "ok"
    await context.finish()
    return context.manager.ingresses[context.session_id].receipts[turn_id]


def _discovery_planner(context, text: str, workspace_access: str):
    calls = []

    async def planner(_ingress, turn_id, receipt, admission):
        calls.append((turn_id, dict(receipt), admission))
        assert receipt["text"] == text
        assert receipt.get("source_user_text", receipt["text"]) == text
        assert receipt["auip_context"] == {
            "action":"engage", "timing":"now", "instruction":"",
            "app_session_id":"", "target":"团队计时器", "project_ref":"",
            "reason":"entry_target_not_found"}
        return planned(context.manager.provider, text, text, "execute",
            one_off=True, _host_workspace_access=workspace_access)

    context.manager.work_planner = planner
    return calls


@pytest.mark.parametrize(("role_action", "turn_id"), [
    ({"op":"work"}, "role-work-discovers-entry"),
    ({"op":"auip"}, "role-auip-discovers-entry"),
    (None, "role-null-discovers-entry"),
])
@pytest.mark.parametrize("workspace_access", ["none", "read"])
async def test_missing_managed_entry_reaches_one_professional_new_draft(
        entry_host, role_action, turn_id, workspace_access):
    context, state, _launch, existing, *_ = entry_host
    text = "打开一个还没接入的团队计时器应用。"
    state.role_action = role_action
    state.target = "团队计时器"
    calls = _discovery_planner(context, text, workspace_access)

    receipt = await _send(context, text, turn_id)

    assert len(calls) == 1
    assert context.host.adapter.calls == 1
    request = context.host.adapter.requests[0]["request"]
    assert request.task == text
    assert request.metadata["source_user_text"] == text
    assert request.requirements.workspace_access == workspace_access
    assert receipt["state"] == "work_started"
    assert receipt["work_item_id"] != existing.work_item_id
    created = context.host.work.get_work_item(receipt["work_item_id"])
    assert created is not None
    assert Path(created.workspace_path).resolve() == Path(request.cwd).resolve()
    assert context.manager.destination.is_unkept_draft(created.workspace_path)
    assert len(context.host.work.list_work_items()) == 2
    admission = context.host.control_store.find_admission(
        "chat:" + context.session_id, turn_id)
    assert admission and admission["plan_id"]
    assert any(effect["kind"] == "work"
        for effect in json.loads(admission["plan_json"])["effects"])


async def test_valid_existing_app_launch_adds_no_discovery_work(entry_host):
    context, state, _launch, existing, *_ = entry_host
    state.role_action = {"op":"auip"}
    state.target = "2048"
    planner = AsyncMock(side_effect=AssertionError(
        "a valid launch must not become discovery Work"))
    context.manager.work_planner = planner

    receipt = await _send(context, "把刚才那个2048打开。", "valid-existing-launch")

    assert receipt["state"] == "auip_entry_pending"
    assert context.host.adapter.calls == 0
    assert [item.work_item_id for item in context.host.work.list_work_items()] == [
        existing.work_item_id]
    planner.assert_not_awaited()


async def test_invalid_discovery_plan_cannot_execute_missing_entry(entry_host):
    context, state, _launch, existing, *_ = entry_host
    text = "打开一个还没接入的团队计时器应用。"
    state.role_action = {"op":"work"}
    state.target = "团队计时器"
    planner = AsyncMock(return_value=CompoundControlPlan(
        status="invalid", reason="controlled discovery refusal"))
    context.manager.work_planner = planner

    receipt = await _send(context, text, "invalid-entry-discovery")

    assert receipt["state"] in {"rejected", "not_accepted"}
    assert context.host.adapter.calls == 0
    assert [item.work_item_id for item in context.host.work.list_work_items()] == [
        existing.work_item_id]
    planner.assert_awaited_once()
