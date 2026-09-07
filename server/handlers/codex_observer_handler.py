"""Local Codex observation on the authenticated Amadeus backend."""
import asyncio
import logging
from pathlib import Path

from server.codex_desktop_observer import CodexDesktopObserver
from server.event_bus import bus
from server.ws_handler import RequestHandler

logger = logging.getLogger(__name__)


class CodexObserverHandler(RequestHandler):
    methods = ["companion.tasks"]

    def __init__(self, thread_ids: list[str] | None, codex_home: Path):
        self.observer = CodexDesktopObserver(codex_home, thread_ids) if thread_ids != [] else None
        self.snapshot = {"tasks": []}
        self.job = None

    def start(self):
        if self.observer:
            self.job = asyncio.create_task(self.watch())

    async def watch(self):
        while True:
            snapshot = await asyncio.to_thread(self.observer.poll)
            if snapshot != self.snapshot:
                previous_keys = {task["key"] for task in self.snapshot["tasks"]}
                for task in snapshot["tasks"]:
                    if task["announce"] and task["key"] not in previous_keys:
                        logger.info("codex notification observed task=%s phase=%s", task["id"], task["phase"])
                self.snapshot = snapshot
                await bus.emit("companion.tasks", snapshot)
            await asyncio.sleep(0.25)

    async def handle(self, method, params):
        return self.snapshot

    async def close(self):
        if self.job:
            self.job.cancel()
            try:
                await self.job
            except asyncio.CancelledError:
                pass
