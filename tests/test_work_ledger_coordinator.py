"""WorkLedgerCoordinator behavior tests without app or provider processes.

The suite is standalone-runner and pytest compatible.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_types import ProviderRunRequest
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.protocol import Method
from server.auip_app_source import (
    discover_exported_auip_apps,
    discover_launchable_auip_app,
    discover_registered_auip_app,
)
from server.work_export_service import WorkExportService
from server.work_ledger_coordinator import (
    DEFAULT_WORK_SURFACE,
    WORKSPACE_ROUTING_SURFACE,
    WorkLedgerCoordinator,
)


def _prepare(
    coordinator: WorkLedgerCoordinator,
    *,
    cwd: Path,
    task: str,
    mode: str = "agent",
    work_item_id: str = "",
    continuation: str = "",
    project_id: str = "",
    turn_id: str = "",
) -> tuple[ProviderRunRequest, str, str]:
    metadata = {"source": "test"}
    if work_item_id:
        metadata["work"] = {"work_item_id": work_item_id}
    if continuation:
        metadata["continuation"] = continuation
        if continuation == "retry" and work_item_id:
            attempts = coordinator.store.list_attempts(work_item_id)
            if attempts:
                metadata["retry_of"] = attempts[-1].attempt_id
    if project_id:
        metadata["project_id"] = project_id
    if turn_id:
        metadata["turn_id"] = turn_id
    request = ProviderRunRequest(
        provider="fake",
        task=task,
        cwd=str(cwd),
        mode=mode,
        metadata=metadata,
    )
    prepared = coordinator.prepare_request(request)
    work = prepared.metadata["work"]
    return prepared, str(work["work_item_id"]), str(work["attempt_id"])


def test_chat_turn_identity_survives_work_intake() -> None:
    with tempfile.TemporaryDirectory(prefix="work_coordinator_turn_identity_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            prepared, _item_id, attempt_id = _prepare(
                coordinator,
                cwd=workspace,
                task="Prepare the existing app",
                turn_id="turn-auip-prepare",
            )

            attempt = store.get_attempt(attempt_id)
            assert attempt is not None
            assert attempt.metadata["turn_id"] == "turn-auip-prepare"
            operation = store.get_operation(str(prepared.metadata["work"]["operation_id"]))
            assert operation is not None
            assert operation.metadata["turn_id"] == "turn-auip-prepare"


def test_amendment_inherits_the_workitems_host_outcome_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="work_outcome_contract_amend_") as temp:
        root = Path(temp)
        workspace = root / "project"
        desktop = root / "Desktop"
        workspace.mkdir()
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(
                store,
                export_service=WorkExportService(store, desktop_path=desktop),
            )
            original = coordinator.prepare_request(
                ProviderRunRequest(
                    provider="fake",
                    task="Prepare the Gomoku bundle for AUIP on Desktop.",
                    cwd=str(workspace),
                    mode="agent",
                    metadata={
                        "source": "auip_prepare",
                        "external_export": {
                            "target": "desktop",
                            "filename": "gomoku.html",
                        },
                        "host_outcome_requirement": {
                            "operation": "prepare",
                            "facet": "auip.application",
                            "expected": {"current_attempt_contribution": True},
                        },
                    },
                )
            )
            original_work = original.metadata["work"]
            store.update_attempt(
                str(original_work["attempt_id"]),
                execution_status="succeeded",
            )
            store.release_writer_lease(
                str(original_work["attempt_id"]),
                status="released",
            )

            amended = coordinator.prepare_request(
                ProviderRunRequest(
                    provider="fake",
                    task="Export the already fixed bundle to Desktop.",
                    cwd=str(workspace),
                    mode="agent",
                    metadata={
                        "source": "llm_delegate",
                        "intent": "amend",
                        "continuation": "amend",
                        "work": {
                            "work_item_id": str(original_work["work_item_id"]),
                        },
                        "external_export": {"target": "desktop"},
                    },
                )
            )

            assert amended.metadata["host_outcome_requirement"] == {
                "operation": "prepare",
                "facet": "auip.application",
                "expected": {"current_attempt_contribution": True},
            }
            plan = amended.metadata["export_plan"]
            assert plan["publication_shape"] == "bundle"
            assert plan["host_validates_auip_bundle"] is True
            assert set(plan["host_materialized_assets"]) == {
                "sdk/auip-core/managed-v0.js",
                "sdk/auip-core/controller-v0.js",
                "sdk/auip-core/situations-v0.js",
                "sdk/auip-web/auip-v0.js",
            }
            stage = Path(plan["staging_root"])
            _write_staged_auip_bundle(
                stage,
                entry="gomoku.html",
                manifest={
                    "schema": "amadeus.auip/v0",
                    "app": {"id": "gomoku", "title": "Gomoku"},
                    "events": {"game.ready": {"beat": True}},
                    "actions": {},
                    "stances": ["spectator"],
                },
            )
            amended_attempt = store.get_attempt(
                str(amended.metadata["work"]["attempt_id"])
            )
            amended_item = store.get_work_item(
                str(amended.metadata["work"]["work_item_id"])
            )
            assert amended_attempt is not None and amended_item is not None
            export = coordinator.export_service.discover_staged_exports(
                amended_attempt,
                amended_item,
                plan,
            )
            assert export["available"] is True
            assert export["permission"] is not None
            published_root = str(plan["target_relative_root"])
            assert set(export["permission"].metadata["preview_opaque_files"]) == {
                f"Desktop/{published_root}/sdk/auip-core/controller-v0.js",
                f"Desktop/{published_root}/sdk/auip-core/managed-v0.js",
                f"Desktop/{published_root}/sdk/auip-core/situations-v0.js",
                f"Desktop/{published_root}/sdk/auip-web/auip-v0.js",
            }


def test_host_write_intent_cannot_reach_provider_as_read_only() -> None:
    with tempfile.TemporaryDirectory(prefix="work_write_contract_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            request = ProviderRunRequest(
                provider="codex",
                task="Create the approved HTML artifact.",
                cwd=str(workspace),
                mode="delegate",
                metadata={
                    "source": "test",
                    "write_intent": True,
                    "provider_manifest": CODEX_APP_SERVER_MANIFEST.to_dict(),
                },
                requirements=ProviderRequirements(
                    task_kind="general",
                    workspace_access="none",
                    preferred_provider="codex",
                    preference_policy="require",
                ),
            )
            with patch(
                "server.work_ledger_coordinator.app_settings.WORK_WORKTREE_ISOLATION",
                False,
            ):
                prepared = coordinator.prepare_request(request)

            assert prepared.requirements is not None
            assert prepared.requirements.task_kind == "general"
            assert prepared.requirements.workspace_access == "write"
            assert prepared.metadata["provider_requirements"]["workspace_access"] == "write"
            attempt = store.get_attempt(prepared.metadata["work"]["attempt_id"])
            assert attempt is not None
            assert attempt.metadata["provider_requirements"]["workspace_access"] == "write"
            assert store.get_writer_lease(attempt.attempt_id) is not None


def test_explicit_amendment_supersedes_only_the_obsolete_pending_export() -> None:
    with tempfile.TemporaryDirectory(prefix="work_amend_supersedes_export_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            _first, item_id, first_attempt_id = _prepare(
                coordinator,
                cwd=workspace,
                task="Build a Gomoku game with a built-in opponent.",
                turn_id="turn-first",
            )
            store.update_attempt(first_attempt_id, execution_status="succeeded")
            permission = store.create_permission_request(
                item_id,
                attempt_id=first_attempt_id,
                capability="filesystem.export",
                action="copy_to_desktop",
                scope_paths=[str(root / "Desktop" / "gomoku.html")],
                reason="Copy the first revision to Desktop.",
                reversibility="copy",
                options=["allow_once", "deny"],
                metadata={"kind": "desktop_export"},
            )
            store.update_attempt(
                first_attempt_id,
                metadata={
                    "deferred_terminal_narration": {
                        "summary": "The old built-in AI version is complete.",
                        "reason": "desktop_export_pending",
                    },
                    "terminal_work_notice_outbox": [
                        {
                            "delivery_id": "old-terminal",
                            "state": "pending",
                            "note": {"summary": "Old result"},
                        }
                    ],
                },
            )

            _second, same_item_id, second_attempt_id = _prepare(
                coordinator,
                cwd=workspace,
                task="Amend it so the main assistant can play with me.",
                work_item_id=item_id,
                continuation="amend",
                turn_id="turn-correction",
            )

            assert same_item_id == item_id
            assert second_attempt_id != first_attempt_id
            resolved = store.get_permission_request(permission.request_id)
            assert resolved is not None and resolved.status == "expired"
            assert resolved.metadata["resolution"] == "superseded_by_work_amendment"
            assert resolved.metadata["superseding_turn_id"] == "turn-correction"
            first_attempt = store.get_attempt(first_attempt_id)
            assert first_attempt is not None
            deferred = first_attempt.metadata["deferred_terminal_narration"]
            assert deferred["resolved_by"] == "superseded_by_work_amendment"
            outbox = first_attempt.metadata["terminal_work_notice_outbox"]
            assert outbox[0]["state"] == "superseded"
            assert "note" not in outbox[0]


def test_explicit_amendment_does_not_bypass_an_execution_permission() -> None:
    with tempfile.TemporaryDirectory(prefix="work_amend_keeps_permission_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            _first, item_id, first_attempt_id = _prepare(
                coordinator,
                cwd=workspace,
                task="Build the game.",
            )
            store.update_attempt(first_attempt_id, execution_status="succeeded")
            permission = store.create_permission_request(
                item_id,
                attempt_id=first_attempt_id,
                capability="shell.execute",
                action="run_command",
                reason="Run an external command.",
                reversibility="unknown",
                options=["allow_once", "deny"],
                metadata={"kind": "provider_permission"},
            )

            try:
                _prepare(
                    coordinator,
                    cwd=workspace,
                    task="Amend the game.",
                    work_item_id=item_id,
                    continuation="amend",
                )
            except WorkLedgerConflict as exc:
                assert "pending permission" in str(exc)
            else:
                raise AssertionError("an execution permission must remain blocking")
            current = store.get_permission_request(permission.request_id)
            assert current is not None and current.status == "pending"
            assert len(store.list_attempts(item_id)) == 1


def test_code_provider_receives_attempt_local_auip_authoring_inputs() -> None:
    with tempfile.TemporaryDirectory(prefix="work_coordinator_authoring_inputs_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            prepared = coordinator.prepare_request(
                ProviderRunRequest(
                    provider="codex",
                    task="Prepare this game for an AUIP experience.",
                    cwd=str(workspace),
                    mode="agent",
                        metadata={
                            "source": "auip_prepare",
                            "host_outcome_requirement": {
                                "operation": "prepare",
                                "facet": "auip.application",
                                "expected": {
                                    "current_attempt_contribution": True
                                },
                            },
                            "provider_manifest": CODEX_APP_SERVER_MANIFEST.to_dict(),
                        },
                )
            )

            skill = Path(str(prepared.metadata["auip_authoring_skill_path"]))
            assert skill.is_file()
            assert skill.is_relative_to(workspace.resolve())
            assert ".amadeus" in skill.parts
            assert not (skill.parent / "references" / "protocol-v0.md").exists()
            assert not (skill.parents[2] / "sdk" / "auip-web" / "auip-v0.js").exists()
            assert (workspace / "sdk" / "auip-web" / "auip-v0.js").is_file()
            assert (workspace / "sdk" / "auip-core" / "managed-v0.js").is_file()
            metrics = prepared.metadata["auip_authoring_inputs"]
            assert metrics["required_read_file_count"] == 2
            assert metrics["required_read_bytes"] < metrics["staged_bytes"]
            attempt = store.get_attempt(prepared.metadata["work"]["attempt_id"])
            assert attempt is not None
            assert attempt.metadata["auip_authoring_inputs"] == metrics
            assert attempt.metadata["auip_authoring_bundle_mode"] == "lean_host_managed"
            assert attempt.metadata["auip_host_validates_bundle"] is True
            assert attempt.metadata["auip_host_materialized_files"] == [
                "sdk/auip-core/controller-v0.js",
                "sdk/auip-core/managed-v0.js",
                "sdk/auip-core/situations-v0.js",
                "sdk/auip-web/auip-v0.js",
            ]
            assert (workspace / ".amadeus" / ".gitignore").read_text(
                encoding="utf-8"
            ) == "*\n"


def test_ordinary_code_work_does_not_receive_auip_authoring_inputs() -> None:
    with tempfile.TemporaryDirectory(prefix="work_coordinator_no_auip_inputs_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            prepared = coordinator.prepare_request(
                ProviderRunRequest(
                    provider="codex",
                    task="Build a standalone HTML puzzle.",
                    cwd=str(workspace),
                    mode="agent",
                    metadata={
                        "source": "llm_delegate",
                        "provider_manifest": CODEX_APP_SERVER_MANIFEST.to_dict(),
                    },
                )
            )

            assert "auip_authoring_skill_path" not in prepared.metadata
            assert "auip_authoring_inputs" not in prepared.metadata
            attempt = store.get_attempt(prepared.metadata["work"]["attempt_id"])
            assert attempt is not None
            assert "auip_authoring_inputs" not in attempt.metadata
            assert not (workspace / ".amadeus" / "runtime" / "authoring_inputs").exists()


def test_host_outcome_contract_blocks_false_auip_completion() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="work_coordinator_auip_outcome_") as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store)
                request = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="codex",
                        task="Prepare this game for an AUIP experience.",
                        cwd=str(workspace),
                        mode="agent",
                        metadata={
                            "source": "auip_prepare",
                            "host_outcome_requirement": {
                                "operation": "prepare",
                                "facet": "auip.application",
                                "expected": {"current_attempt_contribution": True},
                            },
                            "provider_manifest": CODEX_APP_SERVER_MANIFEST.to_dict(),
                        },
                    )
                )
                work = request.metadata["work"]
                await _bind_run(coordinator, request, "codex-auip-outcome")
                await _finish_run(
                    coordinator,
                    request,
                    "codex-auip-outcome",
                    result="The AUIP preparation is complete.",
                )

                completions = store.list_completions(str(work["work_item_id"]))
                assert completions
                assert completions[-1].attention == "error"
                assert completions[-1].completeness == "incomplete"
                attempt = store.get_attempt(str(work["attempt_id"]))
                assert attempt is not None
                verdict = attempt.metadata["outcome_verdict"]
                assert verdict["facet"] == "auip.application"
                assert verdict["verified"] is False
                assert verdict["provider_report_allowed"] is False

    asyncio.run(scenario())


def test_cancelled_auip_preparation_keeps_cancellation_as_terminal_truth() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="work_coordinator_auip_cancel_") as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store)
                original = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="codex",
                        task="Build the existing game.",
                        cwd=str(workspace),
                        mode="agent",
                        metadata={
                            "source": "test",
                            "provider_manifest": CODEX_APP_SERVER_MANIFEST.to_dict(),
                        },
                    )
                )
                original_work = original.metadata["work"]
                await _bind_run(coordinator, original, "codex-original-game")
                await _finish_run(
                    coordinator,
                    original,
                    "codex-original-game",
                    result="The existing game is ready for review.",
                )
                original_item = store.get_work_item(str(original_work["work_item_id"]))
                assert original_item is not None
                assert original_item.state == "review_ready"

                request = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="codex",
                        task="Connect the existing game for shared play.",
                        cwd=str(workspace),
                        mode="agent",
                        metadata={
                            "source": "auip_prepare",
                            "intent": "amend",
                            "continuation": "amend",
                            "work": {
                                "work_item_id": str(original_work["work_item_id"]),
                            },
                            "host_outcome_requirement": {
                                "operation": "prepare",
                                "facet": "auip.application",
                                "expected": {"current_attempt_contribution": True},
                            },
                            "provider_manifest": CODEX_APP_SERVER_MANIFEST.to_dict(),
                        },
                    )
                )
                work = request.metadata["work"]
                await _bind_run(coordinator, request, "codex-auip-cancelled")
                await _finish_run(
                    coordinator,
                    request,
                    "codex-auip-cancelled",
                    result="I was still reading the integration contract.",
                    status="cancelled",
                )

                attempt = store.get_attempt(str(work["attempt_id"]))
                assert attempt is not None
                assert attempt.execution_status == "cancelled"
                assert "outcome_verdict" not in attempt.metadata
                completion = store.latest_completion(str(work["work_item_id"]))
                assert completion is not None
                assert completion.execution_status == "cancelled"
                assert "cancelled" in completion.rationale.lower()
                assert "AUIP" not in completion.rationale
                assert "previous review-ready result remains available" in completion.rationale
                item = store.get_work_item(str(work["work_item_id"]))
                assert item is not None and item.state == "review_ready"

    asyncio.run(scenario())


def test_host_outcome_contract_accepts_registered_auip_revision() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="work_coordinator_auip_verified_") as temp:
            root = Path(temp)
            workspace = root / "project"
            workspace.mkdir()
            for args in (
                ("init", "--quiet"),
                ("config", "user.email", "amadeus-test@example.invalid"),
                ("config", "user.name", "Amadeus Test"),
            ):
                completed = subprocess.run(
                    ["git", "-C", str(workspace), *args],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                assert completed.returncode == 0, completed.stderr
            index = workspace / "index.html"
            index.write_text("<button id='play'>Play</button>\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(workspace), "add", "index.html"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(workspace), "commit", "--quiet", "-m", "fixture"],
                check=True,
            )
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store)
                request = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="codex",
                        task="Prepare this game for an AUIP experience.",
                        cwd=str(workspace),
                        mode="agent",
                        metadata={
                            "source": "auip_prepare",
                            "host_outcome_requirement": {
                                "operation": "prepare",
                                "facet": "auip.application",
                                "expected": {"current_attempt_contribution": True},
                            },
                            "provider_manifest": CODEX_APP_SERVER_MANIFEST.to_dict(),
                        },
                    )
                )
                work = request.metadata["work"]
                await _bind_run(coordinator, request, "codex-auip-verified")
                _write_staged_auip_bundle(
                    workspace,
                    entry="index.html",
                    manifest={
                        "schema": "amadeus.auip/v0",
                        "app": {
                            "id": "test-game",
                            "title": "Test Game",
                            "version": "0.1.0",
                        },
                        "events": {"game.ready": {"beat": True}},
                        "actions": {},
                        "stances": ["spectator"],
                    },
                )
                await _finish_run(
                    coordinator,
                    request,
                    "codex-auip-verified",
                    result="The AUIP preparation is complete.",
                )

                discovered = discover_registered_auip_app(
                    store,
                    str(work["work_item_id"]),
                )
                assert discovered is not None
                assert discovered["attempt_id"] == work["attempt_id"]
                attempt = store.get_attempt(str(work["attempt_id"]))
                assert attempt is not None
                verdict = attempt.metadata["outcome_verdict"]
                assert verdict["verified"] is True
                assert verdict["observed"]["bundle_validation_required"] is True
                assert verdict["observed"]["bundle_validation_verified"] is True
                completions = store.list_completions(str(work["work_item_id"]))
                assert completions[-1].completeness == "partial"
                assert completions[-1].attention == "review"

                # Attach revalidates the Host-owned sidecars instead of
                # trusting the earlier completion verdict.
                (workspace / "sdk" / "auip-core" / "managed-v0.js").write_text(
                    "tampered",
                    encoding="utf-8",
                )
                assert discover_registered_auip_app(
                    store,
                    str(work["work_item_id"]),
                ) is None

    asyncio.run(scenario())


def test_auip_outcome_materializes_and_permissions_one_complete_desktop_bundle() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="work_coordinator_auip_staging_") as temp:
            root = Path(temp)
            workspace = root / "project"
            desktop = root / "Desktop"
            workspace.mkdir()
            desktop.mkdir()
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(
                    store,
                    export_service=WorkExportService(store, desktop_path=desktop),
                )
                request = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="codex",
                        task="Connect the existing Gomoku delivery so we can play together.",
                        cwd=str(workspace),
                        mode="agent",
                        metadata={
                            "source": "auip_prepare",
                            "session_id": "session-auip-staging",
                            "turn_id": "turn-auip-staging",
                            "intent": "amend",
                            "external_export": {
                                "target": "desktop",
                                "filename": "gomoku.html",
                            },
                            "host_outcome_requirement": {
                                "operation": "prepare",
                                "facet": "auip.application",
                                "expected": {"current_attempt_contribution": True},
                            },
                            "provider_manifest": CODEX_APP_SERVER_MANIFEST.to_dict(),
                        },
                    )
                )
                work = request.metadata["work"]
                plan = request.metadata["export_plan"]
                stage = Path(plan["staging_root"])
                assert plan["host_validates_auip_bundle"] is True
                assert plan["host_materialized_files"] == [
                    "sdk/auip-core/controller-v0.js",
                    "sdk/auip-core/managed-v0.js",
                    "sdk/auip-core/situations-v0.js",
                    "sdk/auip-web/auip-v0.js",
                ]
                assert set(plan["host_materialized_assets"]) == set(
                    plan["host_materialized_files"]
                )
                assert all(
                    len(identity["sha256"]) == 64
                    and int(identity["size_bytes"]) > 0
                    for identity in plan["host_materialized_assets"].values()
                )
                assert all(
                    (stage / Path(name)).is_file()
                    for name in plan["host_materialized_files"]
                )
                assert "do not open, copy, edit, regenerate" in request.task
                assert "do not search for or duplicate runtime-integrity" in request.task
                assert "after the required checks pass" in request.task
                assert request.metadata["auip_authoring_bundle_mode"] == "lean_host_managed"
                skill_path = Path(request.metadata["auip_authoring_skill_path"])
                authoring_root = skill_path.parents[2]
                assert not (
                    authoring_root / "sdk" / "auip-core" / "managed-v0.js"
                ).exists()
                _write_staged_auip_bundle(
                    stage,
                    entry="gomoku.html",
                    manifest={
                        "schema": "amadeus.auip/v0",
                        "app": {
                            "id": "gomoku",
                            "title": "Gomoku",
                            "version": "0.1.0",
                            "interactionSummary": (
                                "The participant can place one legal stone. "
                                "For example, 'block that line' selects one legal block."
                            ),
                        },
                        "events": {"game.changed": {"beat": True}},
                        "situationKinds": ["grid/v1"],
                        "actions": {
                            "game.place_stone": {
                                "description": (
                                    "Place one stone only when state.turn belongs to "
                                    "the participant and "
                                    "state.board.rows[payload.y][payload.x] is empty."
                                ),
                                "risk": "local_execution",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "x": {
                                            "type": "integer",
                                            "minimum": 0,
                                            "maximum": 14,
                                        },
                                        "y": {
                                            "type": "integer",
                                            "minimum": 0,
                                            "maximum": 14,
                                        },
                                    },
                                    "required": ["x", "y"],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        "stances": ["spectator", "participant"],
                    },
                )

                await _bind_run(coordinator, request, "codex-auip-staged")
                await _finish_run(coordinator, request, "codex-auip-staged")

                # Host validation may register the staged bytes as current
                # Attempt evidence, but delivery staging is not a stable app
                # revision and must not be launched before approval.
                assert discover_registered_auip_app(
                    store,
                    str(work["work_item_id"]),
                ) is None
                staged_business_files = {
                    Path(str(artifact.path)).name
                    for artifact in store.list_artifacts(
                        str(work["work_item_id"]),
                        attempt_id=str(work["attempt_id"]),
                    )
                    if artifact.kind == "business.file"
                    and artifact.metadata.get("attribution")
                    == "host_outcome:auip.application"
                }
                assert staged_business_files == {
                    "auip.manifest.json",
                    "auip-v0.js",
                    "controller-v0.js",
                    "gomoku.html",
                    "managed-v0.js",
                    "situations-v0.js",
                }

                # An AUIP application is the whole verified delivery, not an
                # HTML file with missing runtime sidecars. The one permission
                # therefore names every file under a new Desktop directory.
                permissions = store.list_permission_requests(
                    str(work["work_item_id"]),
                    attempt_id=str(work["attempt_id"]),
                )
                assert len(permissions) == 1
                preview_entries = {
                    entry["staging_relative_path"]: entry
                    for entry in permissions[0].metadata["entries"]
                }
                for runtime_name in plan["host_materialized_files"]:
                    assert preview_entries[runtime_name]["preview_status"] == (
                        "host_verified_opaque"
                    )
                    assert (
                        f"diff --git a/Desktop/gomoku/{runtime_name}"
                        not in permissions[0].metadata["preview_patch"]
                )
                assert preview_entries["gomoku.html"]["preview_status"] == (
                    "complete_text"
                )
                assert "gomoku.html" in permissions[0].metadata["preview_patch"]
                assert sorted(
                    entry["relative_path"]
                    for entry in permissions[0].metadata["entries"]
                ) == [
                    "gomoku/auip.manifest.json",
                    "gomoku/gomoku.html",
                    "gomoku/sdk/auip-core/controller-v0.js",
                    "gomoku/sdk/auip-core/managed-v0.js",
                    "gomoku/sdk/auip-core/situations-v0.js",
                    "gomoku/sdk/auip-web/auip-v0.js",
                ]
                resolution = coordinator.export_service.resolve(
                    permissions[0].request_id,
                    allow=True,
                )
                assert sorted(Path(path).name for path in resolution.exported_paths) == [
                    "auip-v0.js",
                    "auip.manifest.json",
                    "controller-v0.js",
                    "gomoku.html",
                    "managed-v0.js",
                    "situations-v0.js",
                ]
                app = discover_launchable_auip_app(store, str(work["work_item_id"]))
                assert app is not None
                assert app["attempt_id"] == work["attempt_id"]
                assert app["app"]["title"] == "Gomoku"
                assert Path(app["entry_path"]) == desktop / "gomoku" / "gomoku.html"
                exported_apps = discover_exported_auip_apps(
                    store,
                    json.loads((stage / "auip.manifest.json").read_text(encoding="utf-8")),
                )
                assert len(exported_apps) == 1
                assert Path(exported_apps[0]["bundle_path"]) == desktop / "gomoku"
                attempt = store.get_attempt(str(work["attempt_id"]))
                assert attempt is not None
                assert attempt.metadata["outcome_verdict"]["verified"] is True
                assert attempt.metadata["host_outcome_materialization"]["file_count"] == 6

    asyncio.run(scenario())


def test_progressive_auip_prepare_uses_current_attempt_despite_role_rename() -> None:
    """Natural "connect it" keeps the approved Artifact identity across Attempts."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="work_coordinator_auip_lineage_") as temp:
            root = Path(temp)
            workspace = root / "project"
            desktop = root / "Desktop"
            workspace.mkdir()
            desktop.mkdir()
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(
                    store,
                    export_service=WorkExportService(store, desktop_path=desktop),
                )
                created = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="codex",
                        task="Create gomoku.html on the Desktop.",
                        cwd=str(workspace),
                        mode="agent",
                        metadata={
                            "source": "llm_delegate",
                            "source_user_text": "你能帮我在桌面写一个五子棋游戏吗？",
                            "external_export": {"target": "desktop"},
                            "provider_manifest": CODEX_APP_SERVER_MANIFEST.to_dict(),
                        },
                    )
                )
                created_work = created.metadata["work"]
                created_stage = Path(created.metadata["export_plan"]["staging_root"])
                original_bytes = b"<!doctype html><title>Gomoku</title>\n"
                (created_stage / "gomoku.html").write_bytes(original_bytes)
                await _bind_run(coordinator, created, "codex-created-gomoku")
                await _finish_run(coordinator, created, "codex-created-gomoku")
                created_permissions = store.list_permission_requests(
                    str(created_work["work_item_id"]),
                    attempt_id=str(created_work["attempt_id"]),
                )
                assert len(created_permissions) == 1
                coordinator.export_service.resolve(
                    created_permissions[0].request_id,
                    allow=True,
                )
                assert (desktop / "gomoku.html").read_bytes() == original_bytes

                prepared = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="codex",
                        task=(
                            "五子棋をAUIP対応にして、ファイル名は "
                            "wuziqi.html とする。"
                        ),
                        cwd=str(workspace),
                        mode="agent",
                        metadata={
                            "source": "auip_prepare",
                            "source_user_text": "你能接入它吗？我想和你玩一把。",
                            "intent": "amend",
                            "continuation": "amend",
                            "work": {
                                "work_item_id": str(created_work["work_item_id"]),
                            },
                            "host_outcome_requirement": {
                                "operation": "prepare",
                                "facet": "auip.application",
                                "expected": {"current_attempt_contribution": True},
                            },
                            "provider_manifest": CODEX_APP_SERVER_MANIFEST.to_dict(),
                        },
                    )
                )
                prepared_work = prepared.metadata["work"]
                assert prepared_work["work_item_id"] == created_work["work_item_id"]
                assert prepared_work["attempt_id"] != created_work["attempt_id"]
                plan = prepared.metadata["export_plan"]
                assert plan["entry_filename"] == "gomoku.html"
                stage = Path(plan["staging_root"])
                assert stage.name == prepared_work["attempt_id"]
                assert (stage / "gomoku.html").read_bytes() == original_bytes

                _write_staged_auip_bundle(
                    stage,
                    entry="gomoku.html",
                    manifest={
                        "schema": "amadeus.auip/v0",
                        "app": {
                            "id": "progressive-gomoku",
                            "title": "Progressive Gomoku",
                            "version": "0.1.0",
                            "interactionSummary": (
                                "The participant can place one legal stone. "
                                "For example, 'take the center' selects that cell when legal."
                            ),
                        },
                        "events": {"game.changed": {"beat": True}},
                        "situationKinds": ["grid/v1"],
                        "actions": {
                            "game.place_stone": {
                                "description": (
                                    "Place one stone only when state.turn belongs to "
                                    "the participant and "
                                    "state.board.rows[payload.y][payload.x] is empty."
                                ),
                                "risk": "local_execution",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "x": {
                                            "type": "integer",
                                            "minimum": 0,
                                            "maximum": 14,
                                        },
                                        "y": {
                                            "type": "integer",
                                            "minimum": 0,
                                            "maximum": 14,
                                        },
                                    },
                                    "required": ["x", "y"],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        "stances": ["spectator", "participant"],
                    },
                )
                await _bind_run(coordinator, prepared, "codex-auip-progressive")
                await _finish_run(coordinator, prepared, "codex-auip-progressive")

                assert discover_registered_auip_app(
                    store,
                    str(created_work["work_item_id"]),
                ) is None
                attempt = store.get_attempt(str(prepared_work["attempt_id"]))
                assert attempt is not None
                assert attempt.metadata["outcome_verdict"]["verified"] is True
                bundle_permissions = store.list_permission_requests(
                    str(created_work["work_item_id"]),
                    attempt_id=str(prepared_work["attempt_id"]),
                )
                assert len(bundle_permissions) == 1
                assert sorted(
                    entry["relative_path"]
                    for entry in bundle_permissions[0].metadata["entries"]
                ) == [
                    "gomoku/auip.manifest.json",
                    "gomoku/gomoku.html",
                    "gomoku/sdk/auip-core/controller-v0.js",
                    "gomoku/sdk/auip-core/managed-v0.js",
                    "gomoku/sdk/auip-core/situations-v0.js",
                    "gomoku/sdk/auip-web/auip-v0.js",
                ]
                resolution = coordinator.export_service.resolve(
                    bundle_permissions[0].request_id,
                    allow=True,
                )
                assert resolution.permission.status == "allowed"
                app = discover_launchable_auip_app(
                    store,
                    str(created_work["work_item_id"]),
                )
                assert app is not None
                assert app["attempt_id"] == prepared_work["attempt_id"]
                assert Path(app["entry_path"]) == desktop / "gomoku" / "gomoku.html"

    asyncio.run(scenario())


def test_invalid_host_managed_auip_bundle_cannot_reach_export_permission() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="work_coordinator_auip_invalid_") as temp:
            root = Path(temp)
            workspace = root / "project"
            desktop = root / "Desktop"
            workspace.mkdir()
            desktop.mkdir()
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(
                    store,
                    export_service=WorkExportService(store, desktop_path=desktop),
                )
                request = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="codex",
                        task="Prepare the interactive app for AUIP.",
                        cwd=str(workspace),
                        mode="agent",
                        metadata={
                            "source": "auip_prepare",
                            "external_export": {
                                "target": "desktop",
                                "filename": "index.html",
                            },
                            "host_outcome_requirement": {
                                "operation": "prepare",
                                "facet": "auip.application",
                                "expected": {"current_attempt_contribution": True},
                            },
                            "provider_manifest": CODEX_APP_SERVER_MANIFEST.to_dict(),
                        },
                    )
                )
                work = request.metadata["work"]
                stage = Path(request.metadata["export_plan"]["staging_root"])
                manifest = {
                    "schema": "amadeus.auip/v0",
                    "app": {"id": "invalid-app", "title": "Invalid", "version": "0.1.0"},
                    "events": {"app.ready": {"beat": True}},
                    "actions": {},
                    "stances": ["spectator"],
                }
                (stage / "auip.manifest.json").write_text(
                    json.dumps(manifest),
                    encoding="utf-8",
                )
                # It names the Web asset but omits the embedded manifest and
                # Managed Core, so no user approval may be requested.
                (stage / "index.html").write_text(
                    "<!doctype html><script src='./auip-v0.js'></script>",
                    encoding="utf-8",
                )

                await _bind_run(coordinator, request, "codex-invalid-auip")
                await _finish_run(coordinator, request, "codex-invalid-auip")

                attempt = store.get_attempt(str(work["attempt_id"]))
                assert attempt is not None
                validation = attempt.metadata["host_auip_bundle_validation"]
                assert validation["verified"] is False
                assert validation["code"] == "embedded_manifest_missing"
                assert attempt.metadata["outcome_verdict"]["verified"] is False
                assert store.list_permission_requests(
                    str(work["work_item_id"]),
                    attempt_id=str(work["attempt_id"]),
                ) == []

    asyncio.run(scenario())


