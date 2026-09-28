"""Provider and lifecycle facts describe Work without becoming its identity."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

from agent_host.provider_types import ProviderInputDelivery
from agent_host.provider_runtime import ProviderRuntime
from server.cooperative_chat_ingress import CooperativeChatManager
from server.cooperative_provider_loop import CooperativeProviderLoop
from server.handlers.work_ledger_handler import WorkLedgerHandler
from server.reference_catalog import TypedReferenceCandidate
from server.work_completion import CompletionEvidence, assess_completion
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_targets import catalog_candidate
from test_cooperative_planned_work import configure_professional_planner, planned, send
from test_work_effect_executor import _admission, _host, _payload


def _manager(host) -> CooperativeChatManager:
    manager = object.__new__(CooperativeChatManager)
    manager.ledger = host.control_store
    manager.work_control = host.control
    manager.work_executor = host.executor
    manager.runtime = host.runtime
    manager.ingresses = {}
    return manager


async def _role_frame(manager: CooperativeChatManager, tmp_path) -> dict:
    frames: list[dict] = []

    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        frames.append(frame)
        return json.dumps({"action": None, "say": "確認したわ。"}, ensure_ascii=False)

    loop = CooperativeProviderLoop(
        ProviderRuntime(),
        query,
        lambda _label, _child_id: tmp_path,
        provider="configured-role-provider",
        context_requirements={},
        owns_runtime=False,
        recipient_work=lambda context_id: manager.work_for_recipient(
            "session-c2", context_id
        ),
    )
    ingress = SimpleNamespace(session_id="session-c2", loop=loop)
    manager.ingresses = {ingress.session_id: ingress}
    loop.task_contexts = lambda: manager.task_stop_candidates(
        ingress,
        include_provider=False,
    )
    try:
        receipt = await loop.submit("今の作業状況を教えて。", turn_id="provider-facts")
        assert receipt["state"] == "no_action"
        return frames[-1]
    finally:
        await loop.close()
        manager.ingresses = {}


async def test_role_projects_actual_provider_and_work_state_for_distinct_same_provider_work(
    tmp_path,
) -> None:
    async with _host(tmp_path, provider="generic-alpha") as host:
        first = await host.executor.execute(host.effect_id)
        second_admission = _admission(
            suffix="second-provider-fact",
            epoch=2,
            text="Build the second accepted artifact",
        )
        host.control.admit(second_admission, fence_scope="foreground-chat")
        second_effect = host.control.seal(
            second_admission,
            _payload(
                host.project.project_id,
                host.adapter.provider_id,
                suffix="second-provider-fact",
                source="Build the second accepted artifact",
                task="Build the second accepted artifact",
            ),
        )
        second = await host.executor.execute(second_effect["effect_id"])

        first_id = first["binding"]["work_item_id"]
        second_id = second["binding"]["work_item_id"]
        assert first_id != second_id
        manager = _manager(host)
        frame = await _role_frame(manager, tmp_path)

        current = frame["context"]["current_work"]
        latest_attempt = host.work.list_attempts(second_id)[-1]
        latest_item = host.work.get_work_item(second_id)
        assert current["work_item_id"] == second_id
        assert current["provider"] == latest_attempt.provider == "generic-alpha"
        assert current["execution_status"] == latest_attempt.execution_status
        assert current["work_state"] == latest_item.state

        tasks = {row["token"]: row for row in frame["work_tasks"]}
        expected_tokens = {
            "work_item:" + first_id,
            "work_item:" + second_id,
        }
        assert set(tasks) == expected_tokens
        assert len(expected_tokens) == 2
        for work_item_id in (first_id, second_id):
            row = tasks["work_item:" + work_item_id]
            attempt = host.work.list_attempts(work_item_id)[-1]
            item = host.work.get_work_item(work_item_id)
            assert row["provider"] == attempt.provider == "generic-alpha"
            assert row["execution_status"] == attempt.execution_status
            assert row["work_state"] == item.state


async def test_absent_work_and_attempt_do_not_invent_provider_or_state(
    tmp_path,
    monkeypatch,
) -> None:
    async with _host(tmp_path, provider="generic-alpha") as host:
        manager = _manager(host)
        empty = await _role_frame(manager, tmp_path)
        assert "current_work" not in empty["context"]
        assert "work_tasks" not in empty

        no_attempt = host.work.create_work_item(
            host.project.project_id,
            title="Accepted identity without execution",
            goal="Remain distinct from any Provider until an Attempt exists.",
        )
        candidate = TypedReferenceCandidate(
            kind="work_item",
            entity_id=no_attempt.work_item_id,
            label=no_attempt.title,
            scope="project",
            parent_project_id=host.project.project_id,
            state=no_attempt.state,
        )
        monkeypatch.setattr(
            "server.cooperative_chat_ingress.candidate_catalog_from_coordinator",
            lambda *_args, **_kwargs: ((candidate,), True, ""),
        )

        frame = await _role_frame(manager, tmp_path)
        assert "current_work" not in frame["context"]
        assert frame["work_tasks"] == [
            {
                "token": candidate.token,
                "goal": no_attempt.title,
                "execution_status": "",
                "work_state": no_attempt.state,
                "provider": "",
            }
        ]


async def test_recipient_work_identity_uses_latest_attempt_provider_and_completion(
    tmp_path,
) -> None:
    async with _host(tmp_path, provider="generic-alpha") as host:
        first = await host.executor.execute(host.effect_id)
        binding = first["binding"]
        work_item_id = binding["work_item_id"]
        original = host.work.get_attempt(binding["attempt_id"])
        assert original is not None and original.execution_status == "succeeded"

        latest = host.work.create_attempt(
            work_item_id,
            operation_id=original.operation_id,
            provider="host-retry-provider",
            task="Retry the same accepted operation without native execution",
            provider_run_id="run-host-store-retry",
            metadata={"session_id":"session-c2", "turn_id":"turn-host-store-retry"},
        )
        latest = host.work.update_attempt(
            latest.attempt_id,
            execution_status="failed",
            error="deterministic Host/store retry evidence",
        )
        completion = host.work.record_completion(
            work_item_id,
            assess_completion(CompletionEvidence(
                execution_status="failed",
                current_state=host.work.get_work_item(work_item_id).state,
            )),
            attempt_id=latest.attempt_id,
            source="host",
        )

        current = _manager(host).work_for_recipient("session-c2", "")
        assert current is not None
        assert current["work_item_id"] == binding["work_item_id"]
        assert latest.operation_id == original.operation_id
        assert latest.attempt_id != binding["attempt_id"]
        assert current["provider"] == latest.provider == "host-retry-provider"
        assert current["execution_status"] == latest.execution_status == "failed"
        assert current["work_state"] == completion.work_item_state == "open"
        assert current["completeness"] == completion.completeness == "incomplete"
        assert current["attention"] == completion.attention == "error"
        assert "input_requirements" not in current


async def test_recipient_work_projects_real_accepted_input_requirements(
    pending_host,
) -> None:
    context = pending_host
    adapter = context.host.adapter
    adapter.manifest = replace(
        adapter.manifest,
        capabilities=replace(adapter.manifest.capabilities, append_input=True),
    )
    delivered = []

    async def append_input(run_id, text):
        delivered.append((run_id, text))
        return ProviderInputDelivery("delivered")

    adapter.append_input = append_input
    context.host.runtime.register(adapter)
    owner = WorkLedgerHandler(
        context.host.coordinator,
        provider_input=context.host.runtime.append_input,
    )
    context.manager.work_input = owner.submit_input
    create = "帮我做个菜谱页。"
    followup = "再加个人数选项，份量跟着人数变。"
    plans = {"create":planned(
        context.manager.provider, create, create, "execute", one_off=True)}

    def planner(_ingress, turn, *_args):
        return plans[turn]

    configure_professional_planner(context, planner, work_texts={create, followup})
    adapter.release.clear()
    try:
        first = await send(context, create, "create")
        await asyncio.wait_for(adapter.started.wait(), 3)
        await context.host.coordinator.drain_provider_facts()
        work_item_id = first["work_item_id"]
        plans["followup"] = planned(
            context.manager.provider,
            followup,
            followup,
            "amend",
            catalog_candidate(context, "work_item", work_item_id),
        )
        result = await send(context, followup, "followup")
        assert result["state"] == "work_input_accepted"
        await owner.drain_inputs()

        detail = context.host.coordinator.detail(work_item_id)
        ingress = context.manager.ingresses[context.session_id]
        current = ingress.loop.recipient_work(ingress.loop.bound_context_id)
        assert current is not None
        assert current["work_item_id"] == work_item_id
        assert current["input_requirements"] == detail["inputRequirements"]
        assert len(current["input_requirements"]) == 1
        requirement = current["input_requirements"][0]
        amendment = context.host.work.list_operations(work_item_id)[-1]
        assert requirement == {
            "operation_id":amendment.operation_id,
            "input_id":"followup",
            "attempt_id":first["attempt_id"],
            "text":followup,
            "delivery_state":"delivered",
            "delivery_reason":"",
        }
        assert delivered == [(first["run_id"], followup)]
    finally:
        adapter.release.set()
        await context.finish()
        await owner.drain_inputs()
