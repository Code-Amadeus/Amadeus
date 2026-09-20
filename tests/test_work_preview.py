"""Host-owned static Work Preview discovery, serving, and ledger binding."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.work_ledger_store import WorkLedgerStore
from server.canvas_action_router import CanvasActionRouter
from server.handlers.work_ledger_handler import WorkLedgerHandler
from server.protocol import Method
from server.work_ledger_coordinator import WorkLedgerCoordinator
from server.work_preview import (
    WorkPreviewError,
    WorkPreviewHandler,
    WorkPreviewManager,
    _discover_static_entry,
)


def _ledger_item(
    store: WorkLedgerStore,
    workspace: Path,
    *,
    attempt_metadata: dict[str, Any] | None = None,
) -> tuple[str, str]:
    project = store.create_or_get_project(workspace, name="Preview project")
    item = store.create_work_item(
        project.project_id,
        title="Build a web page",
        workspace_path=workspace,
    )
    attempt = store.create_attempt(
        item.work_item_id,
        provider="codex",
        task="Build it",
        metadata=attempt_metadata,
    )
    return item.work_item_id, attempt.attempt_id


def test_static_discovery_is_bounded_and_fails_closed_on_ambiguity() -> None:
    with tempfile.TemporaryDirectory(prefix="work_preview_discovery_") as temp:
        root = Path(temp)
        (root / ".git").mkdir()
        (root / ".git" / "index.html").write_text("git", encoding="utf-8")
        (root / "node_modules" / "pkg").mkdir(parents=True)
        (root / "node_modules" / "pkg" / "index.html").write_text(
            "dependency", encoding="utf-8"
        )
        app = root / "app"
        app.mkdir()
        (app / "index.html").write_text("app", encoding="utf-8")

        found = _discover_static_entry(root)
        assert found.status == "ready"
        assert found.entry == (app / "index.html").resolve()
        assert found.web_root == app.resolve()

        other = root / "other"
        other.mkdir()
        (other / "index.html").write_text("other", encoding="utf-8")
        ambiguous = _discover_static_entry(root)
        assert ambiguous.status == "ambiguous"
        assert ambiguous.error == "multiple_preview_entries"

        # A root entry is an explicit, stable convention and wins over nested
        # demo/test fixtures without guessing between them.
        (root / "index.html").write_text("root", encoding="utf-8")
        direct = _discover_static_entry(root)
        assert direct.status == "ready"
        assert direct.entry == (root / "index.html").resolve()


def test_preview_binds_ledger_identity_serves_only_web_assets_and_hot_reloads() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_preview_runtime_") as temp:
            workspace = Path(temp) / "project"
            workspace.mkdir()
            (workspace / "index.html").write_text(
                '<!doctype html><script src="app.js"></script>',
                encoding="utf-8",
            )
            (workspace / "app.js").write_text("window.value = 1", encoding="utf-8")
            (workspace / "assets").mkdir()
            (workspace / "assets" / "absolute.js").write_text(
                "window.absolute = true", encoding="utf-8"
            )
            (workspace / "secret.py").write_text("TOKEN='no'", encoding="utf-8")
            (workspace / ".env").write_text("SECRET=no", encoding="utf-8")
            events: list[tuple[str, dict[str, Any]]] = []

            async def publish(method: str, params: dict[str, Any]) -> None:
                events.append((method, params))

            with WorkLedgerStore(Path(temp) / "ledger.sqlite3") as store:
                work_item_id, attempt_id = _ledger_item(store, workspace)
                coordinator = WorkLedgerCoordinator(store)
                coordinator.select(work_item_id)
                manager = WorkPreviewManager(
                    store,
                    publisher=publish,
                    poll_interval=0.03,
                    debounce=0.03,
                )
                handler = WorkPreviewHandler(coordinator, manager)
                snapshot = coordinator.snapshot()
                opened = await handler.open_from_work_action(
                    {
                        "workItemId": work_item_id,
                        "attemptId": attempt_id,
                        "revision": snapshot["revision"],
                        # These untrusted values are deliberately irrelevant.
                        "cwd": str(Path(temp).parent),
                        "url": "https://example.invalid/steal",
                        "command": "python -m http.server",
                    }
                )
                assert opened["ok"] is True
                preview = opened["preview"]
                assert preview["status"] == "ready"
                assert preview["attemptId"] == attempt_id
                assert preview["entry"] == "index.html"
                assert preview["url"].startswith("http://127.0.0.1:")
                assert any(method == Method.WORK_PREVIEW_OPEN_REQUESTED for method, _ in events)

                html, response_headers = await asyncio.to_thread(
                    _read_response,
                    preview["url"],
                )
                assert b"app.js" in html
                cookie = response_headers.get("Set-Cookie", "").split(";", 1)[0]
                assert cookie.startswith("amadeus_preview=")
                app_url = preview["url"].rsplit("/", 1)[0] + "/app.js"
                assert b"window.value = 1" in await asyncio.to_thread(_read_url, app_url)
                origin = preview["url"].split("/", 3)[:3]
                absolute_asset_url = "/".join(origin) + "/assets/absolute.js"
                assert b"window.absolute = true" in await asyncio.to_thread(
                    _read_url,
                    absolute_asset_url,
                    {"Cookie": cookie},
                )
                await asyncio.to_thread(
                    _assert_http_error,
                    preview["url"],
                    400,
                    {"Host": "preview.invalid"},
                )
                await asyncio.to_thread(
                    _assert_http_error,
                    preview["url"].rsplit("/", 1)[0] + "/secret.py",
                    404,
                )
                await asyncio.to_thread(
                    _assert_http_error,
                    preview["url"].rsplit("/", 1)[0] + "/.env",
                    404,
                )
                await asyncio.to_thread(
                    _assert_http_error,
                    preview["url"].rsplit("/", 1)[0] + "/..%2Fsecret.py",
                    404,
                )

                old_revision = preview["revision"]
                (workspace / "app.js").write_text("window.value = 2", encoding="utf-8")
                changed = await _wait_for_revision(manager, work_item_id, old_revision)
                assert changed["status"] == "ready"
                assert any(
                    method == Method.WORK_PREVIEW_UPDATED
                    and params.get("reason") == "content_changed"
                    for method, params in events
                )

                # Provider terminal state does not own the user's preview
                # window.  The final bytes remain playable until explicit close.
                store.update_attempt(attempt_id, execution_status="succeeded")
                await asyncio.sleep(0.1)
                assert (await manager.get(work_item_id))["status"] == "ready"
                assert b"window.value = 2" in await asyncio.to_thread(_read_url, app_url)

                next_attempt = store.create_attempt(
                    work_item_id,
                    provider="codex",
                    task="Polish the page",
                )
                generation = await _wait_for_attempt(
                    manager,
                    work_item_id,
                    next_attempt.attempt_id,
                )
                assert generation["attemptGeneration"] == 2
                assert generation["url"] == preview["url"]

                closed = await manager.close(work_item_id)
                assert closed["status"] == "closed"
                await asyncio.to_thread(_assert_url_unavailable, app_url)
                await manager.close_all()

    asyncio.run(run())


def test_export_preview_uses_attempt_staging_root_and_hot_reloads() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_preview_export_stage_") as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            # A desktop export must not accidentally preview an unrelated
            # workspace entry while the Provider writes its real deliverable
            # into the Host-owned staging directory.
            (workspace / "index.html").write_text("WORKSPACE DECOY", encoding="utf-8")
            events: list[tuple[str, dict[str, Any]]] = []

            async def publish(method: str, params: dict[str, Any]) -> None:
                events.append((method, params))

            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                work_item_id, attempt_id = _ledger_item(store, workspace)
                staging_root = (
                    workspace
                    / ".amadeus"
                    / "proposed_exports"
                    / attempt_id
                )
                staging_root.mkdir(parents=True)
                store.update_attempt(
                    attempt_id,
                    metadata={
                        "export_plan": {
                            "kind": "desktop",
                            "staging_root": str(staging_root),
                            "requested_filename": "",
                        }
                    },
                )
                manager = WorkPreviewManager(
                    store,
                    publisher=publish,
                    poll_interval=0.03,
                    debounce=0.03,
                )
                waiting = await manager.open(
                    work_item_id,
                    expected_attempt_id=attempt_id,
                )
                assert waiting["status"] == "waiting"
                assert waiting["url"] == ""

                site = staging_root / "Makise_Kurisu_Website"
                site.mkdir()
                (site / "index.html").write_text(
                    '<!doctype html><script src="app.js"></script><p>STAGED</p>',
                    encoding="utf-8",
                )
                app_js = site / "app.js"
                app_js.write_text("window.previewVersion = 1", encoding="utf-8")
                ready = await _wait_for_status(manager, work_item_id, "ready")
                assert ready["entry"] == "index.html"
                assert b"STAGED" in await asyncio.to_thread(_read_url, ready["url"])

                previous_revision = ready["revision"]
                app_js.write_text("window.previewVersion = 2", encoding="utf-8")
                changed = await _wait_for_revision(
                    manager,
                    work_item_id,
                    previous_revision,
                )
                assert changed["contentRevision"] > ready["contentRevision"]
                app_url = changed["url"].rsplit("/", 1)[0] + "/app.js"
                assert b"previewVersion = 2" in await asyncio.to_thread(
                    _read_url,
                    app_url,
                )
                assert any(
                    method == Method.WORK_PREVIEW_UPDATED
                    and params.get("reason") == "content_changed"
                    for method, params in events
                )

                # A newer Attempt owns a different staging root. The same
                # surface must stop serving the old Attempt before adopting
                # the next entry.
                store.update_attempt(attempt_id, execution_status="succeeded")
                next_attempt = store.create_attempt(
                    work_item_id,
                    provider="codex",
                    task="Revise the exported page",
                )
                next_stage = (
                    workspace
                    / ".amadeus"
                    / "proposed_exports"
                    / next_attempt.attempt_id
                )
                next_stage.mkdir(parents=True)
                store.update_attempt(
                    next_attempt.attempt_id,
                    metadata={
                        "export_plan": {
                            "kind": "desktop",
                            "staging_root": str(next_stage),
                            "requested_filename": "",
                        }
                    },
                )
                rebound = await _wait_for_attempt(
                    manager,
                    work_item_id,
                    next_attempt.attempt_id,
                )
                assert rebound["status"] == "waiting"
                assert rebound["url"] == ""
                (next_stage / "index.html").write_text(
                    "<!doctype html><p>SECOND ATTEMPT</p>",
                    encoding="utf-8",
                )
                next_ready = await _wait_for_status(manager, work_item_id, "ready")
                assert b"SECOND ATTEMPT" in await asyncio.to_thread(
                    _read_url,
                    next_ready["url"],
                )
                await manager.close_all()

    asyncio.run(run())


def test_export_preview_rejects_noncanonical_staging_root() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_preview_export_escape_") as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "index.html").write_text("ESCAPED", encoding="utf-8")
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                work_item_id, attempt_id = _ledger_item(store, workspace)
                manager = WorkPreviewManager(store)
                for invalid_root in (outside, workspace):
                    store.update_attempt(
                        attempt_id,
                        metadata={
                            "export_plan": {
                                "kind": "desktop",
                                "staging_root": str(invalid_root),
                            }
                        },
                    )
                    try:
                        await manager.open(
                            work_item_id,
                            expected_attempt_id=attempt_id,
                        )
                    except WorkPreviewError as exc:
                        assert exc.code == "invalid_preview_staging_root"
                    else:
                        raise AssertionError(
                            "noncanonical export staging root was previewed"
                        )
                await manager.close_all()

    asyncio.run(run())


def test_preview_rebinds_when_export_plan_arrives_after_open() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_preview_late_export_plan_") as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            events: list[tuple[str, dict[str, Any]]] = []

            async def publish(method: str, params: dict[str, Any]) -> None:
                events.append((method, params))

            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                work_item_id, attempt_id = _ledger_item(store, workspace)
                manager = WorkPreviewManager(
                    store,
                    publisher=publish,
                    poll_interval=0.03,
                    debounce=0.03,
                )
                waiting = await manager.open(
                    work_item_id,
                    expected_attempt_id=attempt_id,
                )
                assert waiting["status"] == "waiting"

                # Simulate the narrow intake window in which the Attempt is
                # visible before its Host-generated export plan is persisted.
                # Once the plan arrives, the workspace entry must not win.
                (workspace / "index.html").write_text("WORKSPACE DECOY", encoding="utf-8")
                staging_root = (
                    workspace
                    / ".amadeus"
                    / "proposed_exports"
                    / attempt_id
                )
                staging_root.mkdir(parents=True)
                store.update_attempt(
                    attempt_id,
                    metadata={
                        "export_plan": {
                            "kind": "desktop",
                            "staging_root": str(staging_root),
                        }
                    },
                )
                (staging_root / "index.html").write_text(
                    "<!doctype html><p>LATE STAGED PLAN</p>",
                    encoding="utf-8",
                )
                ready = await _wait_for_status(manager, work_item_id, "ready")
                assert b"LATE STAGED PLAN" in await asyncio.to_thread(
                    _read_url,
                    ready["url"],
                )
                assert any(
                    method == Method.WORK_PREVIEW_UPDATED
                    and params.get("reason") == "preview_root_changed"
                    for method, params in events
                )
                await manager.close_all()

    asyncio.run(run())


def test_preview_lifecycle_holds_freezes_and_thaws_without_false_content_reload() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_preview_lifecycle_") as temp:
            workspace = Path(temp) / "project"
            workspace.mkdir()
            page = workspace / "index.html"
            page.write_text("FIRST", encoding="utf-8")
            with WorkLedgerStore(Path(temp) / "ledger.sqlite3") as store:
                work_item_id, attempt_id = _ledger_item(store, workspace)
                manager = WorkPreviewManager(
                    store,
                    publisher=lambda _method, _params: asyncio.sleep(0),
                    poll_interval=0.03,
                    debounce=0.03,
                )
                opened = await manager.open(
                    work_item_id,
                    expected_attempt_id=attempt_id,
                )
                assert opened["lifecycle"] == "live"
                initial_content_revision = opened["contentRevision"]

                store.update_attempt(attempt_id, execution_status="succeeded")
                holding = await _wait_for_lifecycle(manager, work_item_id, "holding")
                assert holding["status"] == "ready" and holding["url"] == opened["url"]
                assert holding["contentRevision"] == initial_content_revision

                page.write_text("IGNORED WHILE HOLDING", encoding="utf-8")
                await asyncio.sleep(0.12)
                unchanged = await manager.get(work_item_id)
                assert unchanged["contentRevision"] == initial_content_revision

                store.set_work_item_state(work_item_id, "accepted")
                frozen = await _wait_for_lifecycle(manager, work_item_id, "frozen")
                assert frozen["url"] == ""
                assert frozen["contentRevision"] == initial_content_revision + 1

                store.set_work_item_state(work_item_id, "open")
                next_attempt = store.create_attempt(
                    work_item_id,
                    provider="codex",
                    task="Polish it",
                )
                live = await _wait_for_lifecycle(manager, work_item_id, "live")
                assert live["attemptId"] == next_attempt.attempt_id
                assert live["url"]
                assert live["contentRevision"] == frozen["contentRevision"] + 1
                await manager.close_all()

    asyncio.run(run())


def test_only_authoritative_auip_attempt_with_manifest_enters_assembling() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_preview_assembling_") as temp:
            root = Path(temp)
            metadata = {
                "auip_authoring_skill_path": str(root / "SKILL.md"),
                "auip_authoring_bundle_mode": "lean_host_managed",
            }
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                ordinary = root / "ordinary"
                ordinary.mkdir()
                (ordinary / "index.html").write_text("ordinary", encoding="utf-8")
                (ordinary / "auip.manifest.json").write_text("{}", encoding="utf-8")
                ordinary_id, ordinary_attempt = _ledger_item(store, ordinary)

                metadata_only = root / "metadata-only"
                metadata_only.mkdir()
                (metadata_only / "index.html").write_text("metadata", encoding="utf-8")
                metadata_id, metadata_attempt = _ledger_item(
                    store,
                    metadata_only,
                    attempt_metadata=metadata,
                )

                assembling = root / "assembling"
                assembling.mkdir()
                (assembling / "index.html").write_text("auip", encoding="utf-8")
                (assembling / "auip.manifest.json").write_text("{}", encoding="utf-8")
                assembling_id, assembling_attempt = _ledger_item(
                    store,
                    assembling,
                    attempt_metadata=metadata,
                )
                manager = WorkPreviewManager(
                    store,
                    publisher=lambda _method, _params: asyncio.sleep(0),
                    poll_interval=0.03,
                    debounce=0.03,
                )
                ordinary_preview = await manager.open(
                    ordinary_id,
                    expected_attempt_id=ordinary_attempt,
                )
                metadata_preview = await manager.open(
                    metadata_id,
                    expected_attempt_id=metadata_attempt,
                )
                assembling_preview = await manager.open(
                    assembling_id,
                    expected_attempt_id=assembling_attempt,
                )
                assert ordinary_preview["lifecycle"] == "live"
                assert metadata_preview["lifecycle"] == "live"
                assert assembling_preview["lifecycle"] == "assembling"
                assert assembling_preview["status"] == "ready"
                assert assembling_preview["url"] == ""
                await manager.close_all()

    asyncio.run(run())


def test_auip_handoff_commits_exact_active_artifact_and_times_out_safely() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_preview_handoff_") as temp:
            workspace = Path(temp) / "project"
            workspace.mkdir()
            (workspace / "index.html").write_text("auip", encoding="utf-8")
            (workspace / "auip.manifest.json").write_text("{}", encoding="utf-8")
            metadata = {
                "auip_authoring_skill_path": str(workspace / "SKILL.md"),
                "auip_authoring_bundle_mode": "lean_host_managed",
            }
            with WorkLedgerStore(Path(temp) / "ledger.sqlite3") as store:
                work_item_id, attempt_id = _ledger_item(
                    store,
                    workspace,
                    attempt_metadata=metadata,
                )
                manager = WorkPreviewManager(
                    store,
                    publisher=lambda _method, _params: asyncio.sleep(0),
                    poll_interval=0.03,
                    debounce=0.03,
                    handoff_timeout=0.12,
                )
                await manager.open(work_item_id, expected_attempt_id=attempt_id)
                source = {
                    "work_item_id": work_item_id,
                    "attempt_id": attempt_id,
                    "artifact_ref": "artifact:interactive-app@1",
                    "host_surface_id": "surface-1",
                }
                handoff = await manager.begin_auip_handoff(source)
                assert handoff["lifecycle"] == "handoff"
                assert handoff["attemptId"] == attempt_id
                assert handoff["artifactRef"] == source["artifact_ref"]
                assert handoff["hostSurfaceId"] == source["host_surface_id"]
                assert "appSessionId" not in handoff
                await manager.on_auip_updated(
                    {
                        "artifact_ref": "artifact:other@1",
                        "app_session_id": "app-other",
                        "host_surface_id": "surface-1",
                        "status": "active",
                    }
                )
                await manager.on_auip_updated(
                    {
                        "artifact_ref": source["artifact_ref"],
                        "app_session_id": "app-1",
                        "host_surface_id": "surface-other",
                        "status": "active",
                    }
                )
                assert (await manager.get(work_item_id))["lifecycle"] == "handoff"
                rolled_back = await _wait_for_lifecycle(
                    manager,
                    work_item_id,
                    "assembling",
                )
                assert "artifactRef" not in rolled_back

                await manager.begin_auip_handoff(source)
                await manager.on_auip_updated(
                    {
                        "artifact_ref": source["artifact_ref"],
                        "app_session_id": "app-1",
                        "host_surface_id": source["host_surface_id"],
                        "status": "active",
                    }
                )
                attached = await manager.get(work_item_id)
                assert attached["lifecycle"] == "attached"
                assert attached["attemptId"] == attempt_id
                assert attached["artifactRef"] == source["artifact_ref"]
                assert attached["appSessionId"] == "app-1"
                assert attached["hostSurfaceId"] == source["host_surface_id"]

                # A second prepare cannot silently replace a still-active
                # AppSession, even if it names the same Work Attempt.
                ignored_prepare = await manager.begin_auip_handoff(
                    {
                        **source,
                        "artifact_ref": "artifact:replacement@1",
                        "host_surface_id": "surface-2",
                    }
                )
                assert ignored_prepare["lifecycle"] == "attached"
                assert ignored_prepare["artifactRef"] == source["artifact_ref"]
                assert ignored_prepare["appSessionId"] == "app-1"

                # Work acceptance does not imply that the Host closed an
                # active application surface.
                store.set_work_item_state(work_item_id, "accepted")
                await asyncio.sleep(0.12)
                assert (await manager.get(work_item_id))["lifecycle"] == "attached"

                await manager.on_auip_updated(
                    {
                        "artifact_ref": source["artifact_ref"],
                        "app_session_id": "app-1",
                        "host_surface_id": source["host_surface_id"],
                        "status": "completed",
                    }
                )
                assert (await manager.get(work_item_id))["lifecycle"] == "attached"

                # auip.leave first closes protocol participation.  The same
                # shell stays attached until its exact close receipt arrives.
                await manager.on_auip_updated(
                    {
                        "artifact_ref": source["artifact_ref"],
                        "app_session_id": "app-1",
                        "host_surface_id": source["host_surface_id"],
                        "status": "closed",
                        "surface_close_status": "pending",
                    }
                )
                await asyncio.sleep(0.08)
                assert (await manager.get(work_item_id))["lifecycle"] == "attached"
                await manager.on_auip_updated(
                    {
                        "artifact_ref": source["artifact_ref"],
                        "app_session_id": "app-1",
                        "host_surface_id": source["host_surface_id"],
                        "status": "closed",
                        "surface_close_status": "failed",
                    }
                )
                await asyncio.sleep(0.08)
                assert (await manager.get(work_item_id))["lifecycle"] == "attached"
                await manager.on_auip_updated(
                    {
                        "artifact_ref": source["artifact_ref"],
                        "app_session_id": "app-wrong",
                        "host_surface_id": source["host_surface_id"],
                        "status": "closed",
                        "surface_close_status": "closed",
                    }
                )
                assert (await manager.get(work_item_id))["lifecycle"] == "attached"
                await manager.on_auip_updated(
                    {
                        "artifact_ref": source["artifact_ref"],
                        "app_session_id": "app-1",
                        "host_surface_id": source["host_surface_id"],
                        "status": "closed",
                        "surface_close_status": "closed",
                    }
                )
                frozen = await manager.get(work_item_id)
                assert frozen["lifecycle"] == "frozen"
                assert frozen["artifactRef"] == source["artifact_ref"]
                assert frozen["appSessionId"] == "app-1"
                assert frozen["hostSurfaceId"] == source["host_surface_id"]

                store.update_attempt(attempt_id, execution_status="succeeded")
                store.set_work_item_state(work_item_id, "open")
                next_attempt = store.create_attempt(
                    work_item_id,
                    provider="codex",
                    task="Revise AUIP",
                    metadata=metadata,
                )
                next_lifecycle = await _wait_for_attempt(
                    manager,
                    work_item_id,
                    next_attempt.attempt_id,
                )
                assert next_lifecycle["lifecycle"] == "assembling"
                assert next_lifecycle["attemptId"] == next_attempt.attempt_id
                assert "artifactRef" not in next_lifecycle
                assert "appSessionId" not in next_lifecycle
                assert "hostSurfaceId" not in next_lifecycle

                second_source = {
                    "work_item_id": work_item_id,
                    "attempt_id": next_attempt.attempt_id,
                    "artifact_ref": "artifact:interactive-app@2",
                    "host_surface_id": "surface-2",
                }
                await manager.begin_auip_handoff(second_source)
                await manager.on_auip_updated(
                    {
                        "artifact_ref": second_source["artifact_ref"],
                        "app_session_id": "app-2",
                        "host_surface_id": second_source["host_surface_id"],
                        "status": "active",
                    }
                )
                assert (await manager.get(work_item_id))["lifecycle"] == "attached"
                await manager.on_auip_updated(
                    {
                        "artifact_ref": second_source["artifact_ref"],
                        "app_session_id": "app-2",
                        "host_surface_id": second_source["host_surface_id"],
                        "status": "disconnected",
                    }
                )
                assert (await manager.get(work_item_id))["lifecycle"] == "frozen"
                await manager.close_all()

    asyncio.run(run())


def test_auip_handoff_opens_the_shared_surface_and_app_close_freezes_it() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_preview_attach_first_") as temp:
            workspace = Path(temp) / "project"
            workspace.mkdir()
            (workspace / "index.html").write_text("auip", encoding="utf-8")
            (workspace / "auip.manifest.json").write_text("{}", encoding="utf-8")
            metadata = {
                "auip_authoring_skill_path": str(workspace / "SKILL.md"),
                "auip_authoring_bundle_mode": "lean_host_managed",
            }
            events: list[tuple[str, dict[str, Any]]] = []

            async def publish(method: str, params: dict[str, Any]) -> None:
                events.append((method, params))

            with WorkLedgerStore(Path(temp) / "ledger.sqlite3") as store:
                work_item_id, attempt_id = _ledger_item(
                    store,
                    workspace,
                    attempt_metadata=metadata,
                )
                manager = WorkPreviewManager(
                    store,
                    publisher=publish,
                    poll_interval=0.03,
                    debounce=0.03,
                )
                source = {
                    "work_item_id": work_item_id,
                    "attempt_id": attempt_id,
                    "artifact_ref": "artifact:attach-first@1",
                    "host_surface_id": "surface-attach-first",
                }
                handoff = await manager.begin_auip_handoff(source)
                assert handoff["lifecycle"] == "handoff"
                assert handoff["artifactRef"] == source["artifact_ref"]
                assert handoff["hostSurfaceId"] == source["host_surface_id"]
                assert any(
                    method == Method.WORK_PREVIEW_OPEN_REQUESTED
                    for method, _params in events
                )

                update = {
                    "artifact_ref": source["artifact_ref"],
                    "app_session_id": "app-attach-first",
                    "host_surface_id": source["host_surface_id"],
                    "status": "active",
                }
                await manager.on_auip_updated(update)
                assert (await manager.get(work_item_id))["lifecycle"] == "attached"

                # An app-initiated close has no pending Host surface receipt;
                # the authoritative closed update is sufficient to freeze it.
                await manager.on_auip_updated(
                    {
                        **update,
                        "status": "closed",
                        "surface_close_status": "not_requested",
                    }
                )
                closed = await manager.get(work_item_id)
                assert closed["lifecycle"] == "frozen"
                assert closed["appSessionId"] == "app-attach-first"
                await manager.close_all()

    asyncio.run(run())


def test_waiting_preview_adopts_first_entry_without_reopening_surface() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_preview_waiting_") as temp:
            workspace = Path(temp) / "project"
            workspace.mkdir()
            with WorkLedgerStore(Path(temp) / "ledger.sqlite3") as store:
                work_item_id, attempt_id = _ledger_item(store, workspace)
                manager = WorkPreviewManager(
                    store,
                    publisher=lambda _method, _params: asyncio.sleep(0),
                    poll_interval=0.03,
                    debounce=0.03,
                )
                waiting = await manager.open(
                    work_item_id,
                    expected_attempt_id=attempt_id,
                )
                assert waiting["status"] == "waiting"
                assert waiting["url"] == ""
                preview_id = waiting["previewId"]

                (workspace / "index.html").write_text(
                    "<!doctype html><p>first runnable frame</p>",
                    encoding="utf-8",
                )
                ready = await _wait_for_status(manager, work_item_id, "ready")
                assert ready["previewId"] == preview_id
                assert ready["url"].startswith("http://127.0.0.1:")
                await manager.close_all()

    asyncio.run(run())


def test_two_work_items_keep_independent_preview_generations() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_preview_isolation_") as temp:
            root = Path(temp)
            first_workspace = root / "first"
            second_workspace = root / "second"
            first_workspace.mkdir()
            second_workspace.mkdir()
            (first_workspace / "index.html").write_text("FIRST", encoding="utf-8")
            (second_workspace / "index.html").write_text("SECOND", encoding="utf-8")
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                first_id, first_attempt = _ledger_item(store, first_workspace)
                second_id, second_attempt = _ledger_item(store, second_workspace)
                manager = WorkPreviewManager(
                    store,
                    publisher=lambda _method, _params: asyncio.sleep(0),
                    poll_interval=0.03,
                    debounce=0.03,
                )
                first = await manager.open(first_id, expected_attempt_id=first_attempt)
                second = await manager.open(second_id, expected_attempt_id=second_attempt)
                assert first["previewId"] != second["previewId"]
                assert first["url"] != second["url"]

                second_revision = second["revision"]
                (first_workspace / "index.html").write_text(
                    "FIRST UPDATED",
                    encoding="utf-8",
                )
                await _wait_for_revision(manager, first_id, first["revision"])
                unchanged_second = await manager.get(second_id)
                assert unchanged_second["revision"] == second_revision
                assert b"SECOND" in await asyncio.to_thread(
                    _read_url,
                    unchanged_second["url"],
                )
                await manager.close_all()

    asyncio.run(run())


def test_canvas_preview_forwards_only_bounded_ledger_fields() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_preview_canvas_") as temp:
            workspace = Path(temp) / "project"
            workspace.mkdir()
            with WorkLedgerStore(Path(temp) / "ledger.sqlite3") as store:
                work_item_id, attempt_id = _ledger_item(store, workspace)
                coordinator = WorkLedgerCoordinator(store)
                coordinator.select(work_item_id)
                captured: list[dict[str, Any]] = []

                async def open_preview(payload: dict[str, Any]) -> dict[str, Any]:
                    captured.append(dict(payload))
                    return {"ok": True, "preview": {"status": "waiting"}}

                handler = WorkLedgerHandler(coordinator, preview_open=open_preview)
                router = CanvasActionRouter(work_action=handler.route_action)
                snapshot = coordinator.snapshot()
                result = await router.route(
                    {
                        "target": "work_item",
                        "action": "open_preview",
                        "workItemId": work_item_id,
                        "attemptId": attempt_id,
                        "revision": snapshot["revision"],
                        "cwd": "C:/",
                        "url": "https://example.invalid",
                        "command": "do-not-forward",
                    }
                )
                assert result["ok"] is True
                assert captured == [
                    {
                        "work_item_id": work_item_id,
                        "attempt_id": attempt_id,
                        "revision": snapshot["revision"],
                    }
                ]

    asyncio.run(run())


def _read_response(
    url: str,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, Any]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=2.0) as response:
        return response.read(), response.headers


def _read_url(url: str, headers: dict[str, str] | None = None) -> bytes:
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers or {}),
        timeout=2.0,
    ) as response:
        return response.read()


def _assert_http_error(
    url: str,
    status: int,
    headers: dict[str, str] | None = None,
) -> None:
    try:
        _read_url(url, headers)
    except urllib.error.HTTPError as exc:
        assert exc.code == status
    else:  # pragma: no cover - assertion branch
        raise AssertionError(f"expected HTTP {status}: {url}")


def _assert_url_unavailable(url: str) -> None:
    try:
        _read_url(url)
    except (OSError, urllib.error.URLError):
        return
    raise AssertionError(f"preview server remained reachable after close: {url}")


async def _wait_for_revision(
    manager: WorkPreviewManager,
    work_item_id: str,
    previous: int,
) -> dict[str, Any]:
    for _ in range(80):
        current = await manager.get(work_item_id)
        if current.get("revision") != previous:
            return current
        await asyncio.sleep(0.03)
    raise AssertionError("preview revision did not advance")


async def _wait_for_attempt(
    manager: WorkPreviewManager,
    work_item_id: str,
    attempt_id: str,
) -> dict[str, Any]:
    for _ in range(80):
        current = await manager.get(work_item_id)
        if current.get("attemptId") == attempt_id:
            return current
        await asyncio.sleep(0.03)
    raise AssertionError("preview did not adopt the latest attempt generation")


async def _wait_for_lifecycle(
    manager: WorkPreviewManager,
    work_item_id: str,
    lifecycle: str,
) -> dict[str, Any]:
    for _ in range(80):
        current = await manager.get(work_item_id)
        if current.get("lifecycle") == lifecycle:
            return current
        await asyncio.sleep(0.03)
    raise AssertionError(f"preview did not reach lifecycle {lifecycle!r}")


async def _wait_for_status(
    manager: WorkPreviewManager,
    work_item_id: str,
    status: str,
) -> dict[str, Any]:
    for _ in range(80):
        current = await manager.get(work_item_id)
        if current.get("status") == status:
            return current
        await asyncio.sleep(0.03)
    raise AssertionError(f"preview did not reach status {status!r}")
