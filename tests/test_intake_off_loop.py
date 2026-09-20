"""Control-plane intake runs off the event loop without losing its guarantees.

The intake hook (`WorkLedgerCoordinator.prepare_request`) blocks on subprocess
IO once worktree isolation is on: `locus api workspaces ensure` checks out a git
worktree. Moving it to a worker thread costs two things that inline execution on
the loop provided for free, and both are covered here:

- `bus.emit_now` reached subscribers only via `asyncio.get_running_loop()`, so a
  worker thread dropped every event silently;
- the hook is a check-then-write sequence, so concurrent starts must not
  interleave.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderRunRequest, ProviderRunResult
from server.event_bus import EventBus


class IdleAdapter:
    provider_id = "idle"

    async def run(self, request, run_id, emit):
        return ProviderRunResult(status="done", result="idle")

    async def cancel(self, run_id):
        return None


def test_emit_now_reaches_subscribers_from_a_worker_thread() -> None:
    async def run() -> None:
        bus = EventBus()
        bus.bind_loop()
        seen: list[tuple[str, dict]] = []
        emitting_thread: list[str] = []

        async def on_event(method: str, params: dict) -> None:
            seen.append((method, params))

        bus.on("work.updated", on_event)

        def emit_off_loop() -> None:
            emitting_thread.append(threading.current_thread().name)
            bus.emit_now("work.updated", {"reason": "attempt.created"})

        await asyncio.to_thread(emit_off_loop)
        await asyncio.sleep(0.05)

        assert emitting_thread and emitting_thread[0] != threading.current_thread().name
        assert seen == [("work.updated", {"reason": "attempt.created"})]

    asyncio.run(run())


def test_emit_now_without_a_bound_loop_drops_instead_of_raising() -> None:
    bus = EventBus()
    seen: list[str] = []

    async def on_event(method: str, params: dict) -> None:
        seen.append(method)

    bus.on("work.updated", on_event)
    # No running loop and no bind_loop(): the degraded path must stay quiet
    # rather than take down the calling thread.
    bus.emit_now("work.updated", {"reason": "no-loop"})
    assert seen == []


def test_blocking_intake_does_not_stall_the_event_loop() -> None:
    async def run() -> None:
        runtime = ProviderRuntime()
        runtime.register(IdleAdapter())
        block_s = 0.3

        def prepare(request: ProviderRunRequest) -> ProviderRunRequest:
            # Stands in for `locus api workspaces ensure` + `git worktree add`.
            time.sleep(block_s)
            return request

        runtime.set_request_preparer(prepare)

        ticks = 0
        stop = asyncio.Event()

        async def ticker() -> None:
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        pulse = asyncio.create_task(ticker())
        record = await runtime.start(
            ProviderRunRequest(provider="idle", task="blocking intake")
        )
        stop.set()
        await pulse
        assert record.task_handle is not None
        await record.task_handle

        # Inline on the loop this would have been ~1 tick; off-loop the pump
        # keeps running for the whole block.
        assert ticks >= 10, f"event loop stalled during intake (ticks={ticks})"
        runtime.set_request_preparer(None)

    asyncio.run(run())


def test_concurrent_intakes_do_not_interleave() -> None:
    async def run() -> None:
        runtime = ProviderRuntime()
        runtime.register(IdleAdapter())
        trace: list[str] = []

        def prepare(request: ProviderRunRequest) -> ProviderRunRequest:
            # A check-then-write critical section: the guard reads state, then
            # the commit writes it. An interleaved reader would see stale state.
            trace.append(f"enter:{request.task}")
            time.sleep(0.05)
            trace.append(f"exit:{request.task}")
            return request

        runtime.set_request_preparer(prepare)
        records = await asyncio.gather(
            runtime.start(ProviderRunRequest(provider="idle", task="a")),
            runtime.start(ProviderRunRequest(provider="idle", task="b")),
        )
        for record in records:
            assert record.task_handle is not None
            await record.task_handle

        assert len(trace) == 4
        assert trace[0].startswith("enter:") and trace[1] == trace[0].replace("enter:", "exit:")
        assert trace[2].startswith("enter:") and trace[3] == trace[2].replace("enter:", "exit:")
        runtime.set_request_preparer(None)

    asyncio.run(run())


def test_async_preparer_and_sync_failures_keep_their_contract() -> None:
    async def run() -> None:
        runtime = ProviderRuntime()
        runtime.register(IdleAdapter())

        async def prepare_async(request: ProviderRunRequest) -> ProviderRunRequest:
            request.metadata["display_task"] = "awaited"
            return request

        runtime.set_request_preparer(prepare_async)
        record = await runtime.start(
            ProviderRunRequest(provider="idle", task="async intake")
        )
        assert record.task_handle is not None
        await record.task_handle
        assert record.task == "awaited"

        class Refused(RuntimeError):
            pass

        def prepare_refusing(request: ProviderRunRequest) -> ProviderRunRequest:
            # R11: ensure failure refuses the run; the caller must still see it.
            raise Refused("workspace ensure failed (worktree_failed)")

        runtime.set_request_preparer(prepare_refusing)
        try:
            await runtime.start(ProviderRunRequest(provider="idle", task="refused"))
        except Refused as exc:
            assert "worktree_failed" in str(exc)
        else:
            raise AssertionError("a refusing intake must propagate to the caller")

        # A refused intake must not leave a run record behind.
        assert all(record.task != "refused" for record in runtime._runs.values())
        runtime.set_request_preparer(None)

    asyncio.run(run())


def _main() -> None:
    test_emit_now_reaches_subscribers_from_a_worker_thread()
    test_emit_now_without_a_bound_loop_drops_instead_of_raising()
    test_blocking_intake_does_not_stall_the_event_loop()
    test_concurrent_intakes_do_not_interleave()
    test_async_preparer_and_sync_failures_keep_their_contract()
    print("ok: control-plane intake runs off-loop, serialized, and still emits")


if __name__ == "__main__":
    _main()
