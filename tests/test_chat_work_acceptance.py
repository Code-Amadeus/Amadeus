"""One actual admitted Chat root reaches existing v3 Work intake and receipts."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_host.provider_runtime import ProviderRuntime
from server.event_bus import bus
from server.handlers.chat_handler import ChatHandler
from server.work_control import WorkControl
from server.work_effect_executor import WorkEffectExecutor
from server.work_ledger_coordinator import WorkLedgerCoordinator
from tools.probes.chat_work_acceptance import AcceptedWorkChatRunner
from test_chat_control_ingress import context as context, request
from test_work_effect_executor import _RuntimeAdapter


def decision(text, **changes):
    return {"proposal_index":0, "provider":"codex", "intent":"execute",
            "source_clause":text, "references":None, "reference_mode":"none",
            "work_placement":"inherit", "session_context":"unchanged",
            "workspace_effect":"write", "payload_continuity":"current_turn", **changes}


@pytest.fixture
async def host(context, monkeypatch):
    monkeypatch.setattr("config.settings.WORK_PROJECT_ALLOWLIST", context.project.canonical_path)
    monkeypatch.setattr("config.settings.WORK_WORKTREE_ISOLATION", False)
    monkeypatch.setattr("config.settings.WORK_ROSTER_CANDIDATES", True)
    control = WorkControl(context.ledger, context.store)
    coordinator = WorkLedgerCoordinator(context.store, work_control=control)
    runtime = ProviderRuntime()
    adapter = _RuntimeAdapter("codex", context.store)
    runtime.register(adapter)
    runtime.set_request_preparer(coordinator.prepare_request)
    monkeypatch.setattr("agent_host.provider_runtime.runtime", runtime)
    monkeypatch.setattr("llm.client.remote_llm_query", Mock(side_effect=AssertionError("unplanned query")))
    coordinator.configure()
    coordinator.bind_session_context("A", context.project.project_id)
    # The reused ingress fixture captures Chat emits. Also deliver real Provider
    # events to the existing Work terminal pipeline in this joined assembly.
    async def emit(method, params):
        await context.emit(method, params)
        await type(bus).emit(bus, method, params)
    monkeypatch.setattr(bus, "emit", emit)
    state = SimpleNamespace(context=context, control=control, coordinator=coordinator,
        runtime=runtime, adapter=adapter, calls=[], role=None, rows=None, after_control=None)

    async def query(phase, messages):
        state.calls.append((phase, messages))
        text = messages[-1]["content"].split("\n\n[Host control frame]", 1)[0]
        if phase == "role":
            return state.role if state.role is not None else (
                'I will prepare it. [DELEGATE provider="codex" intent="execute" cwd="ROLE_WORKSPACE" task="ROLE_REWRITE"]')
        assert phase == "control"
        if state.after_control:
            await state.after_control()
        rows = state.rows if state.rows is not None else [decision(text)]
        return json.dumps({"decisions":rows})

    executor = WorkEffectExecutor(control, runtime, coordinator)
    runner = AcceptedWorkChatRunner(control, executor, coordinator, query,
        fence_scope="foreground", record_turn=ChatHandler._save_direct_turn)
    handler, legacy, direct = context.make(runner=runner)
    state.runner, state.handler, state.legacy, state.direct = runner, handler, legacy, direct
    try:
        yield state
    finally:
        await handler.close()
        adapter.release.set()
        await asyncio.gather(*(r.task_handle for r in runtime._runs.values() if r.task_handle), return_exceptions=True)
        await runtime.close()
        await coordinator.drain_provider_facts()
        coordinator.close()


async def send(host, text="Build accepted-c2.txt", utterance="u1"):
    result = await host.handler._handle_send(request(utterance, text=text))
    assert result["status"] == "ok"
    await host.handler._stream_task
    host.legacy.assert_not_called()
    host.direct.assert_not_called()
    return host.runner.observations[-1]


async def test_real_chat_grant_proposal_intake_and_receipt_share_one_root(host):
    record = await send(host)
    admission = host.context.ledger.find_admission("chat:A", "u1")
    binding = record["execution"]["binding"]
    receipt = record["execution"]["receipt"]
    item = host.context.store.get_work_item(binding["work_item_id"])
    assert admission["chat_epoch"] == record["chat_epoch"] == 1
    assert admission["root_id"] == record["root_id"]
    assert item.origin_effect_id == record["accepted"]["effect_id"]
    assert item.goal == "Build accepted-c2.txt" and item.goal != "ROLE_REWRITE"
    assert item.workspace_path == host.context.project.canonical_path
    assert host.adapter.requests[0]["request"].cwd == host.context.project.canonical_path
    assert record["proposals"][0]["cwd"] == "ROLE_WORKSPACE"
    assert host.adapter.calls == 1 and len(host.context.store.list_work_items()) == 1
    assert receipt["outcome"] == "succeeded"
    assert receipt["external_id"] == binding["provider_run_id"]
    assert host.context.store.get_writer_lease(binding["attempt_id"]).status == "released"
    assert len(host.context.store.list_completions(item.work_item_id)) == 1
    assert [phase for phase, _ in host.calls] == ["role", "control"]
    assert "HISTORY_A" in host.adapter.requests[0]["metadata"]["source_user_context"]
    assert "HISTORY_B" not in str(host.adapter.requests[0]["metadata"])
    replay = await host.handler._handle_send(request("u1", turn="later-alias", text="Build accepted-c2.txt"))
    assert replay["status"] == "replayed"
    assert len(host.calls) == 2 and host.adapter.calls == 1


@pytest.mark.parametrize("no_proposal", [False, True])
async def test_no_work_disposition_is_accepted_without_execution(host, no_proposal):
    if no_proposal:
        host.role = "We can discuss this first."
    host.rows = []
    record = await send(host, "Let us discuss the idea first.")
    assert record["accepted"]["disposition"] == "no_effect_accepted"
    assert record["accepted"]["effect_count"] == 0
    assert not host.context.store.list_work_items() and host.adapter.calls == 0
    assert len(host.calls) == (1 if no_proposal else 2)


@pytest.mark.parametrize("kind", ["unknown", "focus", "multiple", "prior"])
async def test_outside_scope_cannot_become_a_new_work_effect(host, kind):
    source = "Build alpha. Build beta."
    row = decision(source)
    if kind == "unknown":
        row.update(intent="amend", subject="work_item", references=[], reference_mode="candidates", work_placement="not_applicable")
    elif kind == "focus":
        row.update(subject="project", references=["project:"+host.context.project.project_id], reference_mode="candidates",
                   work_placement="project", session_context="bind")
    elif kind == "prior":
        row.update(payload_continuity="confirmed_prior_request")
    host.rows = [row] if kind != "multiple" else [decision("Build alpha."), decision("Build beta.", proposal_index=1)]
    record = await send(host, source)
    assert "error" in record and "accepted" not in record
    assert host.adapter.calls == 0 and not host.context.store.list_work_items()
    assert host.context.ledger.find_admission("chat:A", "u1")["plan_id"] is None


async def test_newer_durable_fence_prevents_old_plan_from_entering_work(host):
    async def supersede():
        host.context.ledger.advance_epoch("foreground")
    host.after_control = supersede
    record = await send(host)
    assert "error" in record and "accepted" not in record
    assert host.adapter.calls == 0 and not host.context.store.list_work_items()


@pytest.mark.parametrize("phase", ["role", "control"])
async def test_chat_cancel_before_seal_creates_no_work(host, phase):
    entered, release = asyncio.Event(), asyncio.Event()
    original = host.runner.query
    async def blocked(current_phase, messages):
        if current_phase == phase:
            entered.set()
            await release.wait()
        return await original(current_phase, messages)
    host.runner.query = blocked
    await host.handler._handle_send(request(text="Build accepted-c2.txt"))
    await asyncio.wait_for(entered.wait(), 5)
    await host.handler._handle_abort({"turn_id":"turn-u1"})
    await asyncio.gather(host.handler._stream_task, return_exceptions=True)
    admission = host.context.ledger.find_admission("chat:A", "u1")
    assert admission["plan_id"] is None and admission["lifecycle"] == "discarded"
    assert host.adapter.calls == 0 and not host.context.store.list_work_items()


async def test_chat_cancel_after_acceptance_replays_existing_terminal_work(host):
    host.adapter.release.clear()
    await host.handler._handle_send(request(text="Build accepted-c2.txt"))
    await asyncio.wait_for(host.adapter.started.wait(), 5)
    record = host.runner.observations[-1]
    effect_id = record["accepted"]["effect_id"]
    await host.handler._handle_abort({"turn_id":"turn-u1"})
    await asyncio.gather(host.handler._stream_task, return_exceptions=True)
    host.adapter.release.set()
    result = await host.runner.executor.execute(effect_id)
    assert result["receipt"]["outcome"] == "succeeded"
    assert host.adapter.calls == 1 and len(host.context.store.list_work_items()) == 1
