"""Semantic contract for AUIP capability discovery and experience launch."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from agent_host.work_ledger_store import WorkLedgerStore
from core.chat_runtime import ChatRuntime, _TurnState
from server.attention_request import AttentionRequestCoordinator
from server.auip_app_source import (
    discover_launchable_auip_app,
    discover_registered_auip_app,
)
from server.auip_contract import AUIP_SCHEMA, AuipProtocolError
from server.auip_control_decision import (
    AuipControlDecisionResolver,
    render_auip_role_grounding,
)
from server.auip_launch import AuipLaunchCoordinator
from server.auip_runtime import AuipRuntime
from server.capability_composition import auip_app_capability_packages
from server.handlers.auip_handler import AuipHandler
from server.protocol import Method
from server.work_export_service import WorkExportService
from server.work_ledger_coordinator import WorkLedgerCoordinator
from core.chat_control_envelope import parse_inline_control_chunk
from llm.stream_parser import StreamTagParser


SESSION = "chat-auip-launch"


class _NoActiveApp:
    def focused_projection(self, _session_id: str):
        return None


def _manifest(title: str) -> dict[str, Any]:
    return {
        "schema": AUIP_SCHEMA,
        "app": {"id": title.lower().replace(" ", "-"), "title": title, "version": "0.1.0"},
        "events": {"game.changed": {"beat": True}},
        "actions": {
            "game.move": {
                "description": "Make one declared move.",
                "risk": "local_execution",
            }
        },
        "stances": ["spectator", "participant"],
    }


def _register_file(store: WorkLedgerStore, item, attempt, path: Path) -> Any:
    return store.register_artifact(
        item.work_item_id,
        attempt_id=attempt.attempt_id,
        kind="business.file",
        title=path.name,
        path=path,
        status="registered",
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _seed_app(
    store: WorkLedgerStore,
    project,
    root: Path,
    *,
    title: str,
    turn_id: str,
    terminal: bool = True,
    with_manifest: bool = True,
) -> tuple[Any, Any, Any]:
    workspace = root / title.lower().replace(" ", "-")
    workspace.mkdir(parents=True)
    item = store.create_work_item(
        project.project_id,
        title=title,
        workspace_path=workspace,
    )
    attempt = store.create_attempt(
        item.work_item_id,
        provider="locus",
        task=f"Build {title}",
        metadata={"session_id": SESSION, "turn_id": turn_id},
    )
    entry = workspace / "index.html"
    entry.write_text("<!doctype html><title>game</title>", encoding="utf-8")
    entry_artifact = _register_file(store, item, attempt, entry)
    if with_manifest:
        manifest = workspace / "auip.manifest.json"
        manifest.write_text(json.dumps(_manifest(title)), encoding="utf-8")
        _register_file(store, item, attempt, manifest)
    if terminal:
        store.update_attempt(attempt.attempt_id, execution_status="succeeded")
    return item, store.get_attempt(attempt.attempt_id), entry_artifact


def test_generic_html_is_not_an_auip_application() -> None:
    with tempfile.TemporaryDirectory(prefix="auip_launch_generic_") as temp:
        root = Path(temp)
        store = WorkLedgerStore(root / "ledger.sqlite3")
        project = store.create_or_get_project(root / "project")
        item, _attempt, _entry = _seed_app(
            store,
            project,
            root,
            title="Plain Game",
            turn_id="turn-plain",
            with_manifest=False,
        )
        assert discover_registered_auip_app(store, item.work_item_id) is None
        coordinator = AuipLaunchCoordinator(
            artifacts=store,
            work_roster=WorkLedgerCoordinator(store),
            attention=AttentionRequestCoordinator(),
        )
        assert coordinator.candidates(SESSION) == []
        preparation = coordinator.preparation_candidates(SESSION)
        assert len(preparation) == 1
        assert preparation[0].work_item_id == item.work_item_id
        assert preparation[0].files == ("index.html",)
        prompt = coordinator.render_prompt_context(
            SESSION,
            language="en",
            include_control_contract=False,
        )
        assert "not proof that they are currently open" in prompt
        assert "authoring_needed_apps" not in prompt
        assert "Plain Game" not in prompt
        store.close()


def test_deleted_historical_index_does_not_hide_current_named_entry() -> None:
    with tempfile.TemporaryDirectory(prefix="auip_launch_renamed_entry_") as temp:
        root = Path(temp)
        store = WorkLedgerStore(root / "ledger.sqlite3")
        project = store.create_or_get_project(root / "project")
        item, first_attempt, old_entry = _seed_app(
            store,
            project,
            root,
            title="Renamed Game",
            turn_id="turn-original-entry",
        )
        Path(str(old_entry.path)).unlink()
        second_attempt = store.create_attempt(
            item.work_item_id,
            provider="codex",
            task="Rename the application entry.",
            metadata={"session_id": SESSION, "turn_id": "turn-renamed-entry"},
        )
        current_entry = Path(str(item.workspace_path)) / "signal_game.html"
        current_entry.write_text("<!doctype html><title>current</title>", encoding="utf-8")
        _register_file(store, item, second_attempt, current_entry)

        discovered = discover_registered_auip_app(store, item.work_item_id)

        assert discovered is not None
        assert Path(discovered["entry_path"]).name == "signal_game.html"
        assert discovered["contributing_attempt_ids"] == sorted(
            [first_attempt.attempt_id, second_attempt.attempt_id]
        )
        store.close()


def test_approved_desktop_html_is_a_preparable_delivery_not_a_launchable_app() -> None:
    """Production exports remain visible to AUIP without bypassing approval.

    Desktop delivery uses ``business.proposed_export`` plus an approved
    ``business.export`` rather than ``business.file``.  The approved row may
    make the owning WorkItem eligible for AUIP authoring, but neither row is a
    launch receipt and the pending proposal alone must never be enough.
    """

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="auip_launch_export_") as temp:
            root = Path(temp)
            store = WorkLedgerStore(root / "ledger.sqlite3")
            project = store.create_or_get_project(root / "project")
            workspace = root / "gomoku-workspace"
            workspace.mkdir()
            item = store.create_work_item(
                project.project_id,
                title="Gomoku",
                workspace_path=workspace,
            )
            attempt = store.create_attempt(
                item.work_item_id,
                provider="codex",
                task="Build a desktop Gomoku game",
                metadata={"session_id": SESSION, "turn_id": "turn-build"},
            )

            proposed = workspace / ".amadeus" / "proposed_exports" / "gobang.html"
            proposed.parent.mkdir(parents=True)
            proposed.write_text("<!doctype html><title>Gomoku</title>", encoding="utf-8")
            store.register_artifact(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                kind="business.proposed_export",
                title=proposed.name,
                path=proposed,
                status="registered",
                sha256=hashlib.sha256(proposed.read_bytes()).hexdigest(),
            )
            pending_only = AuipLaunchCoordinator(
                artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=AttentionRequestCoordinator(),
            )
            assert pending_only.preparation_candidates(SESSION) == []

            desktop = root / "desktop"
            desktop.mkdir()
            exported = desktop / "gobang.html"
            exported.write_bytes(proposed.read_bytes())
            store.register_artifact(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                kind="business.export",
                title=f"Exported {exported.name}",
                path=exported,
                location="external",
                status="approved",
                sha256=hashlib.sha256(exported.read_bytes()).hexdigest(),
            )
            store.update_attempt(attempt.attempt_id, execution_status="succeeded")

            prepared: list[tuple[str, str]] = []

            async def prepare_work(candidate, mode):
                prepared.append((candidate.work_item_id, mode))

            coordinator = AuipLaunchCoordinator(
                artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=AttentionRequestCoordinator(),
            )
            assert discover_registered_auip_app(store, item.work_item_id) is None
            assert coordinator.candidates(SESSION) == []
            preparation = coordinator.preparation_candidates(SESSION)
            assert len(preparation) == 1
            assert preparation[0].work_item_id == item.work_item_id
            assert preparation[0].files == ("gobang.html",)

            captured = []

            async def decide(messages):
                captured.extend(messages)
                return json.dumps(
                    {
                        "action": "engage",
                        "timing": "now",
                        "mode": "collaborate",
                        "target": "Gomoku",
                        "work_relation": "subsumed",
                    }
                )

            resolver = AuipControlDecisionResolver(
                query=decide,
                app_runtime=_NoActiveApp(),
                launch_catalog=coordinator,
            )
            pending = resolver.capture(
                session_id=SESSION,
                user_text="你能接入它吗，我想和你一起玩",
            )
            assert pending is not None
            decision = await pending
            assert decision.action == "prepare"
            assert decision.mode == "collaborate"
            assert decision.preparation_work_item_id == item.work_item_id
            assert "preparable_apps" in json.dumps(captured)
            grounding = render_auip_role_grounding(decision)
            assert "私には関われない" in grounding
            assert "requested_transition=prepare" in grounding

            prompt = coordinator.render_prompt_context(
                SESSION,
                language="en",
                include_control_contract=False,
            )
            assert "launchable_apps:\n- none" in prompt
            assert "cannot operate applications" in prompt

            routed = await coordinator.route_control(
                {
                    "action": "prepare",
                    "mode": "collaborate",
                    "_host_preparation_work_item_id": item.work_item_id,
                },
                session_id=SESSION,
                turn_id="turn-play-together",
                prepare_work=prepare_work,
            )
            assert routed["ok"] is True
            assert routed["preparing"] is True
            assert prepared == [(item.work_item_id, "collaborate")]
            store.close()

    asyncio.run(scenario())


def test_one_approved_auip_bundle_launches_without_duplicate_preparation() -> None:
    """The interaction runs against the exact user-approved Desktop revision."""

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="auip_launch_approved_bundle_") as temp:
            root = Path(temp)
            desktop = root / "Desktop"
            workspace = root / "workspace"
            desktop.mkdir()
            workspace.mkdir()
            store = WorkLedgerStore(root / "ledger.sqlite3")
            project = store.create_or_get_project(workspace)
            item = store.create_work_item(
                project.project_id,
                title="Gomoku",
                workspace_path=workspace,
            )
            attempt = store.create_attempt(
                item.work_item_id,
                provider="codex",
                task="Build an AUIP Gomoku game on Desktop",
                metadata={"session_id": SESSION, "turn_id": "turn-build-auip"},
            )
            export = WorkExportService(store, desktop_path=desktop)
            plan = export.prepare_plan(
                provider="codex",
                mode="agent",
                task=attempt.task,
                item=item,
                attempt=attempt,
                metadata={"external_export": {"target": "desktop"}},
            )
            assert plan is not None
            store.update_attempt(
                attempt.attempt_id,
                metadata={"export_plan": dict(plan)},
            )
            stage = Path(plan["staging_root"])
            (stage / "gomoku.html").write_text(
                "<!doctype html><script src='auip-v0.js'></script>",
                encoding="utf-8",
            )
            (stage / "auip-v0.js").write_text("window.AmadeusAUIP = {};", encoding="utf-8")
            (stage / "auip.manifest.json").write_text(
                json.dumps(_manifest("Gomoku")),
                encoding="utf-8",
            )
            permission = export.discover_staged_exports(attempt, item, plan)["permission"]
            assert permission is not None
            export.resolve(permission.request_id, allow=True)
            store.update_attempt(attempt.attempt_id, execution_status="succeeded")

            assert discover_registered_auip_app(store, item.work_item_id) is None
            approved = discover_launchable_auip_app(store, item.work_item_id)
            assert approved is not None
            assert approved["entry_path"] == str(desktop / "gomoku.html")

            emitted: list[tuple[str, dict[str, Any]]] = []

            async def emit(method: str, payload: dict[str, Any]) -> None:
                emitted.append((method, payload))

            coordinator = AuipLaunchCoordinator(
                artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=AttentionRequestCoordinator(),
                emit=emit,
            )
            assert coordinator.preparation_candidates(SESSION) == []
            candidates = coordinator.candidates(SESSION)
            assert len(candidates) == 1
            assert candidates[0].artifact_ref.startswith("export-bundle:")
            capability_packages = auip_app_capability_packages(candidates)
            assert len(capability_packages) == 1
            app_capability = capability_packages[0].contributions[0]
            assert app_capability.native_ref == candidates[0].artifact_ref
            assert app_capability.metadata["app_id"] == "gomoku"
            assert app_capability.metadata["modes"] == [
                "observe",
                "collaborate",
                "delegate",
            ]
            routed = await coordinator.route_control(
                {"action": "launch", "target": "Gomoku", "mode": "collaborate"},
                session_id=SESSION,
                turn_id="turn-open-approved",
            )
            assert routed["ok"] is True and routed["requested"] is True
            launch_payload = next(
                payload
                for method, payload in emitted
                if method == Method.AUIP_LAUNCH_REQUESTED
            )
            app_runtime = AuipRuntime()
            handler = AuipHandler(
                app_runtime,
                artifacts=store,
                current_session_id=lambda: SESSION,
                launch=coordinator,
            )
            prepared = await handler.handle(
                Method.AUIP_ATTACH_PREPARE,
                {
                    "artifact_id": launch_payload["artifact_id"],
                    "request_id": launch_payload["request_id"],
                    "mode": "collaborate",
                },
            )
            assert prepared is not None and prepared["ok"] is True
            assert prepared["entry_path"] == str(desktop / "gomoku.html")
            assert prepared["launch_url"].startswith("file:")
            store.close()

    asyncio.run(scenario())


def test_preparation_targets_the_existing_generic_work_and_reserves_launch() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="auip_launch_prepare_") as temp:
            root = Path(temp)
            store = WorkLedgerStore(root / "ledger.sqlite3")
            project = store.create_or_get_project(root / "project")
            item, _attempt, _entry = _seed_app(
                store,
                project,
                root,
                title="Plain Game",
                turn_id="turn-build",
                with_manifest=False,
            )
            prepared = []

            async def prepare_work(candidate, mode):
                prepared.append((candidate.work_item_id, mode))

            coordinator = AuipLaunchCoordinator(
                artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=AttentionRequestCoordinator(),
            )
            result = await coordinator.route_control(
                {
                    "action": "prepare",
                    "mode": "collaborate",
                    "_host_preparation_work_item_id": item.work_item_id,
                },
                session_id=SESSION,
                turn_id="turn-play",
                prepare_work=prepare_work,
            )
            assert result == {
                "ok": True,
                "deferred": True,
                "preparing": True,
                "turn_id": "turn-play",
            }
            assert prepared == [(item.work_item_id, "collaborate")]
            pending = coordinator._deferred[(SESSION, "turn-play")]
            assert pending.work_item_id == item.work_item_id
            store.close()

    asyncio.run(scenario())


def test_failed_preparation_uses_the_work_terminal_without_a_second_launch_report() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="auip_launch_prepare_failure_") as temp:
            root = Path(temp)
            store = WorkLedgerStore(root / "ledger.sqlite3")
            project = store.create_or_get_project(root / "project")
            item, _attempt, _entry = _seed_app(
                store,
                project,
                root,
                title="Plain Game",
                turn_id="turn-build",
                with_manifest=False,
            )
            emitted: list[tuple[str, dict[str, Any]]] = []

            async def emit(method: str, payload: dict[str, Any]) -> None:
                emitted.append((method, payload))

            async def prepare_work(candidate, _mode):
                failed = store.create_attempt(
                    candidate.work_item_id,
                    provider="locus",
                    task="Prepare AUIP",
                    metadata={"session_id": SESSION, "turn_id": "turn-play"},
                )
                store.update_attempt(failed.attempt_id, execution_status="failed")

            coordinator = AuipLaunchCoordinator(
                artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=AttentionRequestCoordinator(),
                emit=emit,
            )
            await coordinator.route_control(
                {
                    "action": "prepare",
                    "mode": "collaborate",
                    "_host_preparation_work_item_id": item.work_item_id,
                },
                session_id=SESSION,
                turn_id="turn-play",
                prepare_work=prepare_work,
            )
            await coordinator.on_work_updated(
                Method.WORK_UPDATED,
                {"reason": "provider.result"},
            )

            assert emitted == []
            assert (SESSION, "turn-play") not in coordinator._deferred
            store.close()

    asyncio.run(scenario())


def test_attach_discovery_logs_the_exact_host_bundle_error(caplog) -> None:
    with tempfile.TemporaryDirectory(prefix="auip_launch_attach_error_") as temp:
        root = Path(temp)
        store = WorkLedgerStore(root / "ledger.sqlite3")
        project = store.create_or_get_project(root / "project")
        item, _attempt, _entry = _seed_app(
            store,
            project,
            root,
            title="Verified Game",
            turn_id="turn-build",
        )
        with (
            patch(
                "server.auip_app_source._validate_host_managed_workspace_bundle",
                side_effect=AuipProtocolError(
                    "auip_runtime_asset_modified",
                    "sdk/auip-web/auip-v0.js",
                ),
            ),
            caplog.at_level(logging.WARNING, logger="server.auip_app_source"),
        ):
            assert discover_registered_auip_app(store, item.work_item_id) is None

        messages = [record.getMessage() for record in caplog.records]
        assert any("code=auip_runtime_asset_modified" in message for message in messages)
        assert any("sdk/auip-web/auip-v0.js" in message for message in messages)
        store.close()


def test_expired_deferred_launch_publishes_one_terminal_blocking_note() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="auip_launch_expiry_") as temp:
            root = Path(temp)
            store = WorkLedgerStore(root / "ledger.sqlite3")
            project = store.create_or_get_project(root / "project")
            item, _attempt, _entry = _seed_app(
                store,
                project,
                root,
                title="Plain Game",
                turn_id="turn-build",
                with_manifest=False,
            )
            emitted: list[tuple[str, dict[str, Any]]] = []
            now = [100.0]

            async def emit(method: str, payload: dict[str, Any]) -> None:
                emitted.append((method, payload))

            async def prepare_work(_candidate, _mode):
                return None

            coordinator = AuipLaunchCoordinator(
                artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=AttentionRequestCoordinator(),
                emit=emit,
                clock=lambda: now[0],
            )
            await coordinator.route_control(
                {
                    "action": "prepare",
                    "mode": "collaborate",
                    "_host_preparation_work_item_id": item.work_item_id,
                },
                session_id=SESSION,
                turn_id="turn-expiring-prepare",
                prepare_work=prepare_work,
            )
            assert coordinator._deferred
            now[0] += 30 * 60 + 1

            await coordinator.on_work_updated(
                Method.WORK_UPDATED,
                {"reason": "timer_probe"},
            )

            assert coordinator._deferred == {}
            assert len(emitted) == 1
            method, note = emitted[0]
            assert method == Method.CHAT_WORK_NOTE
            assert note["importance"] == "blocking"
            assert note["metadata"] == {
                "auip_launch_failed": True,
                "reason": "deferred_launch_expired",
                "execution_started": False,
            }
            assert note["signals"][0]["text"] == "No AppSession was started"
            store.close()

    asyncio.run(scenario())


def test_successful_preparation_waits_for_artifact_reconciliation_before_launch() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="auip_launch_artifact_race_") as temp:
            root = Path(temp)
            store = WorkLedgerStore(root / "ledger.sqlite3")
            project = store.create_or_get_project(root / "project")
            item, _seed_attempt, _entry = _seed_app(
                store,
                project,
                root,
                title="Plain Game",
                turn_id="turn-build",
                with_manifest=False,
            )
            emitted: list[tuple[str, dict[str, Any]]] = []
            amended: dict[str, Any] = {}

            async def emit(method: str, payload: dict[str, Any]) -> None:
                emitted.append((method, payload))

            async def prepare_work(candidate, _mode):
                attempt = store.create_attempt(
                    candidate.work_item_id,
                    provider="codex",
                    task="Prepare AUIP",
                    metadata={"session_id": SESSION, "turn_id": "turn-play"},
                )
                amended["attempt"] = attempt

            coordinator = AuipLaunchCoordinator(
                artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=AttentionRequestCoordinator(),
                emit=emit,
            )
            await coordinator.route_control(
                {
                    "action": "prepare",
                    "mode": "collaborate",
                    "_host_preparation_work_item_id": item.work_item_id,
                },
                session_id=SESSION,
                turn_id="turn-play",
                prepare_work=prepare_work,
            )
            attempt = amended["attempt"]
            store.update_attempt(attempt.attempt_id, execution_status="succeeded")

            # Provider terminal is visible before artifact reconciliation.
            await coordinator.on_work_updated(
                Method.WORK_UPDATED,
                {"reason": "provider.event:run.finished"},
            )
            assert emitted == []
            assert (SESSION, "turn-play") in coordinator._deferred

            workspace = Path(item.workspace_path)
            entry = workspace / "index.html"
            entry.write_text("<!doctype html><title>Prepared</title>", encoding="utf-8")
            manifest = workspace / "auip.manifest.json"
            manifest.write_text(json.dumps(_manifest("Prepared")), encoding="utf-8")
            _register_file(store, item, attempt, entry)
            _register_file(store, item, attempt, manifest)

            await coordinator.on_work_updated(
                Method.WORK_UPDATED,
                {"reason": "artifact.registered"},
            )
            assert len(emitted) == 1
            assert emitted[0][0] == Method.AUIP_LAUNCH_REQUESTED
            assert emitted[0][1]["title"] == "Prepared"
            assert coordinator._deferred == {}
            store.close()

    asyncio.run(scenario())


def test_rejected_host_outcome_retires_deferred_launch_without_second_error() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="auip_launch_outcome_rejected_") as temp:
            root = Path(temp)
            store = WorkLedgerStore(root / "ledger.sqlite3")
            project = store.create_or_get_project(root / "project")
            item, _seed_attempt, _entry = _seed_app(
                store,
                project,
                root,
                title="Plain Game",
                turn_id="turn-build",
                with_manifest=False,
            )
            emitted: list[tuple[str, dict[str, Any]]] = []
            prepared: dict[str, Any] = {}

            async def emit(method: str, payload: dict[str, Any]) -> None:
                emitted.append((method, payload))

            async def prepare_work(candidate, _mode):
                prepared["attempt"] = store.create_attempt(
                    candidate.work_item_id,
                    provider="codex",
                    task="Prepare AUIP",
                    metadata={"session_id": SESSION, "turn_id": "turn-play"},
                )

            coordinator = AuipLaunchCoordinator(
                artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=AttentionRequestCoordinator(),
                emit=emit,
            )
            await coordinator.route_control(
                {
                    "action": "prepare",
                    "mode": "collaborate",
                    "_host_preparation_work_item_id": item.work_item_id,
                },
                session_id=SESSION,
                turn_id="turn-play",
                prepare_work=prepare_work,
            )
            attempt = prepared["attempt"]
            store.update_attempt(
                attempt.attempt_id,
                execution_status="succeeded",
                metadata={
                    "outcome_verdict": {
                        "facet": "auip.application",
                        "verified": False,
                        "attention": "error",
                    }
                },
            )

            await coordinator.on_work_updated(
                Method.WORK_UPDATED,
                {"reason": "provider.result"},
            )

            assert emitted == []
            assert coordinator._deferred == {}
            store.close()

    asyncio.run(scenario())


def test_same_turn_launch_tag_can_precede_the_work_control_in_one_stream() -> None:
    parser = StreamTagParser()
    parsed = parse_inline_control_chunk(
        parser,
        '[AUIP action=launch target="delivery" mode="collaborate" after="work"]'
        '[DELEGATE provider="locus" intent="execute" task="build the game"]',
    )
    assert len(parsed.auip_actions) == 1
    assert parsed.auip_actions[0]["attrs"] == {
        "action": "launch",
        "target": "delivery",
        "mode": "collaborate",
        "after": "work",
    }
    assert len(parsed.delegate_actions) == 1


def test_latest_attempt_revision_supersedes_historical_manifest_records() -> None:
    with tempfile.TemporaryDirectory(prefix="auip_launch_revision_") as temp:
        root = Path(temp)
        store = WorkLedgerStore(root / "ledger.sqlite3")
        project = store.create_or_get_project(root / "project")
        item, _first_attempt, first_entry = _seed_app(
            store,
            project,
            root,
            title="Evolving Game",
            turn_id="turn-build",
        )

        workspace = Path(item.workspace_path)
        second_attempt = store.create_attempt(
            item.work_item_id,
            provider="locus",
            task="Add AUIP commentary",
            metadata={"session_id": SESSION, "turn_id": "turn-amend"},
        )
        entry = workspace / "index.html"
        entry.write_text("<!doctype html><title>game v2</title>", encoding="utf-8")
        second_entry = _register_file(store, item, second_attempt, entry)
        manifest = workspace / "auip.manifest.json"
        manifest.write_text(json.dumps(_manifest("Evolving Game v2")), encoding="utf-8")
        _register_file(store, item, second_attempt, manifest)
        store.update_attempt(second_attempt.attempt_id, execution_status="succeeded")

        discovered = discover_registered_auip_app(store, item.work_item_id)
        assert discovered is not None
        assert discovered["artifact_id"] == second_entry.artifact_id
        assert discovered["artifact_id"] != first_entry.artifact_id
        assert discovered["app"]["title"] == "Evolving Game v2"
        assert discovered["contributing_attempt_ids"] == [second_attempt.attempt_id]
        store.close()


def test_same_fragment_schedules_deferred_launch_before_delegate_start() -> None:
    async def scenario() -> None:
        events: list[str] = []
        provider_tasks: list[asyncio.Task[None]] = []

        async def route_auip(_attrs, **_context) -> None:
            events.append("auip")

        def record_delegate(_actions) -> None:
            async def start_provider() -> None:
                events.append("delegate")

            provider_tasks.append(asyncio.create_task(start_provider()))

        runtime = ChatRuntime()
        runtime.configure(auip_control_callback=route_auip)
        state = _TurnState(
            gui_callback=None,
            turn_id="turn-build-and-play",
            question="build it, then play with me",
            session_id=SESSION,
        )
        with patch("core.chat_runtime.record_actions", side_effect=record_delegate):
            runtime._consume_stream_chunk(
                state,
                '[AUIP action=launch target="delivery" mode="collaborate" after="work"]'
                '[DELEGATE provider="locus" intent="execute" task="build the game"]',
            )
            await runtime._wait_for_auip_controls(state)
            await asyncio.gather(*provider_tasks)
        assert events == ["auip", "delegate"]

    asyncio.run(scenario())


def test_later_launch_is_independent_from_the_work_delivery() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="auip_launch_later_") as temp:
            root = Path(temp)
            store = WorkLedgerStore(root / "ledger.sqlite3")
            project = store.create_or_get_project(root / "project")
            item, _attempt, entry = _seed_app(
                store,
                project,
                root,
                title="Tic Tac Toe",
                turn_id="turn-build",
            )
            emitted: list[tuple[str, dict[str, Any]]] = []

            async def emit(method: str, payload: dict[str, Any]) -> None:
                emitted.append((method, payload))

            coordinator = AuipLaunchCoordinator(
                artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=AttentionRequestCoordinator(),
                emit=emit,
            )
            candidate = coordinator.candidates(SESSION)[0]
            assert candidate.work_item_id == item.work_item_id
            assert candidate.artifact_id == entry.artifact_id
            prompt = coordinator.render_prompt_context(SESSION, language="en")
            assert "not proof that launch succeeded" in prompt
            assert "until the Host reports an AppSession connection" in prompt
            source_local_prompt = coordinator.render_prompt_context(
                SESSION,
                language="en",
                include_control_contract=False,
            )
            assert "always obey the shared control-outcome format" in source_local_prompt
            assert "another delivery" in source_local_prompt
            assert "[AUIP action=" not in source_local_prompt
            result = await coordinator.route_control(
                {"action": "launch", "target": candidate.title, "mode": "collaborate"},
                session_id=SESSION,
                turn_id="turn-open-later",
            )
            assert result["requested"] is True
            assert emitted == [
                (
                    Method.AUIP_LAUNCH_REQUESTED,
                    {
                        "request_id": result["request_id"],
                        "session_id": SESSION,
                        "artifact_id": entry.artifact_id,
                        "work_item_id": item.work_item_id,
                        "title": "Tic Tac Toe",
                        "mode": "collaborate",
                    },
                )
            ]
            store.close()

    asyncio.run(scenario())


def test_launch_timing_cannot_mix_an_existing_app_with_work_continuation() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="auip_launch_timing_") as temp:
            root = Path(temp)
            store = WorkLedgerStore(root / "ledger.sqlite3")
            project = store.create_or_get_project(root / "project")
            _seed_app(store, project, root, title="Chess", turn_id="turn-chess")
            coordinator = AuipLaunchCoordinator(
                artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=AttentionRequestCoordinator(),
                emit=lambda _method, _payload: asyncio.sleep(0),
            )
            result = await coordinator.route_control(
                {"action": "launch", "target": "Chess", "after": "work"},
                session_id=SESSION,
                turn_id="turn-invalid",
            )
            assert result == {"ok": False, "error": "invalid_launch_timing"}
            store.close()

    asyncio.run(scenario())


def test_same_turn_launch_waits_for_that_turns_successful_auip_delivery() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="auip_launch_deferred_") as temp:
            root = Path(temp)
            store = WorkLedgerStore(root / "ledger.sqlite3")
            project = store.create_or_get_project(root / "project")
            emitted: list[tuple[str, dict[str, Any]]] = []

            async def emit(method: str, payload: dict[str, Any]) -> None:
                emitted.append((method, payload))

            coordinator = AuipLaunchCoordinator(
                artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=AttentionRequestCoordinator(),
                emit=emit,
            )
            pending = await coordinator.route_control(
                {"action": "launch", "target": "delivery", "mode": "collaborate", "after": "work"},
                session_id=SESSION,
                turn_id="turn-build-and-play",
            )
            assert pending["deferred"] is True
            assert emitted == []

            _seed_app(
                store,
                project,
                root,
                title="Gomoku",
                turn_id="turn-build-and-play",
            )
            await coordinator.on_work_updated(Method.WORK_UPDATED, {"reason": "provider.result"})
            assert len(emitted) == 1
            assert emitted[0][0] == Method.AUIP_LAUNCH_REQUESTED
            assert emitted[0][1]["title"] == "Gomoku"
            assert emitted[0][1]["mode"] == "collaborate"

            # Replayed ledger snapshots cannot repeat the one-shot launch.
            await coordinator.on_work_updated(Method.WORK_UPDATED, {"reason": "provider.result"})
            assert len(emitted) == 1
            store.close()

    asyncio.run(scenario())


def test_same_turn_launch_can_be_bound_to_one_work_item_in_a_compound_turn() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="auip_launch_exact_work_") as temp:
            root = Path(temp)
            store = WorkLedgerStore(root / "ledger.sqlite3")
            project = store.create_or_get_project(root / "project")
            emitted: list[tuple[str, dict[str, Any]]] = []

            async def emit(method: str, payload: dict[str, Any]) -> None:
                emitted.append((method, payload))

            coordinator = AuipLaunchCoordinator(
                artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=AttentionRequestCoordinator(),
                emit=emit,
            )
            result = await coordinator.route_control(
                {
                    "action": "launch",
                    "target": "delivery",
                    "mode": "collaborate",
                    "after": "work",
                    "_host_work_binding": "turn",
                    "_host_work_item_id": "work-owned-app",
                },
                session_id=SESSION,
                turn_id="turn-compound",
            )
            assert result["deferred"] is True
            pending = coordinator._deferred[(SESSION, "turn-compound")]
            assert pending.work_item_id == "work-owned-app"

            _seed_app(
                store,
                project,
                root,
                title="Unrelated Result",
                turn_id="turn-compound",
            )
            await coordinator.on_work_updated(
                Method.WORK_UPDATED,
                {"reason": "provider.result"},
            )
            assert emitted == []
            assert coordinator._deferred[(SESSION, "turn-compound")] == pending
            store.close()

    asyncio.run(scenario())


def test_deferred_launch_waits_for_approved_export_instead_of_staging() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="auip_launch_export_wait_") as temp:
            root = Path(temp)
            workspace = root / "workspace"
            desktop = root / "Desktop"
            workspace.mkdir()
            desktop.mkdir()
            store = WorkLedgerStore(root / "ledger.sqlite3")
            project = store.create_or_get_project(workspace)
            item = store.create_work_item(
                project.project_id,
                title="Reactor",
                workspace_path=workspace,
            )
            attempt = store.create_attempt(
                item.work_item_id,
                provider="codex",
                task="Build an AUIP reactor on Desktop",
                metadata={"session_id": SESSION, "turn_id": "turn-reactor"},
            )
            emitted: list[tuple[str, dict[str, Any]]] = []

            async def emit(method: str, payload: dict[str, Any]) -> None:
                emitted.append((method, payload))

            coordinator = AuipLaunchCoordinator(
                artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=AttentionRequestCoordinator(),
                emit=emit,
            )
            pending = await coordinator.route_control(
                {
                    "action": "launch",
                    "target": "delivery",
                    "mode": "collaborate",
                    "after": "work",
                    "_host_work_binding": "active",
                    "_host_active_work_attempt_ids": (attempt.attempt_id,),
                },
                session_id=SESSION,
                turn_id="turn-open-reactor",
            )
            assert pending["deferred"] is True

            export = WorkExportService(store, desktop_path=desktop)
            plan = export.prepare_plan(
                provider="codex",
                mode="agent",
                task=attempt.task,
                item=item,
                attempt=attempt,
                metadata={"external_export": {"target": "desktop"}},
            )
            assert plan is not None
            store.update_attempt(
                attempt.attempt_id,
                metadata={"export_plan": dict(plan)},
            )
            stage = Path(plan["staging_root"])
            entry = stage / "reactor.html"
            manifest = stage / "auip.manifest.json"
            entry.write_text("<!doctype html><title>Reactor</title>", encoding="utf-8")
            manifest.write_text(json.dumps(_manifest("Reactor")), encoding="utf-8")
            _register_file(store, item, attempt, entry)
            _register_file(store, item, attempt, manifest)
            store.update_attempt(attempt.attempt_id, execution_status="succeeded")

            # A succeeded Attempt with a Desktop plan can become visible one
            # snapshot before export discovery creates its permission row.
            # Absence of that row is an in-flight transaction, not a terminal
            # "no app" conclusion.
            assert discover_registered_auip_app(store, item.work_item_id) is None
            await coordinator.on_work_updated(
                Method.WORK_UPDATED,
                {"reason": "provider.event:run.finished"},
            )
            assert emitted == []
            assert len(coordinator._deferred) == 1

            permission = export.discover_staged_exports(attempt, item, plan)["permission"]
            assert permission is not None and permission.status == "pending"
            await coordinator.on_work_updated(
                Method.WORK_UPDATED,
                {"reason": "provider.result"},
            )
            assert emitted == []
            assert len(coordinator._deferred) == 1

            resolution = export.resolve(permission.request_id, allow=True)
            assert resolution.permission.status == "allowed"
            await coordinator.on_work_updated(
                Method.WORK_UPDATED,
                {"reason": "permission.allowed.auto_accepted"},
            )
            assert len(emitted) == 1
            assert emitted[0][0] == Method.AUIP_LAUNCH_REQUESTED
            launched = store.get_artifact(emitted[0][1]["artifact_id"])
            assert launched is not None
            assert launched.kind == "business.export"
            assert Path(launched.path) == desktop / "reactor.html"
            assert coordinator._deferred == {}

            # Any later Work projection is just a replay of settled facts.
            await coordinator.on_work_updated(Method.WORK_UPDATED, {"reason": "work.accepted"})
            assert len(emitted) == 1
            store.close()

    asyncio.run(scenario())


def test_followup_launch_freezes_the_active_operation_without_redelegating() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="auip_launch_active_work_") as temp:
            root = Path(temp)
            store = WorkLedgerStore(root / "ledger.sqlite3")
            project = store.create_or_get_project(root / "project")
            item, _old_attempt, _old_entry = _seed_app(
                store,
                project,
                root,
                title="Evolving Game",
                turn_id="turn-build",
            )
            operation, active = store.create_operation_attempt(
                item.work_item_id,
                intent="amend",
                instruction="Add participant support",
                provider="locus",
                task="Add participant support",
                attempt_metadata={
                    "session_id": SESSION,
                    "turn_id": "turn-add-auip",
                },
            )
            emitted: list[tuple[str, dict[str, Any]]] = []

            async def emit(method: str, payload: dict[str, Any]) -> None:
                emitted.append((method, payload))

            coordinator = AuipLaunchCoordinator(
                artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=AttentionRequestCoordinator(),
                emit=emit,
            )
            pending = await coordinator.route_control(
                {
                    "action": "launch",
                    "target": "delivery",
                    "mode": "observe",
                    "after": "work",
                    "_host_work_binding": "active",
                    "_host_active_work_attempt_ids": (active.attempt_id,),
                },
                session_id=SESSION,
                turn_id="turn-open-after-active",
            )
            assert pending["deferred"] is True
            assert emitted == []

            workspace = Path(item.workspace_path)
            entry = workspace / "index.html"
            entry.write_text("<!doctype html><title>game v2</title>", encoding="utf-8")
            latest_entry = _register_file(store, item, active, entry)
            manifest = workspace / "auip.manifest.json"
            manifest.write_text(
                json.dumps(_manifest("Evolving Game v2")),
                encoding="utf-8",
            )
            _register_file(store, item, active, manifest)
            store.update_attempt(active.attempt_id, execution_status="succeeded")

            await coordinator.on_work_updated(
                Method.WORK_UPDATED,
                {"reason": "provider.result"},
            )
            assert len(emitted) == 1
            assert emitted[0][0] == Method.AUIP_LAUNCH_REQUESTED
            assert emitted[0][1]["artifact_id"] == latest_entry.artifact_id
            assert emitted[0][1]["title"] == "Evolving Game v2"
            assert operation.operation_id == active.operation_id
            store.close()

    asyncio.run(scenario())


def test_ambiguous_launch_uses_one_shot_attention_selection() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="auip_launch_attention_") as temp:
            root = Path(temp)
            store = WorkLedgerStore(root / "ledger.sqlite3")
            project = store.create_or_get_project(root / "project")
            _seed_app(store, project, root, title="Chess", turn_id="turn-chess")
            _seed_app(store, project, root, title="Gomoku", turn_id="turn-gomoku")
            emitted: list[tuple[str, dict[str, Any]]] = []
            attention = AttentionRequestCoordinator()

            async def emit(method: str, payload: dict[str, Any]) -> None:
                emitted.append((method, payload))

            coordinator = AuipLaunchCoordinator(
                artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=attention,
                emit=emit,
            )
            routed = await coordinator.route_control(
                {"action": "launch", "mode": "observe"},
                session_id=SESSION,
                turn_id="turn-ambiguous",
            )
            assert routed["deferred"] is True
            assert emitted == []
            request = attention.list_pending(SESSION)[0]
            assert {option["label"] for option in request["options"]} == {"Chess", "Gomoku"}
            resolved = await attention.resolve(
                session_id=SESSION,
                request_id=request["id"],
                option_id=request["options"][0]["id"],
            )
            assert resolved["ok"] is True
            assert len(emitted) == 1
            replay = await attention.resolve(
                session_id=SESSION,
                request_id=request["id"],
                option_id=request["options"][0]["id"],
            )
            assert replay["ok"] is False
            assert len(emitted) == 1
            store.close()

    asyncio.run(scenario())


def test_deferred_launch_uses_attention_to_freeze_one_active_operation() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="auip_launch_active_attention_") as temp:
            root = Path(temp)
            store = WorkLedgerStore(root / "ledger.sqlite3")
            project = store.create_or_get_project(root / "project")
            chess, _old_chess, _entry = _seed_app(
                store,
                project,
                root,
                title="Chess",
                turn_id="turn-chess",
            )
            gomoku, _old_gomoku, _entry = _seed_app(
                store,
                project,
                root,
                title="Gomoku",
                turn_id="turn-gomoku",
            )
            chess_operation, chess_attempt = store.create_operation_attempt(
                chess.work_item_id,
                intent="amend",
                instruction="Add AUIP support to Chess",
                provider="locus",
                task="Add AUIP support to Chess",
                attempt_metadata={"session_id": SESSION, "turn_id": "turn-chess-active"},
            )
            _gomoku_operation, gomoku_attempt = store.create_operation_attempt(
                gomoku.work_item_id,
                intent="amend",
                instruction="Add AUIP support to Gomoku",
                provider="locus",
                task="Add AUIP support to Gomoku",
                attempt_metadata={"session_id": SESSION, "turn_id": "turn-gomoku-active"},
            )
            emitted: list[tuple[str, dict[str, Any]]] = []
            attention = AttentionRequestCoordinator()

            async def emit(method: str, payload: dict[str, Any]) -> None:
                emitted.append((method, payload))

            coordinator = AuipLaunchCoordinator(
                artifacts=store,
                work_roster=WorkLedgerCoordinator(store),
                attention=attention,
                emit=emit,
            )
            routed = await coordinator.route_control(
                {
                    "action": "launch",
                    "target": "delivery",
                    "mode": "observe",
                    "after": "work",
                    "_host_work_binding": "active",
                    "_host_active_work_attempt_ids": (
                        chess_attempt.attempt_id,
                        gomoku_attempt.attempt_id,
                    ),
                },
                session_id=SESSION,
                turn_id="turn-open-after-one-active",
            )
            assert routed["deferred"] is True
            request = attention.list_pending(SESSION)[0]
            assert {option["label"] for option in request["options"]} == {
                "Chess",
                "Gomoku",
            }
            chess_option = next(
                option for option in request["options"] if option["label"] == "Chess"
            )
            resolved = await attention.resolve(
                session_id=SESSION,
                request_id=request["id"],
                option_id=chess_option["id"],
            )
            assert resolved["ok"] is True
            pending = next(iter(coordinator._deferred.values()))
            assert pending.work_item_id == chess.work_item_id
            assert pending.operation_id == chess_operation.operation_id

            store.update_attempt(
                gomoku_attempt.attempt_id,
                execution_status="succeeded",
            )
            await coordinator.on_work_updated(
                Method.WORK_UPDATED,
                {"reason": "provider.result"},
            )
            assert emitted == []

            workspace = Path(chess.workspace_path)
            entry = workspace / "index.html"
            entry.write_text("<!doctype html><title>chess v2</title>", encoding="utf-8")
            _register_file(store, chess, chess_attempt, entry)
            manifest = workspace / "auip.manifest.json"
            manifest.write_text(json.dumps(_manifest("Chess v2")), encoding="utf-8")
            _register_file(store, chess, chess_attempt, manifest)
            store.update_attempt(chess_attempt.attempt_id, execution_status="succeeded")
            await coordinator.on_work_updated(
                Method.WORK_UPDATED,
                {"reason": "provider.result"},
            )
            assert len(emitted) == 1
            assert emitted[0][1]["title"] == "Chess v2"
            store.close()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_generic_html_is_not_an_auip_application()
    test_deleted_historical_index_does_not_hide_current_named_entry()
    test_approved_desktop_html_is_a_preparable_delivery_not_a_launchable_app()
    test_one_approved_auip_bundle_launches_without_duplicate_preparation()
    test_same_turn_launch_tag_can_precede_the_work_control_in_one_stream()
    test_failed_preparation_uses_the_work_terminal_without_a_second_launch_report()
    test_latest_attempt_revision_supersedes_historical_manifest_records()
    test_same_fragment_schedules_deferred_launch_before_delegate_start()
    test_later_launch_is_independent_from_the_work_delivery()
    test_launch_timing_cannot_mix_an_existing_app_with_work_continuation()
    test_same_turn_launch_waits_for_that_turns_successful_auip_delivery()
    test_followup_launch_freezes_the_active_operation_without_redelegating()
    test_ambiguous_launch_uses_one_shot_attention_selection()
    test_deferred_launch_uses_attention_to_freeze_one_active_operation()
    print("ok: AUIP launch is capability-based, independent, bounded, and one-shot")
