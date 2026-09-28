"""Active amendments retain Work semantics; questions remain ordinary inputs."""
import asyncio
from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from agent_host.provider_types import ProviderInputDelivery
from server.handlers.work_ledger_handler import WorkLedgerHandler
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_targets import catalog_candidate
from test_cooperative_planned_work import configure_professional_planner, planned, send


@pytest.mark.parametrize("amendment", [False, True])
@pytest.mark.parametrize("delivery", ["delivered", "rejected", "unknown", "late", "late_rejected", "late_user_accepted"])
async def test_same_run_requirement_and_late_delivery_are_visible(pending_host, monkeypatch, amendment, delivery):
    context = pending_host
    adapter = context.host.adapter
    adapter.manifest = replace(adapter.manifest,
        capabilities=replace(adapter.manifest.capabilities, append_input=True))
    release, entered = asyncio.Event(), asyncio.Event()
    late = delivery.startswith("late")
    if not late:
        release.set()
    calls = []

    async def append_input(run_id, text):
        calls.append((run_id, text))
        entered.set()
        await release.wait()
        return ProviderInputDelivery("rejected" if delivery == "late_rejected"
            else "delivered" if late else delivery)

    adapter.append_input = append_input
    context.host.runtime.register(adapter)
    owner = WorkLedgerHandler(context.host.coordinator, provider_input=context.host.runtime.append_input)
    context.manager.work_input = owner.submit_input
    create = "帮我做个菜谱页。"
    followup = "再加个人数选项，份量跟着人数变。" if amendment else "现在还差哪些？"
    plans = {"create":planned(context.manager.provider, create, create, "execute", one_off=True)}
    planner_turns = []
    def planner(_ingress, turn, *_args):
        planner_turns.append(turn)
        return plans[turn]
    configure_professional_planner(context, planner, work_texts={create, followup})
    original_query = context.manager.query

    async def query(messages, **kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame.get("source_kind") == "user" and frame["current"]["text"] == followup:
            return '{"action":{"op":"send"},"say":"実行側に伝えるわ。"}'
        return await original_query(messages, **kwargs)

    context.manager.query = query
    adapter.release.clear()
    try:
        first = await send(context, create, "create")
        await asyncio.wait_for(adapter.started.wait(), 3)
        await context.host.coordinator.drain_provider_facts()
        work_id = first["work_item_id"]
        original_operation = context.host.work.get_attempt(first["attempt_id"]).operation_id
        target = catalog_candidate(context, "work_item", work_id)
        plans["followup"] = (planned(context.manager.provider, followup, followup,
            "amend", target) if amendment else
            planned(context.manager.provider, followup, followup,
                "message", _host_workspace_access="none"))
        result = await send(context, followup, "followup")
        assert result["state"] == "work_input_accepted"
        await asyncio.wait_for(entered.wait(), 3)
        if not late:
            await owner.drain_inputs()
        detail = context.host.coordinator.detail(work_id)
        assert detail["operationCount"] == 1 + int(amendment)
        assert detail["operationId"] == original_operation and detail["attemptId"] == first["attempt_id"]
        assert len(detail["inputRequirements"]) == int(amendment)
        if amendment:
            operation = context.host.work.list_operations(work_id)[-1]
            assert operation.intent == "amend" and operation.instruction == followup
            assert operation.metadata["work_input_id"] == "followup"
            assert operation.metadata["attempt_id"] == first["attempt_id"]
            assert detail["inputRequirements"][0]["text"] == followup
        adapter.release.set()
        await context.finish()
        completion = context.host.work.latest_completion(work_id)
        unknown = amendment and (late or delivery == "unknown")
        assert completion.evidence["pending_inputs"] == int(unknown)
        assert completion.attention == ("input" if unknown else "review")
        assert len(completion.evidence["input_requirements"]) == int(amendment)
        if amendment and delivery == "rejected":
            assert followup in "\n".join(completion.evidence["missing_requirements"])
        if unknown:
            assert "delivery" in completion.rationale and "user input" not in completion.rationale
        export_policy = Mock(return_value=True)
        monkeypatch.setattr(context.host.coordinator.permission_service, "auto_accept_approved_export", export_policy)
        allowed = context.host.coordinator._auto_accept_approved_export(request=None, resolved=None,
            attempt=context.host.work.get_attempt(first["attempt_id"]), exported_paths=[])
        assert allowed == (not amendment or delivery == "delivered")
        assert export_policy.called == allowed
        if delivery == "late_user_accepted":
            await context.host.coordinator.dispose_work_item(work_id, action="accept", rationale="我看过了，就这样。")
            user_assessment = context.host.work.latest_completion(work_id).assessment_id
            release.set()
            await owner.drain_inputs()
            assert context.host.work.latest_completion(work_id).assessment_id == user_assessment
            assert context.host.work.get_work_item(work_id).state == "accepted"
        elif late:
            release.set()
            await owner.drain_inputs()
            completion = context.host.work.latest_completion(work_id)
            assert completion.evidence["pending_inputs"] == 0
            assert completion.attention == "review" and completion.completeness == "partial"
            assert context.host.work.get_work_item(work_id).state == (
                "open" if amendment and delivery == "late_rejected" else "review_ready")
            if amendment and delivery == "late_rejected":
                assert followup in "\n".join(completion.evidence["missing_requirements"])
        assert calls == [(first["run_id"], followup)]
        assert len(context.host.work.list_attempts(work_id)) == 1
        assert adapter.calls == 1
        assert planner_turns == ["create", "followup"]
        assert context.host.work.get_project_by_path(context.host.work.get_work_item(work_id).workspace_path) is None
    finally:
        release.set()
        adapter.release.set()
        await context.finish()
        await owner.drain_inputs()