def test_staged_export_cannot_mint_auip_capability_without_host_requirement() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="work_coordinator_non_auip_staging_") as temp:
            root = Path(temp)
            workspace = root / "project"
            desktop = root / "Desktop"
            workspace.mkdir()
            desktop.mkdir()
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(
                    store,
                    export_service=WorkExportService(store, desktop_path=desktop),
                )
                request = coordinator.prepare_request(
                    ProviderRunRequest(
                        provider="codex",
                        task="Export gomoku.html to Desktop.",
                        cwd=str(workspace),
                        mode="agent",
                        metadata={
                            "source": "test",
                            "session_id": "session-plain-export",
                            "external_export": {
                                "target": "desktop",
                                "filename": "gomoku.html",
                            },
                            "provider_manifest": CODEX_APP_SERVER_MANIFEST.to_dict(),
                        },
                    )
                )
                work = request.metadata["work"]
                stage = Path(request.metadata["export_plan"]["staging_root"])
                (stage / "gomoku.html").write_text("<!doctype html>", encoding="utf-8")
                (stage / "auip.manifest.json").write_text(
                    json.dumps(
                        {
                            "schema": "amadeus.auip/v0",
                            "app": {
                                "id": "unrequested",
                                "title": "Unrequested",
                                "version": "0.1.0",
                            },
                            "events": {},
                            "actions": {},
                            "stances": ["spectator"],
                        }
                    ),
                    encoding="utf-8",
                )

                await _bind_run(coordinator, request, "codex-plain-staged")
                await _finish_run(coordinator, request, "codex-plain-staged")

                assert discover_registered_auip_app(
                    store,
                    str(work["work_item_id"]),
                ) is None
                assert not any(
                    artifact.kind == "business.file"
                    and artifact.metadata.get("attribution")
                    == "host_outcome:auip.application"
                    for artifact in store.list_artifacts(str(work["work_item_id"]))
                )

    asyncio.run(scenario())


