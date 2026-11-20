"""The shared focus owner publishes context changes without creating Work."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agent_host.work_ledger_store import WorkLedgerStore
from server import app
from server.work_ledger_coordinator import WorkLedgerCoordinator


@pytest.mark.parametrize("modifier", ["set", "clear"])
@pytest.mark.parametrize("resumed", [False, True])
def test_focus_owner_applies_and_publishes_once(tmp_path, modifier, resumed):
    async def run():
        project_path = tmp_path / "project"
        project_path.mkdir()
        with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            coordinator.configure()
            project = store.create_or_get_project(project_path, name="Project")
            session_id = "focus-origin"
            with patch("server.work_ledger_coordinator.cwd_in_project_registry", return_value=True):
                if modifier == "clear":
                    coordinator.set_session_project(session_id, project.project_id)
                try:
                    with (
                        patch("core.session_manager.get_current_session_id", return_value=session_id),
                        patch.object(app, "_schedule_focus_confirmation") as confirmation,
                        patch.object(coordinator, "publish_snapshot", new=AsyncMock()) as publish,
                        patch("agent_host.provider_runtime.runtime.start", new=AsyncMock()) as start,
                    ):
                        result = await app._handle_declared_focus(
                            {"project_id": project.project_id if modifier == "set" else ""},
                            session_id=session_id, announce_result=not resumed,
                        )
                        assert result["ok"] is True
                        publish.assert_awaited_once()
                        assert confirmation.call_count == (0 if resumed else 1)
                        if confirmation.called:
                            assert confirmation.call_args.kwargs["session_id"] == session_id
                        start.assert_not_awaited()
                    assert coordinator.session_project(session_id) == (project.project_id if modifier == "set" else "")
                    assert store.list_work_items() == []
                finally:
                    coordinator.close()
    asyncio.run(run())


def test_rejected_project_focus_preserves_the_previous_destination(tmp_path):
    async def run():
        project_path = tmp_path / "current"
        project_path.mkdir()
        with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            coordinator.configure()
            project = store.create_or_get_project(project_path, name="Current")
            with patch("server.work_ledger_coordinator.cwd_in_project_registry", return_value=True):
                coordinator.set_session_project("focus-origin", project.project_id)
                try:
                    with (
                        patch("core.session_manager.get_current_session_id", return_value="focus-origin"),
                        patch.object(coordinator, "publish_snapshot", new=AsyncMock()) as publish,
                    ):
                        result = await app._handle_declared_focus(
                            {"project_id": "project-missing"}, session_id="focus-origin", announce_result=False,
                        )
                    assert result["ok"] is False
                    assert coordinator.session_project("focus-origin") == project.project_id
                    publish.assert_awaited_once()
                    assert store.list_work_items() == []
                finally:
                    coordinator.close()
    asyncio.run(run())
