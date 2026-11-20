"""The user's AI application interaction switch withdraws only model participation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import settings
from server.auip_contract import AUIP_SCHEMA, AuipProtocolError
from server.auip_engagement import AuipEngagementCoordinator
from server.auip_launch import AuipLaunchCandidate, AuipLaunchCoordinator
from server.auip_narration import AuipNarrationAdapter, AuipNarrationProfile
from server.auip_runtime import AuipRuntime
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


def _off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "AUIP_INTERACTION_ENABLED", False)


def _on(monkeypatch) -> None:
    monkeypatch.setattr(settings, "AUIP_INTERACTION_ENABLED", True)


def _launch_coordinator() -> tuple[AuipLaunchCoordinator, AsyncMock]:
    emit = AsyncMock()
    coordinator = AuipLaunchCoordinator(
        artifacts=MagicMock(),
        work_roster=MagicMock(),
        attention=MagicMock(),
        emit=emit,
    )
    coordinator._announce_failure = AsyncMock()
    return coordinator, emit


def test_off_with_no_active_session_takes_effect_at_once_and_on_restores(
    monkeypatch,
) -> None:
    runtime = AuipRuntime()
    assert runtime.ai_interaction_in_effect() is True
    _off(monkeypatch)
    assert runtime.ai_interaction_in_effect() is False
    _on(monkeypatch)
    assert runtime.ai_interaction_in_effect() is True

    # A session that is no longer active does not hold shutdown open.
    registered = _registered(runtime, _manifest(), "ended")
    runtime.disconnect(registered["app_session_id"])
    _off(monkeypatch)
    assert runtime.ai_interaction_in_effect() is False


def test_pending_off_keeps_both_sessions_usable_until_the_last_one_ends(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        first = _registered(runtime, _manifest(), "pending")
        second = _registered(runtime, _manifest(), "pending")
        calls: list[dict] = []

        async def controller(context: dict) -> dict:
            calls.append(context)
            return {"action": "wait", "reason": "observed"}

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
        )
        host = AuipHandler(
            runtime,
            current_session_id=lambda: "pending",
            engagement=engagement,
        )
        for registered in (first, second):
            runtime.set_engagement_mode(
                app_session_id=registered["app_session_id"], mode="collaborate"
            )

        _off(monkeypatch)
        # Pending off: chat evaluation and both sessions keep working.
        assert runtime.ai_interaction_in_effect() is True
        for number, registered in enumerate((first, second)):
            await engagement.on_update(
                Method.AUIP_UPDATED,
                _turn_ready(runtime, registered, f"pending-turn-{number}"),
            )
            await engagement.wait_for_idle(registered["app_session_id"])
        assert len(calls) == 2
        runtime.focus(
            conversation_id="pending", app_session_id=second["app_session_id"]
        )
        routed = await host.route_control(
            {"action": "observe", "_host_app_session_id": second["app_session_id"]},
            session_id="pending",
            user_text="見ていて",
            turn_id="pending-control",
        )
        assert routed["engagement_mode"] == "observe"

        # Ending the first leaves evaluation running; ending the second stops it.
        runtime.disconnect(first["app_session_id"])
        assert runtime.ai_interaction_in_effect() is True
        runtime.disconnect(second["app_session_id"])
        assert runtime.ai_interaction_in_effect() is False
        await engagement.close()

    asyncio.run(scenario())


def test_pending_off_refuses_a_third_session_and_deferred_launches(monkeypatch) -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        _registered(runtime, _manifest(), "third")
        _registered(runtime, _manifest(), "third")
        coordinator, emit = _launch_coordinator()
        candidate = MagicMock(spec=AuipLaunchCandidate)

        _off(monkeypatch)
        assert runtime.ai_interaction_in_effect() is True
        for attrs in (
            {"action": "launch", "target": "Turn Game", "mode": "collaborate"},
            {"action": "launch", "target": "delivery", "after": "work"},
            {"action": "launch", "_host_launch_candidates": (candidate,)},
        ):
            routed = await coordinator.route_control(
                attrs, session_id="third", turn_id="third-turn"
            )
            assert routed == {"ok": False, "error": "auip_interaction_disabled"}
        assert coordinator._deferred == {}

        # A selection, Attention card or deferred Work result created earlier.
        refused = await coordinator._emit_launch("third", candidate, "observe")
        assert refused == {"ok": False, "error": "auip_interaction_disabled"}
        assert emit.await_count == 0

        # An earlier launch request cannot mint an attach ticket either.
        coordinator._launch_requests["earlier"] = MagicMock(
            session_id="third", artifact_id="artifact-1"
        )
        assert not coordinator.authorize_prepare(
            session_id="third", request_id="earlier", artifact_id="artifact-1"
        )
        _on(monkeypatch)
        assert coordinator.authorize_prepare(
            session_id="third", request_id="earlier", artifact_id="artifact-1"
        )

    asyncio.run(scenario())


def test_switching_back_on_cancels_pending_shutdown(monkeypatch) -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, _manifest(), "cancel")
        coordinator, _emit = _launch_coordinator()
        coordinator.candidates = lambda _session_id: []

        _off(monkeypatch)
        refused = await coordinator.route_control(
            {"action": "launch", "target": "x"}, session_id="cancel", turn_id="t"
        )
        assert refused["error"] == "auip_interaction_disabled"
        _on(monkeypatch)
        runtime.disconnect(registered["app_session_id"])
        # Shutdown is no longer pending: the last session ending leaves it on.
        assert runtime.ai_interaction_in_effect() is True
        routed = await coordinator.route_control(
            {"action": "launch", "target": "x"}, session_id="cancel", turn_id="t"
        )
        assert routed == {"ok": False, "error": "no_launchable_auip_app"}

    asyncio.run(scenario())


def test_after_the_last_session_ends_participation_is_refused_and_on_restores(
    monkeypatch,
) -> None:
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

        _off(monkeypatch)
        # The session was active when the switch went off, so it still gets turns.
        await engagement.on_update(
            Method.AUIP_UPDATED, _turn_ready(runtime, registered, "turn-pending")
        )
        await engagement.wait_for_idle(sid)
        assert len(calls) == 1

        stale = _turn_ready(runtime, registered, "turn-late")
        runtime.disconnect(sid)
        await engagement.on_update(Method.AUIP_UPDATED, stale)
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
        await asyncio.sleep(0)
        assert len(calls) == 1

        _on(monkeypatch)
        assert runtime.ai_interaction_in_effect() is True
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


def test_control_decided_after_the_last_session_ended_is_refused(monkeypatch) -> None:
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

        _off(monkeypatch)
        runtime.disconnect(sid)
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
        assert runtime.get(sid)["operator_status"] == "idle"
        await host.engagement.close()

    asyncio.run(scenario())


def test_narration_continues_while_pending_off_and_stops_after_the_last_session(
    monkeypatch,
) -> None:
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

        _off(monkeypatch)
        await adapter.enqueue_update(Method.AUIP_UPDATED, move(1))
        await adapter.wait_for_idle()
        assert observed == ["move-1"]

        second = move(2)
        runtime.disconnect(sid)
        await adapter.enqueue_update(Method.AUIP_UPDATED, second)
        await adapter.wait_for_idle()
        assert observed == ["move-1"]
        await adapter.close()

    asyncio.run(scenario())


def test_live_setting_is_strict(monkeypatch) -> None:
    async def scenario() -> None:
        handler = SystemHandler()
        _on(monkeypatch)
        monkeypatch.setattr(handler, "_get_config", AsyncMock(return_value={}))
        monkeypatch.setattr("server.handlers.system_handler.bus.emit", AsyncMock())

        with pytest.raises(ValueError, match="boolean"):
            await handler._set_config({"values": {"auip_interaction_enabled": "false"}})
        assert settings.AUIP_INTERACTION_ENABLED is True

        result = await handler._set_config({"values": {"auip_interaction_enabled": False}})
        assert settings.AUIP_INTERACTION_ENABLED is False
        assert "auip_interaction_enabled" in result["updated"]

        await handler._set_config({"values": {"auip_interaction_enabled": True}})
        assert settings.AUIP_INTERACTION_ENABLED is True

    asyncio.run(scenario())


def test_existing_installations_keep_interaction_on_by_default() -> None:
    from config.catalog import application_policy, configuration_field

    field = configuration_field("AUIP_INTERACTION_ENABLED")
    assert field["default"] is True
    assert field["runtime_key"] == "auip_interaction_enabled"
    assert application_policy("AUIP_INTERACTION_ENABLED") == "host"
