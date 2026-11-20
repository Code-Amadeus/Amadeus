"""Source acceptance and exact Work input survive the same failure boundary."""
import asyncio
from dataclasses import replace
import json

import pytest

from agent_host.provider_types import ProviderInputDelivery
from agent_host.work_ledger_store import WorkLedgerConflict
from server.handlers.work_ledger_handler import WorkLedgerHandler
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_targets import catalog_candidate
from test_cooperative_planned_work import configure_professional_planner, planned, send


@pytest.mark.parametrize("boundary", ["before_record", "after_record", "after_requirement", "after_commit", "unknown", "delivered"])
async def test_input_handoff_keeps_acceptance_and_delivery_distinct(pending_host, monkeypatch, boundary):
    context, calls = pending_host, []
    adapter = context.host.adapter
    adapter.manifest = replace(adapter.manifest,
        capabilities=replace(adapter.manifest.capabilities, append_input=True))

    async def append_input(run_id, text):
        calls.append((run_id, text))
        return ProviderInputDelivery("unknown" if boundary == "unknown" else "delivered")

    adapter.append_input = append_input
    context.host.runtime.register(adapter)
    handler = WorkLedgerHandler(context.host.coordinator, provider_input=context.host.runtime.append_input)
    context.manager.work_input = handler.submit_input
    create, amend = "帮我做个菜谱页。", "再加一个人数选项，份量跟着人数变。"
    plans = {"create":planned(context.manager.provider, create, create, "execute", one_off=True)}
    configure_professional_planner(context, lambda _i, turn, *_:plans[turn], work_texts={create, amend})
    adapter.release.clear()
    try:
        first = await send(context, create, "create")
        await asyncio.wait_for(adapter.started.wait(), 3)
        await context.host.coordinator.drain_provider_facts()
        target = catalog_candidate(context, "work_item", first["work_item_id"])
        plans["amend"] = planned(context.manager.provider, amend, amend, "amend", target)
        original = context.host.work.accept_provider_input
        original_operation = context.host.work.create_operation

        def fail_record(**kwargs):
            if boundary == "before_record":
                raise WorkLedgerConflict("injected failure before the input row")
            original(**kwargs)
            raise WorkLedgerConflict("injected failure after the input row")

        async def fail_dispatch(_params, *, accepted):
            assert accepted[0]["state"] == "unknown"
            raise RuntimeError("injected failure after atomic acceptance, before native I/O")

        def fail_requirement(*args, **kwargs):
            original_operation(*args, **kwargs)
            raise WorkLedgerConflict("injected failure after the requirement row")

        if boundary in {"before_record", "after_record"}:
            monkeypatch.setattr(context.host.work, "accept_provider_input", fail_record)
        if boundary == "after_commit":
            context.manager.work_input = fail_dispatch
        if boundary == "after_requirement":
            monkeypatch.setattr(context.host.work, "create_operation", fail_requirement)
        await context.handler.send_text(amend, session_id=context.session_id, turn_id="amend")
        await asyncio.gather(context.handler._stream_task, return_exceptions=True)
        await handler.drain_inputs()
        admission = context.host.control_store.find_admission("chat:" + context.session_id, "amend")
        rows = context.host.work.list_provider_inputs(first["work_item_id"])
        evidence = json.loads(admission["plan_json"])["evidence"]
        if boundary in {"before_record", "after_record", "after_requirement"}:
            assert rows == [] and calls == []
            assert len(context.host.work.list_operations(first["work_item_id"])) == 1
            assert evidence["reason"] == "planned_work_acceptance_rejected"
        else:
            row, = rows
            assert row["input_id"] == "amend" and row["text"] == amend
            assert row["provider_run_id"] == first["run_id"]
            assert row["state"] == ("delivered" if boundary == "delivered" else "unknown")
            operation, = evidence["compound_work_plan"]["operations"]
            assert operation["input_id"] == row["input_id"] and operation["run_id"] == first["run_id"]
            assert len(calls) == (0 if boundary == "after_commit" else 1)
        monkeypatch.setattr(context.host.work, "accept_provider_input", original)
        context.manager.work_input = handler.submit_input
        before_calls = list(calls)
        replay = await context.handler.send_text(amend, session_id=context.session_id, turn_id="amend")
        await handler.drain_inputs()
        assert replay["status"] == "replayed"
        assert calls == before_calls and context.host.work.list_provider_inputs(first["work_item_id"]) == rows
        # Retiring the Chat turn never erases a committed input responsibility.
        await send(context, "嗯，好，先这样。", "ordinary-next-turn")
        assert context.host.work.list_provider_inputs(first["work_item_id"]) == rows
        assert len(context.host.work.list_attempts(first["work_item_id"])) == 1
        assert context.host.work.get_project_by_path(
            context.host.work.get_work_item(first["work_item_id"]).workspace_path) is None
    finally:
        adapter.release.set()
        await context.finish()
        await handler.drain_inputs()
