"""Host scope and handoff evidence contracts, without a live model or AUIP run.

The inert adapter captures the rendered request and returns a synthetic result.
These tests verify Host authority and prompt projection, not whether an LLM would
obey that projection or refrain from performing another operation.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_host.provider_identity import with_parent_conversation_context
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderRunIntakeAuthority, ProviderRunResult
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.control_ledger import ControlLedgerStore
from server.provider_event_ingestion import ProviderEventIngestor
from server.work_control import WorkControl
from server.work_effect_executor import WorkEffectExecutor
from server.work_ledger_coordinator import WorkLedgerCoordinator
from test_work_effect_executor import _RuntimeAdapter, _admission, _payload


CONSTRAINTS = "不要使用 shell，也不要作额外修改。"
ALPHA = "在 alpha 项目创建清单页面。"
BETA = "在 beta 项目创建计时页面。"
SOURCE = f"{CONSTRAINTS}\n{ALPHA}\n{BETA}"


class _InertHandoffAdapter(_RuntimeAdapter):
    async def run(self, request, run_id, _emit):
        self.calls += 1
        self.requests.append(
            {
                "run_id": run_id,
                "request": request,
                "rendered": with_parent_conversation_context(
                    request.task,
                    metadata=request.metadata,
                    execution_provider=self.provider_id,
                ),
            }
        )
        return ProviderRunResult(
            status="done",
            result="Synthetic capture completed; no model or user operation executed.",
        )


@asynccontextmanager
async def _scope_host(tmp_path, source, tasks):
    """Seal real current-turn facts; only the external Provider is inert."""
    database = tmp_path / "shared.sqlite3"
    work = WorkLedgerStore(database)
    ledger = ControlLedgerStore(database)
    control = WorkControl(ledger, work)
    admission = _admission(text=source)
    control.admit(admission, fence_scope="foreground-chat")
    payloads = []
    for ordinal, task in enumerate(tasks):
        workspace = tmp_path / f"project-{ordinal}"
        workspace.mkdir()
        project = work.create_or_get_project(workspace)
        payloads.append(
            replace(
                _payload(
                    project.project_id,
                    f"inert-scope-{ordinal}",
                    source=source,
                    task=task,
                ),
                title=ProviderEventIngestor.task_title(task),
                source_user_context="",
            )
        )
    accepted = control.seal_many(admission, tuple(payloads))
    coordinator = WorkLedgerCoordinator(work, work_control=control)
    runtime = ProviderRuntime()
    adapters = [_InertHandoffAdapter(payload.provider, work) for payload in payloads]
    for adapter in adapters:
        runtime.register(adapter)
    runtime.set_request_preparer(coordinator.prepare_request)
    coordinator.configure()
    executor = WorkEffectExecutor(control, runtime, coordinator)
    with (
        patch("config.settings.WORK_WORKTREE_ISOLATION", False),
        patch("server.work_ledger_coordinator.cwd_in_project_registry", return_value=True),
    ):
        try:
            yield SimpleNamespace(
                work=work,
                ledger=ledger,
                control=control,
                admission=admission,
                payloads=tuple(payloads),
                effect_ids=accepted["effect_ids"],
                runtime=runtime,
                adapters=adapters,
                executor=executor,
            )
        finally:
            await asyncio.gather(
                *(
                    record.task_handle
                    for record in runtime._runs.values()
                    if record.task_handle is not None
                ),
                return_exceptions=True,
            )
            await runtime.close()
            await coordinator.drain_provider_facts()
            runtime.set_request_preparer(None)
            coordinator.close()
            ledger.close()
            work.close()


def _plan(host):
    return json.loads(host.ledger.get_admission(host.admission.root_id)["plan_json"])


def _assert_control_facts_unchanged(host, plan):
    assert _plan(host) == plan
    effects = host.ledger._db.execute(
        "SELECT effect_id FROM control_effect_outbox ORDER BY ordinal"
    ).fetchall()
    assert [row[0] for row in effects] == list(host.effect_ids)
    assert host.work._connection.execute(
        "SELECT COUNT(*) FROM permission_requests"
    ).fetchone()[0] == 0


def _assert_prompt_evidence(request, source, task):
    original = deepcopy(request)
    rendered = with_parent_conversation_context(
        request.task,
        metadata=request.metadata,
        execution_provider=request.provider,
    )
    assert rendered.startswith(task + "\n\n")
    assert json.dumps(task, ensure_ascii=False) in rendered
    assert json.dumps(source, ensure_ascii=False) in rendered
    assert request == original
    return rendered


async def test_two_work_operations_keep_host_scope_and_receive_full_constraint_evidence(
    tmp_path,
):
    async with _scope_host(tmp_path, SOURCE, (ALPHA, BETA)) as host:
        before = _plan(host)
        assert len(before["effects"]) == 2
        assert len({effect["target_key"] for effect in before["effects"]}) == 2
        for effect_id, payload, other in zip(
            host.effect_ids, host.payloads, (BETA, ALPHA)
        ):
            request = host.control.provider_request(effect_id)
            validated = host.control.validate_runtime_request(
                ProviderRunIntakeAuthority(effect_id), request
            )
            assert validated == payload
            assert request.task == payload.task and other not in request.task
            assert request.provider == payload.provider
            assert request.metadata["work"]["project_id"] == payload.project_id
            assert request.metadata["source_user_operation_text"] == payload.task
            assert request.requirements == payload.requirements
            assert request.metadata["write_intent"] is True
            rendered = _assert_prompt_evidence(request, SOURCE, payload.task)
            assert CONSTRAINTS in rendered
            assert json.loads(host.ledger.get_effect(effect_id)["payload_json"]) == (
                payload.to_payload()
            )
        _assert_control_facts_unchanged(host, before)

        results = [await host.executor.execute(effect_id) for effect_id in host.effect_ids]
        assert [result["status"] for result in results] == ["terminal", "terminal"]
        assert len(host.runtime.list_runs()) == len(host.work.list_work_items()) == 2
        for effect_id, payload, adapter, result in zip(
            host.effect_ids, host.payloads, host.adapters, results
        ):
            assert adapter.calls == 1
            captured = adapter.requests[0]
            assert captured["request"].task == payload.task
            assert json.dumps(SOURCE, ensure_ascii=False) in captured["rendered"]
            binding = result["binding"]
            item = host.work.get_work_item(binding["work_item_id"])
            attempt = host.work.get_attempt(binding["attempt_id"])
            assert item.goal == payload.task and item.project_id == payload.project_id
            assert item.origin_effect_id == attempt.origin_effect_id == effect_id
            assert attempt.provider == payload.provider
            assert captured["run_id"] == binding["provider_run_id"]
            assert json.loads(host.ledger.get_effect(effect_id)["payload_json"]) == (
                payload.to_payload()
            )
        _assert_control_facts_unchanged(host, before)


@pytest.mark.parametrize("field", ["task", "source_user_operation_text"])
@pytest.mark.parametrize("replacement", [BETA, SOURCE], ids=["other-operation", "full-source"])
async def test_runtime_rejoin_rejects_replacing_an_accepted_operation(
    tmp_path, field, replacement
):
    async with _scope_host(tmp_path, SOURCE, (ALPHA, BETA)) as host:
        before = _plan(host)
        effect_id = host.effect_ids[0]
        request = host.control.provider_request(effect_id)
        if field == "task":
            request.task = replacement
        else:
            request.metadata[field] = replacement
        authority = ProviderRunIntakeAuthority(effect_id)
        with pytest.raises(WorkLedgerConflict, match="does not match"):
            host.control.validate_runtime_request(authority, request)
        with pytest.raises(WorkLedgerConflict, match="does not match"):
            await host.runtime.start_accepted(request, authority)
        _assert_control_facts_unchanged(host, before)
        assert all(host.control.binding(member) is None for member in host.effect_ids)
        assert all(
            host.ledger.get_effect(member)["state"] == "pending"
            for member in host.effect_ids
        )
        assert host.runtime.list_runs() == host.work.list_work_items() == []
        assert all(adapter.calls == 0 for adapter in host.adapters)


async def test_auip_sentence_is_quoted_evidence_for_one_selected_work_effect(tmp_path):
    auip = "然后在当前棋盘应用中认输。"
    source = f"{CONSTRAINTS}\n{ALPHA}\n{auip}"
    async with _scope_host(tmp_path, source, (ALPHA,)) as host:
        before = _plan(host)
        assert len(before["effects"]) == 1
        assert before["effects"][0]["kind"] == "work"
        request = host.control.provider_request(host.effect_ids[0])
        rendered = _assert_prompt_evidence(request, source, ALPHA)
        assert auip not in request.task
        assert auip not in rendered.replace(json.dumps(source, ensure_ascii=False), "")
        result = await host.executor.execute(host.effect_ids[0])
        assert result["status"] == "terminal"
        assert host.adapters[0].calls == 1
        assert host.adapters[0].requests[0]["request"].task == ALPHA
        _assert_control_facts_unchanged(host, before)
        assert len(host.runtime.list_runs()) == len(host.work.list_work_items()) == 1


async def test_complete_source_without_operation_excerpt_remains_one_work(tmp_path):
    source = "检查项目目录，并保留当前文件。"
    async with _scope_host(tmp_path, source, (source,)) as host:
        before = _plan(host)
        assert len(before["effects"]) == 1
        effect_id = host.effect_ids[0]
        request = host.control.provider_request(effect_id)
        assert "source_user_operation_text" not in request.metadata
        assert request.task == request.metadata["source_user_text"] == source
        _assert_prompt_evidence(request, source, source)
        _assert_prompt_evidence(request, source, source)
        _assert_control_facts_unchanged(host, before)
        first = await host.executor.execute(effect_id)
        replay = await host.executor.execute(effect_id)
        assert first["status"] == replay["status"] == "terminal"
        assert first["replayed"] is False and replay["replayed"] is True
        assert first["binding"] == replay["binding"]
        assert host.adapters[0].calls == 1
        assert len(host.runtime.list_runs()) == len(host.work.list_work_items()) == 1
        _assert_control_facts_unchanged(host, before)