def _write_staged_auip_bundle(
    stage: Path,
    *,
    entry: str,
    manifest: dict,
) -> None:
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2)
    (stage / "auip.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    (stage / entry).write_text(
        "<!doctype html>\n"
        '<script id="auip-manifest" type="application/json">\n'
        f"{rendered}\n"
        "</script>\n"
        '<script src="./sdk/auip-core/managed-v0.js"></script>\n'
        '<script src="./sdk/auip-core/situations-v0.js"></script>\n'
        '<script src="./sdk/auip-web/auip-v0.js"></script>\n',
        encoding="utf-8",
    )


async def _bind_run(
    coordinator: WorkLedgerCoordinator,
    request: ProviderRunRequest,
    run_id: str,
) -> None:
    await coordinator._on_provider_event(
        Method.PROVIDER_EVENT,
        {
            "provider": request.provider,
            "run_id": run_id,
            "type": "run.created",
            "payload": {"task": request.task, "cwd": request.cwd, "mode": request.mode},
            "metadata": request.metadata,
        },
    )


async def _finish_run(
    coordinator: WorkLedgerCoordinator,
    request: ProviderRunRequest,
    run_id: str,
    *,
    result: str = "Finished the requested work.",
    metadata: dict | None = None,
    status: str = "done",
) -> None:
    await coordinator._on_provider_result(
        Method.PROVIDER_RESULT,
        {
            "provider": request.provider,
            "run_id": run_id,
            "status": status,
            "result": result,
            "error": "",
            "metadata": dict(metadata if metadata is not None else request.metadata),
        },
    )


