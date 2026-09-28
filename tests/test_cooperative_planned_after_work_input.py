"""An appended live task requirement shares input delivery and AUIP completion."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from agent_host.provider_types import ProviderInputDelivery
from server.auip_bundle_validation import finalize_staged_auip_web_bundle
from server.event_bus import bus
from server.handlers.work_ledger_handler import WorkLedgerHandler
from server.protocol import Method
from test_auip_bundle_validation import _bundle
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_targets import catalog_candidate
from test_cooperative_planned_work import (
    configure_professional_planner, planned, send,
)
from test_cooperative_planner_after_work import install_after_work
from test_cooperative_planned_work_batch import multi_plan


@pytest.mark.parametrize("delivery", ["delivered", "rejected", "unknown"])
async def test_live_amend_after_work_waits_for_its_input_and_verified_app(
        pending_host, tmp_path, delivery):
    context = pending_host
    adapter = context.host.adapter
    adapter.manifest = replace(adapter.manifest,
        capabilities=replace(adapter.manifest.capabilities, append_input=True))
    delivery_started, release_delivery = asyncio.Event(), asyncio.Event()
    inputs = []

    async def append_input(run_id, text):
        inputs.append((run_id, text))
        delivery_started.set()
        await release_delivery.wait()
        return ProviderInputDelivery(delivery)

    adapter.append_input = append_input
    context.host.runtime.register(adapter)
    handler = WorkLedgerHandler(context.host.coordinator,
        provider_input=context.host.runtime.append_input)
    context.manager.configure_work(context.host.control, context.host.executor,
        input_request=handler.submit_input)
    original_run = adapter.run

    async def author_app(request, run_id, emit):
        result = await original_run(request, run_id, emit)
        root = Path(request.cwd)
        assets = _bundle(root)
        manifest = Path(__file__).resolve().parents[1] / (
            "examples/auip-2048/auip.manifest.json")
        (root / "auip.manifest.json").write_bytes(manifest.read_bytes())
        finalize_staged_auip_web_bundle(root, materialized_files=tuple(assets))
        return result

    adapter.run = author_app
    create = "做个计数器吧。"
    text = "计数器再加个清零按钮，做完打开我们试试。"
    clause = "计数器再加个清零按钮"
    plans = {"create": planned(
        context.manager.provider, create, create, "execute", one_off=True)}

    async def planner(_ingress, turn_id, *_args):
        return plans[turn_id]

    configure_professional_planner(context, planner, work_texts={create, text})
    adapter.release.clear()
    try:
        first = await send(context, create, "create")
        await asyncio.wait_for(adapter.started.wait(), 3)
        await context.host.coordinator.drain_provider_facts()
        candidate = catalog_candidate(context, "work_item", first["work_item_id"])
        launch, events, _ = await install_after_work(context, tmp_path)
        plans["append-open"] = planned(
            context.manager.provider, text, clause, "amend", candidate)
        bus.on(Method.WORK_INPUT_UPDATED, launch.on_work_updated)
        try:
            result = await send(context, text, "append-open")
            assert result["state"] == "work_auip_batch_started", result
            assert result["work"]["state"] == "work_input_accepted"
            assert result["work"]["work_item_id"] == first["work_item_id"]
            await asyncio.wait_for(delivery_started.wait(), 3)
            rows = context.host.work.list_provider_inputs(first["work_item_id"])
            assert len(rows) == 1 and rows[0]["state"] == "unknown"
            attrs = context.manager.auip_router.await_args.args[0]
            assert attrs["_host_work_input_id"] == rows[0]["input_id"]
            assert attrs["_host_active_work_attempt_ids"] == (first["attempt_id"],)
            adapter.release.set()
            await context.finish()
            await launch.on_work_updated(Method.WORK_UPDATED, {})
            assert not any(method == Method.AUIP_LAUNCH_REQUESTED for method, _ in events)
            release_delivery.set()
            await handler.drain_inputs()
            opened = [payload for method, payload in events
                if method == Method.AUIP_LAUNCH_REQUESTED]
            assert len(opened) == (1 if delivery == "delivered" else 0)
            assert bool(launch._deferred) == (delivery == "unknown")
            assert adapter.calls == 1
            assert inputs == [(first["run_id"], clause)]
            assert len(context.host.work.list_attempts(first["work_item_id"])) == 1
            replay = await context.handler.send_text(text, session_id=context.session_id,
                turn_id="append-open")
            assert replay["status"] == "replayed"
            await launch.on_work_updated(Method.WORK_UPDATED, {})
            assert len(inputs) == 1
            assert len([1 for method, _ in events
                if method == Method.AUIP_LAUNCH_REQUESTED]) == len(opened)
        finally:
            bus.off(Method.WORK_INPUT_UPDATED, launch.on_work_updated)
    finally:
        adapter.release.set()
        release_delivery.set()
        await context.finish()
        await handler.drain_inputs()


async def test_after_work_does_not_guess_between_live_input_and_new_work(pending_host, tmp_path):
    context = pending_host
    context.manager.work_input = WorkLedgerHandler(context.host.coordinator,
        provider_input=context.host.runtime.append_input).submit_input
    create = "做个计数器吧。"
    plans = {"create": planned(context.manager.provider, create, create,
        "execute", one_off=True)}
    amend, new = "计数器加个清零按钮。", "再做个便签。"
    text = amend + new + "完成后打开。"

    async def planner(_ingress, turn_id, *_args):
        return plans[turn_id]

    configure_professional_planner(context, planner, work_texts={create, text})
    context.host.adapter.release.clear()
    try:
        first = await send(context, create, "create")
        await asyncio.wait_for(context.host.adapter.started.wait(), 3)
        await context.host.coordinator.drain_provider_facts()
        candidate = catalog_candidate(context, "work_item", first["work_item_id"])
        plans["ambiguous-dependency"] = multi_plan(context.manager.provider, text, (
            {"clause": amend, "intent": "amend", "candidates": (candidate,)},
            {"clause": new, "intent": "execute", "extra": {"one_off": True}},
        ))
        launch, events, _ = await install_after_work(context, tmp_path)
        result = await send(context, text, "ambiguous-dependency")
        assert result["state"] == "rejected"
        assert result["reason"] == "planned_after_work_requires_one_mutation"
        assert not launch._deferred
        assert not any(method == Method.AUIP_LAUNCH_REQUESTED for method, _ in events)
        assert context.host.adapter.calls == 1
        assert len(context.host.work.list_work_items()) == 1
        assert context.host.work.list_provider_inputs(first["work_item_id"]) == []
    finally:
        context.host.adapter.release.set()
        await context.finish()
