"""Accepted amendments preserve the Work while owning a new operation and receipt."""
import asyncio
from dataclasses import fields, replace
import json
from threading import Barrier

import pytest

from agent_host.provider_types import ProviderSessionHandle
from server.control_ledger import ControlLedgerConflict
from server.work_control import WorkAmendPayloadV4
from agent_host.work_ledger_store import WorkLedgerStore, WorkLedgerConflict
from server.control_ledger import ControlLedgerStore, ReconciliationPolicy
from server.work_control import WorkControl
from test_work_effect_executor import _host, _payload, _admission
from test_chat_control_ingress import context as context, request
from test_chat_work_acceptance import host as host, send, decision


CHANGE = "Append the second requirement."


def amend_row(work_id):
    return decision(CHANGE, intent="amend", subject="work_item", references=["work_item:" + work_id],
                    reference_mode="candidates", work_placement="not_applicable")


async def test_chat_amend_preserves_goal_workspace_and_native_context(host, monkeypatch, tmp_path):
    session = ProviderSessionHandle(provider="codex", session_id="native-context", scope="work_item")
    host.adapter.manifest = replace(host.adapter.manifest,
        capabilities=replace(host.adapter.manifest.capabilities, resume="attach"))
    host.runtime.register(host.adapter)
    original_run = host.adapter.run
    async def run(*args):
        result = await original_run(*args)
        result.session = session
        return result
    monkeypatch.setattr(host.adapter, "run", run)
    first = await send(host)
    first_binding = first["execution"]["binding"]
    work_id = first_binding["work_item_id"]
    initial = host.context.store.get_work_item(work_id)
    other_path = tmp_path / "other-project"
    other_path.mkdir()
    other = host.context.store.create_or_get_project(other_path)
    monkeypatch.setattr("config.settings.WORK_PROJECT_ALLOWLIST", initial.workspace_path + ";" + str(other_path))
    host.coordinator.bind_session_context("A", other.project_id)
    host.rows = [amend_row(work_id)]
    second = await send(host, CHANGE, "amend-one")
    assert "error" not in second, second.get("error")
    binding = second["execution"]["binding"]
    final = host.context.store.get_work_item(work_id)
    assert len(host.context.store.list_work_items()) == 1
    assert (final.goal, final.title, final.project_id, final.workspace_path, final.origin_effect_id) == (
        initial.goal, initial.title, initial.project_id, initial.workspace_path, initial.origin_effect_id)
    assert binding["work_item_id"] == work_id
    assert binding["operation_id"] != first_binding["operation_id"]
    operation = host.context.store.get_operation(binding["operation_id"])
    assert operation.intent == "amend" and operation.instruction == CHANGE
    assert operation.origin_effect_id == second["accepted"]["effect_id"] != initial.origin_effect_id
    assert host.adapter.calls == 2
    assert host.adapter.requests[1]["request"].session == session
    assert host.adapter.requests[1]["request"].cwd == initial.workspace_path
    assert host.adapter.requests[1]["request"].task == CHANGE
    assert second["execution"]["receipt"]["outcome"] == "succeeded"
    assert len(host.context.store.list_completions(work_id)) == 2
    assert host.control.binding(first["accepted"]["effect_id"]) == first_binding
    before = len(host.calls)
    replay = await host.handler._handle_send(request("amend-one", turn="amend-alias", text=CHANGE))
    assert replay["status"] == "replayed" and len(host.calls) == before and host.adapter.calls == 2
    result = await host.runner.executor.execute(second["accepted"]["effect_id"])
    assert result["replayed"] and result["receipt"] == second["execution"]["receipt"]
    raw = json.loads(host.context.ledger.get_effect(second["accepted"]["effect_id"])["payload_json"])
    assert raw["version"] == 4 and raw["operation"] == "amend"
    assert WorkAmendPayloadV4.from_payload(raw).to_payload() == raw
    for bad in ({**raw,"work_item_id":""}, {**raw,"operation":"execute"}, {**raw,"extra":True}):
        with pytest.raises(ControlLedgerConflict):
            WorkAmendPayloadV4.from_payload(bad)
    host.rows = None
    third = await send(host, "Build a separate artifact.", "new-goal")
    assert "error" not in third, third.get("error")
    new_id = third["execution"]["binding"]["work_item_id"]
    assert new_id != work_id and len(host.context.store.list_work_items()) == 2
    assert host.context.store.get_work_item(new_id).project_id == other.project_id
    assert host.adapter.requests[2]["request"].session is None
    assert host.context.store.get_work_item(work_id).goal == initial.goal


