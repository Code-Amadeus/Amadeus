"""Approved ephemeral export auto-accept policy tests."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_types import ProviderRunRequest
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from agent_host.work_ledger_types import CompletionDecision
from server.work_export_service import ExportResolution, WorkExportService
from server.work_ledger_coordinator import WorkLedgerCoordinator
from server.event_bus import bus
from server.protocol import Method


class _CommittedExportService(WorkExportService):
    def __init__(self, store: WorkLedgerStore, *, desktop_path: Path, committed: bool = True) -> None:
        super().__init__(store, desktop_path=desktop_path)
        self.committed = committed

    def is_committed_export(self, _permission, _entries) -> bool:
        return self.committed

    def resolve(self, request_id: str, *, allow: bool) -> ExportResolution:
        if not allow:
            permission = self.store.resolve_permission_request(
                request_id,
                "denied",
                metadata={"resolution": "user_denied"},
            )
            return ExportResolution(permission=permission)
        permission = self.store.resolve_permission_request(
            request_id,
            "allowed",
            metadata={"resolution": "user_allowed"},
        )
        self.store.update_attempt(
            permission.attempt_id,
            metadata={
                "export_resolution": {
                    "permission_request_id": permission.request_id,
                    "status": "committed",
                    "exported_paths": ["C:/Desktop/result.html"],
                }
            },
        )
        return ExportResolution(
            permission=permission,
            exported_paths=("C:/Desktop/result.html",),
        )


def test_resolved_export_drops_only_the_obsolete_permission_summary() -> None:
    permission_summary = "The process exited successfully, but a permission decision is still pending."
    permission_only = {
        "reason": "desktop_export_pending",
        "summary": permission_summary,
        "metadata": {
            "attention": "permission",
            "rationale": permission_summary,
        },
    }
    assert WorkLedgerCoordinator._resolved_deferred_execution_summary(permission_only) == ""

    with_verified_outcome = {
        **permission_only,
        "metadata": {
            **permission_only["metadata"],
            "outcome_verdict": {
                "verified": True,
                "summary": "The AUIP application and its connected behavior are verified.",
            },
        },
    }
    assert WorkLedgerCoordinator._resolved_deferred_execution_summary(
        with_verified_outcome
    ) == "The AUIP application and its connected behavior are verified."

    substantive = {
        "reason": "desktop_export_pending",
        "summary": "The game is built and 18 behavior checks passed.",
        "metadata": {
            "attention": "permission",
            "rationale": permission_summary,
        },
    }
    assert WorkLedgerCoordinator._resolved_deferred_execution_summary(
        substantive
    ) == substantive["summary"]


def _scenario(
    root: Path,
    *,
    lifecycle: str = "ephemeral",
    attempt_status: str = "succeeded",
    assessment_status: str = "succeeded",
    permission_status: str = "allowed",
    permission_resolution: str = "user_allowed",
    allow_once: bool = True,
    diagnostic_only: bool = False,
    committed: bool = True,
    enabled: bool = True,
) -> tuple[WorkLedgerCoordinator, str, str, object, object]:
    workspace = root / "workspace"
    workspace.mkdir()
    store = WorkLedgerStore(root / "ledger.sqlite3")
    export_service = _CommittedExportService(
        store,
        desktop_path=root / "desktop",
        committed=committed,
    )
    coordinator = WorkLedgerCoordinator(
        store,
        export_service=export_service,  # type: ignore[arg-type]
        auto_accept_approved_exports=enabled,
    )
    request = ProviderRunRequest(
        provider="fake",
        task="Create an ephemeral HTML artifact",
        cwd=str(workspace),
        mode="agent",
        metadata={"source": "auto-accept-test"},
    )
    coordinator.prepare_request(request)
    binding = request.metadata["work"]
    item_id = str(binding["work_item_id"])
    attempt_id = str(binding["attempt_id"])
    coordinator.record_presentation(item_id, {"lifecycle": lifecycle, "mode": "html"})
    store.update_attempt(attempt_id, execution_status=attempt_status)
    store.record_completion(
        item_id,
        CompletionDecision(
            execution_status=assessment_status,
            completeness="complete",
            attention="review",
            work_item_state="review_ready" if assessment_status == "succeeded" else "open",
            rationale="Terminal execution assessment retained before export approval.",
            terminal=True,
        ),
        attempt_id=attempt_id,
        source="host",
    )
    pending = store.create_permission_request(
        item_id,
        attempt_id=attempt_id,
        capability="filesystem.export",
        action="copy_to_desktop",
        scope_paths=["C:/Desktop/result.html", "C:/Desktop/.result.tmp"],
        reason="Export the validated artifact.",
        reversibility="Creates a new file without overwrite.",
        options=["allow_once", "deny"] if allow_once else ["deny"],
        metadata={
            "kind": "desktop_export",
            "entries": [{"target_path": "C:/Desktop/result.html"}],
            "diagnostic_only": diagnostic_only,
        },
        idempotency_key="approved-export-policy",
    )
    resolved = store.resolve_permission_request(
        pending.request_id,
        permission_status,
        metadata={"resolution": permission_resolution},
    )
    attempt = store.get_attempt(attempt_id)
    assert attempt is not None
    return coordinator, item_id, attempt_id, pending, resolved


def test_condition_matrix() -> None:
    cases = [
        ({}, True, "all conditions satisfied"),
        ({"lifecycle": "durable"}, False, "non-ephemeral"),
        ({"attempt_status": "failed"}, False, "attempt not succeeded"),
        ({"assessment_status": "failed"}, False, "assessment not succeeded"),
        ({"permission_status": "denied", "permission_resolution": "user_denied"}, False, "denied"),
        ({"permission_resolution": "provider_allowed"}, False, "not user allow-once"),
        ({"allow_once": False}, False, "approval option absent"),
        ({"diagnostic_only": True}, False, "diagnostic-only"),
        ({"committed": False}, False, "export uncommitted"),
        ({"enabled": False}, False, "flag off"),
    ]
    for index, (overrides, expected, label) in enumerate(cases):
        with tempfile.TemporaryDirectory(prefix=f"work_auto_accept_{index}_") as temp:
            coordinator, item_id, _attempt_id, pending, resolved = _scenario(
                Path(temp),
                **overrides,
            )
            attempt = coordinator.store.get_attempt(pending.attempt_id)
            assert attempt is not None
            actual = coordinator._auto_accept_approved_export(
                request=pending,
                resolved=resolved,
                attempt=attempt,
                exported_paths=["C:/Desktop/result.html"],
            )
            assert actual is expected, label
            item = coordinator.store.get_work_item(item_id)
            assert item is not None
            assert (item.state == "accepted") is expected, label
            if expected:
                latest = coordinator.store.latest_completion(item_id)
                assert latest is not None and latest.source == "policy"
                assert latest.evidence["export_status"] == "committed"
                assert latest.evidence["permission_resolution"] == "user_allowed"
            coordinator.close()


def test_resolution_path_records_policy_assessment() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_auto_accept_resolution_") as temp:
            root = Path(temp)
            workspace = root / "integration-workspace"
            workspace.mkdir()
            store = WorkLedgerStore(root / "integration.sqlite3")
            service = _CommittedExportService(store, desktop_path=root / "desktop")
            coordinator = WorkLedgerCoordinator(
                store,
                export_service=service,  # type: ignore[arg-type]
                auto_accept_approved_exports=True,
            )
            request = ProviderRunRequest(
                provider="fake",
                task="Create chess.html as an ephemeral artifact",
                cwd=str(workspace),
                mode="agent",
                metadata={"source": "auto-accept-integration"},
            )
            coordinator.prepare_request(request)
            binding = request.metadata["work"]
            item_id = str(binding["work_item_id"])
            attempt_id = str(binding["attempt_id"])
            coordinator.record_presentation(item_id, {"lifecycle": "ephemeral", "mode": "html"})
            store.update_attempt(attempt_id, execution_status="succeeded")
            store.update_attempt(
                attempt_id,
                metadata={
                    "deferred_terminal_narration": {
                        "title": "Desktop deliverable task finished",
                        "summary": (
                            "The endless game was built. Automated headless validation was "
                            "blocked by the sandbox; deterministic manual checks passed."
                        ),
                        "reason": "desktop_export_pending",
                    }
                },
            )
            store.record_completion(
                item_id,
                CompletionDecision(
                    execution_status="succeeded",
                    completeness="complete",
                    attention="review",
                    work_item_state="review_ready",
                    rationale="The ephemeral artifact executed successfully.",
                    terminal=True,
                ),
                attempt_id=attempt_id,
                source="host",
            )
            pending = store.create_permission_request(
                item_id,
                attempt_id=attempt_id,
                capability="filesystem.export",
                action="copy_to_desktop",
                scope_paths=["C:/Desktop/chess.html", "C:/Desktop/.chess.tmp"],
                reason="Export chess.html.",
                reversibility="Creates a new file.",
                options=["allow_once", "deny"],
                metadata={
                    "kind": "desktop_export",
                    "entries": [{"target_path": "C:/Desktop/chess.html"}],
                },
                idempotency_key="integration-approved-export",
            )
            notes: list[dict] = []

            async def capture_note(_method: str, payload: dict) -> None:
                notes.append(payload)

            bus.on(Method.CHAT_WORK_NOTE, capture_note)
            try:
                response = await coordinator.resolve_permission(
                    pending.request_id,
                    allow=True,
                    work_item_id=item_id,
                    attempt_id=attempt_id,
                )
            finally:
                bus.off(Method.CHAT_WORK_NOTE, capture_note)
            assert response["work"]["selected"]["state"] == "accepted"
            assert response["work"]["counts"]["needsAttention"] == 0
            latest = store.latest_completion(item_id)
            assert latest is not None and latest.source == "policy"
            assert latest.evidence["exported_paths"] == ["C:/Desktop/result.html"]
            terminal_notes = [
                note
                for note in notes
                if note.get("metadata", {}).get("narration_keypoint") == "terminal"
            ]
            assert len(terminal_notes) == 1
            assert terminal_notes[0]["metadata"]["work_event"] == "work.accepted"
            assert terminal_notes[0]["metadata"]["work_item_id"] == item_id
            assert terminal_notes[0]["metadata"]["attempt_id"] == attempt_id
            assert terminal_notes[0]["phase"].lower() == "result"
            assert "The endless game was built" in terminal_notes[0]["summary"]
            assert "headless validation was blocked" in terminal_notes[0]["summary"]
            assert "C:/Desktop/result.html" in terminal_notes[0]["summary"]
            assert terminal_notes[0]["metadata"]["execution_summary_included"] is True
            refreshed_attempt = store.get_attempt(attempt_id)
            assert refreshed_attempt is not None
            outbox = refreshed_attempt.metadata["terminal_work_notice_outbox"]
            assert len(outbox) == 1
            assert outbox[0]["state"] == "pending"
            assert "terminal_work_notice_ids" not in refreshed_attempt.metadata
            replayed_notes: list[dict] = []

            async def capture_replay(_method: str, payload: dict) -> None:
                replayed_notes.append(payload)

            bus.on(Method.CHAT_WORK_NOTE, capture_replay)
            try:
                assert await coordinator.replay_pending_terminal_notices() == 1
            finally:
                bus.off(Method.CHAT_WORK_NOTE, capture_replay)
            assert len(replayed_notes) == 1
            assert (
                replayed_notes[0]["metadata"]["delivery_id"]
                == terminal_notes[0]["metadata"]["delivery_id"]
            )
            await coordinator._on_terminal_work_notice_delivered(
                Method.CHAT_WORK_NOTE_DELIVERED,
                {
                    "attempt_id": attempt_id,
                    "delivery_id": terminal_notes[0]["metadata"]["delivery_id"],
                },
            )
            refreshed_attempt = store.get_attempt(attempt_id)
            assert refreshed_attempt is not None
            assert refreshed_attempt.metadata["terminal_work_notice_outbox"][0]["state"] == "delivered"
            assert len(refreshed_attempt.metadata["terminal_work_notice_ids"]) == 1
            assert (
                refreshed_attempt.metadata["deferred_terminal_narration"]["resolved_by"]
                == f"permission:{pending.request_id}:allowed"
            )
            coordinator._defer_terminal_work_notice(
                refreshed_attempt,
                {
                    "title": "Replayed result",
                    "summary": "This replay must not reopen the terminal boundary.",
                },
            )
            replay_guarded = store.get_attempt(attempt_id)
            assert replay_guarded is not None
            assert (
                replay_guarded.metadata["deferred_terminal_narration"]["summary"]
                .startswith("The endless game was built")
            )
            coordinator.close()

    asyncio.run(run())


def test_policy_accept_requires_complete_evidence() -> None:
    with tempfile.TemporaryDirectory(prefix="work_auto_accept_guard_") as temp:
        root = Path(temp)
        coordinator, item_id, attempt_id, _pending, _resolved = _scenario(root)
        try:
            coordinator.store.record_completion(
                item_id,
                CompletionDecision(
                    execution_status="succeeded",
                    completeness="complete",
                    attention="none",
                    work_item_state="accepted",
                    rationale="Incomplete policy evidence must not accept.",
                    terminal=True,
                ),
                attempt_id=attempt_id,
                source="policy",
                evidence={"policy": "auto_accept_approved_export"},
            )
        except WorkLedgerConflict:
            pass
        else:
            raise AssertionError("policy acceptance without commit evidence was allowed")
        coordinator.close()


def test_declined_export_notice_is_non_success_and_idempotent() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_declined_notice_") as temp:
        coordinator, _item_id, _attempt_id, pending, resolved = _scenario(
            Path(temp),
            permission_status="denied",
            permission_resolution="user_denied",
        )
        try:
            attempt = coordinator.store.get_attempt(pending.attempt_id)
            assert attempt is not None
            first = coordinator._claim_export_resolution_notice(
                request=pending,
                resolved=resolved,
                attempt=attempt,
                exported_paths=[],
                work_item_state="open",
                attention="review",
            )
            second = coordinator._claim_export_resolution_notice(
                request=pending,
                resolved=resolved,
                attempt=attempt,
                exported_paths=[],
                work_item_state="open",
                attention="review",
            )
            assert first is not None
            assert first["metadata"]["work_event"] == "work.export_declined"
            assert "no file was copied" in first["summary"]
            assert second is None
        finally:
            coordinator.close()


def _main() -> None:
    test_condition_matrix()
    test_resolution_path_records_policy_assessment()
    test_policy_accept_requires_complete_evidence()
    test_declined_export_notice_is_non_success_and_idempotent()
    print("ok: approved ephemeral exports auto-accept only with all policy evidence")


if __name__ == "__main__":
    _main()
