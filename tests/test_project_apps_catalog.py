from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_host.work_ledger_store import WorkLedgerStore
from config import settings
from server.handlers.work_ledger_handler import WorkLedgerHandler
from server.protocol import Method
from server.work_ledger_coordinator import WorkLedgerCoordinator


def test_project_apps_are_derived_from_verified_work_artifacts() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="project_apps_catalog_") as temp:
            root = Path(temp)
            project_root = root / "project"
            project_root.mkdir()
            entry = project_root / "index.html"
            entry.write_text("<!doctype html><title>Lab</title>", encoding="utf-8")
            previous_allowlist = settings.WORK_PROJECT_ALLOWLIST
            previous_scratch = settings.WORK_SCRATCH_ROOT
            settings.WORK_PROJECT_ALLOWLIST = str(project_root)
            settings.WORK_SCRATCH_ROOT = str(root / "scratch")
            try:
                with WorkLedgerStore(root / "ledger.sqlite3") as store:
                    project = store.create_or_get_project(project_root, name="Research Lab")
                    item = store.create_work_item(
                        project.project_id,
                        title="Interactive lab",
                        workspace_path=project_root,
                    )
                    attempt = store.create_attempt(
                        item.work_item_id,
                        provider="locus",
                        task="Build the interactive lab",
                        metadata={"session_id": "project-app-session"},
                    )
                    artifact = store.register_artifact(
                        item.work_item_id,
                        attempt_id=attempt.attempt_id,
                        kind="business.file",
                        title="index.html",
                        path=entry,
                        sha256="verified",
                    )
                    store.create_work_item(
                        project.project_id,
                        title="Notes without an application",
                        workspace_path=project_root,
                    )
                    discovered = {
                        "artifact_id": artifact.artifact_id,
                        "artifact_ref": f"artifact:{artifact.artifact_id}@verified",
                        "work_item_id": item.work_item_id,
                        "app": {
                            "id": "research-lab",
                            "title": "Research Lab",
                            "version": "1.2.0",
                            "objective": "Explore one verified simulation.",
                            "interactionSummary": "Amadeus can take declared turns.",
                        },
                        "stances": ["spectator", "participant"],
                        "contributing_attempt_ids": [attempt.attempt_id],
                    }
                    coordinator = WorkLedgerCoordinator(store)
                    handler = WorkLedgerHandler(coordinator)
                    with patch(
                        "server.work_read_model.discover_launchable_auip_app",
                        side_effect=lambda _store, work_item_id: (
                            discovered if work_item_id == item.work_item_id else None
                        ),
                    ):
                        response = await handler.handle(
                            Method.PROJECT_APPS_LIST,
                            {"project_id": project.project_id},
                        )
                    assert response is not None and response["ok"] is True
                    assert response["project"]["projectId"] == project.project_id
                    assert response["complete"] is True
                    assert len(response["apps"]) == 1
                    app = response["apps"][0]
                    assert app["workItemId"] == item.work_item_id
                    assert app["artifactId"] == artifact.artifact_id
                    assert app["appId"] == "research-lab"
                    assert app["version"] == "1.2.0"
                    assert app["revision"] == 1
                    assert app["modes"] == ["observe", "collaborate", "delegate"]
                    assert app["sourceSessionId"] == "project-app-session"
                    assert app["canPromote"] is False

                    scratch_root = Path(settings.WORK_SCRATCH_ROOT)
                    scratch_root.mkdir()
                    scratch_project = store.create_or_get_project(scratch_root, name="Drafts")
                    hidden = await handler.handle(
                        Method.PROJECT_APPS_LIST,
                        {"project_id": scratch_project.project_id},
                    )
                    assert hidden is not None and hidden["ok"] is False
                    assert hidden["error"] == "the scratch container is not a project"
            finally:
                settings.WORK_PROJECT_ALLOWLIST = previous_allowlist
                settings.WORK_SCRATCH_ROOT = previous_scratch

    asyncio.run(run())


def test_draft_apps_are_bounded_to_the_five_most_recent_launchable_items() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="draft_apps_catalog_") as temp:
            root = Path(temp)
            scratch_root = root / "scratch"
            scratch_root.mkdir()
            previous_scratch = settings.WORK_SCRATCH_ROOT
            settings.WORK_SCRATCH_ROOT = str(scratch_root)
            tick = [1000.0]

            def clock() -> float:
                tick[0] += 1.0
                return tick[0]

            try:
                with WorkLedgerStore(root / "ledger.sqlite3", clock=clock) as store:
                    scratch_project = store.create_or_get_project(scratch_root, name="Drafts")
                    discovered: dict[str, dict] = {}
                    for index in range(7):
                        workspace = scratch_root / f"draft-{index}"
                        workspace.mkdir()
                        item = store.create_work_item(
                            scratch_project.project_id,
                            title=f"Draft app {index}",
                            workspace_path=workspace,
                        )
                        attempt = store.create_attempt(
                            item.work_item_id,
                            provider="locus",
                            task=f"Build Draft app {index}",
                            metadata={"session_id": f"draft-session-{index}"},
                        )
                        store.update_attempt(
                            attempt.attempt_id,
                            execution_status="succeeded",
                        )
                        discovered[item.work_item_id] = {
                            "artifact_id": f"artifact-{index}",
                            "artifact_ref": f"artifact:artifact-{index}@verified",
                            "work_item_id": item.work_item_id,
                            "app": {
                                "id": f"draft-app-{index}",
                                "title": f"Draft App {index}",
                                "version": "0.1.0",
                            },
                            "stances": ["spectator"],
                            "contributing_attempt_ids": [attempt.attempt_id],
                        }

                    coordinator = WorkLedgerCoordinator(store)
                    handler = WorkLedgerHandler(coordinator)
                    with patch(
                        "server.work_read_model.discover_launchable_auip_app",
                        side_effect=lambda _store, work_item_id: discovered.get(work_item_id),
                    ):
                        response = await handler.handle(Method.DRAFT_APPS_LIST, {"limit": 5})

                    assert response is not None and response["ok"] is True
                    assert response["recentLimit"] == 5
                    assert [app["title"] for app in response["apps"]] == [
                        "Draft App 6",
                        "Draft App 5",
                        "Draft App 4",
                        "Draft App 3",
                        "Draft App 2",
                    ]
                    assert all(app["canPromote"] is True for app in response["apps"])
                    assert response["apps"][0]["sourceSessionId"] == "draft-session-6"
            finally:
                settings.WORK_SCRATCH_ROOT = previous_scratch

    asyncio.run(run())
