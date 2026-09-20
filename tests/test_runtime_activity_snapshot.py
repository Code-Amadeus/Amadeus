"""Durable runtime activity truth and bounded report refresh."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as settings
from _support import settle_provider_runs
from agent_host.provider_contract import ProviderCapabilities, ProviderManifest
from agent_host.provider_identity import PARENT_CONTEXT_DELIVERED_EVENT
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderRunRequest, ProviderRunResult
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server import task_lookup
from server.app import _route_active_amendment
from server.event_bus import bus
from server.handlers.work_activity_handler import WorkActivityCoordinator
from server.protocol import Method
from server.work_activity_snapshot import (
    ACTIVITY_METADATA_KEY,
    activity_report_fields,
    project_activity_event,
    project_activity_result,
    is_material_activity_event,
)
from server.work_ledger_coordinator import WorkLedgerCoordinator
from server.work_export_service import WorkExportService


def _event(sequence: int, event_type: str, payload: dict | None = None, *, at: float) -> dict:
    return {
        "provider": "fake",
        "run_id": "run-activity",
        "type": event_type,
        "sequence": sequence,
        "observed_at": at,
        "payload": dict(payload or {}),
    }


def test_activity_projection_is_monotonic_and_preserves_control_facts() -> None:
    snapshot: dict = {}
    for params, execution in (
        (_event(1, "run.created", at=1_000.0), "queued"),
        (_event(2, "run.started", at=1_001.0), "running"),
        (
            _event(
                3,
                "semantic.progress",
                {"summary": "The board is rendered and rules are being wired."},
                at=1_010.0,
            ),
            "running",
        ),
        (_event(4, "tool.call", {"tool": "Write"}, at=1_012.0), "running"),
        (
            _event(
                5,
                "run.status",
                {
                    "status": "running",
                    "stage": "steer_queued",
                    "revision": 2,
                    "safe_boundary": "after_atomic_action",
                },
                at=1_015.0,
            ),
            "running",
        ),
        (
            _event(
                6,
                "run.status",
                {
                    "status": "stalled",
                    "liveness": "stalled",
                    "silence_s": 300,
                    "probe_status": "running",
                },
                at=1_315.0,
            ),
            "running",
        ),
    ):
        snapshot = project_activity_event(
            snapshot,
            params,
            execution_status=execution,
            now=float(params["observed_at"]),
        )

    assert snapshot["phase"] == "stalled"
    assert snapshot["latestSemanticSummary"].startswith("The board is rendered")
    assert snapshot["toolCount"] == 1 and snapshot["lastTool"] == "Write"
    assert snapshot["steering"] == {
        "state": "queued",
        "revision": 2,
        "replacesRevision": 0,
        "safeBoundary": "after_atomic_action",
        "observedAt": 1015.0,
    }
    assert snapshot["uncertainty"] == "provider_silent"
    assert snapshot["lastEventAt"] == 1_315.0
    assert snapshot["lastSemanticProgressAt"] == 1_010.0

    assert is_material_activity_event(PARENT_CONTEXT_DELIVERED_EVENT) is False

    stale = project_activity_event(
        snapshot,
        _event(4, "semantic.progress", {"summary": "stale"}, at=1_400.0),
        execution_status="running",
        now=1_400.0,
    )
    assert stale == snapshot
    recovered = project_activity_event(
        snapshot,
        _event(
            7,
            "run.status",
            {"status": "running", "liveness": "active", "recovered": True},
            at=1_420.0,
        ),
        execution_status="running",
        now=1_420.0,
    )
    assert recovered["phase"] == "working"
    assert recovered["latestSemanticSummary"] == snapshot["latestSemanticSummary"]
    assert recovered["uncertainty"] == ""

    reviewed = project_activity_result(recovered, status="succeeded", observed_at=1_500.0)
    late = project_activity_event(
        reviewed,
        _event(8, "tool.call", {"tool": "LateTool"}, at=1_501.0),
        execution_status="succeeded",
        now=1_501.0,
    )
    assert late["phase"] == "review"
    print("ok: activity events project monotonically with liveness and steer facts")


def test_context_delivery_receipt_does_not_create_visible_work_activity() -> None:
    async def scenario() -> None:
        coordinator = WorkActivityCoordinator()
        await coordinator._on_provider_event(
            Method.PROVIDER_EVENT,
            {
                "provider": "codex",
                "run_id": "run-context-only",
                "type": PARENT_CONTEXT_DELIVERED_EVENT,
                "metadata": {"source_user_text": "private handoff evidence"},
            },
        )
        assert coordinator._runs == {}
        assert coordinator._active_runs == set()

    asyncio.run(scenario())


def test_dynamic_activity_time_is_computed_at_read_time() -> None:
    fields = activity_report_fields(
        {
            "phase": "working",
            "lastMeaningfulEventAt": 1_010.0,
            "latestSemanticSummary": "Implementing the game loop.",
        },
        execution_status="running",
        created_at=1_000.0,
        started_at=1_005.0,
        finished_at=None,
        now=1_105.0,
    )
    assert fields["activity_elapsed_seconds"] == 100.0
    assert fields["activity_silent_seconds"] == 95.0
    assert fields["activity_last_semantic_progress_at"] == 1_010.0
    legacy_terminal = activity_report_fields(
        {},
        execution_status="succeeded",
        created_at=1_000.0,
        started_at=1_005.0,
        finished_at=1_020.0,
        now=1_105.0,
    )
    assert legacy_terminal["activity_phase"] == "review"
    assert legacy_terminal["activity_uncertainty"] == ""
    print("ok: elapsed and silence are materialised without periodic ledger writes")


def test_retrospective_permission_is_denied_activity_not_waiting_for_user() -> None:
    snapshot = project_activity_event(
        {"phase": "working", "revision": 2},
        _event(
            3,
            "permission.requested",
            {
                "capability": "tool.execute",
                "action": "invoke_tool",
                "reason": "Explicit approval was required.",
                "diagnosticOnly": True,
                "retryRequired": True,
            },
            at=2_000.0,
        ),
        execution_status="running",
        now=2_000.0,
    )
    assert snapshot["phase"] == "working"
    assert snapshot["uncertainty"] == "provider_action_denied"
    assert snapshot["permissionDiagnosticCount"] == 1
    assert snapshot["latestPermissionDiagnostic"]["retryRequired"] is True

    updated = project_activity_event(
        snapshot,
        _event(
            4,
            "assistant.update",
            {
                "text": "Runtime validation was denied; checking the static structure instead.",
                "source": "locus_assistant_update",
            },
            at=2_010.0,
        ),
        execution_status="running",
        now=2_010.0,
    )
    assert updated["latestCandidateSummary"].startswith("Runtime validation was denied")
    assert updated["lastDirectionalUpdateAt"] == 2_010.0
    assert updated["latestSemanticSummary"].startswith("Provider policy blocked")
    assert updated["lastSemanticProgressAt"] == 2_000.0
    assert updated["uncertainty"] == "provider_action_denied"
    fields = activity_report_fields(
        updated,
        execution_status="running",
        created_at=1_990.0,
        started_at=1_995.0,
        finished_at=None,
        now=2_015.0,
    )
    assert fields["activity_last_semantic_progress_at"] == 2_000.0
    assert fields["activity_last_directional_update_at"] == 2_010.0
    assert fields["activity_silent_seconds"] == 5.0
    assert fields["activity_direction_summary"].startswith("Runtime validation was denied")

    actionable = project_activity_event(
        {"phase": "working", "revision": 1},
        _event(
            2,
            "permission.requested",
            {"capability": "filesystem.write", "diagnosticOnly": False},
            at=3_000.0,
        ),
        execution_status="running",
        now=3_000.0,
    )
    assert actionable["phase"] == "waiting_for_user"
    assert actionable["uncertainty"] == "waiting_for_user"
    assert actionable["latestSemanticSummary"].startswith(
        "Provider is waiting for permission"
    )
    print("ok: retrospective denial and live approval remain distinct activity states")


def test_mechanical_events_do_not_reset_semantic_silence() -> None:
    snapshot = project_activity_event(
        {},
        _event(
            1,
            "semantic.progress",
            {"summary": "The game board is implemented."},
            at=4_000.0,
        ),
        execution_status="running",
        now=4_000.0,
    )
    snapshot = project_activity_event(
        snapshot,
        _event(2, "tool.call", {"tool": "Read", "item_id": "read-1"}, at=4_050.0),
        execution_status="running",
        now=4_050.0,
    )
    snapshot = project_activity_event(
        snapshot,
        _event(
            3,
            "tool.result",
            {"tool": "Read", "item_id": "read-1", "ok": True},
            at=4_080.0,
        ),
        execution_status="running",
        now=4_080.0,
    )
    fields = activity_report_fields(
        snapshot,
        execution_status="running",
        created_at=3_990.0,
        started_at=3_995.0,
        finished_at=None,
        now=4_100.0,
    )
    assert fields["activity_last_provider_event_at"] == 4_080.0
    assert fields["activity_last_semantic_progress_at"] == 4_000.0
    assert fields["activity_silent_seconds"] == 100.0


def test_verified_tool_fact_advances_semantic_clock_once() -> None:
    call = project_activity_event(
        {},
        _event(
            1,
            "tool.call",
            {
                "tool": "file_change",
                "item_id": "change-1",
                "changes": [{"path": "src/game.js", "kind": "update"}],
            },
            at=5_000.0,
        ),
        execution_status="running",
        now=5_000.0,
    )
    result = project_activity_event(
        call,
        _event(
            2,
            "tool.result",
            {"item_id": "change-1", "status": "completed"},
            at=5_010.0,
        ),
        execution_status="running",
        now=5_010.0,
    )
    assert result["latestSemanticSummary"] == "Updated project files: game.js."
    assert result["semanticSource"] == "host.tool_observation"
    assert result["semanticVerified"] is True
    assert result["lastSemanticProgressAt"] == 5_010.0


def test_status_query_tracks_the_current_steer_evidence_end_to_end() -> None:
    snapshot = project_activity_event(
        {},
        _event(1, "run.created", at=10.0),
        execution_status="running",
        now=10.0,
    )
    snapshot = project_activity_event(
        snapshot,
        _event(
            2,
            "semantic.progress",
            {
                "milestone": "design",
                "summary": "I will implement the old one-player design.",
                "source": "provider_explicit_progress",
                "verified": False,
            },
            at=12.0,
        ),
        execution_status="running",
        now=12.0,
    )
    snapshot = project_activity_event(
        snapshot,
        _event(
            3,
            "run.status",
            {
                "status": "running",
                "stage": "steer_queued",
                "revision": 1,
                "replaces_revision": 0,
            },
            at=20.0,
        ),
        execution_status="running",
        now=20.0,
    )

    def status_note(current: dict, *, now: float) -> dict:
        fields = activity_report_fields(
            current,
            execution_status="running",
            created_at=10.0,
            started_at=10.0,
            finished_at=None,
            now=now,
        )
        return task_lookup.status_query_narration_note(
            {
                "work_item_id": "work-steer-freshness",
                "attempt_id": "attempt-steer-freshness",
                "title": "Build the game",
                "execution": "running",
                "completion": "unknown",
                "attention": "none",
                **fields,
            }
        )

    stale = status_note(snapshot, now=21.0)
    assert stale["metadata"]["semantic_milestone"] == ""
    assert stale["metadata"]["status_facts"]["steering_revision"] == 1

    snapshot = project_activity_event(
        snapshot,
        _event(
            4,
            "semantic.progress",
            {
                "milestone": "capability",
                "summary": "The two-player controls are wired.",
                "source": "provider_explicit_progress",
                "verified": False,
            },
            at=30.0,
        ),
        execution_status="running",
        now=30.0,
    )
    reported = status_note(snapshot, now=31.0)
    assert reported["metadata"]["semantic_milestone"] == "capability"
    assert reported["metadata"]["status_facts"]["fact_verified"] is False

    snapshot = project_activity_event(
        snapshot,
        _event(
            5,
            "tool.call",
            {
                "tool": "command_execution",
                "item_id": "validation-1",
                "command": "python -m pytest tests/test_game.py",
            },
            at=40.0,
        ),
        execution_status="running",
        now=40.0,
    )
    snapshot = project_activity_event(
        snapshot,
        _event(
            6,
            "tool.result",
            {
                "item_id": "validation-1",
                "success": True,
                "status": "completed",
            },
            at=41.0,
        ),
        execution_status="running",
        now=41.0,
    )
    observed = status_note(snapshot, now=42.0)
    assert observed["metadata"]["semantic_milestone"] == "validation"
    assert observed["metadata"]["status_facts"]["fact_verified"] is True
    assert observed["summary"] == "Project validation passed."


def test_repeated_permission_fact_does_not_reset_clock_after_other_progress() -> None:
    permission_payload = {
        "capability": "tool.execute",
        "action": "invoke_tool",
        "scope": ["python -m unittest"],
        "reason": "approval required",
        "diagnosticOnly": True,
    }
    first = project_activity_event(
        {},
        _event(1, "permission.requested", permission_payload, at=6_000.0),
        execution_status="running",
        now=6_000.0,
    )
    update = project_activity_event(
        first,
        _event(
            2,
            "assistant.update",
            {"text": "Inspecting a narrower path instead."},
            at=6_010.0,
        ),
        execution_status="running",
        now=6_010.0,
    )
    repeated = project_activity_event(
        update,
        _event(3, "permission.requested", permission_payload, at=6_020.0),
        execution_status="running",
        now=6_020.0,
    )
    assert repeated["permissionDiagnosticCount"] == 2
    assert repeated["lastEventAt"] == 6_020.0
    assert repeated["lastSemanticProgressAt"] == 6_000.0
    assert repeated["latestSemanticSummary"].startswith("Provider policy blocked")
    assert repeated["latestCandidateSummary"] == "Inspecting a narrower path instead."


def test_staging_observation_is_bounded_to_the_attempt_namespace() -> None:
    with tempfile.TemporaryDirectory(prefix="activity_staging_") as temp:
        root = Path(temp)
        workspace = root / "project"
        desktop = root / "Desktop"
        workspace.mkdir()
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            project = store.create_or_get_project(workspace)
            item = store.create_work_item(
                project.project_id,
                title="Prepare report",
                workspace_path=workspace,
            )
            attempt = store.create_attempt(
                item.work_item_id,
                provider="fake",
                task="Prepare report",
            )
            service = WorkExportService(store, desktop_path=desktop)
            staging = service.ensure_private_workspace_child(
                workspace,
                "proposed_exports",
                attempt.attempt_id,
            )
            (staging / "report.pdf").write_bytes(b"report")
            observed = service.observe_staged_files(
                attempt,
                item,
                {"staging_root": str(staging), "requested_filename": "report.pdf"},
            )
            assert observed["changed_files"] == ["report.pdf"]
            try:
                service.observe_staged_files(
                    attempt,
                    item,
                    {"staging_root": str(root), "requested_filename": "report.pdf"},
                )
            except WorkLedgerConflict:
                pass
            else:
                raise AssertionError("an unrelated staging root was accepted")
    print("ok: report staging observation cannot escape the attempt namespace")


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_report_refresh_combines_durable_activity_with_live_git_facts() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="activity_report_") as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            _git(workspace, "init")
            _git(workspace, "config", "user.email", "activity@example.invalid")
            _git(workspace, "config", "user.name", "Activity Test")
            (workspace / "README.md").write_text("baseline\n", encoding="utf-8")
            _git(workspace, "add", "README.md")
            _git(workspace, "commit", "-m", "baseline")

            now = [2_000.0]
            store = WorkLedgerStore(root / "ledger.sqlite3", clock=lambda: now[0])
            coordinator = WorkLedgerCoordinator(store, clock=lambda: now[0])
            prepared = coordinator.prepare_request(
                ProviderRunRequest(
                    provider="fake",
                    task="Build an endless game",
                    cwd=str(workspace),
                    metadata={"session_id": "activity-session", "source": "test"},
                )
            )
            binding = prepared.metadata["work"]
            base_event = {
                "provider": "fake",
                "run_id": "run-activity",
                "metadata": dict(prepared.metadata),
            }
            await coordinator._on_provider_event(
                "provider.event",
                {
                    **base_event,
                    "type": "run.created",
                    "sequence": 1,
                    "observed_at": 2_000.0,
                    "payload": {},
                },
            )
            now[0] = 2_001.0
            await coordinator._on_provider_event(
                "provider.event",
                {
                    **base_event,
                    "type": "run.started",
                    "sequence": 2,
                    "observed_at": 2_001.0,
                    "payload": {},
                },
            )
            now[0] = 2_010.0
            await coordinator._on_provider_event(
                "provider.event",
                {
                    **base_event,
                    "type": "semantic.progress",
                    "sequence": 3,
                    "observed_at": 2_010.0,
                    "payload": {"summary": "The canvas loop is implemented."},
                },
            )
            (workspace / "game.js").write_text("requestAnimationFrame(loop);\n", encoding="utf-8")
            now[0] = 2_100.0
            item = store.get_work_item(str(binding["work_item_id"]))
            assert item is not None
            row = coordinator._conversation_row_for_item(item, "activity-session")
            assert row is not None
            refreshed = await coordinator.enrich_report_row(row)
            assert refreshed["activity_phase"] == "working"
            assert refreshed["activity_elapsed_seconds"] == 99.0
            assert refreshed["activity_silent_seconds"] == 90.0
            assert refreshed["activity_semantic_summary"] == "The canvas loop is implemented."
            assert "game.js" in refreshed["workspace_observation"]["changed_files"]
            facts = task_lookup.render_task_facts(refreshed)
            assert "現在段階：working" in facts
            assert "最後の意味のある進展から：1分30秒" in facts
            assert "作業中の変更：game.js" in facts
            assert "返答では必ず日本語" in facts

            attempt = store.get_attempt(str(binding["attempt_id"]))
            assert attempt is not None
            assert attempt.metadata[ACTIVITY_METADATA_KEY]["latestSemanticSummary"] == (
                "The canvas loop is implemented."
            )
            coordinator.close()

            reopened_store = WorkLedgerStore(root / "ledger.sqlite3", clock=lambda: now[0])
            reopened = WorkLedgerCoordinator(reopened_store, clock=lambda: now[0])
            persisted = reopened_store.get_attempt(str(binding["attempt_id"]))
            assert persisted is not None
            assert persisted.metadata[ACTIVITY_METADATA_KEY]["phase"] == "working"
            assert persisted.metadata[ACTIVITY_METADATA_KEY]["eventSequence"] == 3
            reopened.close()

    asyncio.run(run())
    print("ok: report refresh joins restart-safe activity with bounded live Git facts")


class _ReplacementAdapter:
    provider_id = "replacement"
    manifest = ProviderManifest(
        provider_id="replacement",
        display_name="Replacement test",
        capabilities=ProviderCapabilities(
            task_kinds=("general", "workspace_edit"),
            workspace_access="write",
            workspace_ownership="caller",
            steering="none",
            cancellation="confirmed",
        ),
    )

    def __init__(self, *, confirm_cancel: bool = True) -> None:
        self.confirm_cancel = confirm_cancel
        self.started: list[ProviderRunRequest] = []
        self.started_event = asyncio.Event()

    async def run(self, request, _run_id, _emit) -> ProviderRunResult:
        self.started.append(request)
        self.started_event.set()
        await asyncio.Event().wait()
        return ProviderRunResult(status="done", result="unexpected")

    async def cancel(self, _run_id: str) -> dict:
        if self.confirm_cancel:
            return {"confirmed": True, "cancelled": True}
        return {"confirmed": False, "cancelled": False, "reason": "still_running"}


def _replacement_request(workspace: Path) -> ProviderRunRequest:
    return ProviderRunRequest(
        provider="replacement",
        task="Build a one-player game in game.html",
        cwd=str(workspace),
        mode="agent",
        metadata={"source": "test", "session_id": "replacement-session"},
    )


def test_confirmed_cancel_restarts_same_work_item_with_lineage() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="activity_replace_") as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            _git(workspace, "init")
            _git(workspace, "config", "user.email", "replace@example.invalid")
            _git(workspace, "config", "user.name", "Replacement Test")
            (workspace / "game.html").write_text("one player\n", encoding="utf-8")
            _git(workspace, "add", "game.html")
            _git(workspace, "commit", "-m", "baseline")

            store = WorkLedgerStore(root / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(store)
            coordinator.configure()
            runtime = ProviderRuntime()
            adapter = _ReplacementAdapter(confirm_cancel=True)
            runtime.register(adapter)
            runtime.set_request_preparer(coordinator.prepare_request)
            notes: list[dict] = []

            async def capture(_method: str, params: dict) -> None:
                notes.append(params)

            bus.on(Method.CHAT_WORK_NOTE, capture)
            try:
                with patch.object(settings, "WORK_LEDGER_OWNS_TERMINAL_NARRATION", True):
                    first = await runtime.start(_replacement_request(workspace))
                    await asyncio.wait_for(adapter.started_event.wait(), timeout=2.0)
                    binding = dict(first.metadata["work"])
                    route = await _route_active_amendment(
                        runtime=runtime,
                        coordinator=coordinator,
                        work_item_id=str(binding["work_item_id"]),
                        selected_provider="replacement",
                        task_text="Change game.html into a two-player game.",
                        turn_id="turn-2",
                    )
                    replacement = route.get("replacement")
                    assert route.get("handled") is False and isinstance(replacement, dict)
                    predecessor = store.get_attempt(str(binding["attempt_id"]))
                    assert predecessor is not None and predecessor.execution_status == "cancelled"
                    assert predecessor.metadata["steer_replacement"]["state"] == "cancel_pending"
                    assert not any(
                        note.get("metadata", {}).get("attempt_id") == predecessor.attempt_id
                        and note.get("metadata", {}).get("narration_keypoint") == "terminal"
                        for note in notes
                    ), "the cancelled predecessor must not narrate task cancellation"

                    control = dict(replacement["control"])
                    second_metadata = {
                        "source": "test",
                        "session_id": "replacement-session",
                        "intent": "amend",
                        "continuation": "steer_replacement",
                        "replaces_attempt_id": predecessor.attempt_id,
                        "steer_replacement": control,
                        "work": {
                            "work_item_id": str(replacement["work_item_id"]),
                            "project_id": str(replacement["project_id"]),
                            "workspace_path": str(replacement["workspace_path"]),
                            "workspace_mode": str(replacement["workspace_mode"]),
                        },
                        **dict(replacement["lineage"]),
                    }
                    adapter.started_event = asyncio.Event()
                    second = await runtime.start(
                        ProviderRunRequest(
                            provider="replacement",
                            task=str(replacement["instruction"]),
                            cwd=str(replacement["workspace_path"]),
                            mode=str(replacement["mode"]),
                            metadata=second_metadata,
                        )
                    )
                    await asyncio.wait_for(adapter.started_event.wait(), timeout=2.0)
                    second_binding = dict(second.metadata["work"])
                    assert second_binding["work_item_id"] == binding["work_item_id"]
                    assert second_binding["attempt_id"] != binding["attempt_id"]
                    attempts = store.list_attempts(str(binding["work_item_id"]))
                    assert len(attempts) == 2
                    assert attempts[-1].metadata["continuation"] == "steer_replacement"
                    assert attempts[-1].metadata["replaces_attempt_id"] == predecessor.attempt_id
                    assert "Inspect the current workspace and Git state" in attempts[-1].task
                    old = store.get_attempt(predecessor.attempt_id)
                    assert old is not None
                    assert old.metadata["steer_replacement"]["state"] == "replaced"
                    assert old.metadata["steer_replacement"]["successor_attempt_id"] == (
                        second_binding["attempt_id"]
                    )
                    new_activity = attempts[-1].metadata[ACTIVITY_METADATA_KEY]
                    assert new_activity["steering"]["state"] == "restarted"
                    assert new_activity["steering"]["predecessorAttemptId"] == (
                        predecessor.attempt_id
                    )
                    await runtime.cancel(second.run_id)
            finally:
                bus.off(Method.CHAT_WORK_NOTE, capture)
                await settle_provider_runs(runtime)
                coordinator.close()

    asyncio.run(run())
    print("ok: confirmed cancellation creates one same-task replacement with lineage")


def test_unconfirmed_cancel_never_creates_a_second_writer() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="activity_replace_refused_") as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            store = WorkLedgerStore(root / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(store)
            coordinator.configure()
            runtime = ProviderRuntime()
            adapter = _ReplacementAdapter(confirm_cancel=False)
            runtime.register(adapter)
            runtime.set_request_preparer(coordinator.prepare_request)
            try:
                first = await runtime.start(_replacement_request(workspace))
                await asyncio.wait_for(adapter.started_event.wait(), timeout=2.0)
                binding = dict(first.metadata["work"])
                route = await _route_active_amendment(
                    runtime=runtime,
                    coordinator=coordinator,
                    work_item_id=str(binding["work_item_id"]),
                    selected_provider="replacement",
                    task_text="Make it two-player.",
                    turn_id="turn-2",
                )
                assert route == {
                    "handled": True,
                    "message": "[amend blocked] cancellation unconfirmed",
                }
                attempts = store.list_attempts(str(binding["work_item_id"]))
                assert len(attempts) == 1
                assert attempts[0].execution_status == "running"
                assert attempts[0].metadata["steer_replacement"]["state"] == "rejected"
                assert attempts[0].metadata[ACTIVITY_METADATA_KEY]["phase"] == "working"
                assert store.get_writer_lease(attempts[0].attempt_id).status == "active"
                adapter.confirm_cancel = True
                await runtime.cancel(first.run_id)
            finally:
                await settle_provider_runs(runtime)
                coordinator.close()

    asyncio.run(run())
    print("ok: unconfirmed cancellation leaves exactly one active writer")


def test_replacement_predecessor_has_no_terminal_activity_report() -> None:
    async def run() -> None:
        activity = WorkActivityCoordinator()
        canvases: list[dict] = []
        notes: list[dict] = []

        async def capture_canvas(_method: str, params: dict) -> None:
            canvases.append(params)

        async def capture_note(_method: str, params: dict) -> None:
            notes.append(params)

        bus.on(Method.WALLPAPER_CANVAS, capture_canvas)
        bus.on(Method.CHAT_WORK_NOTE, capture_note)
        try:
            await activity._on_provider_event(
                Method.PROVIDER_EVENT,
                {
                    "provider": "locus",
                    "run_id": "old-replaced-run",
                    "type": "run.cancelled",
                    "payload": {"reason": "steer_replacement"},
                    "metadata": {
                        "session_id": "replacement-session",
                        "cancellation": {"reason": "steer_replacement"},
                    },
                },
            )
            await activity._on_provider_result(
                Method.PROVIDER_RESULT,
                {
                    "provider": "locus",
                    "run_id": "old-replaced-run",
                    "status": "cancelled",
                    "result": "",
                    "metadata": {
                        "session_id": "replacement-session",
                        "cancellation": {"reason": "steer_replacement"},
                    },
                },
            )
        finally:
            bus.off(Method.WALLPAPER_CANVAS, capture_canvas)
            bus.off(Method.CHAT_WORK_NOTE, capture_note)
        assert canvases == [] and notes == []

    asyncio.run(run())
    print("ok: replacement predecessor event and result stay silent in WorkActivity")


def test_provider_snapshot_projection_is_burst_coalesced() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="projection_coalesce_") as temp:
            store = WorkLedgerStore(Path(temp) / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(store)
            coordinator._provider_snapshot_min_interval_s = 0.05
            calls: list[str] = []

            async def capture(*, reason: str, surface: str | None = None) -> dict:
                assert surface is None
                calls.append(reason)
                return {}

            coordinator.publish_snapshot = capture  # type: ignore[method-assign]
            await coordinator._publish_provider_snapshot(
                reason="provider.event:run.created"
            )
            await coordinator._publish_provider_snapshot(
                reason="provider.event:tool.started"
            )
            await coordinator._publish_provider_snapshot(
                reason="provider.event:tool.completed"
            )
            # Provider delivery is not allowed to wait for the heavy Work
            # projection.  A zero-delay scheduled snapshot observes the latest
            # reason in this same burst.
            assert calls == []
            await asyncio.sleep(0)
            assert calls == ["provider.event:tool.completed"]
            await asyncio.sleep(0.08)
            assert calls == ["provider.event:tool.completed"]
            coordinator.close()

    asyncio.run(run())
    print("ok: bursty Provider events coalesce heavy Work projections")


def test_provider_snapshot_projection_does_not_backpressure_event_ingest() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="projection_nonblocking_") as temp:
            store = WorkLedgerStore(Path(temp) / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(store)
            projection_started = asyncio.Event()
            release_projection = asyncio.Event()

            async def capture(*, reason: str, surface: str | None = None) -> dict:
                assert reason == "provider.event:semantic.progress"
                assert surface is None
                projection_started.set()
                await release_projection.wait()
                return {}

            coordinator.publish_snapshot = capture  # type: ignore[method-assign]
            try:
                await asyncio.wait_for(
                    coordinator._publish_provider_snapshot(
                        reason="provider.event:semantic.progress"
                    ),
                    timeout=0.05,
                )
                await asyncio.wait_for(projection_started.wait(), timeout=0.05)
                assert coordinator._provider_snapshot_task is not None
                assert not coordinator._provider_snapshot_task.done()
            finally:
                release_projection.set()
                if coordinator._provider_snapshot_task is not None:
                    await asyncio.gather(
                        coordinator._provider_snapshot_task,
                        return_exceptions=True,
                    )
                coordinator.close()

    asyncio.run(run())
    print("ok: heavy Work projection does not stall Provider event ingest")


def test_mechanical_provider_facts_leave_bus_on_one_ordered_ingest_lane() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="provider_fact_lane_") as temp:
            store = WorkLedgerStore(Path(temp) / "ledger.sqlite3")
            coordinator = WorkLedgerCoordinator(store)
            processing_started = asyncio.Event()
            release_processing = asyncio.Event()
            order: list[str] = []

            async def process(_method: str, params: dict) -> None:
                event_type = str(params.get("type") or "")
                order.append(f"start:{event_type}")
                if event_type == "tool.call":
                    processing_started.set()
                    await release_processing.wait()
                order.append(f"end:{event_type}")

            coordinator._on_provider_event = process  # type: ignore[method-assign]
            try:
                await asyncio.wait_for(
                    coordinator._enqueue_provider_event(
                        Method.PROVIDER_EVENT,
                        {"type": "tool.call"},
                    ),
                    timeout=0.05,
                )
                await asyncio.wait_for(processing_started.wait(), timeout=0.05)

                lifecycle = asyncio.create_task(
                    coordinator._enqueue_provider_event(
                        Method.PROVIDER_EVENT,
                        {"type": "run.created"},
                    )
                )
                await asyncio.sleep(0)
                assert not lifecycle.done()
                assert order == ["start:tool.call"]

                release_processing.set()
                await asyncio.wait_for(lifecycle, timeout=0.2)
                assert order == [
                    "start:tool.call",
                    "end:tool.call",
                    "start:run.created",
                    "end:run.created",
                ]
            finally:
                release_processing.set()
                await coordinator.drain_provider_facts()
                coordinator.close()

    asyncio.run(run())
    print("ok: mechanical facts return early while lifecycle edges preserve order")


def main() -> None:
    test_activity_projection_is_monotonic_and_preserves_control_facts()
    test_dynamic_activity_time_is_computed_at_read_time()
    test_retrospective_permission_is_denied_activity_not_waiting_for_user()
    test_mechanical_events_do_not_reset_semantic_silence()
    test_verified_tool_fact_advances_semantic_clock_once()
    test_status_query_tracks_the_current_steer_evidence_end_to_end()
    test_repeated_permission_fact_does_not_reset_clock_after_other_progress()
    test_staging_observation_is_bounded_to_the_attempt_namespace()
    test_report_refresh_combines_durable_activity_with_live_git_facts()
    test_confirmed_cancel_restarts_same_work_item_with_lineage()
    test_unconfirmed_cancel_never_creates_a_second_writer()
    test_replacement_predecessor_has_no_terminal_activity_report()
    test_provider_snapshot_projection_is_burst_coalesced()
    test_provider_snapshot_projection_does_not_backpressure_event_ingest()
    test_mechanical_provider_facts_leave_bus_on_one_ordered_ingest_lane()


if __name__ == "__main__":
    main()
