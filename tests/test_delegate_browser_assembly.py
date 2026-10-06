"""Shared focus authority stays bound to its originating conversation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from server.app import _handle_declared_focus


def test_focus_refuses_to_mutate_after_origin_session_switch() -> None:
    async def run() -> None:
        coordinator = SimpleNamespace(
            set_session_project=AsyncMock(side_effect=AssertionError("must not mutate ambient Session")),
            clear_session_project=AsyncMock(side_effect=AssertionError("must not mutate ambient Session")),
        )
        with (
            patch("core.session_manager.get_current_session_id", return_value="session-b"),
            patch("server.work_ledger_coordinator.get_work_ledger_coordinator", return_value=coordinator),
        ):
            result = await _handle_declared_focus({"project_id": "project-a"}, session_id="session-a")
        assert result["ok"] is False
        assert result["authority_blocked"] is True
        coordinator.set_session_project.assert_not_awaited()
        coordinator.clear_session_project.assert_not_awaited()
    asyncio.run(run())