def test_new_requests_create_distinct_work_items_in_one_reusable_project() -> None:
    with tempfile.TemporaryDirectory(prefix="work_coordinator_prepare_") as temp:
        root = Path(temp)
        project_root = root / "project"
        alternate_workspace = root / "project-worktree"
        project_root.mkdir()
        alternate_workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            first_request, first_item_id, first_attempt_id = _prepare(
                coordinator,
                cwd=project_root,
                task="First semantic instruction",
                mode="plan",
            )
            first_work = first_request.metadata["work"]
            first_project_id = str(first_work["project_id"])
            assert first_work["attempt_number"] == 1
            assert store.get_work_item(first_item_id) is not None
            assert store.get_attempt(first_attempt_id).attempt_number == 1  # type: ignore[union-attr]

            second_request, second_item_id, second_attempt_id = _prepare(
                coordinator,
                cwd=project_root,
                task="Second semantic instruction in the same cwd",
                mode="plan",
            )
            second_work = second_request.metadata["work"]
            assert second_item_id != first_item_id
            assert second_attempt_id != first_attempt_id
            assert second_work["attempt_number"] == 1
            assert second_work["project_id"] == first_project_id
            assert store.get_attempt(second_attempt_id).attempt_number == 1  # type: ignore[union-attr]

            third_request, third_item_id, third_attempt_id = _prepare(
                coordinator,
                cwd=alternate_workspace,
                task="New task in an explicit workspace of the same project",
                mode="plan",
                project_id=first_project_id,
            )
            third_work = third_request.metadata["work"]
            assert third_item_id not in {first_item_id, second_item_id}
            assert third_work["project_id"] == first_project_id
            assert Path(third_work["workspace_path"]) == alternate_workspace.resolve()
            assert store.get_attempt(third_attempt_id).attempt_number == 1  # type: ignore[union-attr]
            assert len(store.list_work_items(project_id=first_project_id)) == 3
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                # Several past workspaces in one project is not an ambiguity:
                # new work goes to the project root. Asking the model to pick
                # between them required it to transcribe a workspace_ref, which
                # it measurably does not do, so the question had no answer.
                new_work = coordinator.resolve_workspace_route(
                    {"project_id": first_project_id}
                )
                assert new_work["status"] == "resolved"
                assert Path(new_work["cwd"]) == project_root.resolve()
                # A past workspace is still reachable -- but only because the
                # host resolved which task was meant and injected its ref.
                explicit = coordinator.resolve_workspace_route(
                    {
                        "project_id": first_project_id,
                        "workspace_ref": third_item_id,
                    }
                )
                assert explicit["status"] == "resolved"
                assert Path(explicit["cwd"]) == alternate_workspace.resolve()


