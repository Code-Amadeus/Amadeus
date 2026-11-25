import asyncio
import gc
import logging
import weakref

from config import background_tasks
from config.background_tasks import spawn_background


async def test_task_is_kept_alive_until_it_finishes():
    loop = asyncio.get_running_loop()
    waiting = loop.create_future()

    async def work():
        release = loop.create_future()
        waiting.set_result(weakref.ref(release))
        await release

    task_ref = weakref.ref(spawn_background(work(), name="kept-alive"))
    release_ref = await waiting
    # Only the registry keeps the suspended task and its waiter reachable.
    gc.collect()
    task = task_ref()
    release = release_ref()
    assert task is not None
    assert release is not None

    release.set_result(None)
    await asyncio.wait_for(task, 1)
    assert task not in background_tasks._BACKGROUND_TASKS


async def test_failure_is_logged_with_task_name(caplog):
    async def boom():
        raise ValueError("broken worker")

    with caplog.at_level(logging.ERROR, logger="config.background_tasks"):
        task = spawn_background(boom(), name="failing-worker")
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert "failing-worker" in caplog.text
    assert "broken worker" in caplog.text


async def test_shared_future_failure_is_reported_once(caplog):
    future = asyncio.get_running_loop().create_future()
    error = ValueError("broken subscriber")
    with caplog.at_level(logging.ERROR, logger=background_tasks.__name__):
        background_tasks.track_background(future)
        background_tasks.track_background(future)
        future.set_exception(error)
        await asyncio.sleep(0)

    records = [record for record in caplog.records if record.name == background_tasks.__name__]
    assert len(records) == 1
    assert records[0].exc_info[1] is error
    assert future not in background_tasks._BACKGROUND_TASKS


async def test_cancellation_is_not_logged(caplog):
    async def forever():
        await asyncio.Event().wait()

    with caplog.at_level(logging.ERROR, logger="config.background_tasks"):
        task = spawn_background(forever(), name="cancelled-worker")
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert caplog.text == ""
    assert task not in background_tasks._BACKGROUND_TASKS


async def test_explicit_loop_is_used():
    loop = asyncio.get_running_loop()

    async def value():
        return 7

    task = spawn_background(value(), loop=loop)
    assert task.get_loop() is loop
    assert await task == 7
