"""The user's AI application interaction switch withdraws only model participation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from config import settings
from server.auip_contract import AUIP_SCHEMA, AuipProtocolError
from server.auip_engagement import AuipEngagementCoordinator
from server.auip_narration import AuipNarrationAdapter, AuipNarrationProfile
from server.auip_runtime import AuipRuntime
from server.event_bus import bus
from server.handlers.auip_handler import AuipHandler
from server.handlers.system_handler import SystemHandler
from server.protocol import Method


def _approve(_context: dict) -> dict:
    return {"decision": "approve", "reason": "test policy"}


def _manifest(*, stances: tuple[str, ...] = ("spectator", "participant")) -> dict:
    return {
        "schema": AUIP_SCHEMA,
        "app": {"id": "turn-game", "title": "Turn Game", "version": "0.1.0"},
        "events": {
            "game.turn_ready": {"participantOpportunity": True},
            "game.move_committed": {"beat": True},
        },
        "actions": {
            "game.move": {
                "description": "Make one legal move.",
                "risk": "local_execution",
            }
        },
        "stances": list(stances),
    }


def _controller_manifest() -> dict:
    manifest = _manifest()
    manifest["actions"]["game.set_policy"] = {
        "description": "Set the exact automatic play policy.",
        "risk": "local_execution",
        "inputSchema": {
            "type": "object",
            "properties": {"style": {"type": "string"}},
            "required": ["style"],
            "additionalProperties": False,
        },
    }
    manifest["situationKinds"] = ["controller/v1"]
    manifest["controller"] = {
        "policyActions": ["game.set_policy"],
        "leaseDurationMs": 30_000,
        "maxActionRateHz": 10,
        "takeover": "immediate",
    }
    return manifest


def _controller_state(status: str, policy_revision: object = None) -> dict:
    return {
        "controller": {
            "kind": "controller/v1",
            "status": status,
            "policyRevision": policy_revision,
            "policyAction": "game.set_policy" if policy_revision else None,
            "policySummary": "Play defensively" if policy_revision else "",
        }
    }


def _registered(runtime: AuipRuntime, manifest: dict, conversation: str) -> dict:
    registered = runtime.register(manifest=manifest, conversation_id=conversation)
    runtime.publish_state(
        app_session_id=registered["app_session_id"],
        bridge_token=registered["bridge_token"],
        revision=1,
        state={"turn": "kurisu"},
    )
    return registered


def _turn_ready(runtime: AuipRuntime, registered: dict, event_id: str) -> dict:
    return runtime.publish_event(
        app_session_id=registered["app_session_id"],
        bridge_token=registered["bridge_token"],
        event_id=event_id,
        type="game.turn_ready",
        actor="app",
        revision=1,
        payload={},
    )


class _Launch:
    def __init__(self) -> None:
        self.retired = 0

    def cancel_all_deferred(self) -> int:
        self.retired += 1
        return 1


def test_turning_off_returns_live_apps_to_observe_and_revokes_the_controller(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = runtime.register(
            manifest=_controller_manifest(),
            conversation_id="controller-off",
        )
        sid, token = registered["app_session_id"], registered["bridge_token"]
        runtime.set_engagement_mode(app_session_id=sid, mode="delegate")
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state=_controller_state("idle"),
        )
        action = runtime.invoke_action(
            app_session_id=sid,
            actor="kurisu",
            type="game.set_policy",
            payload={"style": "defensive"},
            expected_revision=1,
        )["action"]
        runtime.resolve_action(
            app_session_id=sid,
            bridge_token=token,
            action_id=action["action_id"],
            accepted=True,
            resulting_revision=2,
            state=_controller_state(
                "active", action["controller_lease"]["policy_revision"]
            ),
        )
        launch = _Launch()
        host = AuipHandler(
            runtime,
            current_session_id=lambda: "controller-off",
            engagement=AuipEngagementCoordinator(
                app_runtime=runtime,
                controller=lambda _context: {"action": "wait"},
                role_authorizer=_approve,
            ),
            launch=launch,
        )
        revokes: list[dict] = []
        updates: list[dict] = []

        async def capture_revoke(_method: str, payload: dict) -> None:
            revokes.append(payload)

        async def capture_update(_method: str, payload: dict) -> None:
            updates.append(payload)

        monkeypatch.setattr(settings, "AUIP_INTERACTION_ENABLED", False)
        bus.on(Method.AUIP_CONTROLLER_REVOKE_REQUESTED, capture_revoke)
        bus.on(Method.AUIP_UPDATED, capture_update)
        try:
            await host.suspend_interaction()
        finally:
            bus.off(Method.AUIP_CONTROLLER_REVOKE_REQUESTED, capture_revoke)
            bus.off(Method.AUIP_UPDATED, capture_update)

        projection = runtime.get(sid)
        assert projection["engagement_mode"] == "observe"
        assert projection["controller"]["status"] == "stopping"
        assert [item["revoke"]["lease_id"] for item in revokes] == [
            action["controller_lease"]["lease_id"]
        ]
        assert [item["app_session_id"] for item in updates] == [sid]
        assert launch.retired == 1
        # Suspension is idempotent: a second pass finds nothing left to change.
        await host.suspend_interaction()
        assert runtime.get(sid)["decision_generation"] == projection[
            "decision_generation"
        ]
        await host.engagement.close()

    asyncio.run(scenario())


def test_turning_off_keeps_an_already_sent_action_and_its_receipt(monkeypatch) -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, _manifest(), "sent-action")
        sid, token = registered["app_session_id"], registered["bridge_token"]
        runtime.set_engagement_mode(app_session_id=sid, mode="collaborate")
        action = runtime.invoke_action(
            app_session_id=sid,
            actor="kurisu",
            type="game.move",
            payload={},
            expected_revision=1,
        )["action"]
        host = AuipHandler(
            runtime,
            engagement=AuipEngagementCoordinator(
                app_runtime=runtime,
                controller=lambda _context: {"action": "wait"},
                role_authorizer=_approve,
            ),
        )

        monkeypatch.setattr(settings, "AUIP_INTERACTION_ENABLED", False)
        await host.suspend_interaction()

        projection = runtime.get(sid)
        assert projection["engagement_mode"] == "observe"
        assert projection["pending_action"]["action_id"] == action["action_id"]
        assert projection["operator_status"] == "awaiting_receipt"
        runtime.resolve_action(
            app_session_id=sid,
            bridge_token=token,
            action_id=action["action_id"],
            accepted=True,
            resulting_revision=2,
            state={"turn": "user"},
        )
        settled = runtime.get(sid)
        assert settled["pending_action"] is None
        assert settled["latest_verified_self_action"]["action_id"] == action["action_id"]
        await host.engagement.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "stances",
    [("spectator", "participant"), ("participant",)],
    ids=["observable", "participant-only"],
)
def test_turning_off_cancels_a_private_participant_decision(monkeypatch, stances) -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, _manifest(stances=stances), "private-decision")
        sid = registered["app_session_id"]
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def controller(_context: dict) -> dict:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("unreachable")

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
        )
        host = AuipHandler(runtime, engagement=engagement)
        engagement.request_step(app_session_id=sid, instruction="Your move.")
        await asyncio.wait_for(started.wait(), timeout=1.0)

        monkeypatch.setattr(settings, "AUIP_INTERACTION_ENABLED", False)
        await host.suspend_interaction()
        await asyncio.wait_for(cancelled.wait(), timeout=1.0)
        await engagement.wait_for_idle(sid)

        projection = runtime.get(sid)
        assert projection["pending_action"] is None
        assert projection["latest_verified_self_action"] is None
        expected_mode = "observe" if "spectator" in stances else "collaborate"
        assert projection["engagement_mode"] == expected_mode
        await engagement.close()

    asyncio.run(scenario())


def test_off_offers_no_participant_turn_and_on_restores_exactly_one(monkeypatch) -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, _manifest(), "gated-turns")
        sid = registered["app_session_id"]
        calls: list[dict] = []

        async def controller(context: dict) -> dict:
            calls.append(context)
            return {"action": "wait", "reason": "observed"}

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
        )
        runtime.set_engagement_mode(app_session_id=sid, mode="collaborate")

        monkeypatch.setattr(settings, "AUIP_INTERACTION_ENABLED", False)
        await engagement.on_update(Method.AUIP_UPDATED, _turn_ready(runtime, registered, "turn-off"))
        for request in (
            lambda: engagement.request_step(app_session_id=sid, instruction="Move."),
            lambda: engagement.set_mode(app_session_id=sid, mode="collaborate"),
            lambda: engagement.set_mode(app_session_id=sid, mode="delegate"),
        ):
            with pytest.raises(AuipProtocolError) as refused:
                result = request()
                if asyncio.iscoroutine(result):
                    await result
            assert refused.value.code == "auip_interaction_disabled"
        observed = await engagement.set_mode(app_session_id=sid, mode="observe")
        assert observed["engagement_mode"] == "observe"
        await asyncio.sleep(0)
        assert calls == []
        assert runtime.get(sid)["operator_status"] == "idle"

        monkeypatch.setattr(settings, "AUIP_INTERACTION_ENABLED", True)
        await engagement.set_mode(app_session_id=sid, mode="collaborate")
        await engagement.on_update(Method.AUIP_UPDATED, _turn_ready(runtime, registered, "turn-on"))
        await engagement.wait_for_idle(sid)
        assert len(calls) == 1
        await engagement.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
def test_participant_attach_ticket_opens_as_spectator_while_off(monkeypatch, enabled) -> None:
    runtime = AuipRuntime()
    ticket = runtime.issue_attach_ticket(
        conversation_id="late-ticket",
        artifact_ref="artifact:game@digest",
        engagement_mode="collaborate",
    )
    monkeypatch.setattr(settings, "AUIP_INTERACTION_ENABLED", enabled)
    registered = runtime.register_attached(
        manifest=_manifest(),
        attach_ticket=ticket["attach_ticket"],
    )
    assert registered["engagement_mode"] == ("collaborate" if enabled else "observe")
    # An app that cannot spectate still opens in its only mode; gates keep it quiet.
    participant_only = runtime.issue_attach_ticket(
        conversation_id="late-ticket",
        artifact_ref="artifact:game@digest",
        engagement_mode="collaborate",
    )
    assert runtime.register_attached(
        manifest=_manifest(stances=("participant",)),
        attach_ticket=participant_only["attach_ticket"],
    )["engagement_mode"] == "collaborate"


def test_control_captured_before_off_is_refused_without_side_effects(monkeypatch) -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, _manifest(), "late-control")
        sid = registered["app_session_id"]
        runtime.focus(conversation_id="late-control", app_session_id=sid)
        launch = AsyncMock()
        host = AuipHandler(
            runtime,
            current_session_id=lambda: "late-control",
            engagement=AuipEngagementCoordinator(
                app_runtime=runtime,
                controller=lambda _context: {"action": "wait"},
                role_authorizer=_approve,
            ),
            launch=launch,
        )

        monkeypatch.setattr(settings, "AUIP_INTERACTION_ENABLED", False)
        for attrs in (
            {"action": "collaborate", "_host_app_session_id": sid},
            {"action": "step", "_host_app_session_id": sid},
            {"action": "launch", "target": "2048", "mode": "collaborate"},
        ):
            routed = await host.route_control(
                attrs, session_id="late-control", user_text="一緒に遊ぼう", turn_id="late"
            )
            assert routed == {"ok": False, "error": "auip_interaction_disabled"}
        assert launch.route_control.await_count == 0
        projection = runtime.get(sid)
        assert projection["engagement_mode"] == "observe"
        assert projection["operator_status"] == "idle"
        await host.engagement.close()

    asyncio.run(scenario())


def test_off_skips_narration_and_on_resumes_it(monkeypatch) -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        manifest = _manifest(stances=("spectator",))
        del manifest["events"]["game.turn_ready"]
        registered = runtime.register(manifest=manifest, conversation_id="narration")
        sid, token = registered["app_session_id"], registered["bridge_token"]
        observed: list[str] = []

        async def observer(payload: dict) -> dict:
            observed.append(str(payload["event"]["event_id"]))
            return {"action": "silent", "fact_brief": "move"}

        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=observer,
            narrator=lambda _payload: {},
            sink=lambda _payload: {"status": "queued"},
            profile=AuipNarrationProfile(normal_beat_stride=1),
        )

        def move(revision: int) -> dict:
            runtime.publish_state(
                app_session_id=sid,
                bridge_token=token,
                revision=revision,
                state={"moves": revision},
            )
            return runtime.publish_event(
                app_session_id=sid,
                bridge_token=token,
                event_id=f"move-{revision}",
                type="game.move_committed",
                actor="user",
                revision=revision,
                payload={"move": revision},
            )

        monkeypatch.setattr(settings, "AUIP_INTERACTION_ENABLED", False)
        await adapter.enqueue_update(Method.AUIP_UPDATED, move(1))
        await adapter.wait_for_idle()
        assert observed == []

        monkeypatch.setattr(settings, "AUIP_INTERACTION_ENABLED", True)
        await adapter.enqueue_update(Method.AUIP_UPDATED, move(2))
        await adapter.wait_for_idle()
        assert observed == ["move-2"]
        await adapter.close()

    asyncio.run(scenario())


def test_live_setting_is_strict_and_only_turning_off_suspends(monkeypatch) -> None:
    async def scenario() -> None:
        suspend = AsyncMock()
        handler = SystemHandler()
        handler.configure(suspend_auip_interaction=suspend)
        monkeypatch.setattr(settings, "AUIP_INTERACTION_ENABLED", True)
        monkeypatch.setattr(handler, "_get_config", AsyncMock(return_value={}))
        monkeypatch.setattr("server.handlers.system_handler.bus.emit", AsyncMock())

        with pytest.raises(ValueError, match="boolean"):
            await handler._set_config({"values": {"auip_interaction_enabled": "false"}})
        assert settings.AUIP_INTERACTION_ENABLED is True
        assert suspend.await_count == 0

        result = await handler._set_config({"values": {"auip_interaction_enabled": False}})
        assert settings.AUIP_INTERACTION_ENABLED is False
        assert "auip_interaction_enabled" in result["updated"]
        assert suspend.await_count == 1

        await handler._set_config({"values": {"auip_interaction_enabled": True}})
        assert settings.AUIP_INTERACTION_ENABLED is True
        assert suspend.await_count == 1

    asyncio.run(scenario())


def test_existing_installations_keep_interaction_on_by_default() -> None:
    from config.catalog import application_policy, configuration_field

    field = configuration_field("AUIP_INTERACTION_ENABLED")
    assert field["default"] is True
    assert field["runtime_key"] == "auip_interaction_enabled"
    assert application_policy("AUIP_INTERACTION_ENABLED") == "host"
