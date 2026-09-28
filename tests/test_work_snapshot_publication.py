"""One publication shares Work facts without sharing each surface's selection."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent_host.work_ledger_store import WorkLedgerStore
from server.event_bus import EventBus
from server.handlers.wallpaper_handler import WallpaperHandler
from server.protocol import Method
from server.work_ledger_coordinator import WORKSPACE_ROUTING_SURFACE, WorkLedgerCoordinator


def test_sync_publication_reads_once_and_keeps_real_bus_surface_selection(tmp_path, monkeypatch):
    async def run():
        bus = EventBus()
        monkeypatch.setattr("server.work_ledger_coordinator.bus", bus)
        workspace = tmp_path / "project"
        workspace.mkdir()
        clock = lambda: 1000.0
        with WorkLedgerStore(tmp_path / "sync-publication.sqlite3", clock=clock) as store:
            project = store.create_or_get_project(workspace)
            first = store.create_work_item(project.project_id, title="First")
            second = store.create_work_item(project.project_id, title="Second")
            _, attempt = store.create_operation_attempt(first.work_item_id, intent="execute",
                instruction="Build first", task="Build first", provider="fake")
            store.update_attempt(attempt.attempt_id, execution_status="running")
            owner = WorkLedgerCoordinator(store, clock=clock)
            store.set_focus(owner.default_surface, first.work_item_id, mode="auto")
            store.set_focus("work-page", second.work_item_id, mode="auto")
            store.set_focus("another-window", first.work_item_id, mode="auto")
            store.set_focus(WORKSPACE_ROUTING_SURFACE, None, mode="auto")
            surfaces = [owner.default_surface, *[focus.surface for focus in store.list_focus()
                if focus.surface not in {owner.default_surface, WORKSPACE_ROUTING_SURFACE}]]
            seen = []
            delivered = asyncio.Event()

            async def capture(_method, params):
                seen.append(params)
                if len(seen) == len(surfaces):
                    delivered.set()

            bus.on(Method.WORK_UPDATED, capture)
            with (
                patch.object(store, "list_work_items", wraps=store.list_work_items) as read,
                patch.object(owner.read_model, "project_items", wraps=owner.read_model.project_items) as project_items,
            ):
                previous_revision = ""
                for index, execution in enumerate(("running", "succeeded"), start=1):
                    if index == 2:
                        store.update_attempt(attempt.attempt_id, execution_status=execution)
                    seen.clear()
                    delivered.clear()
                    owner._emit_snapshot_now(owner.default_surface, reason="test.sync_update")
                    assert read.call_count == project_items.call_count == index
                    assert seen == []  # emit_now keeps its asynchronous callback scheduling.
                    await asyncio.wait_for(delivered.wait(), 2)
                    assert [event["work"]["surface"] for event in seen] == surfaces
                    assert all(event["reason"] == "test.sync_update" for event in seen)
                    for event in seen:
                        snapshot = event["work"]
                        assert snapshot["selectedWorkItemId"] == (
                            second.work_item_id if snapshot["surface"] == "work-page" else first.work_item_id)
                        assert snapshot["counts"]["running"] == (1 if execution == "running" else 0)
                        assert next(row for row in snapshot["items"] if row["id"] == first.work_item_id)["execution"] == execution
                    assert seen[0]["work"]["revision"] != previous_revision
                    previous_revision = seen[0]["work"]["revision"]
                # A direct synchronous snapshot remains an independent fresh read.
                direct = owner.snapshot(surface="work-page")
                assert read.call_count == project_items.call_count == 3
                assert direct == next(event["work"] for event in seen if event["work"]["surface"] == "work-page")

    asyncio.run(run())


def test_real_wallpaper_event_projects_once_and_never_persists_permission_overlay(tmp_path, monkeypatch):
    async def run():
        bus = EventBus()
        monkeypatch.setattr("server.work_ledger_coordinator.bus", bus)
        monkeypatch.setattr("server.handlers.wallpaper_handler.bus", bus)
        workspace = tmp_path / "project"
        workspace.mkdir()
        with WorkLedgerStore(tmp_path / "wallpaper.sqlite3") as store:
            project = store.create_or_get_project(workspace)
            item = store.create_work_item(project.project_id, title="Original task")
            _, attempt = store.create_operation_attempt(item.work_item_id,
                intent="execute", instruction="Build the result", provider="fake", task="Build the result")
            store.update_attempt(attempt.attempt_id, execution_status="running")
            permission = store.create_permission_request(item.work_item_id,
                attempt_id=attempt.attempt_id, capability="shell", action="run",
                scope_paths=[str(workspace)], options=["allow_once", "deny"])
            owner = WorkLedgerCoordinator(store)
            original = {"schema_id":"amadeus.ai_os.v1", "mode":"workflow",
                "phase":"Result", "title":"Provider report", "lead":"Original report",
                "progress":70, "signals":[]}
            owner.record_presentation(item.work_item_id, original)
            rendered = []
            wallpaper = WallpaperHandler()
            wallpaper.configure(project_root=workspace, canvas_projector=owner.project_canvas)
            wallpaper._wallpaper_host = SimpleNamespace(set_canvas=rendered.append)
            with (
                patch.object(owner.read_model, "project_items", wraps=owner.read_model.project_items) as project_items,
                patch.object(owner, "record_presentation", wraps=owner.record_presentation) as record,
            ):
                first = await owner.publish_snapshot(reason="permission.pending")
                assert project_items.call_count == 1
                record.assert_not_called()
                assert len(rendered) == 1
                assert type(wallpaper._last_canvas_payload) is dict
                assert wallpaper._last_canvas_payload["permissionRequest"]["id"] == permission.request_id
                assert store.get_work_item(item.work_item_id).metadata["presentation"] == original
                store.resolve_permission_request(permission.request_id, "denied")
                store.update_attempt(attempt.attempt_id, execution_status="failed")
                second = await owner.publish_snapshot(reason="permission.denied")
                assert project_items.call_count == 2
                record.assert_not_called()
                assert second["revision"] != first["revision"]
                assert second["selected"]["execution"] == "failed"
                assert len(rendered) == 2
                assert "permissionRequest" not in wallpaper._last_canvas_payload
                assert store.get_work_item(item.work_item_id).metadata["presentation"] == original

    asyncio.run(run())


@pytest.mark.parametrize("source", ["raw", "serialized", "another_owner"])
def test_only_current_owner_python_provenance_skips_canvas_reprojection(tmp_path, source):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with WorkLedgerStore(tmp_path / "canvas-provenance.sqlite3") as store:
        project = store.create_or_get_project(workspace)
        item = store.create_work_item(project.project_id, title="Current task")
        owner = WorkLedgerCoordinator(store)
        other = WorkLedgerCoordinator(store)
        completed = (other if source == "another_owner" else owner).selected_canvas()
        serialized = json.dumps(completed)
        assert "_owner" not in json.loads(serialized)
        incoming = (completed if source == "another_owner" else json.loads(serialized)
            if source == "serialized" else dict(completed))
        incoming["_projected"] = True
        incoming["taskDock"] = {"counts":{"running":999}, "selectedWorkItemId":"invented"}
        with (
            patch.object(owner.read_model, "project_items", wraps=owner.read_model.project_items) as project_items,
            patch.object(owner, "record_presentation", wraps=owner.record_presentation) as record,
        ):
            projected = owner.project_canvas(incoming)
            assert type(projected) is dict
            assert project_items.call_count == 1
            record.assert_called_once()
            assert projected["taskDock"]["counts"]["running"] == 0
            assert projected["taskDock"]["selectedWorkItemId"] == item.work_item_id


def test_publication_projects_once_for_each_surface_and_refreshes_next_time(tmp_path: Path) -> None:
    async def run() -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
            project = store.create_or_get_project(workspace, name="Shared project")
            first = store.create_work_item(project.project_id, title="First task")
            second = store.create_work_item(project.project_id, title="Second task")
            _, attempt = store.create_operation_attempt(
                first.work_item_id,
                intent="execute",
                instruction="Produce the first result.",
                provider="fake",
                task="Produce the first result.",
            )
            store.update_attempt(attempt.attempt_id, execution_status="running")
            coordinator = WorkLedgerCoordinator(store, default_surface="wallpaper")
            store.set_focus("wallpaper", first.work_item_id, mode="auto")
            store.set_focus("work-page", second.work_item_id, mode="auto")
            store.set_focus(WORKSPACE_ROUTING_SURFACE, None, mode="auto")

            with (
                patch("server.work_ledger_coordinator.bus.emit", new_callable=AsyncMock) as emit,
                patch.object(store, "list_work_items", wraps=store.list_work_items) as read,
                patch.object(
                    coordinator.read_model,
                    "project_items",
                    wraps=coordinator.read_model.project_items,
                ) as project_items,
            ):
                previous_revision = ""
                for publication, execution in enumerate(("running", "succeeded"), start=1):
                    if publication == 2:
                        store.update_attempt(attempt.attempt_id, execution_status=execution)
                        emit.reset_mock()
                    result = await coordinator.publish_snapshot(reason="test.state_changed")

                    # Count actual reads/projections, while assertions below verify
                    # the emitted user-facing facts using the real read model.
                    assert read.call_count == publication
                    assert project_items.call_count == publication
                    snapshots = {
                        call.args[1]["work"]["surface"]: call.args[1]["work"]
                        for call in emit.await_args_list
                        if call.args[0] == Method.WORK_UPDATED
                    }
                    assert set(snapshots) == {"wallpaper", "work-page"}
                    assert result == snapshots["wallpaper"]
                    for surface, selected_id in (
                        ("wallpaper", first.work_item_id),
                        ("work-page", second.work_item_id),
                    ):
                        snapshot = snapshots[surface]
                        assert snapshot["selectedWorkItemId"] == selected_id
                        assert snapshot["selected"]["id"] == selected_id
                        rows = {row["id"]: row for row in snapshot["items"]}
                        assert set(rows) == {first.work_item_id, second.work_item_id}
                        assert rows[first.work_item_id]["execution"] == execution
                        assert snapshot["counts"]["running"] == (1 if execution == "running" else 0)

                    canvases = [
                        call.args[1] for call in emit.await_args_list
                        if call.args[0] == Method.WALLPAPER_CANVAS
                    ]
                    assert len(canvases) == 1
                    assert canvases[0]["workContext"]["workItemId"] == first.work_item_id
                    assert canvases[0]["workContext"]["attemptId"] == attempt.attempt_id
                    assert canvases[0]["signals"][0]["text"] == execution
                    assert result["revision"] != previous_revision
                    previous_revision = result["revision"]

    asyncio.run(run())