def test_project_route_rejects_a_foreign_explicit_workspace() -> None:
    with tempfile.TemporaryDirectory(prefix="work_coordinator_project_binding_") as temp:
        root = Path(temp)
        workspace_a = root / "project-a"
        worktree_a = root / "project-a-worktree"
        workspace_b = root / "project-b"
        workspace_a.mkdir()
        worktree_a.mkdir()
        workspace_b.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            request_a, _, _ = _prepare(
                coordinator,
                cwd=workspace_a,
                task="Create project A",
                mode="plan",
            )
            project_a = str(request_a.metadata["work"]["project_id"])
            _, worktree_item_a, _ = _prepare(
                coordinator,
                cwd=worktree_a,
                task="Register project A worktree",
                mode="plan",
                project_id=project_a,
            )
            request_b, item_b, _ = _prepare(
                coordinator,
                cwd=workspace_b,
                task="Create project B",
                mode="plan",
            )
            project_b = str(request_b.metadata["work"]["project_id"])
            assert project_b != project_a

            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                known_workspace = coordinator.resolve_workspace_route(
                    {"project_id": project_a, "cwd": str(worktree_a)}
                )
                assert known_workspace["status"] == "resolved"
                assert known_workspace["projectId"] == project_a
                assert Path(known_workspace["cwd"]) == worktree_a.resolve()

                exact_ref = coordinator.resolve_workspace_route(
                    {"project_id": project_a, "workspace_ref": worktree_item_a}
                )
                assert exact_ref["status"] == "resolved"
                assert exact_ref["source"] == "intent_workspace_ref"
                assert Path(exact_ref["cwd"]) == worktree_a.resolve()

                foreign_workspace = coordinator.resolve_workspace_route(
                    {"project_id": project_a, "cwd": str(workspace_b)}
                )
                assert foreign_workspace == {
                    "status": "invalid",
                    "reason": "workspace_project_mismatch",
                    "projectId": project_a,
                    "cwd": "",
                    "source": "intent_project",
                }

                foreign_ref = coordinator.resolve_workspace_route(
                    {"project_id": project_a, "workspace_ref": item_b}
                )
                assert foreign_ref["status"] == "invalid"
                assert foreign_ref["reason"] == "workspace_project_mismatch"

                try:
                    coordinator.prepare_request(
                        ProviderRunRequest(
                            provider="codex",
                            task="Do not cross project boundaries",
                            cwd=str(workspace_b),
                            mode="plan",
                            metadata={
                                "project_id": project_a,
                                "provider_manifest": CODEX_APP_SERVER_MANIFEST.to_dict(),
                            },
                        )
                    )
                except WorkLedgerConflict as exc:
                    assert "workspace_project_mismatch" in str(exc)
                else:
                    raise AssertionError("Codex intake must reject Project A + workspace B")


