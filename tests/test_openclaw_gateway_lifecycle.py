from __future__ import annotations

import asyncio
from unittest.mock import patch

from openclaw import gateway


def test_unconfigured_optional_gateway_skips_network_and_local_startup() -> None:
    with (
        patch.object(gateway, "OPENCLAW_TOKEN", ""),
        patch.object(gateway, "OPENCLAW_PROJECT_DIR", ""),
        patch.object(gateway.aiohttp, "ClientSession") as client_session,
        patch.object(gateway.asyncio, "create_subprocess_exec") as create_process,
    ):
        assert asyncio.run(gateway.start_openclaw_gateway()) is False

    client_session.assert_not_called()
    create_process.assert_not_called()


def test_owned_gateway_shutdown_waits_for_the_subprocess() -> None:
    class Process:
        returncode = None

        def __init__(self):
            self.terminated = False
            self.waited = False

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        async def wait(self):
            self.waited = True
            return self.returncode

    process = Process()
    with patch.object(gateway, "_openclaw_gateway_proc", process):
        asyncio.run(gateway.close_openclaw_gateway())
        assert gateway._openclaw_gateway_proc is None
    assert process.terminated and process.waited
