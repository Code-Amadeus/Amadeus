"""New user turns remain usable across source replay and Provider input gates.

Real ChatHandler, durable admission/effect ledger and ProviderRuntime; scripted
semantic decisions and an in-memory native adapter. These are not LLM accuracy
or native-provider acceptance tests.
"""
import asyncio
from dataclasses import replace
import json

import pytest

from agent_host.provider_types import ProviderInputDelivery
from core.turn_coordinator import TurnAuthorityError
from test_cooperative_context_recovery import host_factory as host_factory


def configure_messages(host, *, append=True):
    host.adapter.manifest = replace(host.adapter.manifest, capabilities=replace(
        host.adapter.manifest.capabilities, append_input=append))
    host.runtime.register(host.adapter)
    inputs, frames = [], []

    async def deliver(run_id, text):
        inputs.append((run_id, text))
        return ProviderInputDelivery("delivered")

    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        frames.append(frame)
        if frame["source_kind"] != "user":
            return "確認したわ。"
        text = frame["current"]["text"]
        action = None if text == "谢谢，先聊聊天。" else {
            "op": "interrupt" if text == "先停下。" else "send"}
        return json.dumps({"action": action, "say": "好的。"}, ensure_ascii=False)

    host.adapter.append_input = deliver
    host.loop.query = query
    host.adapter.release.clear()
    return inputs, frames


async def test_same_words_new_turn_is_delivered_but_transport_replay_is_not(host_factory):
    host = host_factory()
    inputs, frames = configure_messages(host)
    try:
        first = await host.send("检查一次当前状态。", "start")
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        one = await host.send("再检查一次。", "repeat-one")
        replay = await host.send("再检查一次。", "repeat-one")
        two = await host.send("再检查一次。", "repeat-two")
        assert replay["status"] == "replayed"
        assert one["state"] == two["state"] == "delivered"
        assert [run for run, _ in inputs] == [first["run_id"]] * 2
        assert len(host.adapter.requests) == 1
        assert len([f for f in frames if f["source_kind"] == "user"
            and f["current"]["text"] == "再检查一次。"]) == 2
        assert (await host.send("谢谢，先聊聊天。", "chat"))["state"] == "no_action"
    finally:
        host.adapter.release.set()
        await host.close()


@pytest.mark.parametrize("first_state", ["delivered", "rejected", "unknown"])
async def test_input_receipt_gate_does_not_poison_chat_new_input_or_stop(host_factory, first_state):
    host = host_factory()
    inputs, _ = configure_messages(host)

    async def deliver(run_id, text):
        inputs.append((run_id, text))
        return ProviderInputDelivery(first_state if len(inputs) == 1 else "delivered",
            reason="injected-first-receipt")

    host.adapter.append_input = deliver
    try:
        first = await host.send("开始检查。", "start")
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        result = await host.send("补充第一条。", "input-one")
        assert result["state"] == first_state
        assert (await host.send("补充第一条。", "input-one"))["status"] == "replayed"
        assert len(inputs) == 1
        assert (await host.send("谢谢，先聊聊天。", "chat"))["state"] == "no_action"
        later = await host.send("补充另一条。", "input-two")
        assert later["state"] == "delivered"
        assert [run for run, _ in inputs] == [first["run_id"]] * 2
        assert (await host.send("先停下。", "stop"))["state"] == "stopped"
        assert host.runtime.get_run(first["run_id"]).status == "cancelled"
        assert len(host.adapter.requests) == 1
    finally:
        host.adapter.release.set()
        await host.close()


async def test_missing_append_capability_refuses_only_that_action(host_factory):
    host = host_factory()
    inputs, _ = configure_messages(host, append=False)
    try:
        first = await host.send("开始检查。", "start")
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        rejected = await host.send("补充一条。", "unsupported")
        assert rejected["state"] == "rejected"
        assert not inputs
        assert (await host.send("谢谢，先聊聊天。", "chat"))["state"] == "no_action"
        assert (await host.send("先停下。", "stop"))["state"] == "stopped"
        assert host.runtime.get_run(first["run_id"]).status == "cancelled"
    finally:
        host.adapter.release.set()
        await host.close()


async def test_changed_text_under_same_identity_refuses_only_the_replay(host_factory):
    host = host_factory()
    inputs, _ = configure_messages(host)
    try:
        await host.send("开始检查。", "start")
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        with pytest.raises(TurnAuthorityError):
            await host.send("这是另一条要求。", "start")
        assert (await host.send("这是另一条要求。", "fresh"))["state"] == "delivered"
        assert len(inputs) == 1
        assert (await host.send("谢谢，先聊聊天。", "chat"))["state"] == "no_action"
    finally:
        host.adapter.release.set()
        await host.close()


async def test_pending_append_allows_chat_and_stop_before_receipt_returns(host_factory):
    host = host_factory()
    inputs, _ = configure_messages(host)
    entered, release = asyncio.Event(), asyncio.Event()

    async def deliver(run_id, text):
        inputs.append((run_id, text))
        entered.set()
        await release.wait()
        return ProviderInputDelivery("unknown", reason="lost-ack")

    host.adapter.append_input = deliver
    pending = None
    try:
        first = await host.send("开始检查。", "start")
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        pending = asyncio.create_task(host.send("补充一条。", "pending"))
        await asyncio.wait_for(entered.wait(), 2)
        assert (await asyncio.wait_for(host.send("谢谢，先聊聊天。", "chat"), 2))["state"] == "no_action"
        stopped = await asyncio.wait_for(host.send("先停下。", "stop"), 2)
        assert stopped["state"] == "stopped"
        assert host.runtime.get_run(first["run_id"]).status == "cancelled"
        assert not release.is_set() and len(inputs) == 1
    finally:
        release.set()
        host.adapter.release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await host.close()
