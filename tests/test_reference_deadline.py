"""Reference timer wakeups cannot shorten a precise deadline or duplicate work."""
import asyncio
import threading

import pytest

from server import reference_deadline as timing
from server import memory_service
from server.memory_service import MemoryRequestError, _SingleFlight


async def test_early_timer_wakeup_keeps_the_same_job_until_its_actual_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(timing, "perf_counter", lambda:clock[0])
    release = asyncio.Event()
    calls, waits = [], []
    async def work():
        calls.append("one job")
        await release.wait()
        return "timely result"
    original_wait = asyncio.wait
    async def early_wait(futures, *, timeout):
        task, = futures
        waits.append((task, timeout))
        if len(waits) == 1:
            await asyncio.sleep(0)
            clock[0] = 100.04
            assert not task.done() and not task.cancelling()
            return set(), {task}
        clock[0] = 100.14
        release.set()
        return await original_wait((task,), timeout=1)
    monkeypatch.setattr(timing.asyncio, "wait", early_wait)
    assert await timing.await_until(work(), 100.15) == "timely result"
    assert calls == ["one job"] and waits[0][0] is waits[1][0]
    assert [entry[1] for entry in waits] == pytest.approx([.15, .11])


@pytest.mark.parametrize("completed_before_wakeup", [False, True])
async def test_expired_deadline_rejects_completed_or_cancellation_suppressed_result(monkeypatch, completed_before_wakeup):
    clock = [10.0]
    monkeypatch.setattr(timing, "perf_counter", lambda:clock[0])
    release = asyncio.Event()
    calls, cancelled = [], []
    async def work():
        calls.append("one job")
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
        return "late result"
    original_wait = asyncio.wait
    async def expired_wait(futures, *, timeout):
        task, = futures
        await asyncio.sleep(0)
        clock[0] = 10.16
        if completed_before_wakeup:
            release.set()
            return await original_wait((task,), timeout=1)
        return set(), {task}
    monkeypatch.setattr(timing.asyncio, "wait", expired_wait)
    with pytest.raises(TimeoutError):
        await timing.await_until(work(), 10.15)
    assert calls == ["one job"]
    assert cancelled == ([] if completed_before_wakeup else [True])


@pytest.mark.parametrize("bounded", [False, True])
async def test_external_cancel_propagates_even_when_the_child_returns_a_result(bounded):
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            return "cancelled result"
    waiter = asyncio.create_task(timing.await_until(work(), timing.deadline_now() + 1 if bounded else None))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert cancelled.is_set()


async def test_already_expired_deadline_does_not_start_a_new_job():
    calls = []
    async def work():
        calls.append(True)
    with pytest.raises(TimeoutError):
        await timing.await_until(work(), timing.deadline_now() - 1)
    assert calls == []


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
@pytest.mark.parametrize("mutation", [False, True])
async def test_single_flight_keeps_late_job_owned_and_publishes_once_without_a_queue(stop, mutation):
    lane = _SingleFlight("reference-deadline-owner")
    started, release = threading.Event(), threading.Event()
    calls, published = [], []
    def work():
        calls.append("first")
        started.set()
        assert release.wait(3)
        return "committed result"
    pending = asyncio.create_task(lane.run(work, timeout=.05, mutation=mutation, on_completed=published.append))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        if stop == "cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            with pytest.raises(MemoryRequestError, match="outcome_unknown" if mutation else "busy"):
                await pending
        assert lane._future is not None and published == []
        with pytest.raises(MemoryRequestError, match="busy"):
            await lane.run(lambda:calls.append("queued"))
        assert calls == ["first"]
        release.set()
        while lane._future is not None:
            await asyncio.sleep(.001)
        assert published == ["committed result"]
        assert await lane.run(lambda:"next job") == "next job"
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        lane.close()


async def test_priority_queue_and_job_share_one_absolute_deadline(monkeypatch):
    lane = _SingleFlight("reference-priority-deadline")
    started, release = threading.Event(), threading.Event()
    deadlines = []
    original = memory_service.await_until
    async def observed(awaitable, deadline):
        if deadline is not None:
            deadlines.append(deadline)
        return await original(awaitable, deadline)
    monkeypatch.setattr(memory_service, "await_until", observed)
    def occupied():
        started.set()
        assert release.wait(3)
    first = asyncio.create_task(lane.run(occupied, timeout=None))
    priority = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        priority = asyncio.create_task(lane.run(lambda:"valid", timeout=.5, priority=True))
        while not lane._priority_waiter:
            await asyncio.sleep(0)
        release.set()
        await first
        assert await priority == "valid"
        assert len(deadlines) == 2 and deadlines[0] == deadlines[1]
    finally:
        release.set()
        await asyncio.gather(*(task for task in (first, priority) if task is not None), return_exceptions=True)
        lane.close()


async def test_expired_completed_exception_is_consumed_without_unhandled_warning(monkeypatch):
    import gc
    import weakref
    clock = [20.0]
    monkeypatch.setattr(timing, "perf_counter", lambda:clock[0])
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled, owned = [], []
    loop.set_exception_handler(lambda _loop, context:unhandled.append(context))
    original_wait = asyncio.wait
    async def complete_after_clock_cutoff(futures, *, timeout):
        task, = futures
        owned.append(weakref.ref(task))
        result = await original_wait((task,), timeout=1)
        clock[0] = 20.16
        return result
    monkeypatch.setattr(timing.asyncio, "wait", complete_after_clock_cutoff)
    async def failed():
        raise RuntimeError("synthetic late worker exception")
    try:
        with pytest.raises(TimeoutError):
            await timing.await_until(failed(), 20.15)
        gc.collect()
        await asyncio.sleep(0)
        assert owned and owned[0]() is None
        assert unhandled == []
    finally:
        loop.set_exception_handler(previous_handler)


async def test_in_budget_worker_exception_remains_the_callers_exception():
    async def failed():
        raise ValueError("synthetic in-budget exception")
    with pytest.raises(ValueError, match="in-budget"):
        await timing.await_until(failed(), timing.deadline_now() + 1)
