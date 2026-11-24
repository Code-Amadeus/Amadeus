import asyncio
import gc
import logging

from config import background_tasks
from config.background_tasks import spawn_background


async def test_task_is_kept_alive_until_it_finishes():
    release = asyncio.Event()
    finished = asyncio.Event()

    async def work():
        await release.wait()
        finished.set()

    spawn_background(work(), name="kept-alive")
    gc.collect()
    assert any(t.get_name() == "kept-alive" for t in background_tasks._BACKGROUND_TASKS)

    release.set()
    await asyncio.wait_for(finished.wait(), 1)
    await asyncio.sleep(0)
    assert not any(t.get_name() == "kept-alive" for t in background_tasks._BACKGROUND_TASKS)


async def test_failure_is_logged_with_task_name(caplog):
    async def boom():
        raise ValueError("broken worker")

    with caplog.at_level(logging.ERROR, logger="config.background_tasks"):
        task = spawn_background(boom(), name="failing-worker")
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert "failing-worker" in caplog.text
    assert "broken worker" in caplog.text


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
