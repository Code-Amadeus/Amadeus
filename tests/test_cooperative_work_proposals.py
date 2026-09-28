"""The coarse role proposes Work without choosing its final operation or target."""
import json
from types import SimpleNamespace

import pytest

from agent_host.provider_contract import ProviderRequirements
from server.cooperative_provider_loop import (
    CooperativeProviderLoop,
    LoopConflict,
    _role_coordination_contract,
)


@pytest.mark.parametrize("action", [
    {"op":"work"},
    {"op":"work", "intent":"amend", "target":"wrong Work", "source":"invented"},
    {"op":"report", "target":"wrong Work"},
])
@pytest.mark.parametrize("with_app", [False, True])
async def test_role_work_details_cannot_bypass_independent_planning(action, with_app):
    source = "这步你来，帮我做个清单吧。"
    requests = []

    async def query(messages):
        requests.append(messages)
        return json.dumps({"action":action, "say":"確認して進めるわ。"})

    def allocate(*_args):
        pytest.fail("a coarse proposal cannot allocate an execution workspace")

    loop = CooperativeProviderLoop(SimpleNamespace(get_manifest=lambda _provider:True),
        query, allocate, provider="writer", owns_runtime=False, work_proposals_only=True,
        context_requirements={"writer":ProviderRequirements(workspace_access="write")})
    try:
        result = await loop.submit(source, input_id="coarse-work",
            auip_context={"action":"step", "instruction":"这步你来"} if with_app else None)
        assert result["state"] == "work_plan_required"
        assert result["text"] == source
        if with_app:
            assert result["auip_context"] == {"action":"step", "instruction":"这步你来"}
        assert "target" not in result and "intent" not in result and "source_start" not in result
        assert len(requests) == 1 and loop.children == {}
        assert json.loads(requests[0][-1]["content"])["current"]["text"] == source
    finally:
        await loop.close()


async def test_exact_role_auip_acknowledgement_uses_focused_owner_as_no_action():
    async def query(_messages):
        return json.dumps({"action":{"op":"auip"}, "say":"この操作は引き受けるわ。"})

    loop = CooperativeProviderLoop(SimpleNamespace(get_manifest=lambda _provider:True),
        query, lambda *_args:None, provider="writer", owns_runtime=False,
        work_proposals_only=True,
        context_requirements={"writer":ProviderRequirements(workspace_access="write")})
    try:
        result = await loop.submit("这步你来。", input_id="focused-auip",
            auip_context={"action":"step", "timing":"now",
                "app_session_id":"app-current", "instruction":"这步你来。"})
        assert result["state"] == "no_action"
        assert result["coordination_say"] == "この操作は引き受けるわ。"
        assert loop.children == {}
    finally:
        await loop.close()


@pytest.mark.parametrize(("action", "auip_context"), [
    ({"op":"auip"}, None),
    ({"op":"auip"}, {"action":"step", "timing":"now", "app_session_id":""}),
    ({"op":"auip"}, {"action":"engage", "timing":"after_work",
        "app_session_id":"app-current"}),
    ({"op":"auip", "instruction":"model-owned"},
        {"action":"step", "timing":"now", "app_session_id":"app-current"}),
])
async def test_role_auip_acknowledgement_without_exact_focused_owner_is_rejected(
        action, auip_context):
    async def query(_messages):
        return json.dumps({"action":action, "say":"引き受けるわ。"})

    loop = CooperativeProviderLoop(SimpleNamespace(get_manifest=lambda _provider:True),
        query, lambda *_args:None, provider="writer", owns_runtime=False,
        work_proposals_only=True,
        context_requirements={"writer":ProviderRequirements(workspace_access="write")})
    try:
        with pytest.raises(LoopConflict, match="source-local owner"):
            await loop.submit("操作して。", input_id="unowned-auip",
                auip_context=auip_context)
        assert loop.children == {}
    finally:
        await loop.close()


def test_coarse_contract_removes_detailed_work_schema_and_keeps_native_routing():
    contract = _role_coordination_contract(True)
    assert '[DELEGATE op=work]' in contract
    assert 'JSONの外枠やsayキーを出しません' in contract
    assert "action.intent=amend" not in contract
    assert "action.op=report" not in contract
    assert "source_clause" not in contract
    assert "action.op=send_to" in contract and "action.op=scope_change" in contract
