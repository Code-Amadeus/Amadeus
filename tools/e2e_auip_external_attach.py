"""Run a real-WebSocket AUIP external-app simulation without opening a GUI.

This journey exercises the product boundary that a visible browser app uses:
registered artifact -> host launch descriptor -> /auip/ws registration ->
state -> host action -> accepted receipt -> terminal event -> clean close.
It deliberately substitutes only the application's game mechanics, not the
transport or AUIP runtime.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import socket
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn
import websockets
from fastapi import FastAPI, WebSocket

from server.auip_app_connection import AuipAppConnectionManager
from server.auip_app_launcher import parse_app_launch_url
from server.auip_engagement import AuipEngagementCoordinator
from server.auip_narration import AuipNarrationAdapter, AuipNarrationProfile
from server.auip_narration_llm import (
    decide_with_auip_observer,
    narrate_with_auip_llm,
    present_with_auip_llm,
)
from server.auip_runtime import AuipRuntime
from server.event_bus import bus
from server.handlers.auip_handler import AuipHandler
from server.protocol import Method


MANIFEST = {
    "schema": "amadeus.auip/v0",
    "app": {"id": "sim-counter", "title": "Simulated Counter", "version": "0.1.0"},
    "events": {
        "counter.changed": {"beat": True},
        "counter.finished": {"beat": True, "importance": "important", "terminal": True},
    },
    "actions": {
        "counter.increment": {
            "description": "Increment the local counter once.",
            "risk": "local_execution",
        }
    },
    "stances": ["spectator", "participant"],
}


@dataclass
class _Artifact:
    artifact_id: str
    work_item_id: str
    attempt_id: str
    kind: str
    title: str
    path: str
    status: str
    sha256: str


@dataclass
class _WorkItem:
    workspace_path: str


@dataclass
class _Attempt:
    attempt_id: str
    attempt_number: int = 1
    metadata: dict[str, Any] | None = None


class _Store:
    def __init__(self, root: Path) -> None:
        workspace = root / "workspace"
        workspace.mkdir()
        entry = workspace / "counter.html"
        entry.write_text("<!doctype html><title>Counter</title>", encoding="utf-8")
        manifest = workspace / "auip.manifest.json"
        manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
        self.artifact = _Artifact(
            artifact_id="artifact-sim-counter",
            work_item_id="work-sim-counter",
            attempt_id="attempt-sim-counter",
            kind="business.file",
            title="Simulated Counter",
            path=str(entry),
            status="registered",
            sha256=hashlib.sha256(entry.read_bytes()).hexdigest(),
        )
        self.manifest_artifact = _Artifact(
            artifact_id="artifact-sim-manifest",
            work_item_id="work-sim-counter",
            attempt_id="attempt-sim-counter",
            kind="business.file",
            title="AUIP manifest",
            path=str(manifest),
            status="registered",
            sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        )
        self.item = _WorkItem(workspace_path=str(workspace))
        self.attempt = _Attempt(
            attempt_id="attempt-sim-counter",
            metadata={},
        )

    def get_artifact(self, artifact_id: str) -> Any:
        return next(
            (
                item
                for item in (self.artifact, self.manifest_artifact)
                if artifact_id == item.artifact_id
            ),
            None,
        )

    def get_work_item(self, work_item_id: str) -> Any:
        return self.item if work_item_id == self.artifact.work_item_id else None

    def get_attempt(self, attempt_id: str) -> Any:
        return self.attempt if attempt_id == self.attempt.attempt_id else None

    def list_artifacts(self, work_item_id: str, *, attempt_id: str = "") -> list[Any]:
        if work_item_id != self.artifact.work_item_id:
            return []
        return [self.artifact, self.manifest_artifact]


class _AppClient:
    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket
        self.sequence = 0

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        request_id = f"sim-{self.sequence}"
        await self.websocket.send(
            json.dumps(
                {"type": "req", "id": request_id, "method": method, "params": params},
                separators=(",", ":"),
            )
        )
        while True:
            message = json.loads(await asyncio.wait_for(self.websocket.recv(), timeout=5.0))
            if message.get("type") == "res" and message.get("id") == request_id:
                return dict(message.get("params") or {})

    async def next_event(self, method: str) -> dict[str, Any]:
        while True:
            message = json.loads(await asyncio.wait_for(self.websocket.recv(), timeout=5.0))
            if message.get("type") == "evt" and message.get("method") == method:
                return dict(message.get("params") or {})


def _listening_socket() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    return listener


async def run_simulation(*, real_narration: bool = False) -> dict[str, Any]:
    runtime = AuipRuntime()
    manager = AuipAppConnectionManager(runtime)
    participant_calls: list[dict[str, Any]] = []

    async def participant(context: dict[str, Any]) -> dict[str, Any]:
        participant_calls.append(context)
        return {
            "action": "act",
            "type": "counter.increment",
            "payload": {"amount": 1},
            "private_note": "increment the attached counter once",
        }

    engagement = AuipEngagementCoordinator(
        app_runtime=runtime,
        controller=participant,
        role_authorizer=lambda _context: {
            "decision": "approve",
            "reason": "deterministic E2E policy",
        },
        controller_id="e2e-counter-participant",
    )
    narration_deliveries: list[dict[str, Any]] = []
    narration: AuipNarrationAdapter | None = None
    narration_callback = None
    if real_narration:
        narration = AuipNarrationAdapter(
            runtime=runtime,
            observer=decide_with_auip_observer,
            narrator=narrate_with_auip_llm,
            presenter=present_with_auip_llm,
            presentation_mode="structured",
            sink=lambda payload: narration_deliveries.append(dict(payload))
            or {"status": "queued", "sentence_id": payload.get("line_id")},
            profile=AuipNarrationProfile(normal_beat_stride=1),
            recent_chat=lambda _conversation: [],
            display_language=lambda: "japanese",
        )
        narration_callback = narration.enqueue_update
        bus.on(Method.AUIP_UPDATED, narration_callback)
    listener = _listening_socket()
    port = int(listener.getsockname()[1])
    endpoint = f"ws://127.0.0.1:{port}/auip/ws"
    app = FastAPI()

    @app.websocket("/auip/ws")
    async def auip_endpoint(websocket: WebSocket) -> None:
        await manager.handle_connection(websocket)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if server.started:
                break
            if server_task.done():
                raise RuntimeError("AUIP simulation server exited during startup")
            await asyncio.sleep(0.01)
        if not server.started:
            raise RuntimeError("AUIP simulation server did not start")

        with tempfile.TemporaryDirectory() as tmp:
            store = _Store(Path(tmp))
            host = AuipHandler(
                runtime,
                artifacts=store,
                current_session_id=lambda: "conversation-auip-simulation",
                app_websocket_url=endpoint,
                engagement=engagement,
            )
            prepared = await host.handle(
                Method.AUIP_ATTACH_PREPARE,
                {"artifact_id": store.artifact.artifact_id},
            )
            if not prepared or prepared.get("ok") is not True:
                raise AssertionError(f"Attach prepare failed: {prepared}")
            launch = parse_app_launch_url(str(prepared["launch_url"]))
            assert launch["webSocketUrl"] == endpoint
            assert launch["attachTicket"] == prepared["attach_ticket"]

            async with websockets.connect(endpoint) as websocket:
                client = _AppClient(websocket)
                registered = await client.request(
                    Method.AUIP_REGISTER,
                    {
                        "manifest": MANIFEST,
                        "attach_ticket": launch["attachTicket"],
                        "conversation_id": "forged-by-app",
                    },
                )
                assert registered.get("ok") is True
                assert registered["conversation_id"] == "conversation-auip-simulation"
                app_session_id = str(registered["app_session_id"])
                token = str(registered["bridge_token"])
                auth = {"app_session_id": app_session_id, "bridge_token": token}

                state = await client.request(
                    Method.AUIP_STATE_PUBLISH,
                    {**auth, "revision": 1, "state": {"value": 0, "turn": "kurisu"}},
                )
                assert state["revision"] == 1
                mode = await host.handle(
                    Method.AUIP_MODE_SET,
                    {"app_session_id": app_session_id, "mode": "delegate"},
                )
                assert mode and mode["engagement_mode"] == "delegate"
                event = await client.next_event(Method.AUIP_ACTION_REQUESTED)
                assert event["action"]["type"] == "counter.increment"
                assert event["action"]["expected_revision"] == 1

                receipt = await client.request(
                    Method.AUIP_ACTION_RESULT,
                    {
                        **auth,
                        "action_id": event["action"]["action_id"],
                        "accepted": True,
                        "resulting_revision": 2,
                        "state": {"value": 1, "turn": "user"},
                        "effects": {"value": 1},
                    },
                )
                assert receipt["latest_verified_self_action"]["accepted"] is True
                assert participant_calls and participant_calls[0]["state"]["value"] == 0
                terminal = await client.request(
                    Method.AUIP_EVENT_PUBLISH,
                    {
                        **auth,
                        "event_id": "counter-finished-1",
                        "event_type": "counter.finished",
                        "actor": "app",
                        "revision": 2,
                        "payload": {"value": 1},
                    },
                )
                assert terminal["status"] == "completed"
                if narration is not None:
                    await narration.wait_for_idle()
                    assert narration_deliveries
                    assert narration_deliveries[-1]["source"] == "auip_narrator"
                    assert narration_deliveries[-1]["terminal"] is True
                closed = await client.request(
                    Method.AUIP_SESSION_CLOSE,
                    {**auth, "reason": "simulation_complete"},
                )
                assert closed["status"] == "closed"

            snapshot = runtime.get(app_session_id)
            capsule = snapshot["experience_capsule"]
            context = runtime.render_main_chat_context("conversation-auip-simulation")
            assert snapshot["status"] == "closed"
            assert capsule["close_reason"] == "simulation_complete"
            assert capsule["verified_self_actions"][0]["type"] == "counter.increment"
            assert capsule["terminal"]["type"] == "counter.finished"
            assert "Recent AUIP branch capsule" in context
            assert "attachTicket" not in context
            report = {
                "ok": True,
                "transport": "real_websocket",
                "session": snapshot["status"],
                "final_revision": snapshot["revision"],
                "verified_action": capsule["verified_self_actions"][0]["type"],
                "participant_decisions": len(participant_calls),
                "terminal_event": capsule["terminal"]["type"],
                "close_reason": capsule["close_reason"],
            }
            if real_narration:
                report["structured_narration"] = {
                    "deliveries": len(narration_deliveries),
                    "source": str(narration_deliveries[-1].get("source") or ""),
                    "terminal": bool(narration_deliveries[-1].get("terminal")),
                    "display_text": str(
                        narration_deliveries[-1].get("display_text") or ""
                    ),
                    "retained_in_capsule": (
                        str(narration_deliveries[-1].get("display_text") or "")
                        in list(capsule.get("delivered_narration") or [])
                    ),
                }
            return report
    finally:
        if narration_callback is not None:
            bus.off(Method.AUIP_UPDATED, narration_callback)
        if narration is not None:
            await narration.close()
        await engagement.close()
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=5.0)
        listener.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--real-narration", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run_simulation(real_narration=args.real_narration))
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