async def test_amend_claim_and_operation_write_roll_back_together(host):
    first = await send(host)
    work_id = first["execution"]["binding"]["work_item_id"]
    with host.context.store._transaction() as db:
        db.execute("CREATE TRIGGER reject_amend BEFORE INSERT ON run_attempts WHEN NEW.origin_effect_id<>'' BEGIN SELECT RAISE(ABORT,'amend failed'); END")
    host.rows = [amend_row(work_id)]
    second = await send(host, CHANGE, "amend-one")
    assert "error" in second and "accepted" in second
    assert host.adapter.calls == 1
    assert len(host.context.store.list_operations(work_id)) == len(host.context.store.list_attempts(work_id)) == 1
    assert host.control.binding(second["accepted"]["effect_id"]) is None
    effect = host.context.ledger.get_effect(second["accepted"]["effect_id"])
    assert not effect["external_id"] and not effect["claim_token"]


@pytest.mark.parametrize("obligation", ["export_plan", "host_outcome_requirement"])
async def test_amend_keeps_predecessor_obligations_for_the_work_owner(host, obligation):
    first = await send(host)
    binding = first["execution"]["binding"]
    host.context.store.update_attempt(binding["attempt_id"], metadata={obligation:{"retained":True}})
    host.rows = [amend_row(binding["work_item_id"])]
    second = await send(host, CHANGE, "amend-one")
    assert "error" not in second, second
    assert host.context.store.get_attempt(binding["attempt_id"]).metadata[obligation] == {"retained":True}
    assert host.adapter.calls == 2 and len(host.context.store.list_operations(binding["work_item_id"])) == 2


def seal_amend(host, work_id, suffix="amend", epoch=2):
    admission = _admission(suffix=suffix, epoch=epoch)
    host.control.admit(admission, fence_scope="foreground-chat")
    base = _payload(host.project.project_id, host.adapter.provider_id, suffix=suffix)
    payload = WorkAmendPayloadV4(**{field.name:getattr(base, field.name) for field in fields(base)}, work_item_id=work_id)
    return host.control.seal(admission, payload)["effect_id"]


@pytest.mark.parametrize("status", ["done", "error", "cancelled", "orphaned"])
async def test_amend_outcome_and_replay_keep_original_identity(tmp_path, status):
    async with _host(tmp_path) as host:
        first = await host.executor.execute(host.effect_id)
        work_id = first["binding"]["work_item_id"]
        initial = host.work.get_work_item(work_id)
        effect_id = seal_amend(host, work_id)
        host.adapter.result_status = status
        result = await host.executor.execute(effect_id)
        binding = host.control.runtime_binding(effect_id)
        assert binding == result["binding"] and binding["work_item_id"] == work_id
        replay = await host.executor.execute(effect_id)
        assert replay["binding"] == binding and replay["replayed"]
        assert host.adapter.calls == 2 and len(host.work.list_work_items()) == 1
        assert len(host.work.list_operations(work_id)) == len(host.work.list_attempts(work_id)) == 2
        final = host.work.get_work_item(work_id)
        assert (final.goal, final.workspace_path, final.origin_effect_id) == (initial.goal, initial.workspace_path, initial.origin_effect_id)
        if status == "orphaned":
            assert result["status"] == "unknown" and result["receipt"] is None
            assert len(host.work.list_completions(work_id)) == 1
            assert host.work.get_writer_lease(binding["attempt_id"]).status == "active"
            with pytest.raises(WorkLedgerConflict, match="settled"):
                seal_amend(host, work_id, "after-unknown", epoch=3)
        else:
            assert result["receipt"]["outcome"] == {"done":"succeeded", "error":"failed", "cancelled":"cancelled"}[status]
            assert len(host.work.list_completions(work_id)) == 2
            assert host.work.get_writer_lease(binding["attempt_id"]).status == "released"


async def test_amend_process_loss_after_binding_never_resubmits(tmp_path):
    async with _host(tmp_path) as host:
        first = await host.executor.execute(host.effect_id)
        work_id = first["binding"]["work_item_id"]
        effect_id = seal_amend(host, work_id)
        prepare = host.coordinator.prepare_request
        def lose_process(request, run_id, authority):
            prepare(request, run_id, authority)
            raise RuntimeError("lost after binding")
        host.runtime.set_request_preparer(lose_process)
        with pytest.raises(RuntimeError, match="lost after binding"):
            await host.executor.execute(effect_id)
        binding = host.control.binding(effect_id)
        assert binding["work_item_id"] == work_id and host.adapter.calls == 1
        from agent_host.provider_runtime import ProviderRuntime
        from server.work_effect_executor import WorkEffectExecutor
        restarted = ProviderRuntime()
        try:
            result = await WorkEffectExecutor(host.control, restarted, host.coordinator).execute(effect_id)
            assert result["status"] == "unknown" and result["binding"] == binding
            assert restarted.list_runs() == [] and host.adapter.calls == 1
            assert len(host.work.list_attempts(work_id)) == 2
            assert host.control_store.get_receipt(effect_id) is None
        finally:
            await restarted.close()


