"""Shared fixture support for suites that run real work against a real workspace.

Not named ``test_*`` on purpose: ``tools/run_tests.py`` globs ``test_*.py``, so
this file is importable support rather than a collected suite.
"""

from __future__ import annotations

import asyncio
from typing import Any


async def settle_provider_runs(runtime: Any, *, timeout: float = 10.0) -> None:
    """Wait until started provider runs have finished their terminal bookkeeping.

    ``ProviderRuntime.cancel`` returns once the adapter confirms the request and
    the run task has been cancelled -- not once that task has finished writing
    its terminal facts. That tail still inspects the workspace with git, so a
    fixture torn down the moment ``cancel`` returns races a live ``git status``
    against the directory it is about to delete. On Windows the losing side is
    the fixture: a running child holds its working directory open and the
    temporary tree cannot be removed at all.

    Waiting here keeps that race out of every suite that cancels a run. It does
    not paper over the production question of who owns that tail; it only makes
    the test's own teardown ordered.
    """

    handles = [
        record.task_handle
        for record in list(getattr(runtime, "_runs", {}).values())
        if getattr(record, "task_handle", None) is not None
        and not record.task_handle.done()
    ]
    if not handles:
        return
    await asyncio.wait(handles, timeout=timeout)
