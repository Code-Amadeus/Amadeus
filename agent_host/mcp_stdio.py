"""MCP stdio framing with Host-owned child-process lifetime containment.

The SDK owns JSON-RPC models, negotiation and requests. This transport owns
only pipes and the process tree: a Windows child cannot run before Job
assignment, and closing the Host's Job handle kills all remaining members.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any

import anyio
import mcp_types
from mcp.client._transport import TransportStreams
from mcp.shared.message import SessionMessage

from config.process_tree import ProcessTree
from config.subprocess_environment import isolated_child_environment

if TYPE_CHECKING:
    from agent_host.mcp_connections import McpConnectionSpec

MAX_LINE_BYTES = 8 * 1024 * 1024
_WRITER_FLUSH_SECONDS = 0.5
_EXIT_GRACE_SECONDS = 0.5


class McpStdioError(RuntimeError):
    """A bounded transport diagnostic that never contains raw child output."""


def _spawn(connection: McpConnectionSpec) -> tuple[subprocess.Popen[bytes], ProcessTree]:
    environment = isolated_child_environment(extra=connection.environment)
    command = connection.command
    if not os.path.isabs(command):
        search_path = next((value for key, value in reversed(tuple(environment.items()))
                            if key.upper() == "PATH"), "") if os.name == "nt" else environment.get("PATH", "")
        command = shutil.which(command, path=search_path) or ""
    if not command or not os.path.isfile(command):
        raise McpStdioError("MCP stdio executable is unavailable")
    command = os.path.abspath(command)
    if os.name == "nt" and os.path.splitext(command)[1].lower() in {".bat", ".cmd"}:
        raise McpStdioError("MCP stdio requires a native executable, not a shell script")
    options: dict[str, Any] = {"creationflags": subprocess.CREATE_NO_WINDOW | 0x4} if os.name == "nt" else {"start_new_session": True}
    try:
        process = subprocess.Popen([command, *connection.arguments], cwd=connection.cwd or None,
                                   env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, shell=False, **options)
    except (OSError, ValueError):
        raise McpStdioError("Could not start MCP stdio process") from None
    tree = None
    try:
        tree = ProcessTree(process)
        tree.resume()
        return process, tree
    except BaseException as reason:
        if tree is not None:
            tree.terminate()
        else:
            with suppress(OSError):
                process.kill()
            process.wait(timeout=2)
        for pipe in (process.stdin, process.stdout):
            if pipe is not None:
                pipe.close()
        if not isinstance(reason, Exception):
            raise
        raise McpStdioError("Could not contain MCP stdio process") from None


@asynccontextmanager
async def managed_mcp_stdio(connection: McpConnectionSpec) -> AsyncIterator[TransportStreams]:
    """Yield the official SDK stream pair; callers own session/authorization.

    Stderr is discarded rather than forwarding package credentials to Host
    logs. Pipe reads and writes have independent bounded worker capacity and
    zero-capacity streams, so neither a slow client nor another connection
    can create an unbounded queue or starve this connection's writer.
    """
    read_sender, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_receiver = anyio.create_memory_object_stream[SessionMessage](0)
    workers = anyio.CapacityLimiter(2)
    writer_done = anyio.Event()
    process, tree = _spawn(connection)
    assert process.stdin is not None and process.stdout is not None

    async def report(message: str) -> None:
        with suppress(anyio.ClosedResourceError, anyio.BrokenResourceError):
            await read_sender.send(McpStdioError(message))

    async def read_stdout() -> None:
        async with read_sender:
            try:
                while True:
                    line = await anyio.to_thread.run_sync(process.stdout.readline, MAX_LINE_BYTES + 1,
                                                         abandon_on_cancel=True, limiter=workers)
                    if not line:
                        return
                    if len(line) > MAX_LINE_BYTES:
                        await report("MCP stdio message exceeds 8 MiB")
                        return
                    if not line.endswith(b"\n"):
                        await report("MCP stdio message ended before its newline")
                        return
                    try:
                        message = mcp_types.jsonrpc_message_adapter.validate_json(line, by_name=False)
                    except ValueError:
                        await report("MCP server emitted an invalid JSON-RPC message")
                        return
                    await read_sender.send(SessionMessage(message))
            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                pass
            except (OSError, ValueError):
                await report("MCP stdio read failed")

    def write_line(data: bytes) -> None:
        process.stdin.write(data)
        process.stdin.flush()

    async def write_stdin() -> None:
        try:
            async with write_receiver:
                async for session_message in write_receiver:
                    data = (session_message.message.model_dump_json(by_alias=True, exclude_unset=True) + "\n").encode("utf-8")
                    if len(data) > MAX_LINE_BYTES:
                        await report("MCP stdio message exceeds 8 MiB")
                        read_sender.close()
                        return
                    await anyio.to_thread.run_sync(write_line, data, abandon_on_cancel=True, limiter=workers)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            pass
        except (OSError, ValueError):
            await report("MCP stdio write failed")
            read_sender.close()
        finally:
            writer_done.set()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(read_stdout)
        tasks.start_soon(write_stdin)
        try:
            yield read_stream, write_stream
        finally:
            try:
                with anyio.CancelScope(shield=True):
                    read_stream.close()
                    write_stream.close()
                    with anyio.move_on_after(_WRITER_FLUSH_SECONDS):
                        await writer_done.wait()
                    # Only touch stdin once the writer has released its pipe
                    # lock; a blocked write must be interrupted by tree kill.
                    if writer_done.is_set():
                        process.stdin.close()
                        with anyio.move_on_after(_EXIT_GRACE_SECONDS):
                            while process.poll() is None:
                                await anyio.sleep(0.01)
            finally:
                # No await can let native asyncio cancellation skip tree
                # termination. Closing the Job also kills graceful survivors.
                tree.terminate()
                for pipe in (process.stdin, process.stdout):
                    with suppress(OSError, ValueError):
                        pipe.close()
                for stream in (read_stream, write_stream, read_sender, write_receiver):
                    stream.close()
                tasks.cancel_scope.cancel()