async def test_amend_pending_permission_after_acceptance_refuses_execution(tmp_path):
    async with _host(tmp_path) as host:
        first = await host.executor.execute(host.effect_id)
        work_id = first["binding"]["work_item_id"]
        effect_id = seal_amend(host, work_id)
        permission = host.work.create_permission_request(work_id, attempt_id=first["binding"]["attempt_id"],
            capability="desktop_export", action="export", scope_paths=[str(host.workspace)])
        with pytest.raises(WorkLedgerConflict, match="pending permission"):
            await host.executor.execute(effect_id)
        assert host.control.binding(effect_id) is None and host.adapter.calls == 1
        assert host.work.get_permission_request(permission.request_id).status == "pending"
        assert len(host.work.list_attempts(work_id)) == 1


@pytest.mark.parametrize("change", ["archived", "orphaned", "retired_project"])
async def test_amend_commit_rechecks_changed_target(tmp_path, change):
    async with _host(tmp_path) as host:
        first = await host.executor.execute(host.effect_id)
        work_id = first["binding"]["work_item_id"]
        effect_id = seal_amend(host, work_id)
        with host.work._transaction() as db:
            if change == "archived":
                db.execute("UPDATE work_items SET state='archived' WHERE work_item_id=?", (work_id,))
            elif change == "retired_project":
                db.execute("UPDATE projects SET state='retired' WHERE project_id=?", (host.project.project_id,))
            elif change == "orphaned":
                db.execute("UPDATE run_attempts SET execution_status='orphaned' WHERE attempt_id=?", (first["binding"]["attempt_id"],))
        with pytest.raises(WorkLedgerConflict):
            host.control.bind_dispatch_intent(effect_id, provider_run_id="never-submit", lease_seconds=15,
                reconciliation=ReconciliationPolicy(owner="provider_submission", ttl_seconds=60, max_probes=3, interval_seconds=5))
        assert host.control.binding(effect_id) is None and host.control_store.get_effect(effect_id)["state"] == "pending"
        assert len(host.work.list_operations(work_id)) == len(host.work.list_attempts(work_id)) == 1


@pytest.mark.parametrize("obligation", ["export_plan", "host_outcome_requirement"])
async def test_amend_commit_preserves_new_predecessor_obligation(tmp_path, obligation):
    async with _host(tmp_path) as host:
        first = await host.executor.execute(host.effect_id)
        work_id = first["binding"]["work_item_id"]
        effect_id = seal_amend(host, work_id)
        host.work.update_attempt(first["binding"]["attempt_id"],
            metadata={obligation:{"retained":True}})
        result = host.control.bind_dispatch_intent(effect_id,
            provider_run_id="accepted-amend", lease_seconds=15,
            reconciliation=ReconciliationPolicy(owner="provider_submission",
                ttl_seconds=60, max_probes=3, interval_seconds=5))
        assert result["binding"]["work_item_id"] == work_id
        assert host.work.get_attempt(first["binding"]["attempt_id"]).metadata[obligation] == {"retained":True}
        assert len(host.work.list_operations(work_id)) == len(host.work.list_attempts(work_id)) == 2


async def test_amend_competing_database_connections_share_one_binding(tmp_path):
    async with _host(tmp_path) as host:
        first = await host.executor.execute(host.effect_id)
        work_id = first["binding"]["work_item_id"]
        effect_id = seal_amend(host, work_id)
        second_work = WorkLedgerStore(host.database)
        second_ledger = ControlLedgerStore(host.database)
        second_control = WorkControl(second_ledger, second_work)
        barrier = Barrier(2)
        def bind(control):
            barrier.wait(timeout=5)
            return control.bind_dispatch_intent(effect_id, provider_run_id="one-amend-run", lease_seconds=15,
                reconciliation=ReconciliationPolicy(owner="provider_submission", ttl_seconds=60, max_probes=3, interval_seconds=5))
        try:
            results = await asyncio.gather(*(asyncio.to_thread(bind, control) for control in (host.control, second_control)))
            assert results[0]["binding"] == results[1]["binding"]
            assert results[0]["binding"]["work_item_id"] == work_id
            assert sorted(result["replayed"] for result in results) == [False, True]
            assert len(host.work.list_work_items()) == 1
            assert len(host.work.list_operations(work_id)) == len(host.work.list_attempts(work_id)) == 2
        finally:
            second_ledger.close()
            second_work.close()
