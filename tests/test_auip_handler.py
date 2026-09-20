from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.auip_app_connection import (
    APP_METHODS,
    AuipAppConnectionManager,
    AuipAppRequestHandler,
)
from server.auip_app_launcher import parse_app_launch_url
from server.auip_contract import AUIP_SCHEMA, AuipProtocolError
from server.auip_engagement import AuipEngagementCoordinator
from server.auip_runtime import AuipRuntime
from server.event_bus import bus
from server.handlers.auip_handler import AuipHandler
from server.protocol import Method


def _manifest() -> dict:
    return {
        "schema": AUIP_SCHEMA,
        "app": {"id": "counter", "title": "Counter", "version": "0.1.0"},
        "events": {"counter.changed": {"beat": True}},
        "actions": {
            "counter.increment": {
                "description": "Increment the counter.",
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
    metadata: dict[str, Any]


class _Store:
    def __init__(self, root: Path, *, aliased_paths: bool = False) -> None:
        workspace = root / "workspace"
        workspace.mkdir()
        entry = workspace / "counter.html"
        entry.write_text("<!doctype html><title>Counter</title>", encoding="utf-8")
        manifest = workspace / "auip.manifest.json"
        manifest.write_text(json.dumps(_manifest()), encoding="utf-8")
        record_root = workspace
        if aliased_paths:
            alias = workspace / "alias"
            alias.mkdir()
            record_root = alias / ".."
        self.artifact = _Artifact(
            artifact_id="artifact-1",
            work_item_id="work-1",
            attempt_id="attempt-1",
            kind="business.file",
            title="Counter",
            path=str(record_root / entry.name),
            status="registered",
            sha256=hashlib.sha256(entry.read_bytes()).hexdigest(),
        )
        self.manifest_artifact = _Artifact(
            artifact_id="artifact-manifest",
            work_item_id="work-1",
            attempt_id="attempt-1",
            kind="business.file",
            title="AUIP manifest",
            path=str(record_root / manifest.name),
            status="registered",
            sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        )
        self.item = _WorkItem(workspace_path=str(workspace))
        self.attempt = _Attempt(attempt_id="attempt-1", metadata={})

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


class _FakeWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def iter_text(self):
        while True:
            item = await self.incoming.get()
            if item is None:
                return
            yield item

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def request(self, request_id: str, method: str, params: dict) -> None:
        await self.incoming.put(
            json.dumps(
                {"type": "req", "id": request_id, "method": method, "params": params}
            )
        )


def test_host_prepare_binds_the_current_session_and_app_registration_cannot_replace_it() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        handoffs: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as tmp:
            host = AuipHandler(
                runtime,
                artifacts=_Store(Path(tmp)),
                current_session_id=lambda: "host-session",
                preview_handoff=lambda source: handoffs.append(dict(source)),
            )
            prepared = await host.handle(Method.AUIP_ATTACH_PREPARE, {"artifact_id": "artifact-1"})
        assert prepared and prepared["entry_path"].endswith("counter.html")
        assert len(handoffs) == 1
        assert handoffs[0]["artifact_ref"] == prepared["artifact_ref"]
        assert handoffs[0]["work_item_id"] == "work-1"
        assert handoffs[0]["attempt_id"] == "attempt-1"
        assert handoffs[0]["host_surface_id"] == prepared["host_surface_id"]
        assert "html" not in prepared
        launch = parse_app_launch_url(prepared["launch_url"])
        assert launch["webSocketUrl"] == "ws://127.0.0.1:17777/auip/ws"
        assert launch["attachTicket"] == prepared["attach_ticket"]

        app = AuipAppRequestHandler(runtime)
        registered = await app.handle(
            Method.AUIP_REGISTER,
            {
                "manifest": _manifest(),
                "attach_ticket": prepared["attach_ticket"],
                "conversation_id": "client-forgery",
                "artifact_ref": "artifact:forged",
            },
        )
        assert registered["ok"] is True
        assert registered["conversation_id"] == "host-session"
        assert registered["artifact_ref"] == prepared["artifact_ref"]

        replay = await AuipAppRequestHandler(runtime).handle(
            Method.AUIP_REGISTER,
            {"manifest": _manifest(), "attach_ticket": prepared["attach_ticket"]},
        )
        assert replay["error"] == "invalid_attach_ticket"

    asyncio.run(scenario())


def test_preview_handoff_failure_does_not_weaken_validated_auip_prepare() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()

        def fail_preview(_source: dict[str, Any]) -> None:
            raise RuntimeError("optional preview unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            host = AuipHandler(
                runtime,
                artifacts=_Store(Path(tmp)),
                current_session_id=lambda: "host-session",
                preview_handoff=fail_preview,
            )
            prepared = await host.handle(
                Method.AUIP_ATTACH_PREPARE,
                {"artifact_id": "artifact-1"},
            )
        assert prepared and prepared["ok"] is True
        assert prepared["attach_ticket"]

    asyncio.run(scenario())


def test_prepare_treats_two_spellings_of_the_same_bundle_as_one_location() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        with tempfile.TemporaryDirectory() as tmp:
            store = _Store(Path(tmp), aliased_paths=True)
            host = AuipHandler(
                runtime,
                artifacts=store,
                current_session_id=lambda: "host-session",
            )
            prepared = await host.handle(
                Method.AUIP_ATTACH_PREPARE,
                {"artifact_id": store.artifact.artifact_id},
            )

        assert prepared and prepared["ok"] is True
        assert prepared["entry_path"].endswith("counter.html")

    asyncio.run(scenario())


def test_host_and_app_methods_are_separate_and_receipts_reconcile_the_same_session() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        ticket = runtime.issue_attach_ticket(
            conversation_id="chat-counter",
            artifact_ref="artifact:a@1234",
        )
        app = AuipAppRequestHandler(runtime)
        registered = await app.handle(
            Method.AUIP_REGISTER,
            {"manifest": _manifest(), **ticket},
        )
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        assert Method.AUIP_ACTION_INVOKE not in APP_METHODS
        refused = await app.handle(Method.AUIP_ACTION_INVOKE, {})
        assert refused == {
            "ok": False,
            "error": "unknown_method",
            "detail": Method.AUIP_ACTION_INVOKE,
        }
        assert (await app.handle(Method.WORK_LIST, {}))["error"] == "unknown_method"

        await app.handle(
            Method.AUIP_STATE_PUBLISH,
            {
                "app_session_id": sid,
                "bridge_token": token,
                "revision": 1,
                "state": {"value": 0},
            },
        )
        host = AuipHandler(runtime, current_session_id=lambda: "chat-counter")
        await host.handle(Method.AUIP_STANCE_SET, {"app_session_id": sid, "stance": "participant"})
        actions: list[dict] = []

        async def capture(_method: str, payload: dict) -> None:
            actions.append(payload)

        bus.on(Method.AUIP_ACTION_REQUESTED, capture)
        try:
            invoked = await host.handle(
                Method.AUIP_ACTION_INVOKE,
                {
                    "app_session_id": sid,
                    "actor": "kurisu",
                    "action_type": "counter.increment",
                    "payload": {"amount": 1},
                    "expected_revision": 1,
                },
            )
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)
        assert invoked and actions[-1]["app_session_id"] == sid

        resolved = await app.handle(
            Method.AUIP_ACTION_RESULT,
            {
                "app_session_id": sid,
                "bridge_token": token,
                "action_id": invoked["action"]["action_id"],
                "accepted": True,
                "resulting_revision": 2,
                "state": {"value": 1},
                "effects": {"value": 1},
            },
        )
        assert resolved["latest_verified_self_action"]["accepted"] is True

    asyncio.run(scenario())


def test_app_connection_is_one_session_and_disconnect_is_visible() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        first_ticket = runtime.issue_attach_ticket(
            conversation_id="chat-a",
            artifact_ref="artifact:a@1",
        )
        second_ticket = runtime.issue_attach_ticket(
            conversation_id="chat-b",
            artifact_ref="artifact:b@1",
        )
        first = AuipAppRequestHandler(runtime)
        second = AuipAppRequestHandler(runtime)
        first_registered = await first.handle(
            Method.AUIP_REGISTER, {"manifest": _manifest(), **first_ticket}
        )
        second_registered = await second.handle(
            Method.AUIP_REGISTER, {"manifest": _manifest(), **second_ticket}
        )
        assert first.accepts_action_event(
            {"app_session_id": first_registered["app_session_id"]}
        )
        assert not first.accepts_action_event(
            {"app_session_id": second_registered["app_session_id"]}
        )
        mismatch = await first.handle(
            Method.AUIP_STATE_PUBLISH,
            {
                "app_session_id": second_registered["app_session_id"],
                "bridge_token": first_registered["bridge_token"],
                "revision": 1,
                "state": {},
            },
        )
        assert mismatch["error"] == "connection_session_mismatch"

        await first.disconnect()
        snapshot = runtime.get(first_registered["app_session_id"])
        assert snapshot["status"] == "disconnected"
        assert snapshot["experience_capsule"]["close_reason"] == "connection_lost"

        await second.handle(
            Method.AUIP_SESSION_CLOSE,
            {
                "app_session_id": second_registered["app_session_id"],
                "bridge_token": second_registered["bridge_token"],
                "reason": "finished",
            },
        )
        await second.disconnect()
        assert runtime.get(second_registered["app_session_id"])["status"] == "closed"

    asyncio.run(scenario())


def test_connection_manager_forwards_only_the_bound_session_action() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        manager = AuipAppConnectionManager(runtime)
        sockets = [_FakeWebSocket(), _FakeWebSocket()]
        tasks = [asyncio.create_task(manager.handle_connection(socket)) for socket in sockets]
        tickets = [
            runtime.issue_attach_ticket(
                conversation_id=f"chat-{index}",
                artifact_ref=f"artifact:{index}@1",
            )
            for index in range(2)
        ]
        for index, socket in enumerate(sockets):
            await socket.request(
                f"register-{index}",
                Method.AUIP_REGISTER,
                {"manifest": _manifest(), **tickets[index]},
            )
        for _ in range(100):
            if all(socket.sent for socket in sockets):
                break
            await asyncio.sleep(0)
        session_ids = [socket.sent[0]["params"]["app_session_id"] for socket in sockets]
        await bus.emit(
            Method.AUIP_ACTION_REQUESTED,
            {"app_session_id": session_ids[0], "action": {"action_id": "action-1"}},
        )
        assert any(item.get("type") == "evt" for item in sockets[0].sent)
        assert not any(item.get("type") == "evt" for item in sockets[1].sent)
        await bus.emit(
            Method.AUIP_CONTROLLER_REVOKE_REQUESTED,
            {
                "app_session_id": session_ids[0],
                "revoke": {"lease_id": "lease-1", "generation": 1},
            },
        )
        assert any(
            item.get("method") == Method.AUIP_CONTROLLER_REVOKE_REQUESTED
            for item in sockets[0].sent
        )
        assert not any(
            item.get("method") == Method.AUIP_CONTROLLER_REVOKE_REQUESTED
            for item in sockets[1].sent
        )

        for socket in sockets:
            await socket.incoming.put(None)
        await asyncio.gather(*tasks)
        assert all(runtime.get(session_id)["status"] == "disconnected" for session_id in session_ids)

    asyncio.run(scenario())


def test_host_mode_change_emits_the_current_controller_revoke_envelope() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        manifest = _manifest()
        manifest["actions"]["counter.set_policy"] = {
            "description": "Set the exact counter response policy.",
            "risk": "local_execution",
            "inputSchema": {
                "type": "object",
                "properties": {"whenAbove": {"type": "integer"}},
                "required": ["whenAbove"],
                "additionalProperties": False,
            },
        }
        manifest["situationKinds"] = ["controller/v1"]
        manifest["controller"] = {
            "policyActions": ["counter.set_policy"],
            "leaseDurationMs": 30_000,
            "maxActionRateHz": 10,
            "takeover": "immediate",
        }
        registered = runtime.register(
            manifest=manifest,
            conversation_id="controller-mode",
        )
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.set_engagement_mode(app_session_id=sid, mode="collaborate")
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={
                "controller": {
                    "kind": "controller/v1",
                    "status": "idle",
                    "policyRevision": None,
                    "policyAction": None,
                    "policySummary": "",
                }
            },
        )
        action = runtime.invoke_action(
            app_session_id=sid,
            actor="kurisu",
            type="counter.set_policy",
            payload={"whenAbove": 5},
            expected_revision=1,
        )["action"]
        runtime.resolve_action(
            app_session_id=sid,
            bridge_token=token,
            action_id=action["action_id"],
            accepted=True,
            resulting_revision=2,
            state={
                "controller": {
                    "kind": "controller/v1",
                    "status": "active",
                    "policyRevision": action["controller_lease"]["policy_revision"],
                    "policyAction": "counter.set_policy",
                    "policySummary": "Respond when above five",
                }
            },
        )
        host = AuipHandler(runtime, current_session_id=lambda: "controller-mode")
        revokes: list[dict] = []

        async def capture(_method: str, payload: dict) -> None:
            revokes.append(payload)

        bus.on(Method.AUIP_CONTROLLER_REVOKE_REQUESTED, capture)
        try:
            observed = await host.handle(
                Method.AUIP_MODE_SET,
                {"app_session_id": sid, "mode": "observe"},
            )
        finally:
            bus.off(Method.AUIP_CONTROLLER_REVOKE_REQUESTED, capture)
        assert observed and observed["controller"]["status"] == "stopping"
        assert len(revokes) == 1
        assert revokes[0]["app_session_id"] == sid
        assert revokes[0]["revoke"]["lease_id"] == action["controller_lease"][
            "lease_id"
        ]

    asyncio.run(scenario())


def test_canonical_natural_control_routes_one_step_to_the_focused_appsession() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        app_handler = AuipAppRequestHandler(runtime)
        requested: list[dict[str, Any]] = []

        async def participant(_context: dict[str, Any]) -> dict[str, Any]:
            return {
                "action": "act",
                "type": "counter.increment",
                "payload": {},
            }

        async def capture(_method: str, payload: dict[str, Any]) -> None:
            requested.append(payload)

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=participant,
            role_authorizer=lambda _context: {
                "decision": "approve",
                "reason": "handler policy",
            },
        )
        handler = AuipHandler(
            runtime,
            current_session_id=lambda: "chat-natural-control",
            engagement=engagement,
        )
        ticket = runtime.issue_attach_ticket(
            conversation_id="chat-natural-control",
            artifact_ref="artifact:counter@1",
        )
        registered = await app_handler.handle(
            Method.AUIP_REGISTER,
            {"manifest": _manifest(), **ticket},
        )
        sid = str(registered["app_session_id"])
        await app_handler.handle(
            Method.AUIP_STATE_PUBLISH,
            {
                "app_session_id": sid,
                "bridge_token": registered["bridge_token"],
                "revision": 1,
                "state": {"count": 0},
            },
        )
        bus.on(Method.AUIP_ACTION_REQUESTED, capture)
        try:
            routed = await handler.route_control(
                {"action": "step", "instruction": "increase it once"},
                session_id="chat-natural-control",
                user_text="你来操作一次。",
                turn_id="turn-natural-control",
            )
            assert routed and routed["ok"] is True and routed["scheduled"] is True
            await engagement.wait_for_idle(sid)
            assert len(requested) == 1
            assert requested[0]["app_session_id"] == sid
            assert requested[0]["action"]["type"] == "counter.increment"

            read_only = await handler.route_control(
                {"action": "none"},
                session_id="chat-natural-control",
                user_text="刚才操作了吗？",
                turn_id="turn-natural-status",
            )
            assert read_only == {"ok": True, "action": "none"}
            assert len(requested) == 1
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)
            await engagement.close()

    asyncio.run(scenario())


