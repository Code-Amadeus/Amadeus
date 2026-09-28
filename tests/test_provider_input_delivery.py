from __future__ import annotations

import asyncio
from copy import deepcopy

from agent_host.provider_contract import ProviderCapabilities, ProviderManifest
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderInputDelivery, ProviderRunRequest, ProviderRunResult


class _InputAdapter:
    def __init__(self, provider_id="input-test", *, supported=True):
        self.provider_id = provider_id
        self.manifest = ProviderManifest(
            provider_id=provider_id,
            display_name=provider_id,
            capabilities=ProviderCapabilities(steering="immediate", append_input=supported),
        )
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.entered = asyncio.Event()
        self.acknowledge = asyncio.Event()
        self.inputs = []
        self.result = ProviderInputDelivery("delivered")

    async def run(self, request, run_id, emit):
        self.started.set()
        await self.finish.wait()
        return ProviderRunResult(status="done", result="done")

    async def append_input(self, run_id, text):
        self.inputs.append((run_id, text))
        self.entered.set()
        await self.acknowledge.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def cancel(self, run_id):
        self.finish.set()
        return {"confirmed": True, "cancelled": True}


async def _start(runtime, adapter):
    runtime.register(adapter)
    run = await runtime.start(ProviderRunRequest(provider=adapter.provider_id, task="original"))
    await asyncio.wait_for(adapter.started.wait(), 2)
    return run


async def _finish(adapter, run):
    adapter.finish.set()
    await asyncio.wait_for(run.task_handle, 2)


def test_append_keeps_each_input_and_run_identity_without_steer_revisions():
    async def scenario():
        runtime = ProviderRuntime()
        adapter = _InputAdapter()
        run = await _start(runtime, adapter)
        original = deepcopy(run.metadata)
        first = asyncio.create_task(runtime.append_input(run.run_id, "Only CPU is available."))
        await asyncio.wait_for(adapter.entered.wait(), 2)
        second = asyncio.create_task(runtime.append_input(run.run_id, "No internet is available."))
        adapter.acknowledge.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert [result.state for result in results] == ["delivered", "delivered"]
        assert adapter.inputs == [
            (run.run_id, "Only CPU is available."),
            (run.run_id, "No internet is available."),
        ]
        assert run.task == "original"
        assert run.metadata == original
        assert len(runtime.list_runs()) == 1
        await _finish(adapter, run)
    asyncio.run(scenario())


def test_replacement_capability_does_not_authorize_append_or_fallback():
    async def scenario():
        runtime = ProviderRuntime()
        adapter = _InputAdapter(supported=False)
        run = await _start(runtime, adapter)
        result = await runtime.append_input(run.run_id, "An independent fact")
        assert result == ProviderInputDelivery("rejected", "append_input_not_supported")
        assert adapter.inputs == []
        assert (await runtime.append_input("missing-run", "fact")).state == "rejected"
        assert (await runtime.append_input(run.run_id, "  ")).state == "rejected"
        await _finish(adapter, run)
    asyncio.run(scenario())


def test_waiting_input_rechecks_the_exact_run_after_it_finishes():
    async def scenario():
        runtime = ProviderRuntime()
        adapter = _InputAdapter()
        run = await _start(runtime, adapter)
        first = asyncio.create_task(runtime.append_input(run.run_id, "first"))
        await asyncio.wait_for(adapter.entered.wait(), 2)
        second = asyncio.create_task(runtime.append_input(run.run_id, "late"))
        await _finish(adapter, run)
        adapter.acknowledge.set()
        delivered, rejected = await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert delivered.state == "delivered"
        assert rejected == ProviderInputDelivery("rejected", "run_not_active")
        assert adapter.inputs == [(run.run_id, "first")]
    asyncio.run(scenario())


def test_unknown_delivery_never_becomes_rejection_or_automatic_resend():
    async def scenario():
        for outcome in (OSError("lost acknowledgement"), {"accepted": True},
                        ProviderInputDelivery("unknown", "native receipt lost")):
            runtime = ProviderRuntime()
            adapter = _InputAdapter()
            adapter.acknowledge.set()
            adapter.result = outcome
            run = await _start(runtime, adapter)
            result = await runtime.append_input(run.run_id, "fact")
            assert result.state == "unknown"
            assert adapter.inputs == [(run.run_id, "fact")]
            assert len(runtime.list_runs()) == 1
            await _finish(adapter, run)
    asyncio.run(scenario())


def test_separate_recipients_do_not_share_an_input_queue():
    async def scenario():
        runtime = ProviderRuntime()
        left, right = _InputAdapter("left"), _InputAdapter("right")
        run_left, run_right = await _start(runtime, left), await _start(runtime, right)
        blocked = asyncio.create_task(runtime.append_input(run_left.run_id, "left fact"))
        await asyncio.wait_for(left.entered.wait(), 2)
        right.acknowledge.set()
        delivered = await asyncio.wait_for(runtime.append_input(run_right.run_id, "right fact"), 2)
        assert delivered.state == "delivered" and not blocked.done()
        assert right.inputs == [(run_right.run_id, "right fact")]
        left.acknowledge.set()
        await asyncio.wait_for(blocked, 2)
        await _finish(left, run_left)
        await _finish(right, run_right)
    asyncio.run(scenario())
