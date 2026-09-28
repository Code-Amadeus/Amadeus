"""Rejecting a context operation cannot leave its second spelling executable."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent_host.work_ledger_store import WorkLedgerStore
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from server import app
from server.host_action_dispatcher import HostDispatchBlocked
from server.work_ledger_coordinator import WorkLedgerCoordinator


@pytest.mark.parametrize("modifier", ["set", "clear"])
@pytest.mark.parametrize("reply", ["NONE", RuntimeError("audit unavailable")])
@pytest.mark.parametrize("task", ["", "Update README"])
def test_denied_focus_preserves_only_the_valid_work(tmp_path, modifier, reply, task):
    async def run():
        first, second = tmp_path / "first", tmp_path / "second"
        first.mkdir()
        second.mkdir()
        with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            current = store.create_or_get_project(first, name="First")
            destination = store.create_or_get_project(second, name="Second")
            store.update_session_context("focus-denial", project_id=current.project_id)
            before = store.get_conversation_binding("focus-denial")
            attrs = {
                "provider": "codex",
                "intent": "focus",
                "focus": modifier,
                "task": task,
                "_host_source_user_text": (
                    "Update README，先不要切换默认项目。"
                    if task
                    else "只讨论一下方案，先不要切换项目。"
                ),
            }
            if modifier == "set":
                attrs["project_id"] = destination.project_id

            async def reference(task, values, **_kwargs):
                return "bypass", task, values

            prepared = []

            async def intake(request):
                prepared.append(coordinator.prepare_request(request))
                return SimpleNamespace(
                    task_handle=None,
                    result="intake_only",
                    error="",
                    metadata={"result_type": "ok"},
                )

            with (
                patch("core.session_manager.get_current_session_id", return_value="focus-denial"),
                patch(
                    "server.work_ledger_coordinator.get_work_ledger_coordinator",
                    return_value=coordinator,
                ),
                patch("config.settings.DELEGATE_INTENT_ATTRIBUTE", True),
                patch("config.settings.DELEGATE_FOCUS_INTENT", True),
                patch("config.settings.WORK_PROJECT_ALLOWLIST", f"{first};{second}"),
                patch("config.settings.WORK_WORKTREE_ISOLATION", False),
                patch("config.settings.WORK_SCRATCH_ROOT", str(tmp_path / "scratch")),
                patch("server.work_ledger_coordinator.cwd_in_project_registry", return_value=True),
                patch.object(
                    app,
                    "_consume_captured_interaction_branch_intent",
                    new=AsyncMock(return_value=False),
                ),
                patch.object(
                    app, "_adjudicate_delegate_reference", new=AsyncMock(side_effect=reference)
                ) as resolve,
                patch.object(app, "_schedule_focus_confirmation") as confirm,
                patch(
                    "llm.client.remote_llm_query",
                    **(
                        {"side_effect": reply}
                        if isinstance(reply, Exception)
                        else {"return_value": reply}
                    ),
                ),
                patch.object(
                    store, "update_session_context", wraps=store.update_session_context
                ) as update,
                patch(
                    "agent_host.provider_runtime.runtime.start", new=AsyncMock(side_effect=intake)
                ) as start,
                patch(
                    "agent_host.provider_runtime.runtime.provider_manifests",
                    return_value=(CODEX_APP_SERVER_MANIFEST,),
                ),
                patch(
                    "agent_host.provider_runtime.runtime.get_manifest",
                    return_value=CODEX_APP_SERVER_MANIFEST,
                ),
            ):
                result = await app._handle_delegate(task, attrs)
                after = store.get_conversation_binding("focus-denial")
                update.assert_not_called()
                confirm.assert_not_called()
                if task:
                    assert not isinstance(result, HostDispatchBlocked)
                    assert after.project_id == before.project_id
                    resolve.assert_awaited_once()
                    start.assert_awaited_once()
                    items = store.list_work_items()
                    assert len(items) == 1 and items[0].goal == task
                    assert items[0].metadata["intent"] == "execute"
                    assert "focus" not in prepared[0].metadata["delegate_attrs"]
                    if modifier == "set":
                        assert Path(items[0].workspace_path) == second
                    else:
                        assert Path(items[0].workspace_path).is_relative_to(tmp_path / "scratch")
                else:
                    assert after == before
                    assert isinstance(result, HostDispatchBlocked)
                    resolve.assert_not_awaited()
                    start.assert_not_awaited()
                    assert store.list_work_items() == []
            coordinator.close()

    asyncio.run(run())