def test_host_leave_closes_the_owned_surface_only_after_a_trusted_receipt() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        app_handler = AuipAppRequestHandler(runtime)
        host = AuipHandler(runtime, current_session_id=lambda: "surface-chat")
        requested: list[dict] = []

        async def capture(_method: str, payload: dict) -> None:
            requested.append(payload)

        ticket = runtime.issue_attach_ticket(
            conversation_id="surface-chat",
            artifact_ref="artifact:surface@1",
            host_surface_id="surface-owned-1",
        )
        registered = await app_handler.handle(
            Method.AUIP_REGISTER,
            {"manifest": _manifest(), **ticket},
        )
        sid = str(registered["app_session_id"])
        bus.on(Method.AUIP_SURFACE_CLOSE_REQUESTED, capture)
        try:
            left = await host.handle(
                Method.AUIP_LEAVE,
                {"app_session_id": sid},
            )
            assert left and left["status"] == "closed"
            assert left["surface_close_status"] == "pending"
            assert left["external_process_stopped"] is False
            assert requested == [
                {
                    "app_session_id": sid,
                    "host_surface_id": "surface-owned-1",
                }
            ]

            closed = await host.handle(
                Method.AUIP_SURFACE_CLOSE_RESULT,
                {
                    "app_session_id": sid,
                    "host_surface_id": "surface-owned-1",
                    "status": "closed",
                },
            )
            assert closed and closed["host_surface_closed"] is True
            assert closed["external_process_stopped"] is False
            assert runtime.get(sid)["experience_capsule"][
                "surface_close_status"
            ] == "closed"
            assert runtime.get(sid)["experience_capsule"]["close_reason"] == "user_left"
        finally:
            bus.off(Method.AUIP_SURFACE_CLOSE_REQUESTED, capture)

    asyncio.run(scenario())


