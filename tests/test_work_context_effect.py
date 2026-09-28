"""Explicit recipients and Work goals are separate accepted facts."""
import asyncio
from dataclasses import fields, replace
import json
from threading import Barrier

import pytest

from agent_host.provider_types import ProviderSessionHandle
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.control_ledger import ControlLedgerConflict
from server.work_control import WorkContextPayloadV5
from test_work_effect_executor import _host, _payload, _admission
from test_chat_control_ingress import context as context
from test_chat_work_acceptance import host as host, send


def seal_context(host, source_attempt_id, *, target="", suffix="context", epoch=2):
    admission = _admission(suffix=suffix, epoch=epoch)
    host.control.admit(admission, fence_scope="foreground-chat")
    base = _payload(host.project.project_id, host.adapter.provider_id, suffix=suffix)
    payload = WorkContextPayloadV5(**{f.name:getattr(base, f.name) for f in fields(base)},
        context_attempt_id=source_attempt_id, work_item_id=target)
    return host.control.seal(admission, payload)["effect_id"]


def enable_context(host, monkeypatch, scope="interaction"):
    host.adapter.manifest = replace(host.adapter.manifest,
        capabilities=replace(host.adapter.manifest.capabilities, resume="attach"))
    host.runtime.register(host.adapter)
    run = host.adapter.run
    async def execute(request, run_id, emit):
        result = await run(request, run_id, emit)
        result.session = request.session or ProviderSessionHandle(provider=host.adapter.provider_id,
            session_id=run_id, scope=scope)
        return result
    monkeypatch.setattr(host.adapter, "run", execute)


async def test_new_goal_keeps_explicit_context_without_reusing_work(tmp_path, monkeypatch):
    async with _host(tmp_path) as host:
        enable_context(host, monkeypatch)
        first = await host.executor.execute(host.effect_id)
        before = host.work.get_work_item(first["binding"]["work_item_id"])
        effect = seal_context(host, first["binding"]["attempt_id"])
        second = await host.executor.execute(effect)
        assert second["binding"]["work_item_id"] != before.work_item_id
        assert len(host.work.list_work_items()) == 2
        request = host.adapter.requests[1]["request"]
        original_session = host.work.get_attempt(first["binding"]["attempt_id"]).metadata["provider_session"]
        assert request.session.to_dict() == original_session
        assert request.metadata["intent"] == "execute" and request.metadata["continuation"] == "new"
        assert request.cwd == before.workspace_path and "context_attempt_id" not in request.metadata
        assert second["receipt"]["outcome"] == "succeeded"
        after = host.work.get_work_item(before.work_item_id)
        assert (after.goal, after.title, after.origin_effect_id) == (before.goal, before.title, before.origin_effect_id)
        replay = await host.executor.execute(effect)
        assert replay["replayed"] and replay["receipt"] == second["receipt"] and host.adapter.calls == 2
        raw = json.loads(host.control_store.get_effect(effect)["payload_json"])
        assert raw["version"] == 5 and raw["work_item_id"] == "" and raw["operation"] == "execute"
        assert WorkContextPayloadV5.from_payload(raw).to_payload() == raw
        for changed in ({"context_attempt_id":""}, {"operation":"amend"}, {"work_item_id":None}, {"extra":True}):
            with pytest.raises(ControlLedgerConflict):
                WorkContextPayloadV5.from_payload({**raw, **changed})


async def test_existing_goal_can_use_another_explicit_context(tmp_path, monkeypatch):
    async with _host(tmp_path) as host:
        enable_context(host, monkeypatch)
        first = await host.executor.execute(host.effect_id)
        admission = _admission(suffix="second", epoch=2)
        host.control.admit(admission, fence_scope="foreground-chat")
        second_id = host.control.seal(admission, _payload(host.project.project_id, host.adapter.provider_id, suffix="second"))["effect_id"]
        second = await host.executor.execute(second_id)
        target = first["binding"]["work_item_id"]
        context_effect = seal_context(host, second["binding"]["attempt_id"], target=target, epoch=3)
        third = await host.executor.execute(context_effect)
        assert third["binding"]["work_item_id"] == target and len(host.work.list_work_items()) == 2
        assert host.adapter.requests[2]["request"].session.session_id == second["binding"]["provider_run_id"]
        assert host.adapter.requests[2]["request"].metadata["intent"] == "amend"
        assert len(host.work.list_attempts(target)) == 2
        assert len(host.work.list_attempts(second["binding"]["work_item_id"])) == 1


@pytest.mark.parametrize("scope", ["work_item", "attempt"])
async def test_work_scoped_context_is_not_promoted_by_a_caller(tmp_path, monkeypatch, scope):
    async with _host(tmp_path) as host:
        enable_context(host, monkeypatch, scope=scope)
        first = await host.executor.execute(host.effect_id)
        with pytest.raises(WorkLedgerConflict, match="rebinding"):
            seal_context(host, first["binding"]["attempt_id"])
        assert len(host.work.list_work_items()) == 1 and host.adapter.calls == 1


