"""Professional cooperative Work can use configured workspace-less OpenClaw."""

import asyncio
from dataclasses import replace
import json
from unittest.mock import AsyncMock

import pytest

from agent_host.provider_catalog import OPENCLAW_MANIFEST
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_types import (
    ProviderRunIntakeAuthority,
    ProviderRunResult,
)
from agent_host.work_ledger_store import WorkLedgerConflict
from server.turn_admission import capture_turn_admission
from server.work_control import CurrentTurnSourceSpanV1, WorkEffectPayloadV3
from server.work_planner import RuntimeWorkPlanner
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_work import configure_professional_planner, send


class _FakeOpenClawAdapter:
    provider_id = "openclaw"
    manifest = OPENCLAW_MANIFEST

    def __init__(self):
        self.calls = 0
        self.requests = []
        self.started = asyncio.Event()

    async def run(self, request, run_id, _emit):
        self.calls += 1
        self.requests.append((request, run_id))
        self.started.set()
        return ProviderRunResult(status="done", result="lookup accepted")

    async def cancel(self, _run_id):
        return {"confirmed":True, "cancelled":True}


def _openclaw_requirements():
    capabilities = OPENCLAW_MANIFEST.capabilities
    return ProviderRequirements(task_kind="research",
        workspace_access=capabilities.workspace_access,
        workspace_ownership=capabilities.workspace_ownership,
        durability=capabilities.durability, steering=capabilities.steering,
        resume=capabilities.resume, interaction=capabilities.interaction,
        ownership="managed")


def _decision(source, workspace_effect):
    return json.dumps({"decisions":[{
        "proposal_index":0,
        "source_clause":source,
        "provider":"openclaw",
        "intent":"execute",
        "work_placement":"draft",
        "session_context":"unchanged",
        "workspace_effect":workspace_effect,
        "reference_mode":"none",
        "references":None,
    }]}, ensure_ascii=False)


def _install_openclaw(context, *, source, workspace_effect):
    adapter = _FakeOpenClawAdapter()
    context.host.runtime.register(adapter)
    context.manager.context_requirements[adapter.provider_id] = (
        _openclaw_requirements())
    query = AsyncMock(return_value=_decision(source, workspace_effect))
    planner = RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=query, provider=context.manager.provider)
    configure_professional_planner(context, planner, work_texts={source})
    return adapter, query


async def test_addressless_site_lookup_creates_one_openclaw_draft(pending_host):
    context = pending_host
    source = "帮我找到并打开关于牧瀬紅莉栖的维基百科页面。"
    adapter, query = _install_openclaw(context, source=source,
        workspace_effect="none")

    result = await send(context, source, "openclaw-site-lookup")
    assert result["state"] == "work_started"
    await asyncio.wait_for(adapter.started.wait(), 2)
    await context.finish()

    assert context.host.adapter.calls == 0
    assert adapter.calls == 1 and len(adapter.requests) == 1
    request, _run_id = adapter.requests[0]
    assert request.provider == adapter.provider_id
    assert request.task == source
    assert request.cwd is None
    assert request.requirements == _openclaw_requirements()
    assert request.metadata["source_user_text"] == source
    assert request.metadata["work"]["work_item_id"] == result["work_item_id"]
    item = context.host.work.get_work_item(result["work_item_id"])
    project = context.host.work.get_project(item.project_id)
    attempts = context.host.work.list_attempts(item.work_item_id)
    assert project.metadata.get("scratch") is True
    assert request.metadata["work"]["project_id"] == item.project_id
    assert item.workspace_mode == "none"
    assert item.workspace_path == ""
    assert len(attempts) == 1
    assert attempts[0].provider == "openclaw"
    assert attempts[0].task == source
    assert len(context.host.work.list_work_items()) == 1
    query.assert_awaited_once()

    replay = await context.handler.send_text(source, session_id=context.session_id,
        turn_id="openclaw-site-lookup")
    assert replay["status"] == "replayed"
    assert adapter.calls == 1
    assert len(context.host.work.list_attempts(item.work_item_id)) == 1
    query.assert_awaited_once()


@pytest.mark.parametrize("workspace_effect", ["read", "write"])
async def test_openclaw_none_workspace_cannot_be_upgraded_by_plan(
        pending_host, workspace_effect):
    context = pending_host
    source = "查找一个外部网页目标。"
    adapter, query = _install_openclaw(context, source=source,
        workspace_effect=workspace_effect)

    result = await send(context, source,
        "openclaw-workspace-" + workspace_effect)
    await context.finish()

    assert result["state"] == "rejected"
    assert result["reason"] == "work_provider_not_writable"
    assert context.host.adapter.calls == adapter.calls == 0
    assert adapter.requests == []
    assert context.host.work.list_work_items() == []
    query.assert_awaited_once()


def _sealed_none_workspace_request(context, *, source, suffix):
    admission = capture_turn_admission(utterance_id="openclaw-" + suffix,
        turn_id="openclaw-" + suffix, session_id="openclaw-authority",
        transcript=source, input_source="typed", chat_epoch=1,
        pending=False, authority_mode="turn_decision")
    assert admission is not None
    context.host.control.admit(admission,
        fence_scope="openclaw-authority:" + suffix)
    payload = WorkEffectPayloadV3(provider="openclaw", task=source, title=source,
        project_id=context.host.project.project_id,
        session_id=admission.session_id, utterance_id=admission.utterance_id,
        turn_id=admission.turn_id, source_user_text=source,
        source_user_context="", source_context_scope="chat:" + admission.session_id,
        source_proof=CurrentTurnSourceSpanV1.capture(admission, source,
            start=0, end=len(source)), requirements=_openclaw_requirements())
    effect = context.host.control.seal(admission, payload)["effect_id"]
    return effect, context.host.control.provider_request(effect)


@pytest.mark.parametrize("tamper", ["cwd", "project", "task"])
async def test_none_workspace_authority_rejects_dispatch_tampering(
        pending_host, tmp_path, tamper):
    context = pending_host
    source = "查找经过确认的外部网页。"
    adapter, _query = _install_openclaw(context, source=source,
        workspace_effect="none")
    effect, request = _sealed_none_workspace_request(context,
        source=source, suffix=tamper)
    if tamper == "cwd":
        injected = tmp_path / "injected"
        injected.mkdir()
        request = replace(request, cwd=str(injected))
    elif tamper == "project":
        request = replace(request, metadata={**request.metadata,
            "work":{**request.metadata["work"],
                "project_id":"project-tampered"}})
    else:
        request = replace(request, task=source + " altered")

    with pytest.raises(WorkLedgerConflict):
        await context.host.runtime.start_accepted(
            request, ProviderRunIntakeAuthority(effect))

    assert adapter.calls == 0 and adapter.requests == []
    assert context.host.work.list_work_items() == []