def test_natural_control_can_close_a_completed_owned_surface() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        app_handler = AuipAppRequestHandler(runtime)
        host = AuipHandler(runtime, current_session_id=lambda: "completed-surface-chat")
        requested: list[dict] = []

        async def capture(_method: str, payload: dict) -> None:
            requested.append(payload)

        manifest = _manifest()
        manifest["events"]["app.completed"] = {"beat": True, "terminal": True}
        ticket = runtime.issue_attach_ticket(
            conversation_id="completed-surface-chat",
            artifact_ref="artifact:completed-surface@1",
            host_surface_id="surface-completed-1",
        )
        registered = await app_handler.handle(
            Method.AUIP_REGISTER,
            {"manifest": manifest, **ticket},
        )
        sid = str(registered["app_session_id"])
        token = str(registered["bridge_token"])
        await app_handler.handle(
            Method.AUIP_STATE_PUBLISH,
            {
                "app_session_id": sid,
                "bridge_token": token,
                "revision": 1,
                "state": {"count": 1},
            },
        )
        completed = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="completed-result",
            type="app.completed",
            actor="app",
            revision=1,
            payload={"result": "done"},
        )
        assert completed["status"] == "completed"

        bus.on(Method.AUIP_SURFACE_CLOSE_REQUESTED, capture)
        try:
            left = await host.route_control(
                {"action": "leave"},
                session_id="completed-surface-chat",
                user_text="好了，现在把它关掉吧。",
                turn_id="turn-close-completed-surface",
            )
            assert left and left["status"] == "closed"
            assert left["surface_close_status"] == "pending"
            assert requested == [
                {
                    "app_session_id": sid,
                    "host_surface_id": "surface-completed-1",
                }
            ]
        finally:
            bus.off(Method.AUIP_SURFACE_CLOSE_REQUESTED, capture)

    asyncio.run(scenario())


