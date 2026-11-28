"""Reference waits share one precise deadline and one owned awaitable."""
import asyncio
from time import perf_counter


def deadline_now():
    """Keep absolute reference deadlines in the high-resolution monotonic clock."""
    return perf_counter()


async def await_until(awaitable, deadline):
    """Treat timer wakeups as advisory; cancel only at the actual deadline."""
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            remaining = None if deadline is None else deadline - deadline_now()
            if remaining is not None and remaining <= 0:
                raise TimeoutError
            done, _ = await asyncio.wait((task,), timeout=remaining)
            if not done:
                # Windows asyncio may wake a timer before its advertised delay.
                # Continue waiting on this same job and unchanged deadline.
                continue
            if deadline is not None and deadline_now() >= deadline:
                raise TimeoutError
            return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif not task.cancelled():
            # A deadline can win after an owned job has already failed. Consume
            # that terminal exception even though its result is ineligible.
            task.exception()
