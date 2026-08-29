from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from agent_host.work_ledger_store import WorkLedgerStore
from server.attention_request import AttentionRequestCoordinator
from server.auip_app_connection import AuipAppRequestHandler
from server.auip_app_source import discover_exported_auip_apps
from server.auip_contract import AUIP_SCHEMA
from server.auip_runtime import AuipRuntime
from server.auip_self_attach import AuipSelfAttachCoordinator
from server.protocol import Method


SESSION = "self-attach-session"


def _manifest() -> dict:
    return {
        "schema": AUIP_SCHEMA,
        "app": {"id": "gomoku", "title": "Gomoku", "version": "0.1.0"},
        "events": {"game.changed": {"beat": True}},
        "actions": {
            "game.place_stone": {
                "description": "Place one stone.",
                "risk": "local_execution",
            }
        },
        "stances": ["spectator", "participant"],
    }


def _approved_bundle(store: WorkLedgerStore, root: Path) -> Path:
    project_root = root / "project"
    project_root.mkdir()
    project = store.create_or_get_project(project_root)
    workspace = root / "workspace"
    workspace.mkdir()
    work = store.create_work_item(
        project.project_id,
        title="Gomoku",
        workspace_path=workspace,
    )
    attempt = store.create_attempt(
        work.work_item_id,
        provider="codex",
        task="Prepare Gomoku for AUIP",
    )
    bundle = root / "Desktop" / "Gomoku"
    bundle.mkdir(parents=True)
    files = {
        "gomoku.html": "<!doctype html><script src='./auip-v0.js'></script>",
        "auip-v0.js": "window.AmadeusAUIP = {};\n",
        "auip.manifest.json": json.dumps(_manifest()),
    }
    for index, (name, body) in enumerate(files.items()):
        path = bundle / name
        path.write_text(body, encoding="utf-8")
        store.register_artifact(
            work.work_item_id,
            attempt_id=attempt.attempt_id,
            kind="business.export",
            title=name,
            path=path,
            identity=f"export-target:{attempt.attempt_id}:{index}:Gomoku/{name}",
            status="approved",
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            size_bytes=path.stat().st_size,
            metadata={
                "permission_request_id": "permission-bundle",
                "relative_path": f"Gomoku/{name}",
                "export_status": "approved",
            },
        )
    return bundle / "gomoku.html"


def _self_attach(
    store: WorkLedgerStore,
    runtime: AuipRuntime,
    attention: AttentionRequestCoordinator,
) -> AuipSelfAttachCoordinator:
    return AuipSelfAttachCoordinator(
        runtime=runtime,
        artifacts=store,
        attention=attention,
        current_session_id=lambda: SESSION,
        timeout_s=5,
    )


def test_approved_bundle_self_reports_but_only_user_choice_mints_a_ticket(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
            entry = _approved_bundle(store, tmp_path)
            apps = discover_exported_auip_apps(store, _manifest())
            assert len(apps) == 1
            assert apps[0]["entry_path"] == str(entry.resolve())

            runtime = AuipRuntime()
            attention = AttentionRequestCoordinator()
            handler = AuipAppRequestHandler(
                runtime,
                self_attach=_self_attach(store, runtime, attention),
            )
            pending = asyncio.create_task(
                handler.handle(
                    Method.AUIP_ATTACH_REQUEST,
                    {
                        "manifest": _manifest(),
                        "instance_id": "instance-gomoku-1",
                        "entry_url": entry.resolve().as_uri(),
                    },
                )
            )
            for _ in range(20):
                requests = attention.list_pending(SESSION)
                if requests:
                    break
                await asyncio.sleep(0)
            assert len(requests) == 1
            assert runtime.focused_projection(SESSION) is None
            collaborate = next(
                option
                for option in requests[0]["options"]
                if option["label"] == "Play together"
            )
            resolved = await attention.resolve(
                session_id=SESSION,
                request_id=requests[0]["id"],
                option_id=collaborate["id"],
            )
            assert resolved["ok"] is True
            approved = await pending
            assert approved["ok"] is True
            assert runtime.focused_projection(SESSION) is None

            registered = await handler.handle(
                Method.AUIP_REGISTER,
                {"manifest": _manifest(), "attach_ticket": approved["attach_ticket"]},
            )
            assert registered["ok"] is True
            assert registered["conversation_id"] == SESSION
            assert registered["engagement_mode"] == "collaborate"
            assert registered["artifact_ref"].startswith("export-bundle:")

    asyncio.run(scenario())


def test_denial_and_changed_bundle_never_create_an_app_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
            entry = _approved_bundle(store, tmp_path)
            runtime = AuipRuntime()
            attention = AttentionRequestCoordinator()
            handler = AuipAppRequestHandler(
                runtime,
                self_attach=_self_attach(store, runtime, attention),
            )
            pending = asyncio.create_task(
                handler.handle(
                    Method.AUIP_ATTACH_REQUEST,
                    {
                        "manifest": _manifest(),
                        "instance_id": "instance-gomoku-2",
                        "entry_url": entry.resolve().as_uri(),
                    },
                )
            )
            for _ in range(20):
                requests = attention.list_pending(SESSION)
                if requests:
                    break
                await asyncio.sleep(0)
            deny = next(
                option for option in requests[0]["options"] if option["label"] == "Not now"
            )
            await attention.resolve(
                session_id=SESSION,
                request_id=requests[0]["id"],
                option_id=deny["id"],
            )
            refused = await pending
            assert refused["error"] == "attach_denied"
            assert runtime.focused_projection(SESSION) is None

            entry.write_text("changed after approval", encoding="utf-8")
            stale = await handler.handle(
                Method.AUIP_ATTACH_REQUEST,
                {
                    "manifest": _manifest(),
                    "instance_id": "instance-gomoku-3",
                    "entry_url": entry.resolve().as_uri(),
                },
            )
            assert stale["error"] == "unregistered_app"
            assert attention.list_pending(SESSION) == []

    asyncio.run(scenario())