def test_deferred_active_app_replacement_reserves_new_launch_then_closes_old_surface() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        app_handler = AuipAppRequestHandler(runtime)
        routed: list[dict[str, Any]] = []
        close_requests: list[dict[str, Any]] = []

        class Launch:
            async def route_control(self, attrs, **kwargs):
                routed.append({"attrs": dict(attrs), **kwargs})
                return {
                    "ok": True,
                    "deferred": True,
                    "turn_id": kwargs["turn_id"],
                }

        host = AuipHandler(
            runtime,
            current_session_id=lambda: "replacement-chat",
            launch=Launch(),
        )
        ticket = runtime.issue_attach_ticket(
            conversation_id="replacement-chat",
            artifact_ref="artifact:replacement@1",
            host_surface_id="replacement-surface-old",
        )
        registered = await app_handler.handle(
            Method.AUIP_REGISTER,
            {"manifest": _manifest(), **ticket},
        )
        sid = str(registered["app_session_id"])

        async def capture_close(_method: str, payload: dict[str, Any]) -> None:
            close_requests.append(dict(payload))

        bus.on(Method.AUIP_SURFACE_CLOSE_REQUESTED, capture_close)
        try:
            result = await host.route_control(
                {
                    "action": "launch",
                    "target": "delivery",
                    "mode": "collaborate",
                    "after": "work",
                    "_host_app_session_id": sid,
                },
                session_id="replacement-chat",
                user_text="改好后重新打开",
                turn_id="turn-replace-active-app",
            )

            assert result == {
                "ok": True,
                "deferred": True,
                "turn_id": "turn-replace-active-app",
            }
            assert len(routed) == 1
            assert runtime.get(sid)["status"] == "closed"
            assert runtime.get(sid)["surface_close_status"] == "pending"
            assert close_requests == [
                {
                    "app_session_id": sid,
                    "host_surface_id": "replacement-surface-old",
                }
            ]
        finally:
            bus.off(Method.AUIP_SURFACE_CLOSE_REQUESTED, capture_close)

    asyncio.run(scenario())


