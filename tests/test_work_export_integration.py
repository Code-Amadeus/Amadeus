"""Work ledger integration for staged Desktop deliverables and permissions."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_catalog import DIRECT_CODEX_MANIFEST
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_types import ProviderRunRequest
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.canvas_action_router import CanvasActionRouter
from server.work_export_service import WorkExportService
from server.handlers.work_ledger_handler import WorkLedgerHandler
from server.event_bus import bus
from server.protocol import Method
from server.work_ledger_coordinator import WorkLedgerCoordinator


def _request(workspace: Path, task: str) -> ProviderRunRequest:
    return ProviderRunRequest(
        provider="locus",
        task=task,
        cwd=str(workspace),
        mode="agent",
        metadata={"allow_agent_mode": True, "locus_allow_agent_mode": True},
    )


async def _finish(
    coordinator: WorkLedgerCoordinator,
    request: ProviderRunRequest,
    *,
    run_id: str,
    status: str = "done",
    result: str = "Locus staged and validated the requested deliverable.",
) -> None:
    await coordinator._on_provider_result(
        "provider.result",
        {
            "provider": "locus",
            "run_id": run_id,
            "status": status,
            "result": result,
            "error": "",
            "metadata": {
                **dict(request.metadata),
                "locus": {"job_id": "job-export", "artifacts": []},
            },
        },
    )


def test_empty_provider_artifacts_still_create_diff_permission_and_exact_export() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_export_integration_") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            desktop = root / "Desktop"
            workspace.mkdir()
            desktop.mkdir()
            database = root / "ledger.sqlite3"
            task = "请用 Python 写一个国际象棋程序，并保存到桌面，文件名 chess_game.py。"

            store = WorkLedgerStore(database)
            coordinator = WorkLedgerCoordinator(
                store,
                export_service=WorkExportService(store, desktop_path=desktop),
            )
            request = _request(workspace, task)
            prepared = coordinator.prepare_request(request)
            binding = prepared.metadata["work"]
            item = store.get_work_item(binding["work_item_id"])
            attempt = store.get_attempt(binding["attempt_id"])
            assert item is not None and attempt is not None
            assert item.goal == task and attempt.task == task
            assert prepared.metadata["display_task"] == task
            assert "AMADEUS TWO-PHASE EXPORT POLICY" in prepared.task
            assert prepared.metadata["export_plan"]["staging_root"] in prepared.task

            staged = Path(prepared.metadata["export_plan"]["staging_root"]) / "chess_game.py"
            staged.write_text("print('validated chess')\n", encoding="utf-8")
            publication_order: list[tuple[str, dict]] = []

            async def capture_canvas(_method: str, payload: dict) -> None:
                if payload.get("permissionVisible") is True:
                    publication_order.append(("canvas", payload))

            async def capture_note(_method: str, payload: dict) -> None:
                if payload.get("metadata", {}).get("permission_actionable") is True:
                    publication_order.append(("note", payload))

            bus.on(Method.WALLPAPER_CANVAS, capture_canvas)
            bus.on(Method.CHAT_WORK_NOTE, capture_note)
            try:
                await _finish(coordinator, prepared, run_id="locus-export-1")
            finally:
                bus.off(Method.WALLPAPER_CANVAS, capture_canvas)
                bus.off(Method.CHAT_WORK_NOTE, capture_note)

            refreshed_attempt = store.get_attempt(attempt.attempt_id)
            assert refreshed_attempt is not None
            delta = refreshed_attempt.metadata["export_delta"]
            assert delta["reason"] == "external_export_pending"
            assert delta["changed_files"] == ["Desktop/chess_game.py"]
            assert "+print('validated chess')" in delta["patch"]
            assert not (desktop / "chess_game.py").exists()

            permissions = store.list_permission_requests(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                status="pending",
            )
            assert len(permissions) == 1
            projected = coordinator.snapshot()["selected"]
            assert projected["attention"] == "permission"
            assert projected["pendingPermissionRequestId"] == permissions[0].request_id
            canvas = coordinator.selected_canvas()
            assert canvas is not None and canvas["permissionVisible"] is True
            assert canvas["permissionRequest"]["id"] == permissions[0].request_id
            assert canvas["permissionRequest"]["scope"] == permissions[0].scope_paths
            assert canvas["permissionRequest"]["scope"][0] == str(desktop / "chess_game.py")
            assert canvas["permissionRequest"]["scope"][1].endswith(".tmp")
            assert [kind for kind, _payload in publication_order] == ["canvas", "note"]
            assert publication_order[0][1]["taskDock"]["revision"] == coordinator.snapshot()[
                "revision"
            ]
            permission_note = publication_order[-1][1]
            assert permission_note["source"] == "work_ledger"
            assert permission_note["phase"].lower() == "checkpoint"
            assert permission_note["importance"] == "important"
            assert permission_note["observer_policy"] == "auto"
            assert permission_note["speak"] is True
            assert permission_note["metadata"]["permission_request_id"] == permissions[0].request_id
            assert permission_note["metadata"]["permission_targets"] == [
                str(desktop / "chess_game.py")
            ]
            assert permission_note["metadata"]["permission_filenames"] == ["chess_game.py"]

            replayed_notes: list[dict] = []

            async def capture_replayed_note(_method: str, payload: dict) -> None:
                if payload.get("metadata", {}).get("permission_actionable") is True:
                    replayed_notes.append(payload)

            bus.on(Method.CHAT_WORK_NOTE, capture_replayed_note)
            try:
                await _finish(coordinator, prepared, run_id="locus-export-1")
            finally:
                bus.off(Method.CHAT_WORK_NOTE, capture_replayed_note)
            assert replayed_notes == []

            diff_canvases: list[dict] = []

            async def capture_diff(_method: str, payload: dict) -> None:
                diff_canvases.append(payload)

            bus.on(Method.WALLPAPER_CANVAS, capture_diff)
            try:
                inspected = await coordinator.route_provider_inspection(
                    {"action": "view_diff", "attempt_id": attempt.attempt_id}
                )
            finally:
                bus.off(Method.WALLPAPER_CANVAS, capture_diff)
            assert inspected["ok"] is True and diff_canvases
            assert diff_canvases[-1]["pendingExport"] is True
            assert diff_canvases[-1]["diff"]["files"][0]["path"] == "Desktop/chess_game.py"
            assert any(
                line["kind"] == "add" and line["text"] == "print('validated chess')"
                for hunk in diff_canvases[-1]["diff"]["files"][0]["hunks"]
                for line in hunk["lines"]
            )

            # The permission and proposed diff survive process restart.
            store.close()
            reopened_store = WorkLedgerStore(database)
            reopened = WorkLedgerCoordinator(
                reopened_store,
                export_service=WorkExportService(reopened_store, desktop_path=desktop),
            )
            restarted = reopened.snapshot()["selected"]
            assert restarted["attention"] == "permission"
            restarted_canvas = reopened.selected_canvas()
            assert restarted_canvas is not None
            assert restarted_canvas["permissionRequest"]["id"] == permissions[0].request_id

            handler = WorkLedgerHandler(reopened)
            other_project = reopened_store.create_or_get_project(root / "other-workspace")
            other_item = reopened_store.create_work_item(
                other_project.project_id,
                title="Unrelated task",
            )
            reopened_store.create_attempt(
                other_item.work_item_id,
                provider="locus",
                task="Unrelated task",
                mode="agent",
            )

            # A permission card is authoritative only for the task currently
            # selected in Slice.  Even exact, otherwise-valid A identifiers
            # cannot be replayed while B is selected with the current surface
            # revision.
            reopened.select(other_item.work_item_id)
            background_revision = reopened.snapshot()["revision"]
            background = await CanvasActionRouter(
                work_action=handler.route_action
            ).route(
                {
                    "target": "permission",
                    "action": "allow_once",
                    "permission_request_id": permissions[0].request_id,
                    "work_item_id": item.work_item_id,
                    "attempt_id": attempt.attempt_id,
                    "revision": background_revision,
                }
            )
            assert background["ok"] is False
            assert background["error"] == "permission_work_item_not_selected"
            assert reopened_store.get_permission_request(permissions[0].request_id).status == "pending"  # type: ignore[union-attr]

            reopened.select(item.work_item_id)
            current_revision = reopened.snapshot()["revision"]
            missing_request = await handler.route_action(
                {
                    "target": "permission",
                    "action": "allow_once",
                    "work_item_id": item.work_item_id,
                    "attempt_id": attempt.attempt_id,
                    "revision": current_revision,
                }
            )
            assert missing_request["ok"] is False
            assert missing_request["error"] == "missing_permission_request_id"
            missing_item = await handler.route_action(
                {
                    "target": "permission",
                    "action": "allow_once",
                    "permission_request_id": permissions[0].request_id,
                    "attempt_id": attempt.attempt_id,
                    "revision": current_revision,
                }
            )
            assert missing_item["ok"] is False and missing_item["error"] == "missing_work_item_id"
            missing_revision = await handler.route_action(
                {
                    "target": "permission",
                    "action": "allow_once",
                    "permission_request_id": permissions[0].request_id,
                    "work_item_id": item.work_item_id,
                    "attempt_id": attempt.attempt_id,
                }
            )
            assert missing_revision["ok"] is False and missing_revision["error"] == "missing_revision"
            cross_item = await handler.route_action(
                {
                    "target": "permission",
                    "action": "allow_once",
                    "permission_request_id": permissions[0].request_id,
                    "work_item_id": other_item.work_item_id,
                    "attempt_id": attempt.attempt_id,
                    "revision": current_revision,
                }
            )
            assert cross_item["ok"] is False
            assert cross_item["error"] == "permission_work_item_not_selected"
            wrong_attempt = await handler.route_action(
                {
                    "target": "permission",
                    "action": "allow_once",
                    "permission_request_id": permissions[0].request_id,
                    "work_item_id": item.work_item_id,
                    "attempt_id": "attempt-from-another-context",
                    "revision": current_revision,
                }
            )
            assert wrong_attempt["ok"] is False
            assert wrong_attempt["error"] == "permission_attempt_not_selected"
            assert reopened_store.get_permission_request(permissions[0].request_id).status == "pending"  # type: ignore[union-attr]
            stale = await handler.route_action(
                {
                    "target": "permission",
                    "action": "allow_once",
                    "permission_request_id": permissions[0].request_id,
                    "work_item_id": item.work_item_id,
                    "attempt_id": attempt.attempt_id,
                    "revision": "stale-revision",
                }
            )
            assert stale["ok"] is False and stale["error"] == "stale_revision"
            assert not (desktop / "chess_game.py").exists()
            current_revision = reopened.snapshot()["revision"]
            terminal_notes: list[dict] = []

            async def capture_terminal_note(_method: str, payload: dict) -> None:
                if payload.get("metadata", {}).get("narration_keypoint") == "terminal":
                    terminal_notes.append(payload)

            bus.on(Method.CHAT_WORK_NOTE, capture_terminal_note)
            try:
                resolved = await handler.route_action(
                    {
                        "target": "permission",
                        "action": "allow_once",
                        "permission_request_id": permissions[0].request_id,
                        "work_item_id": item.work_item_id,
                        "attempt_id": attempt.attempt_id,
                        "revision": current_revision,
                        # Authority-bearing renderer paths are intentionally ignored.
                        "source_path": str(root / "attacker.py"),
                        "target_path": str(root / "attacker-target.py"),
                    }
                )
            finally:
                bus.off(Method.CHAT_WORK_NOTE, capture_terminal_note)
            assert resolved["permission"]["status"] == "allowed"
            assert (desktop / "chess_game.py").read_text(encoding="utf-8") == "print('validated chess')\n"
            assert not (root / "attacker-target.py").exists()
            final = reopened.snapshot()["selected"]
            assert final["state"] == "review_ready"
            assert final["attention"] == "review"
            assert len(terminal_notes) == 1
            assert terminal_notes[0]["metadata"]["work_event"] == "work.review_ready"
            assert terminal_notes[0]["metadata"]["exported_paths"] == [
                str(desktop / "chess_game.py")
            ]
            resolved_attempt = reopened_store.get_attempt(attempt.attempt_id)
            assert resolved_attempt is not None
            assert resolved_attempt.metadata["export_delta"]["reason"] == "external_export_complete"
            assert resolved_attempt.metadata["export_delta"]["pending_export"] is False
            final_canvas = reopened.selected_canvas()
            assert final_canvas is not None and final_canvas.get("permissionVisible") is not True
            reopened.close()

    asyncio.run(run())


def test_binary_export_identity_survives_permission_projection_and_restart() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_binary_projection_") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        desktop = root / "Desktop"
        workspace.mkdir()
        desktop.mkdir()
        database = root / "ledger.sqlite3"
        task = "Create a profile website with a portrait and export it to Desktop."

        store = WorkLedgerStore(database)
        coordinator = WorkLedgerCoordinator(
            store,
            export_service=WorkExportService(store, desktop_path=desktop),
        )
        prepared = coordinator.prepare_request(_request(workspace, task))
        binding = prepared.metadata["work"]
        item = store.get_work_item(binding["work_item_id"])
        attempt = store.get_attempt(binding["attempt_id"])
        assert item is not None and attempt is not None
        stage = Path(prepared.metadata["export_plan"]["staging_root"])
        (stage / "index.html").write_text(
            '<!doctype html><img src="portrait.png">\n',
            encoding="utf-8",
        )
        image_bytes = b"\x89PNG\r\n\x1a\npermission-projection"
        (stage / "portrait.png").write_bytes(image_bytes)
        outcome = coordinator.export_service.discover_staged_exports(
            attempt,
            item,
            prepared.metadata["export_plan"],
        )
        permission = outcome["permission"]
        assert permission is not None

        canvas = coordinator.selected_canvas()
        assert canvas is not None and canvas["permissionVisible"] is True
        request = canvas["permissionRequest"]
        expected_preview = {
            "path": next(
                f"Desktop/{entry['relative_path']}"
                for entry in permission.metadata["entries"]
                if entry["staging_relative_path"] == "portrait.png"
            ),
            "status": "binary_identity",
            "mediaType": "image/png",
            "sizeBytes": len(image_bytes),
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
        }
        assert request["previewComplete"] is True
        assert request["previewVersion"] == 2
        assert request["previews"] == [expected_preview]
        serialized = str(request)
        assert str(stage) not in serialized
        assert "source_path" not in serialized
        assert "temporary_path" not in serialized

        coordinator.close()
        reopened_store = WorkLedgerStore(database)
        reopened = WorkLedgerCoordinator(
            reopened_store,
            export_service=WorkExportService(reopened_store, desktop_path=desktop),
        )
        restarted_canvas = reopened.selected_canvas()
        assert restarted_canvas is not None
        assert restarted_canvas["permissionRequest"]["previews"] == [expected_preview]
        reopened.export_service.resolve(permission.request_id, allow=True)
        exported_image = next(desktop.rglob("portrait.png"))
        assert exported_image.read_bytes() == image_bytes
        reopened.close()


def test_nested_application_bundle_projects_an_actionable_permission_card() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_export_bundle_card_") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            desktop = root / "Desktop"
            workspace.mkdir()
            desktop.mkdir()
            store = WorkLedgerStore(root / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(
                store,
                export_service=WorkExportService(store, desktop_path=desktop),
            )
            try:
                request = _request(
                    workspace,
                    "Create Infinite Tower and export the application to Desktop.",
                )
                prepared = coordinator.prepare_request(request)
                binding = prepared.metadata["work"]
                stage = Path(prepared.metadata["export_plan"]["staging_root"])
                bundle = stage / "Infinite Tower"
                bundle.mkdir()
                (bundle / "game.py").write_text("print('ready')\n", encoding="utf-8")
                (bundle / "README.md").write_text("# Ready\n", encoding="utf-8")
                permission_canvases: list[dict] = []

                async def capture_canvas(_method: str, payload: dict) -> None:
                    if payload.get("permissionVisible") is True:
                        permission_canvases.append(payload)

                bus.on(Method.WALLPAPER_CANVAS, capture_canvas)
                try:
                    await _finish(coordinator, prepared, run_id="locus-export-bundle")
                finally:
                    bus.off(Method.WALLPAPER_CANVAS, capture_canvas)

                permissions = store.list_permission_requests(
                    binding["work_item_id"],
                    attempt_id=binding["attempt_id"],
                    status="pending",
                )
                assert len(permissions) == 1
                assert permission_canvases
                card = permission_canvases[-1]
                assert card["permissionVisible"] is True
                assert card["permissionRequest"]["id"] == permissions[0].request_id
                assert card["permissionRequest"]["scope"][0] == str(
                    desktop / "Infinite Tower"
                )

                inspected = await coordinator.route_provider_inspection(
                    {"action": "view_diff", "attempt_id": binding["attempt_id"]}
                )
                assert inspected["ok"] is True
                delta = store.get_attempt(binding["attempt_id"]).metadata["export_delta"]  # type: ignore[union-attr]
                assert delta["pending_export"] is True
                assert delta["changed_files"] == [
                    "Desktop/Infinite Tower/game.py",
                    "Desktop/Infinite Tower/README.md",
                ]
            finally:
                coordinator.close()

    asyncio.run(run())


def test_restart_recovers_directory_export_missed_after_provider_success() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_bundle_restart_") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        desktop = root / "Desktop"
        workspace.mkdir()
        desktop.mkdir()
        database = root / "ledger.sqlite3"

        first_store = WorkLedgerStore(database)
        first = WorkLedgerCoordinator(
            first_store,
            export_service=WorkExportService(first_store, desktop_path=desktop),
        )
        prepared = first.prepare_request(
            _request(
                workspace,
                "Create Infinite Tower and export the application to Desktop.",
            )
        )
        binding = prepared.metadata["work"]
        stage = Path(prepared.metadata["export_plan"]["staging_root"])
        bundle = stage / "Infinite Tower"
        bundle.mkdir()
        (bundle / "game.py").write_text("print('recovered')\n", encoding="utf-8")
        first_store.update_attempt(
            binding["attempt_id"],
            execution_status="succeeded",
            metadata={
                "export_delta": {
                    "available": False,
                    "reason": "staged_export_missing",
                    "artifact_type": "business.proposed_export",
                },
                "git_delta": {
                    "available": False,
                    "reason": "staged_export_missing",
                    "artifact_type": "business.proposed_export",
                },
            },
        )
        first.close()
        first_store.close()

        reopened_store = WorkLedgerStore(database)
        reopened = WorkLedgerCoordinator(
            reopened_store,
            export_service=WorkExportService(reopened_store, desktop_path=desktop),
        )
        try:
            reopened.configure()
            pending = reopened_store.list_permission_requests(
                binding["work_item_id"],
                attempt_id=binding["attempt_id"],
                status="pending",
            )
            assert len(pending) == 1
            attempt = reopened_store.get_attempt(binding["attempt_id"])
            assert attempt is not None
            assert attempt.metadata["export_delta"]["reason"] == "external_export_pending"
            assert attempt.metadata["export_delta"]["changed_files"] == [
                "Desktop/Infinite Tower/game.py"
            ]
            canvas = reopened.selected_canvas()
            assert canvas is not None and canvas["permissionVisible"] is True
            assert canvas["permissionRequest"]["id"] == pending[0].request_id

            # A crash after the permission row but before delta projection is
            # also idempotent: replay repairs Diff without creating a second
            # card for the same attempt batch.
            reopened_store.update_attempt(
                binding["attempt_id"],
                metadata={
                    "export_delta": {
                        "available": False,
                        "reason": "staged_export_missing",
                        "artifact_type": "business.proposed_export",
                    },
                    "git_delta": {
                        "available": False,
                        "reason": "staged_export_missing",
                        "artifact_type": "business.proposed_export",
                    },
                },
            )
            reopened._recover_unclaimed_staged_exports()
            replayed = reopened_store.list_permission_requests(
                binding["work_item_id"],
                attempt_id=binding["attempt_id"],
                status="pending",
            )
            assert [request.request_id for request in replayed] == [
                pending[0].request_id
            ]
            replayed_attempt = reopened_store.get_attempt(binding["attempt_id"])
            assert replayed_attempt is not None
            assert (
                replayed_attempt.metadata["export_delta"]["reason"]
                == "external_export_pending"
            )
        finally:
            reopened.close()
            reopened_store.close()


def test_coordinator_prepares_export_from_workspace_capabilities_not_provider_id() -> None:
    with tempfile.TemporaryDirectory(prefix="provider_neutral_export_intake_") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        desktop = root / "Desktop"
        workspace.mkdir()
        desktop.mkdir()
        store = WorkLedgerStore(root / "ledger.sqlite3")
        coordinator = WorkLedgerCoordinator(
            store,
            export_service=WorkExportService(store, desktop_path=desktop),
        )
        try:
            request = ProviderRunRequest(
                provider="codex",
                task="Create result.txt for the desktop.",
                cwd=str(workspace),
                mode="agent",
                metadata={
                    "provider_manifest": DIRECT_CODEX_MANIFEST.to_dict(),
                    "external_export": {
                        "target": "desktop",
                        "intent_source": "source_user_text",
                    },
                },
                requirements=ProviderRequirements(
                    task_kind="workspace_mutation",
                    workspace_access="write",
                    preferred_provider="codex",
                    preference_policy="require",
                ),
            )

            prepared = coordinator.prepare_request(request)
            assert prepared.provider == "codex"
            assert "AMADEUS TWO-PHASE EXPORT POLICY" in prepared.task
            assert prepared.metadata["export_plan"]["provider"] == "codex"
            assert Path(prepared.metadata["export_plan"]["staging_root"]).is_relative_to(
                Path(prepared.cwd).resolve()
            )
        finally:
            coordinator.close()


def test_missing_attempt_delta_emits_renderable_empty_diff_canvas() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_diff_unavailable_") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = WorkLedgerStore(root / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(store)
            prepared = coordinator.prepare_request(
                _request(workspace, "Inspect this legacy attempt")
            )
            attempt_id = str(prepared.metadata["work"]["attempt_id"])
            canvases: list[dict] = []

            async def capture_canvas(_method: str, payload: dict) -> None:
                canvases.append(payload)

            bus.on(Method.WALLPAPER_CANVAS, capture_canvas)
            try:
                result = await CanvasActionRouter(
                    provider_inspect=coordinator.route_provider_inspection
                ).route(
                    {
                        "target": "provider",
                        "action": "view_diff",
                        "attempt_id": attempt_id,
                    }
                )
            finally:
                bus.off(Method.WALLPAPER_CANVAS, capture_canvas)

            assert result["handled"] is True and result["ok"] is True
            assert result["target"] == "provider" and result["action"] == "view_diff"
            assert canvases
            canvas = canvases[-1]
            assert canvas["mode"] == "diff"
            assert canvas["reasonCode"] == "attempt_diff_unavailable"
            assert canvas["diff"]["available"] is False
            assert canvas["diff"]["reasonCode"] == "attempt_diff_unavailable"
            assert canvas["diff"]["files"] == []
            assert canvas["diff"]["message"]
            coordinator.close()

    asyncio.run(run())


def test_ambiguous_delta_without_hunks_is_not_presented_as_a_diff() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_diff_ambiguous_") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = WorkLedgerStore(root / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(store)
            prepared = coordinator.prepare_request(
                _request(workspace, "Change the existing game")
            )
            attempt_id = str(prepared.metadata["work"]["attempt_id"])
            store.update_attempt(
                attempt_id,
                metadata={
                    "git_delta": {
                        "available": True,
                        "baseline_head": "",
                        "current_head": "",
                        "changed_files": ["two_player_maze.html"],
                        "untracked": ["two_player_maze.html"],
                        "ambiguous_paths": ["two_player_maze.html"],
                        "conflicts": [
                            "pre-existing dirty paths changed during the attempt"
                        ],
                        "patch": "",
                    }
                },
            )
            canvases: list[dict] = []

            async def capture_canvas(_method: str, payload: dict) -> None:
                canvases.append(payload)

            bus.on(Method.WALLPAPER_CANVAS, capture_canvas)
            try:
                result = await coordinator.route_provider_inspection(
                    {"action": "view_diff", "attempt_id": attempt_id}
                )
            finally:
                bus.off(Method.WALLPAPER_CANVAS, capture_canvas)

            try:
                assert result["handled"] is True and result["ok"] is True
                canvas = canvases[-1]
                assert canvas["reasonCode"] == "attempt_diff_ambiguous"
                assert canvas["diff"]["available"] is False
                assert canvas["diff"]["clean"] is False
                assert canvas["diff"]["ambiguousPaths"] == ["two_player_maze.html"]
                assert "no trustworthy diff" in canvas["diff"]["message"]
                assert "ambiguous" in canvas["lead"].lower()
            finally:
                coordinator.close()

    asyncio.run(run())


def test_exported_game_amendment_keeps_workspace_lineage_and_replaces_same_target() -> None:
    """Assembly regression for the real one-player -> two-player failure."""

    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_export_amend_integration_") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            desktop = root / "Desktop"
            workspace.mkdir()
            desktop.mkdir()
            store = WorkLedgerStore(root / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(
                store,
                export_service=WorkExportService(store, desktop_path=desktop),
            )
            try:
                first = coordinator.prepare_request(
                    _request(workspace, "Create endless_game.html on the Desktop")
                )
                first_work = first.metadata["work"]
                first_stage = (
                    Path(first.metadata["export_plan"]["staging_root"])
                    / "endless_game.html"
                )
                original = "<html><p>one player</p></html>\n"
                first_stage.write_text(original, encoding="utf-8")
                await _finish(coordinator, first, run_id="locus-game-one")
                first_permission = store.list_permission_requests(
                    first_work["work_item_id"],
                    attempt_id=first_work["attempt_id"],
                    status="pending",
                )[0]
                handler = WorkLedgerHandler(coordinator)
                await handler.route_action(
                    {
                        "target": "permission",
                        "action": "allow_once",
                        "permission_request_id": first_permission.request_id,
                        "work_item_id": first_work["work_item_id"],
                        "attempt_id": first_work["attempt_id"],
                        "revision": coordinator.snapshot()["revision"],
                    }
                )
                assert (desktop / "endless_game.html").read_text(encoding="utf-8") == original

                amend_request = _request(
                    workspace,
                    "Add a second player to endless_game.html",
                )
                amend_request.metadata.update(
                    {
                        "intent": "amend",
                        "related_work_item_id": first_work["work_item_id"],
                    }
                )
                amend = coordinator.prepare_request(amend_request)
                amend_work = amend.metadata["work"]
                assert amend.metadata["export_plan"]["replace_existing"] is True
                assert amend.metadata["related_work_item_id"] == first_work["work_item_id"]
                amend_stage = (
                    Path(amend.metadata["export_plan"]["staging_root"])
                    / "endless_game.html"
                )
                assert amend_stage.read_text(encoding="utf-8") == original
                revised = "<html><p>two players</p></html>\n"
                amend_stage.write_text(revised, encoding="utf-8")
                await _finish(coordinator, amend, run_id="locus-game-two")
                amend_permission = store.list_permission_requests(
                    amend_work["work_item_id"],
                    attempt_id=amend_work["attempt_id"],
                    status="pending",
                )[0]
                assert amend_permission.metadata["replaces_existing"] is True
                await handler.route_action(
                    {
                        "target": "permission",
                        "action": "allow_once",
                        "permission_request_id": amend_permission.request_id,
                        "work_item_id": amend_work["work_item_id"],
                        "attempt_id": amend_work["attempt_id"],
                        "revision": coordinator.snapshot()["revision"],
                    }
                )
                assert (desktop / "endless_game.html").read_text(encoding="utf-8") == revised
                assert list(desktop.glob("endless_game*.html")) == [
                    desktop / "endless_game.html"
                ]
            finally:
                coordinator.close()

    asyncio.run(run())


def test_missing_staged_desktop_file_is_partial_without_fake_approval() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_export_missing_") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            desktop = root / "Desktop"
            database = root / "ledger.sqlite3"
            workspace.mkdir()
            desktop.mkdir()
            store = WorkLedgerStore(database)
            coordinator = WorkLedgerCoordinator(
                store,
                export_service=WorkExportService(store, desktop_path=desktop),
            )
            prepared = coordinator.prepare_request(
                _request(workspace, "Create note.txt on the Desktop")
            )
            await _finish(coordinator, prepared, run_id="locus-export-missing")
            binding = prepared.metadata["work"]
            attempt = store.get_attempt(binding["attempt_id"])
            assessment = store.latest_completion(binding["work_item_id"])
            assert attempt is not None and assessment is not None
            assert attempt.metadata["export_delta"]["reason"] == "staged_export_missing"
            assert assessment.completeness == "partial"
            assert assessment.attention == "review"
            assert store.list_permission_requests(binding["work_item_id"]) == []
            assert list(desktop.iterdir()) == []
            coordinator.close()

    asyncio.run(run())


def test_failed_or_cancelled_attempt_never_promotes_staged_byproducts() -> None:
    async def run() -> None:
        for terminal_status in ("failed", "cancelled"):
            with tempfile.TemporaryDirectory(
                prefix=f"work_export_{terminal_status}_"
            ) as temp:
                root = Path(temp)
                workspace = root / "workspace"
                desktop = root / "Desktop"
                workspace.mkdir()
                desktop.mkdir()
                store = WorkLedgerStore(root / "ledger.sqlite3")
                coordinator = WorkLedgerCoordinator(
                    store,
                    export_service=WorkExportService(store, desktop_path=desktop),
                )
                prepared = coordinator.prepare_request(
                    _request(
                        workspace,
                        f"Create {terminal_status}.txt on the Desktop",
                    )
                )
                binding = prepared.metadata["work"]
                staged = (
                    Path(prepared.metadata["export_plan"]["staging_root"])
                    / f"{terminal_status}.txt"
                )
                staged.write_text(
                    "provider left an unverified partial file\n",
                    encoding="utf-8",
                )

                await _finish(
                    coordinator,
                    prepared,
                    run_id=f"locus-export-{terminal_status}",
                    status=terminal_status,
                    result="provider did not finish the requested task",
                )

                attempt = store.get_attempt(binding["attempt_id"])
                assert attempt is not None
                assert attempt.execution_status == terminal_status
                assert attempt.metadata["export_delta"]["reason"] == "staged_export_unverified"
                assert attempt.metadata["export_delta"]["changed_files"] == []
                assert attempt.metadata["export_delta"]["pending_export"] is False
                assert store.list_permission_requests(
                    binding["work_item_id"],
                    attempt_id=binding["attempt_id"],
                ) == []
                assert list(desktop.iterdir()) == []
                coordinator.close()

    asyncio.run(run())


def test_provider_permission_event_is_durable_and_idempotent() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="provider_permission_ledger_") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = WorkLedgerStore(root / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(store)
            prepared = coordinator.prepare_request(
                _request(workspace, "Modify the repository README")
            )
            event = {
                "provider": "locus",
                "run_id": "locus-permission-1",
                "type": "permission.requested",
                "metadata": {
                    **dict(prepared.metadata),
                    "locus_sequence": 12,
                },
                "payload": {
                    "toolName": "Write",
                    "toolUseId": "tool-write-12",
                    "capability": "filesystem.write",
                    "action": "write_file",
                    "scope": {"kind": "path", "path": str(workspace / "README.md")},
                    "reason": "Explicit approval is required.",
                    "diagnosticOnly": True,
                    "retryRequired": True,
                    "options": [
                        {"id": "approve_once", "kind": "allow_once"},
                        {"id": "reject", "kind": "reject_once"},
                    ],
                },
            }
            await coordinator._on_provider_event("provider.event", event)
            await coordinator._on_provider_event("provider.event", event)
            workspace_event = {
                **event,
                "metadata": {
                    **dict(prepared.metadata),
                    "locus_sequence": 13,
                },
                "payload": {
                    **event["payload"],
                    "toolUseId": "tool-workspace-13",
                    "scope": {"kind": "workspace", "path": ""},
                },
            }
            await coordinator._on_provider_event("provider.event", workspace_event)
            await coordinator._on_provider_event("provider.event", workspace_event)
            duplicate_workspace_event = {
                **workspace_event,
                "metadata": {
                    **dict(prepared.metadata),
                    "locus_sequence": 14,
                },
                "payload": {
                    **workspace_event["payload"],
                    "toolUseId": "tool-workspace-14",
                },
            }
            await coordinator._on_provider_event(
                "provider.event", duplicate_workspace_event
            )
            multi_scope_event = {
                **event,
                "metadata": {
                    **dict(prepared.metadata),
                    "locus_sequence": 15,
                },
                "payload": {
                    **event["payload"],
                    "toolUseId": "tool-multi-15",
                    "capability": "FileSystem.Write",
                    "action": "Write_File",
                    "scope": [
                        str(workspace / "README.md"),
                        str(workspace / "notes.md"),
                    ],
                    "reasonCode": "SHELL_APPROVAL",
                },
            }
            await coordinator._on_provider_event("provider.event", multi_scope_event)
            await coordinator._on_provider_event(
                "provider.event",
                {
                    **multi_scope_event,
                    "metadata": {
                        **dict(prepared.metadata),
                        "locus_sequence": 16,
                    },
                    "payload": {
                        **multi_scope_event["payload"],
                        "toolUseId": "tool-multi-16",
                        "capability": "filesystem.write",
                        "action": "write_file",
                        "scope": [
                            str(workspace / "notes.md").upper(),
                            str(workspace / "README.md").upper(),
                        ],
                        "reasonCode": "shell_approval",
                    },
                },
            )
            binding = prepared.metadata["work"]
            requests = store.list_permission_requests(binding["work_item_id"])
            assert len(requests) == 3
            exact_path = next(
                request
                for request in requests
                if request.metadata["provider_request_id"] == "tool-write-12"
            )
            workspace_scope = next(
                request
                for request in requests
                if request.metadata["provider_request_id"] == "tool-workspace-13"
            )
            multi_scope = next(
                request
                for request in requests
                if request.metadata["provider_request_id"] == "tool-multi-15"
            )
            item = store.get_work_item(binding["work_item_id"])
            assert item is not None
            assert exact_path.scope_paths == [str(workspace / "README.md")]
            assert workspace_scope.scope_paths == [item.workspace_path]
            assert multi_scope.scope_paths == [
                str(workspace / "README.md"),
                str(workspace / "notes.md"),
            ]
            assert multi_scope.capability == "filesystem.write"
            assert multi_scope.action == "write_file"
            assert exact_path.options == ["deny"]
            assert exact_path.metadata["retry_required"] is True
            assert exact_path.status == "denied"
            assert workspace_scope.status == "denied"
            assert exact_path.metadata["resolution"] == "provider_denied"
            assert exact_path.metadata["resolved_automatically"] is True
            assert store.list_permission_requests(
                binding["work_item_id"],
                attempt_id=binding["attempt_id"],
                status="pending",
            ) == []
            projected = coordinator.snapshot()["selected"]
            assert projected["attention"] != "permission"
            assert projected["pendingPermissionCount"] == 0
            facts = coordinator._event_fact("locus-permission-1")
            assert facts["permission_failure_suppressions"] == 5
            for _ in range(5):
                coordinator._record_tool_evidence(
                    "locus-permission-1",
                    {"tool": "shell", "ok": False},
                )
            assert facts["conflicts"] == []
            coordinator._record_tool_evidence(
                "locus-permission-1",
                {"tool": "unrelated", "ok": False},
            )
            assert facts["conflicts"] == []
            assert facts["tool_diagnostics"][-1]["tool"] == "unrelated"
            coordinator.close()

    asyncio.run(run())


def test_tool_failure_evidence_uses_identity_and_epistemic_classification() -> None:
    with tempfile.TemporaryDirectory(prefix="tool_evidence_contract_") as temp:
        store = WorkLedgerStore(Path(temp) / "ledger.sqlite3")
        coordinator = WorkLedgerCoordinator(store)
        try:
            facts = coordinator._event_fact("provider-neutral-run")
            facts["provider_permission_tool_ids"] = ["permission-tool"]
            facts["permission_failure_suppressions"] = 1

            coordinator._record_tool_evidence(
                "provider-neutral-run",
                {
                    "tool": "PowerShell",
                    "tool_use_id": "permission-tool",
                    "ok": False,
                },
            )
            assert facts["conflicts"] == []
            assert facts["permission_failure_suppressions"] == 0

            coordinator._record_tool_evidence(
                "provider-neutral-run",
                {
                    "tool": "PowerShell",
                    "tool_use_id": "unverified-check",
                    "ok": False,
                },
            )
            assert facts["conflicts"] == []
            assert facts["tool_diagnostics"][0]["classification"] == "unverified"

            coordinator._record_tool_evidence(
                "provider-neutral-run",
                {
                    "tool": "Write",
                    "tool_use_id": "failed-write",
                    "ok": False,
                },
            )
            assert facts["conflicts"] == []
            assert facts["tool_diagnostics"][-1]["classification"] == "failed"

            coordinator._record_tool_evidence(
                "provider-neutral-run",
                {
                    "tool": "PowerShell",
                    "tool_use_id": "failed-shell",
                    "ok": False,
                    "error": "command was denied",
                },
            )
            assert facts["conflicts"] == []
            assert facts["tool_diagnostics"][-1]["classification"] == "failed"
        finally:
            coordinator.close()


def test_unverified_tool_failure_is_durable_diagnostic_not_completion_truth() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="tool_evidence_completion_") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = WorkLedgerStore(root / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(store)
            try:
                prepared = coordinator.prepare_request(
                    _request(workspace, "Create result.txt and verify it")
                )
                binding = prepared.metadata["work"]
                run_id = "provider-neutral-unverified-run"
                await coordinator._on_provider_event(
                    "provider.event",
                    {
                        "provider": prepared.provider,
                        "run_id": run_id,
                        "type": "tool.result",
                        "metadata": dict(prepared.metadata),
                        "payload": {
                            "tool": "PowerShell",
                            "tool_use_id": "syntax-check",
                            "ok": False,
                        },
                    },
                )
                await _finish(coordinator, prepared, run_id=run_id)

                attempt = store.get_attempt(binding["attempt_id"])
                assert attempt is not None
                assert attempt.metadata["tool_evidence"]["unverified"][0][
                    "tool_use_id"
                ] == "syntax-check"
                completion = store.list_completions(binding["work_item_id"])[-1]
                assert completion.attention == "review"
                assert "goal completeness still needs user review" in completion.rationale
                assert "conflict" not in completion.rationale
            finally:
                coordinator.close()

    asyncio.run(run())


def test_generic_filesystem_export_permission_has_no_desktop_semantics() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="generic_export_permission_") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            desktop = root / "Desktop"
            workspace.mkdir()
            desktop.mkdir()
            store = WorkLedgerStore(root / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(
                store,
                export_service=WorkExportService(store, desktop_path=desktop),
            )
            prepared = coordinator.prepare_request(
                _request(workspace, "Approve a provider-managed export operation")
            )
            binding = prepared.metadata["work"]
            await coordinator._on_provider_event(
                "provider.event",
                {
                    "provider": "locus",
                    "run_id": "generic-provider-export",
                    "type": "permission.requested",
                    "metadata": dict(prepared.metadata),
                    "payload": {
                        "toolName": "ProviderExport",
                        "toolUseId": "generic-export-1",
                        "capability": "filesystem.export",
                        "action": "provider_upload",
                        "scope": {
                            "kind": "path",
                            "path": str(desktop / "must-not-be-created.txt"),
                        },
                        "reason": "The provider requests an unrelated export capability.",
                        "diagnosticOnly": True,
                        "options": ["allow_once", "deny"],
                    },
                },
            )
            permission = store.list_permission_requests(
                binding["work_item_id"],
                attempt_id=binding["attempt_id"],
            )[0]
            assert permission.capability == "filesystem.export"
            assert permission.action == "provider_upload"
            assert permission.metadata["kind"] == "provider_permission"
            assert permission.status == "denied"
            store.update_attempt(binding["attempt_id"], execution_status="succeeded")

            # Provider permission events are retrospective diagnostics.  The
            # host cannot grant policy to the already-stopped provider, and a
            # forged Canvas action must not turn a deny-only contract into an
            # approval.
            assert permission.options == ["deny"]
            handler = WorkLedgerHandler(coordinator)
            rejected_allow = await handler.route_action(
                {
                    "target": "permission",
                    "action": "allow_once",
                    "permission_request_id": permission.request_id,
                    "work_item_id": binding["work_item_id"],
                    "attempt_id": binding["attempt_id"],
                    "revision": coordinator.snapshot()["revision"],
                }
            )
            assert rejected_allow["ok"] is False
            assert rejected_allow["error"] == "permission_request_not_current"
            assert list(desktop.iterdir()) == []
            selected = coordinator.snapshot()["selected"]
            assert selected["state"] == "open"
            assert selected["state"] != "review_ready"
            assert selected["attention"] != "permission"
            attempt = store.get_attempt(binding["attempt_id"])
            assert attempt is not None
            assert "external_export_complete" not in str(attempt.metadata)
            assert not any(
                artifact.kind == "business.export"
                for artifact in store.list_artifacts(binding["work_item_id"])
            )
            coordinator.close()

    asyncio.run(run())


def test_authorized_desktop_export_recovers_on_coordinator_restart() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_export_restart_recovery_") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            desktop = root / "Desktop"
            database = root / "ledger.sqlite3"
            workspace.mkdir()
            desktop.mkdir()

            store = WorkLedgerStore(database)
            service = WorkExportService(store, desktop_path=desktop)
            coordinator = WorkLedgerCoordinator(store, export_service=service)
            prepared = coordinator.prepare_request(
                _request(workspace, "Create restart.txt on the Desktop")
            )
            binding = prepared.metadata["work"]
            staged = Path(prepared.metadata["export_plan"]["staging_root"]) / "restart.txt"
            staged.write_text("restart-safe bytes\n", encoding="utf-8")
            await _finish(coordinator, prepared, run_id="locus-export-restart")
            permission = store.list_permission_requests(
                binding["work_item_id"],
                attempt_id=binding["attempt_id"],
                status="pending",
            )[0]
            original_publish = service._publish_atomic_no_replace

            def interrupt_after_atomic_publish(*args, **kwargs):
                original_publish(*args, **kwargs)
                raise RuntimeError("simulated process interruption")

            with patch.object(
                service,
                "_publish_atomic_no_replace",
                side_effect=interrupt_after_atomic_publish,
            ):
                try:
                    await coordinator.resolve_permission(
                        permission.request_id,
                        allow=True,
                        work_item_id=binding["work_item_id"],
                        attempt_id=binding["attempt_id"],
                    )
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("the simulated interruption must escape")

            assert (desktop / "restart.txt").read_text(encoding="utf-8") == "restart-safe bytes\n"
            failed = store.latest_completion(binding["work_item_id"])
            assert failed is not None and failed.attention == "conflict"
            coordinator.close()

            reopened_store = WorkLedgerStore(database)
            reopened = WorkLedgerCoordinator(
                reopened_store,
                export_service=WorkExportService(reopened_store, desktop_path=desktop),
            )
            reopened.configure()
            recovered = reopened.snapshot()["selected"]
            assert recovered["state"] == "review_ready"
            assert recovered["attention"] == "review"
            recovered_attempt = reopened_store.get_attempt(binding["attempt_id"])
            assert recovered_attempt is not None
            assert recovered_attempt.metadata["export_resolution"]["status"] == "committed"
            assert recovered_attempt.metadata["export_delta"]["reason"] == "external_export_complete"
            assert [
                artifact.status
                for artifact in reopened_store.list_artifacts(binding["work_item_id"])
                if artifact.kind == "business.export"
            ] == ["approved"]
            reopened.close()

    asyncio.run(run())


def test_interrupted_export_projects_retry_card_and_recovers_in_same_session() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_export_live_recovery_") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            desktop = root / "Desktop"
            workspace.mkdir()
            desktop.mkdir()
            store = WorkLedgerStore(root / "ledger.sqlite3")
            service = WorkExportService(store, desktop_path=desktop)
            coordinator = WorkLedgerCoordinator(store, export_service=service)
            prepared = coordinator.prepare_request(
                _request(workspace, "Create recover-live.txt on the Desktop")
            )
            binding = prepared.metadata["work"]
            staged = (
                Path(prepared.metadata["export_plan"]["staging_root"])
                / "recover-live.txt"
            )
            staged.write_text("recover this exact publication\n", encoding="utf-8")
            await _finish(coordinator, prepared, run_id="locus-export-live-recovery")
            permission = store.list_permission_requests(
                binding["work_item_id"],
                attempt_id=binding["attempt_id"],
                status="pending",
            )[0]
            original_publish = service._publish_atomic_no_replace

            def interrupt_after_publish(*args, **kwargs):
                original_publish(*args, **kwargs)
                raise RuntimeError("simulated interruption before commit")

            with patch.object(
                service,
                "_publish_atomic_no_replace",
                side_effect=interrupt_after_publish,
            ):
                try:
                    await coordinator.resolve_permission(
                        permission.request_id,
                        allow=True,
                        work_item_id=binding["work_item_id"],
                        attempt_id=binding["attempt_id"],
                    )
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("the simulated interruption must escape")

            projected = coordinator.snapshot()["selected"]
            assert projected["attention"] == "conflict"
            assert projected["recoverableExportRequestId"] == permission.request_id
            recovery_canvas = coordinator.selected_canvas()
            assert recovery_canvas is not None
            assert recovery_canvas["title"] == "Export recovery required"
            assert recovery_canvas["permissionVisible"] is True
            assert recovery_canvas["permissionRequest"]["id"] == permission.request_id
            assert recovery_canvas["permissionRequest"]["status"] == "allowed"
            assert recovery_canvas["permissionRequest"]["options"] == [
                "retry_export",
                "abandon_export",
            ]
            assert len(
                store.list_permission_requests(
                    binding["work_item_id"],
                    attempt_id=binding["attempt_id"],
                )
            ) == 1

            handler = WorkLedgerHandler(coordinator)
            router = CanvasActionRouter(work_action=handler.route_action)
            recovered = await router.route(
                {
                    "target": "permission",
                    "action": "retry_export",
                    "permission_request_id": permission.request_id,
                    "work_item_id": binding["work_item_id"],
                    "attempt_id": binding["attempt_id"],
                    "revision": coordinator.snapshot()["revision"],
                }
            )
            assert recovered["ok"] is True
            assert recovered["action"] == "retry_export"
            assert recovered["exportedPaths"] == [str(desktop / "recover-live.txt")]
            assert (desktop / "recover-live.txt").read_text(encoding="utf-8") == (
                "recover this exact publication\n"
            )
            assert len(
                store.list_permission_requests(
                    binding["work_item_id"],
                    attempt_id=binding["attempt_id"],
                )
            ) == 1
            assert store.get_permission_request(permission.request_id).status == "allowed"  # type: ignore[union-attr]
            final_attempt = store.get_attempt(binding["attempt_id"])
            assert final_attempt is not None
            assert final_attempt.metadata["export_resolution"]["status"] == "committed"
            assert final_attempt.metadata["export_delta"]["reason"] == "external_export_complete"
            coordinator.close()

    asyncio.run(run())


def test_committed_export_without_completion_reconciles_on_restart() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_export_completion_recovery_") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            desktop = root / "Desktop"
            database = root / "ledger.sqlite3"
            workspace.mkdir()
            desktop.mkdir()

            store = WorkLedgerStore(database)
            service = WorkExportService(store, desktop_path=desktop)
            coordinator = WorkLedgerCoordinator(store, export_service=service)
            prepared = coordinator.prepare_request(
                _request(workspace, "Create committed.txt on the Desktop")
            )
            binding = prepared.metadata["work"]
            staged = Path(prepared.metadata["export_plan"]["staging_root"]) / "committed.txt"
            staged.write_text("committed before completion\n", encoding="utf-8")
            await _finish(coordinator, prepared, run_id="locus-export-completion-gap")
            permission = store.list_permission_requests(
                binding["work_item_id"],
                attempt_id=binding["attempt_id"],
                status="pending",
            )[0]

            # Simulate a crash after Desktop publication and the service's
            # committed marker, but before the coordinator can replace the old
            # attention=permission completion assessment.
            with patch.object(
                store,
                "record_completion",
                side_effect=RuntimeError("simulated completion crash"),
            ):
                try:
                    await coordinator.resolve_permission(
                        permission.request_id,
                        allow=True,
                        work_item_id=binding["work_item_id"],
                        attempt_id=binding["attempt_id"],
                    )
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("the simulated completion crash must escape")

            committed_attempt = store.get_attempt(binding["attempt_id"])
            assert committed_attempt is not None
            assert committed_attempt.metadata["export_resolution"]["status"] == "committed"
            assert store.get_permission_request(permission.request_id).status == "allowed"  # type: ignore[union-attr]
            assert (desktop / "committed.txt").is_file()
            assert coordinator.snapshot()["selected"]["attention"] == "permission"
            coordinator.close()

            reopened_store = WorkLedgerStore(database)
            reopened = WorkLedgerCoordinator(
                reopened_store,
                export_service=WorkExportService(reopened_store, desktop_path=desktop),
            )
            reopened.configure()
            reconciled = reopened.snapshot()["selected"]
            assert reconciled["state"] == "review_ready"
            assert reconciled["attention"] == "review"
            canvas = reopened.selected_canvas()
            assert canvas is not None
            assert canvas.get("permissionVisible") is not True
            assert canvas.get("blocking") is not True
            reopened.close()

    asyncio.run(run())


def test_failed_export_recovery_can_be_abandoned_without_new_side_effects() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_export_abandon_recovery_") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            desktop = root / "Desktop"
            database = root / "ledger.sqlite3"
            workspace.mkdir()
            desktop.mkdir()
            store = WorkLedgerStore(database)
            service = WorkExportService(store, desktop_path=desktop)
            coordinator = WorkLedgerCoordinator(store, export_service=service)
            prepared = coordinator.prepare_request(
                _request(workspace, "Create abandon-me.txt on the Desktop")
            )
            binding = prepared.metadata["work"]
            staged = Path(prepared.metadata["export_plan"]["staging_root"]) / "abandon-me.txt"
            staged.write_text("staged but unavailable during recovery\n", encoding="utf-8")
            await _finish(coordinator, prepared, run_id="locus-export-abandon")
            permission = store.list_permission_requests(
                binding["work_item_id"],
                attempt_id=binding["attempt_id"],
                status="pending",
            )[0]

            with patch.object(
                service,
                "_publish_atomic_no_replace",
                side_effect=RuntimeError("simulated interruption before publication"),
            ):
                try:
                    await coordinator.resolve_permission(
                        permission.request_id,
                        allow=True,
                        work_item_id=binding["work_item_id"],
                        attempt_id=binding["attempt_id"],
                    )
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("the simulated publication interruption must escape")

            staged.unlink()
            handler = WorkLedgerHandler(coordinator)
            router = CanvasActionRouter(work_action=handler.route_action)
            try:
                await router.route(
                    {
                        "target": "permission",
                        "action": "retry_export",
                        "permission_request_id": permission.request_id,
                        "work_item_id": binding["work_item_id"],
                        "attempt_id": binding["attempt_id"],
                        "revision": coordinator.snapshot()["revision"],
                    }
                )
            except WorkLedgerConflict:
                pass
            else:
                raise AssertionError("missing staged bytes must fail recovery")

            recovery_canvas = coordinator.selected_canvas()
            assert recovery_canvas is not None
            assert recovery_canvas["permissionRequest"]["options"] == [
                "retry_export",
                "abandon_export",
            ]
            abandoned = await router.route(
                {
                    "target": "permission",
                    "action": "abandon_export",
                    "permission_request_id": permission.request_id,
                    "work_item_id": binding["work_item_id"],
                    "attempt_id": binding["attempt_id"],
                    "revision": coordinator.snapshot()["revision"],
                }
            )
            assert abandoned["ok"] is True, abandoned
            assert abandoned["action"] == "abandon_export"
            assert abandoned["exportedPaths"] == []
            assert list(desktop.iterdir()) == []
            attempt = store.get_attempt(binding["attempt_id"])
            assert attempt is not None
            assert attempt.metadata["export_resolution"]["status"] == "abandoned"
            selected = coordinator.snapshot()["selected"]
            assert selected["recoverableExportRequestId"] == ""
            assert "canContinue" not in selected
            assert selected["state"] == "open"
            replayed_outcome = service.discover_staged_exports(
                attempt,
                store.get_work_item(binding["work_item_id"]),  # type: ignore[arg-type]
                prepared.metadata["export_plan"],
            )
            assert replayed_outcome["reason"] == "external_export_abandoned"
            assert replayed_outcome["recovery_required"] is False
            assert len(
                store.list_permission_requests(
                    binding["work_item_id"], attempt_id=binding["attempt_id"]
                )
            ) == 1
            coordinator.close()

            reopened_store = WorkLedgerStore(database)
            reopened = WorkLedgerCoordinator(
                reopened_store,
                export_service=WorkExportService(reopened_store, desktop_path=desktop),
            )
            reopened.configure()
            restarted_attempt = reopened_store.get_attempt(binding["attempt_id"])
            assert restarted_attempt is not None
            assert restarted_attempt.metadata["export_resolution"]["status"] == "abandoned"
            assert restarted_attempt.metadata["export_delta"]["reason"] == "external_export_abandoned"
            restarted = reopened.snapshot()["selected"]
            assert restarted["recoverableExportRequestId"] == ""
            assert "canContinue" not in restarted
            assert restarted["state"] == "open"
            assert list(desktop.iterdir()) == []
            reopened.close()

    asyncio.run(run())


def test_restart_never_replays_committed_allow_once_after_user_deletes_target() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_export_no_replay_restart_") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            desktop = root / "Desktop"
            database = root / "ledger.sqlite3"
            workspace.mkdir()
            desktop.mkdir()

            store = WorkLedgerStore(database)
            coordinator = WorkLedgerCoordinator(
                store,
                export_service=WorkExportService(store, desktop_path=desktop),
            )
            prepared = coordinator.prepare_request(
                _request(workspace, "Create one-shot.txt on the Desktop")
            )
            binding = prepared.metadata["work"]
            staged = Path(prepared.metadata["export_plan"]["staging_root"]) / "one-shot.txt"
            staged.write_text("one authorization only\n", encoding="utf-8")
            await _finish(coordinator, prepared, run_id="locus-export-one-shot")
            permission = store.list_permission_requests(
                binding["work_item_id"],
                attempt_id=binding["attempt_id"],
                status="pending",
            )[0]
            await coordinator.resolve_permission(
                permission.request_id,
                allow=True,
                work_item_id=binding["work_item_id"],
                attempt_id=binding["attempt_id"],
            )
            target = desktop / "one-shot.txt"
            target.unlink()
            coordinator.close()

            reopened_store = WorkLedgerStore(database)
            reopened = WorkLedgerCoordinator(
                reopened_store,
                export_service=WorkExportService(reopened_store, desktop_path=desktop),
            )
            reopened.configure()
            assert not target.exists()
            attempt = reopened_store.get_attempt(binding["attempt_id"])
            assert attempt is not None
            assert attempt.metadata["export_resolution"]["status"] == "committed"
            # Startup trusts the committed no-replay receipt and does not hash
            # all historical Desktop bytes.  Integrity is refreshed only when
            # this attempt is explicitly inspected.
            assert attempt.metadata["export_delta"]["reason"] == "external_export_complete"
            selected = reopened.snapshot()["selected"]
            assert selected["recoverableExportRequestId"] == ""
            await reopened.route_provider_inspection(
                {"action": "view_diff", "attempt_id": binding["attempt_id"]}
            )
            refreshed = reopened_store.get_attempt(binding["attempt_id"])
            assert refreshed is not None
            assert refreshed.metadata["export_delta"]["reason"] == "external_export_drift"
            reopened.close()

    asyncio.run(run())


def test_resolved_permission_shell_returns_to_neutral_task_canvas() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="resolved_permission_canvas_") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = WorkLedgerStore(root / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(store)
            prepared = coordinator.prepare_request(
                _request(workspace, "Modify the repository README")
            )
            binding = prepared.metadata["work"]
            item_id = binding["work_item_id"]
            attempt_id = binding["attempt_id"]
            permission = store.create_permission_request(
                item_id,
                attempt_id=attempt_id,
                capability="filesystem.write",
                action="write_file",
                scope_paths=[str(workspace / "README.md")],
                reason="Approval is needed before writing README.md.",
                reversibility="reversible",
                options=["allow_once", "deny"],
                idempotency_key="permission-shell-regression",
            )
            store.update_attempt(attempt_id, execution_status="succeeded")
            coordinator.record_presentation(
                item_id,
                {
                    "schema_id": "amadeus.ai_os.v1",
                    "mode": "permission",
                    "phase": "Checkpoint",
                    "title": "Locus needs permission",
                    "lead": "Approval is needed before writing README.md.",
                    "blocking": True,
                    "permissionVisible": True,
                    "permissionRequest": {"id": permission.request_id},
                    "signals": [
                        {
                            "schema_id": "amadeus.ai_os.v1",
                            "kind": "permission",
                            "label": "permission",
                            "text": "Approval required",
                            "importance": "blocking",
                        },
                        {
                            "schema_id": "amadeus.ai_os.v1",
                            "kind": "status",
                            "label": "checkpoint",
                            "text": "Waiting for approval",
                            "importance": "blocking",
                        },
                    ],
                    "metadata": {
                        "attention": "permission",
                        "work": dict(binding),
                    },
                },
            )

            pending_canvas = coordinator.selected_canvas()
            assert pending_canvas is not None
            assert pending_canvas["permissionVisible"] is True
            assert pending_canvas["blocking"] is True

            await coordinator.resolve_permission(
                permission.request_id,
                allow=True,
                work_item_id=item_id,
                attempt_id=attempt_id,
            )
            resolved_canvas = coordinator.selected_canvas()
            assert resolved_canvas is not None
            assert resolved_canvas["mode"] == "workflow"
            assert resolved_canvas["phase"] == "Review"
            assert resolved_canvas["title"] != "Locus needs permission"
            assert resolved_canvas["lead"] != "Approval is needed before writing README.md."
            assert resolved_canvas.get("blocking") is False
            assert resolved_canvas.get("permissionVisible") is False
            assert "permissionRequest" not in resolved_canvas
            assert all(
                signal.get("kind") != "permission"
                and signal.get("importance") != "blocking"
                and signal.get("label") != "checkpoint"
                for signal in resolved_canvas["signals"]
            )
            assert resolved_canvas["workContext"]["workItemId"] == item_id
            assert resolved_canvas["taskDock"]["selectedWorkItemId"] == item_id

            # A real report/diff presentation must remain intact after the
            # transient approval overlay has gone away.
            diff = {"files": [{"path": "README.md", "hunks": []}], "available": True}
            coordinator.record_presentation(
                item_id,
                {
                    "schema_id": "amadeus.ai_os.v1",
                    "mode": "diff",
                    "phase": "Preview",
                    "title": "README diff",
                    "lead": "One attributed file",
                    "diff": diff,
                    "reportMarkdown": "# Run report\n\nValidated README change.",
                    "signals": [],
                },
            )
            diff_canvas = coordinator.selected_canvas()
            assert diff_canvas is not None
            assert diff_canvas["mode"] == "diff"
            assert diff_canvas["title"] == "README diff"
            assert diff_canvas["diff"] == diff
            assert diff_canvas["reportMarkdown"].startswith("# Run report")
            coordinator.close()

    asyncio.run(run())


def _main() -> None:
    test_empty_provider_artifacts_still_create_diff_permission_and_exact_export()
    test_binary_export_identity_survives_permission_projection_and_restart()
    test_nested_application_bundle_projects_an_actionable_permission_card()
    test_restart_recovers_directory_export_missed_after_provider_success()
    test_missing_attempt_delta_emits_renderable_empty_diff_canvas()
    test_ambiguous_delta_without_hunks_is_not_presented_as_a_diff()
    test_missing_staged_desktop_file_is_partial_without_fake_approval()
    test_failed_or_cancelled_attempt_never_promotes_staged_byproducts()
    test_provider_permission_event_is_durable_and_idempotent()
    test_generic_filesystem_export_permission_has_no_desktop_semantics()
    test_authorized_desktop_export_recovers_on_coordinator_restart()
    test_interrupted_export_projects_retry_card_and_recovers_in_same_session()
    test_committed_export_without_completion_reconciles_on_restart()
    test_failed_export_recovery_can_be_abandoned_without_new_side_effects()
    test_restart_never_replays_committed_allow_once_after_user_deletes_target()
    test_resolved_permission_shell_returns_to_neutral_task_canvas()
    print("ok: staged Desktop export is durable, reviewable, and approval-gated")


if __name__ == "__main__":
    _main()
