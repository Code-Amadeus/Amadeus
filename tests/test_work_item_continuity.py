"""Assembly tests for stable WorkItem goals and per-instruction Operations."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config.settings as settings
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from agent_host.provider_contract import ProviderRequirements, ProviderSelection
from agent_host.provider_types import ProviderRunRequest
from agent_host.work_ledger_store import WorkLedgerStore
from server.app import _handle_delegate
from server.interaction_branch import (
    InteractionBranchCoordinator,
    InteractionBranchState,
)
from server.work_ledger_coordinator import WorkLedgerCoordinator


def _finish_latest(store: WorkLedgerStore, work_item_id: str, status: str = "succeeded") -> None:
    attempt = store.list_attempts(work_item_id)[-1]
    store.update_attempt(attempt.attempt_id, execution_status=status)
    store.release_writer_lease(attempt.attempt_id)


def test_long_session_switches_goals_without_splitting_amendment_delivery() -> None:
    with tempfile.TemporaryDirectory(prefix="work_item_continuity_") as temp:
        root = Path(temp)
        project_root = root / "project"
        project_b_root = root / "project-b"
        draft_a = root / "scratch" / "draft-a"
        draft_b = root / "scratch" / "draft-b"
        project_root.mkdir()
        project_b_root.mkdir()
        draft_a.mkdir(parents=True)
        draft_b.mkdir(parents=True)
        previous_scratch = settings.WORK_SCRATCH_ROOT
        previous_allowlist = settings.WORK_PROJECT_ALLOWLIST
        settings.WORK_SCRATCH_ROOT = str(root / "scratch")
        settings.WORK_PROJECT_ALLOWLIST = f"{project_root};{project_b_root}"
        try:
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store)
                project = store.create_or_get_project(project_root, name="Amadeus")
                project_b = store.create_or_get_project(project_b_root, name="Game Lab")
                coordinator.set_session_project("voice-session", project.project_id)

                project_a_work = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="fake-a",
                        task="Audit the Amadeus routing model",
                        cwd=str(project_root),
                        mode="plan",
                        metadata={"session_id": "voice-session", "intent": "execute"},
                    )
                )
                project_a_work_id = str(
                    project_a_work.metadata["work"]["work_item_id"]
                )
                _finish_latest(store, project_a_work_id)

                first = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="fake-a",
                        task="Design a chess prototype",
                        cwd=str(draft_a),
                        mode="plan",
                        metadata={
                            "session_id": "voice-session",
                            "intent": "execute",
                            "source_user_text": "另外做个一次性的象棋原型",
                        },
                    )
                )
                first_work = str(first.metadata["work"]["work_item_id"])
                first_operation = str(first.metadata["work"]["operation_id"])
                _finish_latest(store, first_work)
                roster = coordinator.conversation_work_items_for_resolution(
                    "voice-session"
                )
                first_row = next(
                    row for row in roster["items"] if row["work_item_id"] == first_work
                )
                assert first_row["source_user_text"] == "另外做个一次性的象棋原型"

                # A one-off Draft becomes the narrow active referent without
                # erasing the Project used for otherwise-unplaced new goals.
                assert coordinator.session_project("voice-session") == project.project_id
                context = coordinator.conversation_binding("voice-session")
                assert context is not None
                assert context["bindingKind"] == "work_item"
                assert context["workItemId"] == first_work
                assert context["defaultProjectId"] == project.project_id
                assert context["projectId"] == ""

                amended = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="fake-b",
                        task="Make the chess prototype two-player",
                        cwd=str(draft_a),
                        mode="plan",
                        metadata={
                            "session_id": "voice-session",
                            "intent": "amend",
                            "continuation": "amend",
                            "work": {"work_item_id": first_work},
                        },
                    )
                )
                assert amended.metadata["work"]["work_item_id"] == first_work
                assert amended.metadata["work"]["operation_id"] != first_operation
                _finish_latest(store, first_work)

                unrelated = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="fake-a",
                        task="Design an unrelated timer",
                        cwd=str(draft_b),
                        mode="plan",
                        metadata={"session_id": "voice-session", "intent": "execute"},
                    )
                )
                second_work = str(unrelated.metadata["work"]["work_item_id"])
                assert second_work != first_work
                _finish_latest(store, second_work)
                assert coordinator.conversation_binding("voice-session")["workItemId"] == second_work  # type: ignore[index]

                # An explicit Project switch clears only the narrow referent.
                # Work in that Project receives its own goal context.
                coordinator.set_session_project("voice-session", project_b.project_id)
                project_b_work = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="fake-b",
                        task="Build a game-lab score board",
                        cwd=str(project_b_root),
                        mode="plan",
                        metadata={"session_id": "voice-session", "intent": "execute"},
                    )
                )
                project_b_work_id = str(
                    project_b_work.metadata["work"]["work_item_id"]
                )
                _finish_latest(store, project_b_work_id)

                # Targeting a WorkItem in another Project for one Operation is
                # not a persistent focus switch. The active item grounds "this
                # project" while Project B remains the Session default.
                project_a_amend = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="fake-b",
                        task="Record the routing audit conclusion",
                        mode="plan",
                        metadata={
                            "session_id": "voice-session",
                            "intent": "amend",
                            "continuation": "amend",
                            "work": {"work_item_id": project_a_work_id},
                        },
                    )
                )
                assert project_a_amend.metadata["work"]["work_item_id"] == project_a_work_id
                _finish_latest(store, project_a_work_id)
                project_context = coordinator.conversation_binding("voice-session")
                assert project_context is not None
                assert project_context["projectId"] == project.project_id
                assert project_context["defaultProjectId"] == project_b.project_id
                project_snapshot = coordinator.project_status_snapshot(
                    project.project_id
                )
                assert project_snapshot is not None
                assert project_snapshot["projectId"] == project.project_id

                returned = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="fake-a",
                        task="Add a restart button to the chess prototype",
                        cwd=str(draft_a),
                        mode="plan",
                        metadata={
                            "session_id": "voice-session",
                            "intent": "amend",
                            "continuation": "amend",
                            "work": {"work_item_id": first_work},
                        },
                    )
                )
                assert returned.metadata["work"]["work_item_id"] == first_work
                draft_context = coordinator.conversation_binding("voice-session")
                assert draft_context is not None
                assert draft_context["projectId"] == ""
                assert draft_context["defaultProjectId"] == project_b.project_id
                draft_project_snapshot = coordinator.project_status_snapshot(
                    project_b.project_id
                )
                assert draft_project_snapshot is not None
                assert draft_project_snapshot["projectId"] == project_b.project_id
                assert len(store.list_work_items(limit=20)) == 4
                assert len(store.list_operations(first_work)) == 3
                assert len(store.list_attempts(first_work)) == 3
                assert [
                    operation.intent for operation in store.list_operations(first_work)
                ] == ["execute", "amend", "amend"]
                assert coordinator.session_project("voice-session") == project_b.project_id
                coordinator.close()
        finally:
            settings.WORK_SCRATCH_ROOT = previous_scratch
            settings.WORK_PROJECT_ALLOWLIST = previous_allowlist


def test_host_delegate_passes_amend_as_work_item_operation_target() -> None:
    async def run() -> None:
        workspace = str(Path(__file__).resolve().parents[1])
        start = AsyncMock(
            return_value=SimpleNamespace(
                task_handle=None,
                result="updated",
                error="",
                metadata={"result_type": "ok"},
            )
        )
        requirements = ProviderRequirements(
            task_kind="workspace_write",
            workspace_access="write",
            workspace_ownership="negotiated",
        )
        selection = ProviderSelection(
            provider_id="codex",
            reason="test",
            compatible_candidates=("codex",),
        )
        with (
            patch(
                "server.app._delegate_provider_selection",
                return_value=(requirements, selection),
            ),
            patch(
                "server.app._delegate_workspace_route",
                return_value={
                    "status": "resolved",
                    "cwd": workspace,
                    "projectId": "project-game",
                    "workItemId": "work-game",
                    "workspaceMode": "worktree",
                    "source": "intent_workspace_ref",
                },
            ),
            patch(
                "agent_host.provider_runtime.runtime.get_manifest",
                return_value=CODEX_APP_SERVER_MANIFEST,
            ),
            patch("agent_host.provider_runtime.runtime.start", new=start),
            patch(
                "server.work_ledger_coordinator.get_work_ledger_coordinator",
                return_value=None,
            ),
        ):
            result = await _handle_delegate(
                "Make the game two-player",
                {
                    "provider": "codex",
                    "intent": "amend",
                    "workspace_ref": "work-game",
                },
            )
        assert result == "updated"
        request = start.await_args.args[0]
        assert request.metadata["continuation"] == "amend"
        assert request.metadata["work"]["work_item_id"] == "work-game"
        assert request.metadata["work"]["workspace_ref"] == "work-game"
        assert "related_work_item_id" not in request.metadata

    asyncio.run(run())


def test_browser_next_turn_reuses_work_item_but_mid_run_steer_reuses_attempt() -> None:
    async def run() -> None:
        started: list[dict] = []
        steered: list[dict] = []

        async def provider_run(params: dict) -> dict:
            started.append(params)
            return {"run": {"run_id": "browser-next", "status": "running"}}

        async def provider_steer(params: dict) -> dict:
            steered.append(params)
            return {"accepted": True, "run": {"run_id": "browser-live", "status": "running"}}

        coordinator = InteractionBranchCoordinator(
            provider_run=provider_run,
            provider_steer=provider_steer,
        )
        branch = InteractionBranchState(
            branch_id="branch-1",
            parent_session_id="voice-session",
            provider="browser",
            status="idle",
            goal="Find the Amadeus page",
            browser_session_id="browser-session",
            work_item_id="work-browser",
            operation_id="operation-browser-1",
        )
        coordinator._active_by_session["voice-session"] = branch

        await coordinator.continue_from_delegate(
            session_id="voice-session",
            task="Open the first result",
            turn_id="turn-2",
        )
        assert started[0]["metadata"]["continuation"] == "amend"
        assert started[0]["metadata"]["work"] == {"work_item_id": "work-browser"}

        branch.active_run_id = "browser-live"
        await coordinator.continue_from_delegate(
            session_id="voice-session",
            task="Use the second result instead",
            turn_id="turn-3",
        )
        assert len(started) == 1
        assert len(steered) == 1
        assert steered[0]["run_id"] == "browser-live"

    asyncio.run(run())
