from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from openclaw import gateway


class _ResponseContext:
    def __init__(self, status: int | None) -> None:
        self._status = status

    async def __aenter__(self):
        if self._status is None:
            raise OSError("gateway is not running yet")
        return SimpleNamespace(status=self._status)

    async def __aexit__(self, *_args) -> None:
        return None


class _ClientSession:
    statuses = iter((None, 200))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    def get(self, *_args, **_kwargs):
        return _ResponseContext(next(self.statuses))


def test_installed_openclaw_cli_starts_gateway_without_source_checkout() -> None:
    cli = r"C:\ProgramData\npm\openclaw.cmd"
    module = (
        r"C:\ProgramData\npm\node_modules"
        r"\openclaw\openclaw.mjs"
    )
    process = SimpleNamespace(returncode=None)
    create_process = AsyncMock(return_value=process)

    def exists(path: str) -> bool:
        return path == module

    with (
        patch.object(gateway, "OPENCLAW_PROJECT_DIR", ""),
        patch.object(gateway, "OPENCLAW_TOKEN", "local-dev-token"),
        patch.object(gateway.aiohttp, "ClientSession", _ClientSession),
        patch.object(gateway.asyncio, "create_subprocess_exec", create_process),
        patch.object(gateway.asyncio, "sleep", AsyncMock()),
        patch.object(gateway.os.path, "exists", side_effect=exists),
        patch("shutil.which", return_value=cli),
    ):
        assert asyncio.run(gateway.start_openclaw_gateway()) is True

    assert create_process.await_args.args[:4] == (
        "node",
        module,
        "gateway",
        "run",
    )
    assert create_process.await_args.args[4:] == (
        "--port",
        "18789",
        "--bind",
        "loopback",
    )
    assert create_process.await_args.kwargs["cwd"] is None


def test_stop_openclaw_gateway_waits_for_managed_process() -> None:
    process = SimpleNamespace(
        returncode=None,
        terminate=Mock(),
        wait=AsyncMock(return_value=0),
    )
    gateway._openclaw_gateway_proc = process

    asyncio.run(gateway.stop_openclaw_gateway())

    process.terminate.assert_called_once_with()
    process.wait.assert_awaited_once_with()
    assert gateway._openclaw_gateway_proc is None