def test_existing_work_item_rejects_continue_but_explicit_retry_is_new_attempt() -> None:
    with tempfile.TemporaryDirectory(prefix="work_coordinator_retry_only_") as temp:
        root = Path(temp)
        cwd = root / "project"
        cwd.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            _, work_item_id, first_attempt_id = _prepare(
                coordinator,
                cwd=cwd,
                task="Retryable task",
                mode="plan",
            )
            store.update_attempt(first_attempt_id, execution_status="failed")

            try:
                _prepare(
                    coordinator,
                    cwd=cwd,
                    task="Continue the old task",
                    mode="plan",
                    work_item_id=work_item_id,
                )
            except WorkLedgerConflict as exc:
                assert "require explicit amend, Retry" in str(exc)
            else:
                raise AssertionError("existing WorkItems must reject a new/continue intake")
            assert [attempt.attempt_number for attempt in store.list_attempts(work_item_id)] == [1]

            retried, retried_item_id, retry_attempt_id = _prepare(
                coordinator,
                cwd=cwd,
                task="Retryable task",
                mode="plan",
                work_item_id=work_item_id,
                continuation="retry",
            )
            assert retried_item_id == work_item_id
            assert retried.metadata["work"]["attempt_number"] == 2
            assert store.get_attempt(retry_attempt_id).attempt_number == 2  # type: ignore[union-attr]


def test_selection_is_view_only_and_explicit_workspace_pin_controls_routing() -> None:
    with tempfile.TemporaryDirectory(prefix="work_coordinator_workspace_route_") as temp:
        root = Path(temp)
        workspace_a = root / "project-a"
        workspace_b = root / "project-b"
        workspace_a.mkdir()
        workspace_b.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            _, item_a, _ = _prepare(
                coordinator,
                cwd=workspace_a,
                task="Task A",
                mode="plan",
            )
            _, item_b, _ = _prepare(
                coordinator,
                cwd=workspace_b,
                task="Task B",
                mode="plan",
            )

            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                coordinator.select(item_a, surface=DEFAULT_WORK_SURFACE)
                assert coordinator.workspace_routing_focus()["mode"] == "auto"
                selected_b = coordinator.resolve_workspace_route({"cwd": str(workspace_b)})
                assert selected_b["status"] == "resolved"
                assert selected_b["source"] == "intent_cwd"
                assert Path(selected_b["cwd"]) == workspace_b.resolve()

                pinned = coordinator.set_focus(
                    mode="pinned",
                    work_item_id=item_a,
                    surface=DEFAULT_WORK_SURFACE,
                )
                assert pinned["focusMode"] == "auto"
                assert pinned["selectedWorkItemId"] == item_a
                assert pinned["workspaceFocusMode"] == "pinned"
                assert pinned["workspaceFocusWorkItemId"] == item_a
                routed_while_pinned = coordinator.resolve_workspace_route(
                    {"cwd": str(workspace_b)}
                )
                assert routed_while_pinned["source"] == "workspace_pin"
                assert Path(routed_while_pinned["cwd"]) == workspace_a.resolve()

                coordinator.select(item_b, surface=DEFAULT_WORK_SURFACE)
                assert coordinator.workspace_routing_focus()["workItemId"] == item_a

                unlocked = coordinator.set_focus(
                    mode="auto",
                    work_item_id="",
                    surface=DEFAULT_WORK_SURFACE,
                )
                assert unlocked["focusMode"] == "auto"
                assert unlocked["selectedWorkItemId"] == item_b
                assert unlocked["workspaceFocusMode"] == "auto"
                routed_after_unlock = coordinator.resolve_workspace_route(
                    {"cwd": str(workspace_b)}
                )
                assert routed_after_unlock["source"] == "intent_cwd"
                assert Path(routed_after_unlock["cwd"]) == workspace_b.resolve()

                store.set_work_item_state(item_b, "archived")
                archived_view = coordinator.select(
                    item_b,
                    surface=DEFAULT_WORK_SURFACE,
                )
                assert archived_view["selectedWorkItemId"] == item_b
                assert archived_view["selected"]["state"] == "archived"


