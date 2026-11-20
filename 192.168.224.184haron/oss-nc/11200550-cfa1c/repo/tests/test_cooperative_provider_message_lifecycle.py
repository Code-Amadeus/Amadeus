"""Accepted Provider message continuations retain the existing input owner."""

import asyncio
from dataclasses import replace
import json

from agent_host.provider_types import ProviderInputDelivery
from core.turn_coordinator import get_turn_coordinator
from test_cooperative_context_recovery import CooperativeWorkFixture
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_work import planned


# These integration fixtures persist real SQLite and Session files. Bound a
# deadlock, not CI disk latency: independence is asserted while append remains
# explicitly gated, rather than by requiring chat completion within four seconds.
STREAM_DEADLOCK_TIMEOUT = 30


def configure(context, tmp_path, *, planner, query):
    adapter = CooperativeWorkFixture()
    adapter.provider_id = context.manager.provider
    adapter.manifest = replace(adapter.manifest, provider_id=adapter.provider_id,
        capabilities=replace(adapter.manifest.capabilities,
            task_kinds=("general", "workspace_mutation"), append_input=True))
    context.host.runtime.register(adapter)
    original_prepare = context.host.coordinator.prepare_request

    def prepare(request, run_id, intake_authority=None):
        if getattr(intake_authority, "kind", "") == "cooperative_provider_effect":
            return context.manager.prepare_runtime_request(
                request, run_id, intake_authority)
        return original_prepare(request, run_id, intake_authority)

    context.host.runtime.set_request_preparer(prepare)
    def allocate(_label, child_id):
        workspace = tmp_path / child_id
        workspace.mkdir()
        return workspace
    context.manager.allocate = allocate
    context.manager.work_planner = planner
    context.manager.query = query
    return adapter


async def submit(context, text, turn_id):
    accepted = await context.handler.send_text(text,
        session_id=context.session_id, turn_id=turn_id)
    assert accepted["status"] == "ok"
    await asyncio.wait_for(context.handler._stream_task, STREAM_DEADLOCK_TIMEOUT)
    return context.manager.ingresses[context.session_id].receipts[turn_id]


async def test_accepted_provider_append_survives_new_chat_and_replays_once(
        pending_host, tmp_path):
    context = pending_host
    seed, append, later = "先给我讲讲这个算法。", "后面多举几个例子吧。", "今天真累，休息一下。"
    planner_calls = []

    async def planner(_ingress, turn_id, receipt, _admission):
        planner_calls.append(turn_id)
        return planned(context.manager.provider, receipt["text"], receipt["text"],
            "message", _host_workspace_access="none")

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] != "user":
            return "送信結果を確認したわ。"
        action = {"op":"send"} if frame["current"]["text"] in {seed, append} else None
        return json.dumps({"action":action,
            "say":"担当に送るわ。" if action else "休みましょう。"}, ensure_ascii=False)

    adapter = configure(context, tmp_path, planner=planner, query=query)
    adapter.release.clear()
    first = await submit(context, seed, "provider-seed")
    assert first["state"] == "started"
    context.manager.ingresses[context.session_id].loop.task_contexts = None
    entered, release_append, append_cancelled = (
        asyncio.Event(), asyncio.Event(), asyncio.Event())
    inputs = []

    async def pending_append(run_id, text):
        inputs.append((run_id, text))
        entered.set()
        try:
            await release_append.wait()
        except asyncio.CancelledError:
            append_cancelled.set()
            raise
        return ProviderInputDelivery("delivered")

    adapter.append_input = pending_append
    await context.handler.send_text(append, session_id=context.session_id,
        turn_id="provider-append")
    old_stream = context.handler._stream_task
    await asyncio.wait_for(entered.wait(), 3)
    admission = context.manager.ledger.find_admission(
        "chat:" + context.session_id, "provider-append")
    assert admission is not None and admission["plan_id"]

    await context.handler.send_text(later, session_id=context.session_id,
        turn_id="later-chat")
    await asyncio.wait_for(context.handler._stream_task, 3)
    assert context.manager.ingresses[context.session_id].receipts[
        "later-chat"]["state"] == "no_action"
    assert not append_cancelled.is_set()

    release_append.set()
    await asyncio.gather(old_stream, return_exceptions=True)
    stored = context.manager.ingresses[context.session_id].loop._inputs[
        "provider-append"][1]
    result = await asyncio.wait_for(asyncio.shield(stored), 3)
    assert result["state"] == "delivered"
    assert len(inputs) == 1
    replay = await context.handler.send_text(append,
        session_id=context.session_id, turn_id="provider-append")
    assert replay["status"] == "replayed"
    assert len(inputs) == 1 and planner_calls == ["provider-seed", "provider-append"]
    adapter.release.set()
    await context.finish()


