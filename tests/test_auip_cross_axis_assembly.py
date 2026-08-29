"""Assembly contract from Provider Work delivery to an AUIP AppSession."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from agent_host.provider_types import ProviderRunRequest
from agent_host.work_ledger_store import WorkLedgerStore
from server.attention_request import AttentionRequestCoordinator
from server.auip_app_connection import AuipAppRequestHandler
from server.auip_contract import AUIP_SCHEMA
from server.auip_launch import AuipLaunchCoordinator
from server.auip_runtime import AuipRuntime
from server.handlers.auip_handler import AuipHandler
from server.protocol import Method
from server.work_export_service import WorkExportService
from server.work_ledger_coordinator import WorkLedgerCoordinator


SESSION = "conversation-cross-axis"


def _manifest() -> dict[str, Any]:
    return {
        "schema": AUIP_SCHEMA,
        "app": {
            "id": "cross-axis-game",
            "title": "Cross Axis Game",
            "version": "0.1.0",
            "interactionSummary": (
                "The participant can make one declared move. "
                "For example, 'take one move' selects one available choice."
            ),
        },
        "events": {"game.changed": {"beat": True}},
        "situationKinds": ["choice/v1"],
        "actions": {
            "game.move": {
                "description": "Make one declared move.",
                "risk": "local_execution",
            }
        },
        "stances": ["spectator", "participant"],
    }


def _register_file(
    store: WorkLedgerStore,
    *,
    work_item_id: str,
    attempt_id: str,
    path: Path,
) -> None:
    store.register_artifact(
        work_item_id,
        attempt_id=attempt_id,
        kind="business.file",
        title=path.name,
        path=path,
        status="registered",
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def test_provider_delivery_launches_through_host_and_registers_one_app_session(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = WorkLedgerStore(tmp_path / "ledger.sqlite3")
        project_root = tmp_path / "project"
        project_root.mkdir()
        project = store.create_or_get_project(project_root)
        workspace = tmp_path / "delivery"
        workspace.mkdir()
        work = store.create_work_item(
            project.project_id,
            title="Cross Axis Game",
            workspace_path=workspace,
        )
        attempt = store.create_attempt(
            work.work_item_id,
            provider="codex",
            task="Build one AUIP game",
            metadata={"session_id": SESSION, "turn_id": "turn-build"},
        )
        entry = workspace / "index.html"
        entry.write_text("<!doctype html><title>Cross Axis Game</title>", encoding="utf-8")
        manifest_path = workspace / "auip.manifest.json"
        manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
        _register_file(
            store,
            work_item_id=work.work_item_id,
            attempt_id=attempt.attempt_id,
            path=entry,
        )
        _register_file(
            store,
            work_item_id=work.work_item_id,
            attempt_id=attempt.attempt_id,
            path=manifest_path,
        )
        store.update_attempt(attempt.attempt_id, execution_status="succeeded")

        emitted: list[tuple[str, dict[str, Any]]] = []

        async def emit(method: str, payload: dict[str, Any]) -> None:
            emitted.append((method, payload))

        launch = AuipLaunchCoordinator(
            artifacts=store,
            work_roster=WorkLedgerCoordinator(store),
            attention=AttentionRequestCoordinator(),
            emit=emit,
        )
        requested = await launch.route_control(
            {"action": "launch", "mode": "collaborate"},
            session_id=SESSION,
            turn_id="turn-open",
        )
        assert requested["ok"] is True
        assert requested["requested"] is True
        assert emitted[-1][0] == Method.AUIP_LAUNCH_REQUESTED
        launch_payload = emitted[-1][1]
        assert launch_payload["work_item_id"] == work.work_item_id

        app_runtime = AuipRuntime()
        host = AuipHandler(
            app_runtime,
            artifacts=store,
            current_session_id=lambda: SESSION,
            launch=launch,
        )
        prepared = await host.handle(
            Method.AUIP_ATTACH_PREPARE,
            {
                "request_id": requested["request_id"],
                "artifact_id": launch_payload["artifact_id"],
                "mode": "collaborate",
            },
        )
        assert prepared and prepared["ok"] is True
        assert prepared["launch_url"].startswith("file:")

        app = AuipAppRequestHandler(app_runtime)
        registered = await app.handle(
            Method.AUIP_REGISTER,
            {
                "manifest": _manifest(),
                "attach_ticket": prepared["attach_ticket"],
            },
        )
        assert registered["ok"] is True
        assert registered["conversation_id"] == SESSION
        assert registered["artifact_ref"] == prepared["artifact_ref"]
        assert registered["engagement_mode"] == "collaborate"

        # Crossing into an AppSession is a separate authority transition. It
        # must not invent another Provider Attempt or repurpose Provider ids.
        attempts = store.list_attempts(work.work_item_id)
        assert [row.attempt_id for row in attempts] == [attempt.attempt_id]
        assert attempts[0].provider == "codex"
        assert not registered["app_session_id"].startswith("codex")
        store.close()

    asyncio.run(scenario())


def test_deferred_launch_consumes_the_materialized_staged_auip_delivery(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = WorkLedgerStore(tmp_path / "ledger.sqlite3")
        workspace = tmp_path / "workspace"
        desktop = tmp_path / "Desktop"
        workspace.mkdir()
        desktop.mkdir()
        work = WorkLedgerCoordinator(
            store,
            export_service=WorkExportService(store, desktop_path=desktop),
        )
        request = work.prepare_request(
            ProviderRunRequest(
                provider="codex",
                task="Connect this game so we can play together.",
                cwd=str(workspace),
                mode="agent",
                metadata={
                    "source": "auip_prepare",
                    "session_id": SESSION,
                    "turn_id": "turn-connect-and-play",
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
        binding = request.metadata["work"]
        emitted: list[tuple[str, dict[str, Any]]] = []

        async def emit(method: str, payload: dict[str, Any]) -> None:
            emitted.append((method, payload))

        launch = AuipLaunchCoordinator(
            artifacts=store,
            work_roster=work,
            attention=AttentionRequestCoordinator(),
            emit=emit,
        )
        pending = await launch.route_control(
            {
                "action": "launch",
                "target": "delivery",
                "mode": "collaborate",
                "after": "work",
            },
            session_id=SESSION,
            turn_id="turn-connect-and-play",
        )
        assert pending["deferred"] is True

        stage = Path(request.metadata["export_plan"]["staging_root"])
        manifest = _manifest()
        rendered = json.dumps(manifest, ensure_ascii=False, indent=2)
        (stage / "gomoku.html").write_text(
            "<!doctype html>\n"
            '<script id="auip-manifest" type="application/json">\n'
            f"{rendered}\n"
            "</script>\n"
            '<script src="./sdk/auip-core/managed-v0.js"></script>\n'
            '<script src="./sdk/auip-core/situations-v0.js"></script>\n'
            '<script src="./sdk/auip-web/auip-v0.js"></script>\n',
            encoding="utf-8",
        )
        (stage / "auip.manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        await work._on_provider_event(
            Method.PROVIDER_EVENT,
            {
                "provider": request.provider,
                "run_id": "codex-cross-axis-staged",
                "type": "run.created",
                "payload": {"task": request.task, "cwd": request.cwd},
                "metadata": request.metadata,
            },
        )
        await work._on_provider_result(
            Method.PROVIDER_RESULT,
            {
                "provider": request.provider,
                "run_id": "codex-cross-axis-staged",
                "status": "done",
                "result": "Prepared the requested AUIP application.",
                "error": "",
                "metadata": request.metadata,
            },
        )

        permission = store.list_permission_requests(
            str(binding["work_item_id"]),
            attempt_id=str(binding["attempt_id"]),
            status="pending",
        )[0]
        await work.resolve_permission(
            permission.request_id,
            allow=True,
            work_item_id=str(binding["work_item_id"]),
            attempt_id=str(binding["attempt_id"]),
        )
        await launch.on_work_updated(Method.WORK_UPDATED, {"reason": "provider.result"})
        assert len(emitted) == 1
        assert emitted[0][0] == Method.AUIP_LAUNCH_REQUESTED
        assert emitted[0][1]["work_item_id"] == binding["work_item_id"]
        assert emitted[0][1]["mode"] == "collaborate"
        assert len(store.list_attempts(str(binding["work_item_id"]))) == 1
        store.close()

    asyncio.run(scenario())