@pytest.mark.parametrize("state", ["running", "orphaned"])
async def test_two_work_goals_cannot_occupy_the_same_context(tmp_path, monkeypatch, state):
    async with _host(tmp_path) as host:
        enable_context(host, monkeypatch)
        first = await host.executor.execute(host.effect_id)
        effect = seal_context(host, first["binding"]["attempt_id"])
        host.adapter.result_status = "orphaned" if state == "orphaned" else "done"
        host.adapter.started.clear()
        if state == "running":
            host.adapter.release.clear()
        pending = asyncio.create_task(host.executor.execute(effect))
        await asyncio.wait_for(host.adapter.started.wait(), 5)
        if state == "orphaned":
            assert (await pending)["status"] == "unknown"
        # A read-only Attempt has no workspace writer lease to protect this
        # boundary; the shared context must still have one execution owner.
        try:
            with pytest.raises(WorkLedgerConflict, match="context already"):
                host.work.create_operation_attempt(first["binding"]["work_item_id"],
                    intent="amend", instruction="Read this context", task="Read this context",
                    provider=host.adapter.provider_id, attempt_metadata={"write_intent":False,
                        "provider_session":host.work.get_attempt(first["binding"]["attempt_id"]).metadata["provider_session"]})
            assert len(host.work.list_attempts(first["binding"]["work_item_id"])) == 1
            assert host.adapter.calls == 2 and len(host.work.list_work_items()) == 2
        finally:
            host.adapter.release.set()
            await pending


async def test_recipient_change_after_prepare_rolls_back_new_work_and_claim(tmp_path, monkeypatch):
    async with _host(tmp_path) as host:
        enable_context(host, monkeypatch)
        first = await host.executor.execute(host.effect_id)
        source_id = first["binding"]["attempt_id"]
        effect = seal_context(host, source_id)
        original = host.control.bind_runtime_dispatch_intent
        def changed(*args, **kwargs):
            host.work.set_work_item_state(first["binding"]["work_item_id"], "archived")
            return original(*args, **kwargs)
        monkeypatch.setattr(host.control, "bind_runtime_dispatch_intent", changed)
        with pytest.raises(WorkLedgerConflict, match="available local Project"):
            await host.executor.execute(effect)
        assert len(host.work.list_work_items()) == 1 and host.adapter.calls == 1
        assert host.control.binding(effect) is None
        assert host.control_store.get_effect(effect)["state"] == "pending"


async def test_chat_freezes_explicit_recipient_separately_from_new_goal(host, monkeypatch):
    enable_context(host, monkeypatch)
    first = await send(host)
    source_id = first["execution"]["binding"]["attempt_id"]
    host.runner.recipient_attempt_id = source_id
    async def change_selection():
        host.runner.recipient_attempt_id = "a-later-ui-selection"
    host.after_control = change_selection
    second = await send(host, "Build another independent artifact.", "another-goal")
    assert "error" not in second, second
    assert second["recipient_attempt_id"] == source_id
    assert second["execution"]["binding"]["work_item_id"] != first["execution"]["binding"]["work_item_id"]
    raw = json.loads(host.context.ledger.get_effect(second["accepted"]["effect_id"])["payload_json"])
    assert raw["context_attempt_id"] == source_id and raw["work_item_id"] == ""
    assert second["execution"]["receipt"]["outcome"] == "succeeded"


async def test_explicit_recipient_requires_current_provider_attach_capability(tmp_path, monkeypatch):
    async with _host(tmp_path) as host:
        enable_context(host, monkeypatch)
        first = await host.executor.execute(host.effect_id)
        effect = seal_context(host, first["binding"]["attempt_id"])
        host.adapter.manifest = replace(host.adapter.manifest,
            capabilities=replace(host.adapter.manifest.capabilities, resume="none"))
        host.runtime.register(host.adapter)
        with pytest.raises(WorkLedgerConflict, match="does not support context"):
            await host.executor.execute(effect)
        assert host.control.binding(effect) is None and host.adapter.calls == 1


async def test_context_reservation_is_atomic_across_database_connections(tmp_path, monkeypatch):
    async with _host(tmp_path) as host:
        enable_context(host, monkeypatch)
        first = await host.executor.execute(host.effect_id)
        session = host.work.get_attempt(first["binding"]["attempt_id"]).metadata["provider_session"]
        other = WorkLedgerStore(host.database)
        gate = Barrier(2)
        def create(work):
            gate.wait(timeout=5)
            try:
                return work.create_work_item_with_attempt(host.project.project_id, title="Read context",
                    goal="Read context", intent="execute", instruction="Read context", task="Read context",
                    provider=host.adapter.provider_id, attempt_metadata={"provider_session":session, "write_intent":False})
            except WorkLedgerConflict as exc:
                return exc
        try:
            results = await asyncio.gather(*(asyncio.to_thread(create, work) for work in (host.work, other)))
            assert sum(isinstance(row, WorkLedgerConflict) for row in results) == 1
            assert len(host.work.list_work_items()) == 2
            assert sum(len(host.work.list_attempts(item.work_item_id)) for item in host.work.list_work_items()) == 2
        finally:
            other.close()