def test_stale_active_app_replacement_is_rejected_before_launch_reservation() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        app_handler = AuipAppRequestHandler(runtime)
        routed: list[dict[str, Any]] = []

        class Launch:
            async def route_control(self, attrs, **kwargs):
                routed.append({"attrs": dict(attrs), **kwargs})
                return {"ok": True, "deferred": True}

        host = AuipHandler(
            runtime,
            current_session_id=lambda: "replacement-stale-chat",
            launch=Launch(),
        )
        ticket = runtime.issue_attach_ticket(
            conversation_id="replacement-stale-chat",
            artifact_ref="artifact:replacement@2",
        )
        registered = await app_handler.handle(
            Method.AUIP_REGISTER,
            {"manifest": _manifest(), **ticket},
        )
        sid = str(registered["app_session_id"])

        try:
            await host.route_control(
                {
                    "action": "launch",
                    "target": "delivery",
                    "mode": "collaborate",
                    "after": "work",
                    "_host_app_session_id": "app-stale",
                },
                session_id="replacement-stale-chat",
                user_text="改好后重新打开",
                turn_id="turn-stale-replacement",
            )
        except AuipProtocolError as exc:
            assert exc.code == "app_session_changed"
        else:
            raise AssertionError("a stale AppSession must not reserve deferred launch")

        assert routed == []
        assert runtime.get(sid)["status"] == "active"

    asyncio.run(scenario())


def _main() -> None:
    test_host_prepare_binds_the_current_session_and_app_registration_cannot_replace_it()
    test_host_and_app_methods_are_separate_and_receipts_reconcile_the_same_session()
    test_app_connection_is_one_session_and_disconnect_is_visible()
    test_connection_manager_forwards_only_the_bound_session_action()
    test_canonical_natural_control_routes_one_step_to_the_focused_appsession()
    test_host_leave_closes_the_owned_surface_only_after_a_trusted_receipt()
    test_natural_control_can_close_a_completed_owned_surface()
    print("ok: AUIP external apps attach through one restricted host-owned session")


if __name__ == "__main__":
    _main()