def test_single_writer_blocks_a_new_item_but_plan_mode_can_share_cwd() -> None:
    with tempfile.TemporaryDirectory(prefix="work_coordinator_writer_") as temp:
        root = Path(temp)
        cwd = root / "project"
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            _prepare(coordinator, cwd=cwd, task="Writer A", mode="agent")
            try:
                _prepare(coordinator, cwd=cwd, task="Writer B", mode="agent")
            except WorkLedgerConflict as exc:
                assert "active writer" in str(exc)
            else:
                raise AssertionError("a second local writer must be rejected")

            _, plan_item_id, _ = _prepare(
                coordinator,
                cwd=cwd,
                task="Read-only plan B",
                mode="plan",
            )
            assert store.get_work_item(plan_item_id) is not None
            assert len(store.list_work_items()) == 2


def test_done_is_review_not_accept_and_permission_text_stays_partial() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_coordinator_completion_") as temp:
            root = Path(temp)
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store)
                first, first_item_id, _ = _prepare(
                    coordinator,
                    cwd=root / "project-a",
                    task="Finish ordinary task",
                )
                await _bind_run(coordinator, first, "fake_run_done")
                await _finish_run(coordinator, first, "fake_run_done")
                first_item = store.get_work_item(first_item_id)
                first_assessment = store.latest_completion(first_item_id)
                assert first_item is not None and first_item.state == "review_ready"
                assert first_item.state != "accepted"
                assert first_assessment is not None
                assert first_assessment.execution_status == "succeeded"
                assert first_assessment.completeness == "partial"
                assert first_assessment.attention == "review"

                permission, permission_item_id, _ = _prepare(
                    coordinator,
                    cwd=root / "project-b",
                    task="Export a generated file",
                )
                await _bind_run(coordinator, permission, "fake_run_permission")
                await _finish_run(
                    coordinator,
                    permission,
                    "fake_run_permission",
                    result="Please approve saving this file. Once approved, I can finish the export.",
                )
                permission_item = store.get_work_item(permission_item_id)
                permission_assessment = store.latest_completion(permission_item_id)
                assert permission_item is not None and permission_item.state == "open"
                assert permission_assessment is not None
                assert permission_assessment.completeness == "partial"
                assert permission_assessment.attention == "permission"

    asyncio.run(run())


def test_explicit_and_external_artifacts_are_registered() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_coordinator_artifact_") as temp:
            root = Path(temp)
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store)
                request, item_id, attempt_id = _prepare(
                    coordinator,
                    cwd=root / "project",
                    task="Generate artifacts",
                )
                await _bind_run(coordinator, request, "fake_run_artifact")
                await coordinator._on_provider_event(
                    Method.PROVIDER_EVENT,
                    {
                        "provider": "fake",
                        "run_id": "fake_run_artifact",
                        "type": "artifact.created",
                        "payload": {
                            "artifact_type": "markdown.report",
                            "title": "Run report",
                            "uri": "work-ledger://report/fake_run_artifact",
                        },
                    },
                )
                external_path = root / "Desktop" / "chess_game.py"
                external_path.parent.mkdir(parents=True, exist_ok=True)
                external_path.write_text("print('chess')\n", encoding="utf-8")
                await coordinator._on_provider_event(
                    Method.PROVIDER_EVENT,
                    {
                        "provider": "fake",
                        "run_id": "fake_run_artifact",
                        "type": "artifact.created",
                        "payload": {
                            "artifact_type": "file",
                            "title": "Chess game",
                            "path": str(external_path),
                        },
                    },
                )
                artifacts = store.list_artifacts(item_id, attempt_id=attempt_id)
                assert len(artifacts) == 2
                report = next(item for item in artifacts if item.kind == "markdown.report")
                external = next(item for item in artifacts if item.kind == "file")
                assert report.location == "virtual" and report.status == "registered"
                assert external.location == "external" and external.status == "pending"
                assert Path(external.path) == external_path.resolve()

    asyncio.run(run())


def test_workspace_pin_keeps_visual_focus_auto_and_presentations_restore() -> None:
    with tempfile.TemporaryDirectory(prefix="work_coordinator_focus_") as temp:
        root = Path(temp)
        db_path = root / "ledger.sqlite3"
        store = WorkLedgerStore(db_path)
        coordinator = WorkLedgerCoordinator(store)
        (root / "project-a").mkdir()
        (root / "project-b").mkdir()
        _, item_a, attempt_a = _prepare(
            coordinator,
            cwd=root / "project-a",
            task="Task A",
            mode="plan",
        )
        _, item_b, attempt_b = _prepare(
            coordinator,
            cwd=root / "project-b",
            task="Task B",
            mode="plan",
        )
        auto = coordinator.snapshot(surface=DEFAULT_WORK_SURFACE)
        assert auto["focusMode"] == "auto"
        assert auto["selectedWorkItemId"] == item_b

        with patch(
            "server.work_ledger_coordinator.cwd_in_project_registry",
            return_value=True,
        ):
            pinned = coordinator.set_focus(
                mode="pinned",
                work_item_id=item_a,
                surface=DEFAULT_WORK_SURFACE,
            )
        assert pinned["selectedWorkItemId"] == item_a
        assert pinned["focusMode"] == "auto"
        assert pinned["workspaceFocusMode"] == "pinned"
        assert pinned["workspaceFocusWorkItemId"] == item_a

        visible_a = coordinator.project_canvas(
            {
                "mode": "markdown",
                "title": "Canvas A",
                "markdown": "A presentation",
                "workContext": {"workItemId": item_a, "attemptId": attempt_a},
            }
        )
        assert visible_a["title"] == "Canvas A"
        projected_b = coordinator.project_canvas(
            {
                "mode": "diff",
                "title": "Canvas B",
                "diff": {"files": []},
                "workContext": {"workItemId": item_b, "attemptId": attempt_b},
            }
        )
        assert projected_b["title"] == "Canvas A"
        assert projected_b["workContext"]["workItemId"] == item_a
        saved_b = store.get_work_item(item_b)
        assert saved_b is not None
        assert saved_b.metadata["presentation"]["title"] == "Canvas B"
        coordinator.close()

        reopened_store = WorkLedgerStore(db_path)
        reopened = WorkLedgerCoordinator(reopened_store)
        restored_focus = reopened_store.get_focus(DEFAULT_WORK_SURFACE)
        restored_workspace_focus = reopened_store.get_focus(WORKSPACE_ROUTING_SURFACE)
        restored_canvas = reopened.selected_canvas(surface=DEFAULT_WORK_SURFACE)
        assert restored_focus is not None and restored_focus.mode == "auto"
        assert restored_focus.work_item_id == item_a
        assert restored_workspace_focus is not None and restored_workspace_focus.mode == "pinned"
        assert restored_workspace_focus.work_item_id == item_a
        assert restored_canvas is not None and restored_canvas["title"] == "Canvas A"
        assert reopened_store.get_work_item(item_b).metadata["presentation"]["title"] == "Canvas B"  # type: ignore[union-attr]
        reopened.close()


