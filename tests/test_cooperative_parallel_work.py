"""Session conversation ownership is not a single-Work execution lock."""
import asyncio
from dataclasses import replace
import json
from unittest.mock import AsyncMock

import pytest

from agent_host.provider_types import ProviderInputDelivery, ProviderRunResult
from server.handlers.work_ledger_handler import WorkLedgerHandler
from test_cooperative_pending_turn import pending_host as pending_host


@pytest.mark.parametrize("finish_second", [False, True])
async def test_independent_drafts_keep_separate_execution_input_and_stop(pending_host, finish_second):
    context = pending_host
    adapter = context.host.adapter
    gates, cancelled, inputs = {}, set(), []
    started = asyncio.Queue()
    finishing = False
    target = ""
    adapter.manifest = replace(adapter.manifest,
        capabilities=replace(adapter.manifest.capabilities, append_input=True))

    async def run(request, run_id, _emit):
        adapter.calls += 1
        adapter.requests.append({"run_id":run_id, "request":request})
        gates[run_id] = asyncio.Event()
        started.put_nowait(run_id)
        if finishing:
            gates[run_id].set()
        await gates[run_id].wait()
        return ProviderRunResult(status="cancelled" if run_id in cancelled else "done", result="Finished")

    async def cancel(run_id):
        cancelled.add(run_id)
        gates[run_id].set()
        return {"confirmed":True, "cancelled":True}

    async def append(run_id, text):
        inputs.append((run_id, text))
        return ProviderInputDelivery("delivered")

    adapter.run, adapter.cancel, adapter.append_input = run, AsyncMock(side_effect=cancel), append
    context.host.runtime.register(adapter)
    work_handler = WorkLedgerHandler(context.host.executor.coordinator,
        provider_input=context.host.runtime.append_input)
    context.manager.configure_work(context.host.control, context.host.executor,
        input_request=work_handler.submit_input)

    async def query(messages, **_kwargs):
        try:
            frame = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            return json.dumps({"references":["work_item:" + target]})
        if frame["source_kind"] != "user":
            return "受け付けた状態を確認したわ。"
        action = ({"op":"work", "intent":"amend", "target":"便签"}
            if frame["current"]["text"] == "便签再加个清空按钮吧。" else {"op":"work", "intent":"execute"})
        return json.dumps({"action":action, "say":"わかった、進めるわ。"})

    context.manager.query = query

    async def send(text, turn):
        await context.handler.send_text(text, session_id=context.session_id, turn_id=turn)
        await asyncio.wait_for(context.handler._stream_task, 5)
        receipt = context.manager.ingresses[context.session_id].receipts[turn]
        if receipt["state"] == "work_started":
            assert await asyncio.wait_for(started.get(), 3) == receipt["run_id"]
        return receipt

    try:
        first = await send("做个便签页吧。", "memo")
        target = first["work_item_id"]
        second = await send("再做个计时器吧。", "timer")
        assert second["state"] == "work_started", second
        assert first["work_item_id"] != second["work_item_id"]
        assert adapter.requests[0]["request"].cwd != adapter.requests[1]["request"].cwd
        assert all(context.host.runtime.get_run(row["run_id"]).status == "running" for row in (first, second))
        if finish_second:
            gates[second["run_id"]].set()
            await context.host.runtime.get_run(second["run_id"]).task_handle
            await context.host.executor.coordinator.drain_provider_facts()
            assert context.manager.active_work_for_recipient(context.session_id, "") is None
            assert context.host.runtime.get_run(first["run_id"]).status == "running"
        amended = await send("便签再加个清空按钮吧。", "amend-memo")
        await work_handler.drain_inputs()
        assert amended["state"] == "work_input_accepted"
        assert inputs == [(first["run_id"], "便签再加个清空按钮吧。")]
        stopped = await context.manager.abort_turn("memo", context.session_id)
        assert stopped["state"] == "stopped"
        assert context.host.runtime.get_run(second["run_id"]).status == ("done" if finish_second else "running")
        adapter.cancel.assert_awaited_once_with(first["run_id"])
        assert adapter.calls == 2
        assert len(context.host.work.list_work_items()) == 2
        assert all(len(context.host.work.list_attempts(row["work_item_id"])) == 1 for row in (first, second))
    finally:
        finishing = True
        for gate in gates.values():
            gate.set()
        await work_handler.drain_inputs()
        await context.finish()
