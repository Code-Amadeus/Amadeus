"""Pi's native JSONL protocol; execution authority stays in ProviderRuntime.

Pi is not JSON-RPC/ACP: commands and replies use a `type` field. The official
Node CLI owns sessions and extensions; this client owns only stdio framing,
response correlation and process disposal. It never resends a command.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
from typing import Any
from uuid import uuid4


class PiTransportError(RuntimeError):
    pass


class PiCommandRejected(RuntimeError):
    """Pi explicitly refused a command, before acceptance."""


class PiRpcClient:
    def __init__(self, command: list[str], *, cwd: str, env: dict[str, str], timeout: float = 30):
        self.command, self.cwd, self.env, self.timeout = command, cwd, env, timeout
        self.process: asyncio.subprocess.Process | None = None
        self.events: asyncio.Queue = asyncio.Queue()
        self._pending: dict[str, asyncio.Future] = {}
        self._tasks: list[asyncio.Task] = []
        self._write_lock = asyncio.Lock()
        self._failure: PiTransportError | None = None

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            *self.command, cwd=self.cwd, env=self.env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=4 * 1024 * 1024,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        )
        self._tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._drain_stderr())]

    async def send(self, message: dict[str, Any]) -> None:
        async with self._write_lock:
            if self._failure:
                raise self._failure
            if self.process is None or self.process.returncode is not None:
                raise PiTransportError("Pi process is unavailable")
            try:
                self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionError) as exc:
                raise PiTransportError("Pi input connection closed") from exc

    async def request(self, kind: str, **params: Any) -> dict[str, Any]:
        key = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        try:
            await self.send({**params, "type": kind, "id": key})
            try:
                response = await asyncio.wait_for(future, self.timeout)
            except TimeoutError as exc:
                raise PiTransportError(f"Pi {kind} acknowledgement timed out") from exc
            if response.get("command") != kind:
                raise PiTransportError("Pi response command does not match its request")
            if response.get("success") is not True:
                # Native messages may contain credentials/provider payloads.
                raise PiCommandRejected(f"Pi rejected {kind}")
            return response.get("data") or {}
        finally:
            self._pending.pop(key, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def next_event(self) -> dict[str, Any]:
        value = await self.events.get()
        if isinstance(value, Exception):
            raise value
        return value

    async def _read(self) -> None:
        try:
            while raw := await self.process.stdout.readline():
                # StreamReader.readline splits on LF only (not U+2028/U+2029).
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("expected object")
                if value.get("type") == "response":
                    future = self._pending.get(value.get("id"))
                    if future is not None and not future.done():
                        future.set_result(value)
                else:
                    self.events.put_nowait(value)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail("Pi emitted an invalid or interrupted protocol stream")
        else:
            self._fail("Pi process closed its protocol stream")

    def _fail(self, message: str) -> None:
        self._failure = PiTransportError(message)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(self._failure)
        self.events.put_nowait(self._failure)

    async def _drain_stderr(self) -> None:
        # Drain without forwarding arbitrary native diagnostics or secrets.
        while await self.process.stderr.read(8192):
            pass

    async def close(self) -> None:
        if self.process is not None and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