def test_write_tool_path_hint_registers_external_output_as_conflict() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_coordinator_tool_artifact_") as temp:
            root = Path(temp)
            (root / "project").mkdir()
            external = root / "Desktop" / "chess_game.py"
            external.parent.mkdir()
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store)
                request, item_id, attempt_id = _prepare(
                    coordinator,
                    cwd=root / "project",
                    task="Write a chess game",
                )
                await _bind_run(coordinator, request, "fake_tool_artifact")
                await coordinator._on_provider_event(
                    Method.PROVIDER_EVENT,
                    {
                        "provider": "fake",
                        "run_id": "fake_tool_artifact",
                        "type": "tool.call",
                        "payload": {
                            "tool": "Write",
                            "raw": {"input": {"file_path": str(external)}},
                        },
                    },
                )
                external.write_text("print('chess')\n", encoding="utf-8")
                await _finish_run(coordinator, request, "fake_tool_artifact")

                artifacts = store.list_artifacts(item_id, attempt_id=attempt_id)
                output = next(artifact for artifact in artifacts if artifact.kind == "tool.output")
                assert output.location == "external"
                assert output.status == "pending"
                assessment = store.latest_completion(item_id)
                assert assessment is not None
                assert assessment.completeness == "partial"
                assert assessment.attention == "conflict"

    asyncio.run(run())


def test_runtime_recovery_is_idempotent_and_orphan_needs_attention() -> None:
    with tempfile.TemporaryDirectory(prefix="work_coordinator_recovery_") as temp:
        root = Path(temp)
        (root / "project").mkdir()
        records = [
            {
                "provider": "codex",
                "run_id": "recovered_done",
                "cwd": str(root / "project"),
                "status": "done",
            },
            {
                "provider": "codex",
                "run_id": "recovered_orphan",
                "cwd": str(root / "project"),
                "status": "orphaned",
            },
            {
                "provider": "codex",
                "run_id": "recovered_interrupted",
                "cwd": str(root / "project"),
                "status": "interrupted",
            },
        ]
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            coordinator.adopt_runtime_records(records)
            coordinator.adopt_runtime_records(records)
            assert len(store.list_work_items()) == 3
            done_attempt = store.get_attempt_by_provider_run("recovered_done")
            orphan_attempt = store.get_attempt_by_provider_run("recovered_orphan")
            interrupted_attempt = store.get_attempt_by_provider_run(
                "recovered_interrupted"
            )
            assert done_attempt is not None and orphan_attempt is not None
            assert interrupted_attempt is not None
            assert store.get_work_item(done_attempt.work_item_id).state != "archived"  # type: ignore[union-attr]
            orphan_projection = coordinator.detail(orphan_attempt.work_item_id)
            assert orphan_projection["execution"] == "orphaned"
            assert orphan_projection["attention"] == "error"
            interrupted_projection = coordinator.detail(interrupted_attempt.work_item_id)
            assert interrupted_projection["execution"] == "failed"
            assert interrupted_projection["canResume"] is False
            project = store.get_project(store.get_work_item(orphan_attempt.work_item_id).project_id)  # type: ignore[union-attr]
            assert project is not None and project.state == "active"


def test_restart_reconciles_bound_running_attempt_to_resumable_orphan() -> None:
    with tempfile.TemporaryDirectory(prefix="work_coordinator_restart_orphan_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            _, item_id, attempt_id = _prepare(
                coordinator,
                cwd=workspace,
                task="Resume this interrupted write",
            )
            store.bind_provider_run(attempt_id, "codex_restart_orphan")
            store.update_attempt(attempt_id, execution_status="running")
            assert store.get_writer_lease(attempt_id).status == "active"  # type: ignore[union-attr]

            coordinator.adopt_runtime_records(
                [
                    {
                        "provider": "codex",
                        "run_id": "codex_restart_orphan",
                        "cwd": str(workspace),
                        "status": "orphaned",
                    },
                ]
            )

            attempt = store.get_attempt(attempt_id)
            assert attempt is not None and attempt.execution_status == "orphaned"
            assert attempt.metadata["runtime_resumable"] is True
            assert store.get_writer_lease(attempt_id).status == "stale"  # type: ignore[union-attr]
            projection = coordinator.detail(item_id)
            assert projection["execution"] == "orphaned"
            assert projection["canResume"] is True


def test_restart_terminal_journal_closes_attempt_and_releases_writer() -> None:
    with tempfile.TemporaryDirectory(prefix="work_coordinator_terminal_restart_") as temp:
        root = Path(temp)
        workspace = root / "project"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            _, item_id, attempt_id = _prepare(
                coordinator,
                cwd=workspace,
                task="Finish across the result persistence crash window",
            )
            store.bind_provider_run(attempt_id, "codex_restart_done")
            store.update_attempt(attempt_id, execution_status="running")

            coordinator.adopt_runtime_records(
                [
                    {
                        "provider": "codex",
                        "run_id": "codex_restart_done",
                        "cwd": str(workspace),
                        "status": "done",
                        "result": "Provider completed before host restart.",
                    }
                ]
            )

            attempt = store.get_attempt(attempt_id)
            assert attempt is not None and attempt.execution_status == "succeeded"
            assert attempt.result == "Provider completed before host restart."
            assert store.get_writer_lease(attempt_id).status == "released"  # type: ignore[union-attr]
            item = store.get_work_item(item_id)
            assert item is not None and item.state != "archived"


def test_structured_task_completion_precedes_legacy_prose_fallback() -> None:
    blockers = WorkLedgerCoordinator._compatibility_blockers(
        "Everything completed successfully.",
        {
            "task_completion": {
                "runtimeStatus": "completed",
                "taskOutcome": "needs_input",
                "reportSource": "provider",
                "summary": "A user choice is required.",
                "blocker": "Choose a migration strategy.",
            }
        },
    )

    assert blockers["source"] == "task_completion"
    assert blockers["task_outcome"] == "needs_input"
    assert blockers["pending_inputs"] == 1
    assert blockers["missing_requirements"] == []


def _main() -> None:
    test_chat_turn_identity_survives_work_intake()
    test_explicit_amendment_supersedes_only_the_obsolete_pending_export()
    test_explicit_amendment_does_not_bypass_an_execution_permission()
    test_code_provider_receives_attempt_local_auip_authoring_inputs()
    test_ordinary_code_work_does_not_receive_auip_authoring_inputs()
    test_host_outcome_contract_blocks_false_auip_completion()
    test_host_outcome_contract_accepts_registered_auip_revision()
    test_auip_outcome_materializes_and_permissions_one_complete_desktop_bundle()
    test_progressive_auip_prepare_uses_current_attempt_despite_role_rename()
    test_staged_export_cannot_mint_auip_capability_without_host_requirement()
    test_new_requests_create_distinct_work_items_in_one_reusable_project()
    test_project_route_rejects_a_foreign_explicit_workspace()
    test_existing_work_item_rejects_continue_but_explicit_retry_is_new_attempt()
    test_selection_is_view_only_and_explicit_workspace_pin_controls_routing()
    test_single_writer_blocks_a_new_item_but_plan_mode_can_share_cwd()
    test_done_is_review_not_accept_and_permission_text_stays_partial()
    test_explicit_and_external_artifacts_are_registered()
    test_workspace_pin_keeps_visual_focus_auto_and_presentations_restore()
    test_write_tool_path_hint_registers_external_output_as_conflict()
    test_runtime_recovery_is_idempotent_and_orphan_needs_attention()
    test_restart_reconciles_bound_running_attempt_to_resumable_orphan()
    test_restart_terminal_journal_closes_attempt_and_releases_writer()
    test_structured_task_completion_precedes_legacy_prose_fallback()
    print("ok: work ledger coordinator keeps task identity, evidence, writer, and focus boundaries")


if __name__ == "__main__":
    _main()
