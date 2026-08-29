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