async def test_expired_source_waiting_for_foreground_starts_no_provider(
        pending_host, tmp_path):
    context = pending_host
    old, later = "新しい確認を始めて。", "やっぱり雑談だけ。"
    planner_started, release_planner = asyncio.Event(), asyncio.Event()

    async def planner(_ingress, _turn_id, receipt, _admission):
        planner_started.set()
        await release_planner.wait()
        return planned(context.manager.provider, receipt["text"], receipt["text"],
            "message", _host_workspace_access="none")

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        text = frame.get("current", {}).get("text")
        return json.dumps({"action":{"op":"send"} if text == old else None,
            "say":"確認するわ。"}) if frame["source_kind"] == "user" else "失敗したわ。"

    adapter = configure(context, tmp_path, planner=planner, query=query)
    await context.handler.send_text(old, session_id=context.session_id,
        turn_id="waiting-provider")
    old_stream = context.handler._stream_task
    await asyncio.wait_for(planner_started.wait(), 3)
    loop = context.manager.ingresses[context.session_id].loop
    await loop._foreground.acquire()
    try:
        release_planner.set()
        for _ in range(100):
            task = loop._inputs["waiting-provider"][1]
            if task.get_name() == "provider-message:waiting-provider":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Provider continuation was not handed off")
        owner = get_turn_coordinator()
        snapshot = owner.snapshot()
        owner.open_turn(turn_id="replacement-fence",
            local_next_epoch=snapshot["epochs"]["chat"] + 1,
            session_id=context.session_id, source="test_replacement")
    finally:
        loop._foreground.release()
    await asyncio.gather(old_stream, return_exceptions=True)
    await asyncio.gather(loop._inputs["waiting-provider"][1],
        return_exceptions=True)
    assert adapter.requests == []
    assert context.host.work.list_work_items() == []
    admission = context.manager.ledger.find_admission(
        "chat:" + context.session_id, "waiting-provider")
    assert admission is not None and admission["plan_id"] is None
    await context.handler.send_text(later, session_id=context.session_id,
        turn_id="later-chat")
    await asyncio.wait_for(context.handler._stream_task, 3)
    assert context.manager.ingresses[context.session_id].receipts[
        "later-chat"]["state"] == "no_action"


async def test_pending_provider_input_does_not_block_independent_work(pending_host, tmp_path):
    context = pending_host
    seed, append, independent = "先解释算法。", "继续举例。", "另外独立生成一份报告。"
    async def planner(ingress, turn_id, receipt, admission):
        return planned(context.manager.provider, receipt["text"], receipt["text"],
            "execute" if turn_id == "independent" else "message",
            _host_workspace_access="write" if turn_id == "independent" else "none",
            **({"one_off": True} if turn_id == "independent" else {}))
    async def query(messages, **kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] != "user":
            return "收到。"
        return json.dumps({"action": {"op": "work" if frame["current"]["text"] == independent else "send"}, "say": "收到。"})
    adapter = configure(context, tmp_path, planner=planner, query=query)
    adapter.release.clear()
    entered, release_append, append_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_stream = None
    async def pending_append(run_id, text):
        entered.set()
        try:
            await release_append.wait()
            return ProviderInputDelivery("unknown", reason="injected-lost-ack")
        finally:
            append_finished.set()
    try:
        first = await submit(context, seed, "seed")
        context.manager.ingresses[context.session_id].loop.task_contexts = None
        adapter.append_input = pending_append
        await context.handler.send_text(append, session_id=context.session_id, turn_id="pending")
        original_stream = context.handler._stream_task
        await asyncio.wait_for(entered.wait(), 3)
        second = await submit(context, independent, "independent")
        assert second["state"] == "work_started"
        assert second["run_id"] != first["run_id"]
        assert not release_append.is_set()
        assert not append_finished.is_set()
        assert len(context.host.work.list_work_items()) == 1
        assert context.host.runtime.get_run(first["run_id"]).status == "running"
    finally:
        release_append.set()
        adapter.release.set()
        if original_stream is not None:
            await asyncio.gather(original_stream, return_exceptions=True)
        await context.finish()
