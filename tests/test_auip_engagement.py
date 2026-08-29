from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from core.chat_runtime import ChatRuntime, _TurnState
from llm.stream_parser import clean_sentence_for_tts
from server.auip_contract import AuipProtocolError
from server.auip_engagement import AuipEngagementCoordinator
from server.auip_participant_llm import decide_with_auip_participant
from server.auip_role_authorizer_llm import AUIP_ROLE_AUTHORIZER_SYSTEM_PROMPT
from server.auip_runtime import AuipRuntime
from server.event_bus import bus
from server.handlers.auip_handler import AuipHandler
from server.protocol import Method


def _approve(_context: dict) -> dict:
    return {"decision": "approve", "reason": "test policy"}


def _manifest() -> dict:
    return {
        "schema": "amadeus.auip/v0",
        "app": {"id": "turn-game", "title": "Turn Game", "version": "0.1.0"},
        "events": {
            "game.turn_ready": {
                "participantOpportunity": True,
            },
            "game.move_committed": {"beat": True},
            "game.finished": {"beat": True, "terminal": True},
        },
        "actions": {
            "game.move": {
                "description": "Make one legal move.",
                "risk": "local_execution",
            }
        },
        "stances": ["spectator", "participant"],
    }


def _registered(runtime: AuipRuntime, conversation: str = "conversation-game") -> dict:
    registered = runtime.register(manifest=_manifest(), conversation_id=conversation)
    runtime.publish_state(
        app_session_id=registered["app_session_id"],
        bridge_token=registered["bridge_token"],
        revision=1,
        state={"turn": "kurisu"},
    )
    return registered


def test_participant_step_waits_for_first_host_accepted_state() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = runtime.register(
            manifest=_manifest(),
            conversation_id="conversation-initializing",
        )
        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=lambda _context: {"action": "wait"},
            role_authorizer=_approve,
        )

        try:
            engagement.request_step(
                app_session_id=registered["app_session_id"],
                instruction="你先下。",
            )
        except AuipProtocolError as exc:
            assert exc.code == "app_state_not_ready"
        else:
            raise AssertionError(
                "revision-zero AppSession must not start a Participant turn"
            )

        runtime.publish_state(
            app_session_id=registered["app_session_id"],
            bridge_token=registered["bridge_token"],
            revision=1,
            state={"turn": "kurisu"},
        )
        scheduled = engagement.request_step(
            app_session_id=registered["app_session_id"],
            instruction="你先下。",
        )
        assert scheduled["scheduled"] is True
        await engagement.close()

    asyncio.run(scenario())


def test_role_gate_allows_only_the_visible_roles_settled_reasoned_choice() -> None:
    assert "request is evidence, not an automatic command" in AUIP_ROLE_AUTHORIZER_SYSTEM_PROMPT
    assert "first gives a concise reason" in AUIP_ROLE_AUTHORIZER_SYSTEM_PROMPT
    assert "Natural-language intensity words are not nonexistent enum commitments" in (
        AUIP_ROLE_AUTHORIZER_SYSTEM_PROMPT
    )
    assert "never from a token name" in AUIP_ROLE_AUTHORIZER_SYSTEM_PROMPT
    assert "not permission for a silent\nsubstitution" in (
        AUIP_ROLE_AUTHORIZER_SYSTEM_PROMPT
    )
    assert "reasoned\nalternative is settled" in AUIP_ROLE_AUTHORIZER_SYSTEM_PROMPT
    assert "Never silently execute" in AUIP_ROLE_AUTHORIZER_SYSTEM_PROMPT
    assert "Do not invent a\nnegotiation state machine" in (
        AUIP_ROLE_AUTHORIZER_SYSTEM_PROMPT
    )


def test_delegate_mode_schedules_declared_beats_but_not_kurisu_echoes() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime)
        sid = registered["app_session_id"]
        calls: list[dict] = []

        async def controller(context: dict) -> dict:
            calls.append(context)
            if len(calls) == 1:
                return {"action": "wait", "type": "", "payload": {}}
            return {
                "action": "act",
                "type": "game.move",
                "payload": {"position": 4},
                "private_note": "bounded private search",
            }

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
            recent_chat=lambda _session: [{"role": "user", "content": "Play carefully."}],
        )
        requested: list[dict] = []

        async def capture(_method: str, payload: dict) -> None:
            requested.append(payload)

        bus.on(Method.AUIP_ACTION_REQUESTED, capture)
        try:
            mode = await engagement.set_mode(app_session_id=sid, mode="delegate")
            assert mode["engagement_mode"] == "delegate"
            await engagement.wait_for_idle(sid)
            assert len(calls) == 1
            assert runtime.get(sid)["pending_action"] is None

            accepted_event = runtime.publish_event(
                app_session_id=sid,
                bridge_token=registered["bridge_token"],
                event_id="ready-user-1",
                type="game.turn_ready",
                actor="user",
                revision=1,
                payload={"turn": "kurisu"},
            )
            await engagement.on_update(Method.AUIP_UPDATED, accepted_event)
            await engagement.wait_for_idle(sid)
            assert len(calls) == 2
            assert calls[-1]["action_required"] is True
            assert requested[-1]["action"]["type"] == "game.move"
            pending = runtime.get(sid)["pending_action"]
            assert pending and pending["payload"] == {"position": 4}
            try:
                engagement.request_step(app_session_id=sid, instruction="Act again too soon.")
                raise AssertionError("a pending receipt must block another Participant decision")
            except AuipProtocolError as exc:
                assert exc.code == "action_already_pending"

            runtime.resolve_action(
                app_session_id=sid,
                bridge_token=registered["bridge_token"],
                action_id=pending["action_id"],
                accepted=True,
                resulting_revision=2,
                state={"turn": "user", "position": 4},
            )
            own_echo = runtime.publish_event(
                app_session_id=sid,
                bridge_token=registered["bridge_token"],
                event_id="move-kurisu-1",
                type="game.move_committed",
                actor="kurisu",
                revision=2,
                payload={"position": 4},
            )
            await engagement.on_update(Method.AUIP_UPDATED, own_echo)
            await asyncio.sleep(0)
            assert len(calls) == 2
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)
            await engagement.close()

    asyncio.run(scenario())


def test_event_deduplication_is_scoped_to_each_appsession() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        first = _registered(runtime, "conversation-one")
        second = _registered(runtime, "conversation-two")
        calls: list[str] = []

        async def controller(context: dict) -> dict:
            calls.append(str(context["app_session_id"]))
            return {"action": "wait", "type": "", "payload": {}}

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
        )
        for registered in (first, second):
            sid = registered["app_session_id"]
            await engagement.set_mode(app_session_id=sid, mode="delegate")
            await engagement.wait_for_idle(sid)
            calls.clear()
            event = runtime.publish_event(
                app_session_id=sid,
                bridge_token=registered["bridge_token"],
                event_id="shared-ready-id",
                type="game.turn_ready",
                actor="user",
                revision=1,
                payload={},
            )
            await engagement.on_update(Method.AUIP_UPDATED, event)
            await engagement.wait_for_idle(sid)
            assert calls == [sid]
        await engagement.close()

    asyncio.run(scenario())


def test_observe_invalidates_a_late_participant_proposal_and_leave_is_bounded() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime)
        sid = registered["app_session_id"]
        started = asyncio.Event()

        async def stubborn_controller(_context: dict) -> dict:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return {"action": "act", "type": "game.move", "payload": {"position": 8}}

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=stubborn_controller,
            role_authorizer=_approve,
        )
        await engagement.set_mode(app_session_id=sid, mode="collaborate")
        engagement.request_step(app_session_id=sid, instruction="Take the next turn.")
        await started.wait()
        stopped = await engagement.set_mode(app_session_id=sid, mode="observe")
        assert stopped["stance"] == "spectator"
        await asyncio.sleep(0)
        await engagement.wait_for_idle(sid)
        snapshot = runtime.get(sid)
        assert snapshot["pending_action"] is None
        assert snapshot["operator_status"] == "idle"

        left = await engagement.leave(app_session_id=sid)
        assert left["status"] == "closed"
        assert left["external_process_stopped"] is False
        await engagement.close()

    asyncio.run(scenario())


def test_host_controls_use_focused_appsession_and_do_not_create_work() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "focused-chat")

        async def controller(_context: dict) -> dict:
            return {"action": "wait", "type": "", "payload": {}}

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
        )
        handler = AuipHandler(
            runtime,
            current_session_id=lambda: "focused-chat",
            engagement=engagement,
        )
        changed = await handler.handle(Method.AUIP_MODE_SET, {"mode": "collaborate"})
        assert changed and changed["app_session_id"] == registered["app_session_id"]
        scheduled = await handler.handle(
            Method.AUIP_STEP,
            {"instruction": "Take one turn."},
        )
        assert scheduled and scheduled["scheduled"] is True
        await engagement.wait_for_idle(registered["app_session_id"])
        left = await handler.handle(Method.AUIP_LEAVE, {})
        assert left and left["external_process_stopped"] is False
        await engagement.close()

    asyncio.run(scenario())


def test_active_reactive_controller_suppresses_only_automatic_decision_turns() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        manifest = {
            "schema": "amadeus.auip/v0",
            "app": {"id": "reactive-zone", "title": "Reactive zone"},
            "events": {
                "zone.threat_changed": {
                    "beat": True,
                    "participantOpportunity": True,
                }
            },
            "actions": {
                "zone.set_response_policy": {
                    "description": "Set the exact threat response policy.",
                    "risk": "local_execution",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "minimumSeverity": {"type": "integer"},
                            "response": {"type": "string"},
                        },
                        "required": ["minimumSeverity", "response"],
                        "additionalProperties": False,
                    },
                }
            },
            "stances": ["spectator", "participant"],
            "situationKinds": ["controller/v1"],
            "controller": {
                "policyActions": ["zone.set_response_policy"],
                "leaseDurationMs": 30_000,
                "maxActionRateHz": 20,
                "takeover": "immediate",
            },
        }
        registered = runtime.register(
            manifest=manifest,
            conversation_id="reactive-routing",
        )
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.set_engagement_mode(app_session_id=sid, mode="collaborate")
        idle_state = {
            "controller": {
                "kind": "controller/v1",
                "status": "idle",
                "policyRevision": None,
                "policyAction": None,
                "policySummary": "",
            }
        }
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state=idle_state,
        )
        policy = runtime.invoke_action(
            app_session_id=sid,
            actor="kurisu",
            type="zone.set_response_policy",
            payload={"minimumSeverity": 4, "response": "isolate_zone"},
            expected_revision=1,
        )["action"]
        runtime.resolve_action(
            app_session_id=sid,
            bridge_token=token,
            action_id=policy["action_id"],
            accepted=True,
            resulting_revision=2,
            state={
                "controller": {
                    "kind": "controller/v1",
                    "status": "active",
                    "policyRevision": policy["controller_lease"]["policy_revision"],
                    "policyAction": "zone.set_response_policy",
                    "policySummary": "Isolate zones at severity 4 or higher",
                }
            },
        )
        calls: list[dict] = []

        async def decision(context: dict) -> dict:
            calls.append(context)
            return {"action": "wait", "type": "", "payload": {}}

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=decision,
            role_authorizer=_approve,
        )
        try:
            event = runtime.publish_event(
                app_session_id=sid,
                bridge_token=token,
                event_id="threat-1",
                type="zone.threat_changed",
                actor="app",
                revision=2,
                payload={"severity": 5},
            )
            await engagement.on_update(Method.AUIP_UPDATED, event)
            await asyncio.sleep(0)
            assert calls == []

            explicit = engagement.request_step(
                app_session_id=sid,
                instruction="Replace the active response policy.",
                reason="explicit_step",
            )
            assert explicit["scheduled"] is True
            await engagement.wait_for_idle(sid)
            assert len(calls) == 1
            assert calls[0]["controller"]["status"] == "active"
            assert calls[0]["controller"]["profile"]["policyActions"] == [
                "zone.set_response_policy"
            ]
        finally:
            await engagement.close()

    asyncio.run(scenario())


def test_controller_policy_rebases_across_participant_model_latency() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        manifest = {
            "schema": "amadeus.auip/v0",
            "app": {"id": "reactive-heat", "title": "Reactive heat"},
            "events": {"heat.changed": {"beat": True}},
            "actions": {
                "heat.set_policy": {
                    "description": "Keep heat inside the visible safe range.",
                    "risk": "local_execution",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "strategy": {
                                "type": "string",
                                "enum": ["maintain_safe_range"],
                            }
                        },
                        "required": ["strategy"],
                        "additionalProperties": False,
                    },
                }
            },
            "stances": ["spectator", "participant"],
            "situationKinds": ["controller/v1"],
            "controller": {
                "policyActions": ["heat.set_policy"],
                "leaseDurationMs": 30_000,
                "maxActionRateHz": 4,
                "takeover": "immediate",
            },
        }
        registered = runtime.register(
            manifest=manifest,
            conversation_id="reactive-rebase",
        )
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.set_engagement_mode(app_session_id=sid, mode="collaborate")

        def state(value: float) -> dict:
            return {
                "temperature": value,
                "controller": {
                    "kind": "controller/v1",
                    "status": "idle",
                    "policyRevision": None,
                    "policyAction": None,
                    "policySummary": "",
                },
            }

        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state=state(90.0),
        )
        requested: list[dict] = []

        async def controller(context: dict) -> dict:
            assert context["revision"] == 1
            # Real telemetry crossed a sparse checkpoint while the model was
            # selecting a policy whose meaning and legality stayed unchanged.
            runtime.publish_state(
                app_session_id=sid,
                bridge_token=token,
                revision=2,
                state=state(91.0),
            )
            return {
                "action": "act",
                "type": "heat.set_policy",
                "payload": {"strategy": "maintain_safe_range"},
            }

        async def capture(_method: str, payload: dict) -> None:
            requested.append(payload)

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
        )
        bus.on(Method.AUIP_ACTION_REQUESTED, capture)
        try:
            engagement.request_step(
                app_session_id=sid,
                instruction="Keep it inside the safe range.",
                reason="explicit_step",
            )
            await engagement.wait_for_idle(sid)
            assert len(requested) == 1
            action = requested[0]["action"]
            assert action["type"] == "heat.set_policy"
            assert action["proposal_revision"] == 1
            assert action["expected_revision"] == 2
            assert action["controller_lease"]["policy_revision"] == 1
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)
            await engagement.close()

    asyncio.run(scenario())


def test_collaborate_schedules_only_declared_participant_opportunities() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime)
        sid = registered["app_session_id"]
        calls: list[dict] = []

        async def controller(context: dict) -> dict:
            calls.append(context)
            return {"action": "wait", "type": "", "payload": {}}

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
        )
        await engagement.set_mode(app_session_id=sid, mode="collaborate")

        ordinary_beat = runtime.publish_event(
            app_session_id=sid,
            bridge_token=registered["bridge_token"],
            event_id="ordinary-user-beat",
            type="game.move_committed",
            actor="user",
            revision=1,
            payload={"summary": "state changed"},
        )
        await engagement.on_update(Method.AUIP_UPDATED, ordinary_beat)
        await asyncio.sleep(0)
        assert calls == []

        opportunity = runtime.publish_event(
            app_session_id=sid,
            bridge_token=registered["bridge_token"],
            event_id="assigned-opportunity",
            type="game.turn_ready",
            actor="user",
            revision=1,
            payload={"actor": "participant"},
        )
        assert opportunity["event"]["participant_opportunity"] is True
        await engagement.on_update(Method.AUIP_UPDATED, opportunity)
        await engagement.wait_for_idle(sid)
        assert len(calls) == 1
        trigger = json.loads(calls[0]["global_conversation_context"])
        assert trigger["trigger"] == "collaborate_participant_opportunity"
        assert calls[0]["action_required"] is True
        assert calls[0]["recent_semantic_beats"][-1]["event_id"] == "assigned-opportunity"

        own_opportunity = runtime.publish_event(
            app_session_id=sid,
            bridge_token=registered["bridge_token"],
            event_id="own-opportunity-echo",
            type="game.turn_ready",
            actor="kurisu",
            revision=1,
            payload={},
        )
        await engagement.on_update(Method.AUIP_UPDATED, own_opportunity)
        await asyncio.sleep(0)
        assert len(calls) == 1
        await engagement.close()

    asyncio.run(scenario())


def test_busy_chat_defers_one_participant_opportunity_until_idle() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conversation-busy-chat")
        sid = registered["app_session_id"]
        calls: list[dict] = []

        async def controller(context: dict) -> dict:
            calls.append(context)
            return {"action": "wait", "type": "", "payload": {}}

        busy = True
        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
            is_chat_busy=lambda: busy,
        )
        await engagement.set_mode(app_session_id=sid, mode="collaborate")
        opportunity = runtime.publish_event(
            app_session_id=sid,
            bridge_token=registered["bridge_token"],
            event_id="busy-chat-opportunity",
            type="game.turn_ready",
            actor="app",
            revision=1,
            payload={},
        )
        await engagement.on_update(Method.AUIP_UPDATED, opportunity)
        await asyncio.sleep(0)
        assert calls == []
        assert runtime.get(sid)["pending_action"] is None
        assert len(engagement._deferred_automatic_tasks) == 1
        busy = False
        await asyncio.wait_for(engagement.wait_for_idle(sid), timeout=2)
        assert len(calls) == 1
        context = json.loads(calls[0]["global_conversation_context"])
        assert context["trigger"] == "collaborate_participant_opportunity"
        await engagement.close()

    asyncio.run(scenario())


def test_automatic_opportunity_does_not_invent_visible_role_alignment_gate() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conversation-automatic-authority")
        sid = registered["app_session_id"]
        requested: list[dict] = []

        async def controller(_context: dict) -> dict:
            return {
                "action": "act",
                "type": "game.move",
                "payload": {"position": 4},
            }

        async def authorizer(_context: dict) -> dict:
            raise AssertionError(
                "an automatic application opportunity has no visible role response to align"
            )

        async def capture(_method: str, payload: dict) -> None:
            requested.append(payload)

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=authorizer,
        )
        bus.on(Method.AUIP_ACTION_REQUESTED, capture)
        try:
            await engagement.set_mode(app_session_id=sid, mode="collaborate")
            opportunity = runtime.publish_event(
                app_session_id=sid,
                bridge_token=registered["bridge_token"],
                event_id="automatic-without-role-prose",
                type="game.turn_ready",
                actor="app",
                revision=1,
                payload={},
            )
            await engagement.on_update(Method.AUIP_UPDATED, opportunity)
            await engagement.wait_for_idle(sid)
            assert len(requested) == 1
            assert requested[0]["action"]["payload"] == {"position": 4}
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)
            await engagement.close()

    asyncio.run(scenario())


def test_automatic_reason_with_visible_role_response_keeps_consistency_gate() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conversation-automatic-visible-role")
        sid = registered["app_session_id"]
        authorizations: list[dict] = []
        requested: list[dict] = []

        async def controller(_context: dict) -> dict:
            return {
                "action": "act",
                "type": "game.move",
                "payload": {"position": 4},
            }

        async def authorizer(context: dict) -> dict:
            authorizations.append(context)
            return {"decision": "reject", "reason": "visible role declined"}

        async def capture(_method: str, payload: dict) -> None:
            requested.append(payload)

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=authorizer,
        )
        bus.on(Method.AUIP_ACTION_REQUESTED, capture)
        try:
            engagement.request_step(
                app_session_id=sid,
                instruction="Use the assigned opportunity.",
                reason="collaborate_participant_opportunity",
                current_role_response="I will wait this turn.",
            )
            await engagement.wait_for_idle(sid)
            assert len(authorizations) == 1
            assert requested == []
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)
            await engagement.close()

    asyncio.run(scenario())


def test_stale_deferred_collaborate_opportunity_does_not_act_on_new_revision() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conversation-stale-deferred")
        sid = registered["app_session_id"]
        busy = True
        calls: list[dict] = []

        async def controller(context: dict) -> dict:
            calls.append(context)
            return {"action": "wait", "type": "", "payload": {}}

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
            is_chat_busy=lambda: busy,
        )
        await engagement.set_mode(app_session_id=sid, mode="collaborate")
        opportunity = runtime.publish_event(
            app_session_id=sid,
            bridge_token=registered["bridge_token"],
            event_id="deferred-revision-one",
            type="game.turn_ready",
            actor="app",
            revision=1,
            payload={},
        )
        await engagement.on_update(Method.AUIP_UPDATED, opportunity)
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=registered["bridge_token"],
            revision=2,
            state={"turn": "user"},
        )
        busy = False
        await asyncio.wait_for(engagement.wait_for_idle(sid), timeout=2)
        assert calls == []
        assert runtime.get(sid)["pending_action"] is None
        await engagement.close()

    asyncio.run(scenario())


def test_required_grid_opportunity_replans_occupied_cell_before_role_or_app() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        manifest = _manifest()
        manifest["situationKinds"] = ["grid/v1"]
        manifest["actions"]["game.move"]["preconditions"] = [
            {
                "kind": "grid_cell_empty/v1",
                "statePath": "board",
                "xField": "x",
                "yField": "y",
            }
        ]
        registered = runtime.register(
            manifest=manifest,
            conversation_id="conversation-grid-replan",
        )
        sid = registered["app_session_id"]
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=registered["bridge_token"],
            revision=1,
            state={
                "board": {
                    "kind": "grid/v1",
                    "width": 3,
                    "height": 2,
                    "empty": ".",
                    "legend": {"B": "black"},
                    "rows": [".B.", "..."],
                },
                "turn": "kurisu",
            },
        )
        controller_calls: list[dict] = []
        requested: list[dict] = []

        async def controller(context: dict) -> dict:
            controller_calls.append(context)
            coordinate = {"x": 1, "y": 0} if len(controller_calls) == 1 else {"x": 0, "y": 0}
            return {"action": "act", "type": "game.move", "payload": coordinate}

        async def authorizer(_context: dict) -> dict:
            raise AssertionError("automatic opportunity has no speech to align")

        async def capture(_method: str, payload: dict) -> None:
            requested.append(payload)

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=authorizer,
        )
        bus.on(Method.AUIP_ACTION_REQUESTED, capture)
        try:
            await engagement.set_mode(app_session_id=sid, mode="collaborate")
            opportunity = runtime.publish_event(
                app_session_id=sid,
                bridge_token=registered["bridge_token"],
                event_id="grid-turn-ready",
                type="game.turn_ready",
                actor="app",
                revision=1,
                payload={},
            )
            await engagement.on_update(Method.AUIP_UPDATED, opportunity)
            await engagement.wait_for_idle(sid)
            assert len(controller_calls) == 2
            assert "precondition failed" in controller_calls[1][
                "global_conversation_context"
            ]
            assert len(requested) == 1
            assert requested[0]["action"]["payload"] == {"x": 0, "y": 0}
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)
            await engagement.close()

    asyncio.run(scenario())


def test_busy_chat_coalesces_delegate_beats_until_chat_is_idle() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conversation-delegate-launch")
        sid = registered["app_session_id"]
        busy = True
        calls: list[dict] = []

        async def controller(context: dict) -> dict:
            calls.append(context)
            return {"action": "wait", "type": "", "payload": {}}

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
            is_chat_busy=lambda: busy,
        )
        # Attached apps may register directly in delegate mode without going
        # through engagement.set_mode(). This is the shipping launch path.
        runtime.set_engagement_mode(app_session_id=sid, mode="delegate")
        for index in (1, 2):
            beat = runtime.publish_event(
                app_session_id=sid,
                bridge_token=registered["bridge_token"],
                event_id=f"busy-delegate-beat-{index}",
                type="game.move_committed",
                actor="app",
                revision=1,
                payload={"index": index},
            )
            await engagement.on_update(Method.AUIP_UPDATED, beat)
        await asyncio.sleep(0)
        assert calls == []
        assert len(engagement._deferred_automatic_tasks) == 1

        busy = False
        await asyncio.wait_for(engagement.wait_for_idle(sid), timeout=2)
        assert len(calls) == 1
        context = json.loads(calls[0]["global_conversation_context"])
        assert context["trigger"] == "delegate_semantic_beat"
        assert runtime.get(sid)["pending_action"] is None
        await engagement.close()

    asyncio.run(scenario())


def test_confirmed_user_turn_cancels_only_private_automatic_decision() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conversation-user-priority")
        sid = registered["app_session_id"]
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def controller(_context: dict) -> dict:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
        )
        await engagement.set_mode(app_session_id=sid, mode="collaborate")
        engagement.request_step(
            app_session_id=sid,
            instruction="Use the assigned opportunity.",
            reason="collaborate_participant_opportunity",
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        assert runtime.get(sid)["operator_status"] == "thinking"

        interrupted = await engagement.interrupt_for_user_turn(
            "conversation-user-priority"
        )
        assert interrupted is True
        await asyncio.wait_for(cancelled.wait(), timeout=2)
        snapshot = runtime.get(sid)
        assert snapshot["pending_action"] is None
        assert snapshot["operator_status"] == "idle"
        await engagement.close()

    asyncio.run(scenario())


def test_user_step_replaces_initial_opportunity_without_a_second_action() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conversation-initial-race")
        sid = registered["app_session_id"]
        automatic_started = asyncio.Event()
        calls = 0
        requested: list[dict] = []

        async def controller(_context: dict) -> dict:
            nonlocal calls
            calls += 1
            if calls == 1:
                automatic_started.set()
                await asyncio.Event().wait()
            return {
                "action": "act",
                "type": "game.move",
                "payload": {"position": 4},
            }

        async def capture(_method: str, payload: dict) -> None:
            requested.append(payload)

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
        )
        await engagement.set_mode(app_session_id=sid, mode="collaborate")
        bus.on(Method.AUIP_ACTION_REQUESTED, capture)
        try:
            initial = runtime.publish_event(
                app_session_id=sid,
                bridge_token=registered["bridge_token"],
                event_id="initial-opportunity",
                type="game.turn_ready",
                actor="app",
                revision=1,
                payload={},
            )
            await engagement.on_update(Method.AUIP_UPDATED, initial)
            await asyncio.wait_for(automatic_started.wait(), timeout=2)

            assert await engagement.interrupt_for_user_turn(
                "conversation-initial-race"
            ) is True
            engagement.request_step(
                app_session_id=sid,
                instruction="Make exactly one move now.",
                reason="explicit_step",
            )
            await engagement.wait_for_idle(sid)

            assert calls == 2
            assert len(requested) == 1
            assert requested[0]["action"]["payload"] == {"position": 4}
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)
            await engagement.close()

    asyncio.run(scenario())


def test_new_chat_does_not_cancel_an_explicit_step_before_application_request() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conversation-explicit-preserved")
        sid = registered["app_session_id"]
        started = asyncio.Event()

        async def controller(_context: dict) -> dict:
            started.set()
            await asyncio.Event().wait()

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
        )
        await engagement.set_mode(app_session_id=sid, mode="collaborate")
        engagement.request_step(
            app_session_id=sid,
            instruction="Make one explicit move.",
            reason="explicit_step",
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        assert await engagement.interrupt_for_user_turn(
            "conversation-explicit-preserved"
        ) is False
        assert engagement._tasks[sid].done() is False
        await engagement.close()

    asyncio.run(scenario())


def test_participant_failure_surfaces_operator_error_without_requesting_an_action() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime)
        sid = registered["app_session_id"]
        requested: list[dict] = []

        async def unavailable(**_kwargs):
            return None

        async def capture(_method: str, payload: dict) -> None:
            requested.append(payload)

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=decide_with_auip_participant,
            role_authorizer=_approve,
        )
        bus.on(Method.AUIP_ACTION_REQUESTED, capture)
        try:
            with patch("server.auip_participant_llm.call_auip_tool", unavailable):
                engagement.request_step(
                    app_session_id=sid,
                    instruction="你来下先手吧",
                )
                await engagement.wait_for_idle(sid)
            snapshot = runtime.get(sid)
            assert snapshot["operator_status"] == "error"
            assert snapshot["operator_error"] == "participant_decision_unavailable"
            assert snapshot["pending_action"] is None
            assert requested == []
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)
            await engagement.close()

    asyncio.run(scenario())


def test_inline_auip_control_interleaves_with_role_text_without_vts_or_delegate() -> None:
    async def scenario() -> None:
        captured: list[tuple[dict, dict]] = []

        async def route(attrs: dict, **context) -> None:
            captured.append((attrs, context))

        runtime = ChatRuntime()
        runtime.configure(auip_control_callback=route)
        state = _TurnState(
            gui_callback=None,
            turn_id="turn-auip",
            question="下一步你来",
            session_id="session-auip",
        )
        with patch("core.chat_runtime.record_actions") as record:
            visible = runtime._consume_stream_chunk(
                state,
                'わかった。[AUIP action="step" instruction="Take the next turn"]続けるわ。',
            )
            await runtime._wait_for_auip_controls(state)
        assert visible == "わかった。続けるわ。"
        assert not record.called
        assert captured[0][0]["action"] == "step"
        assert captured[0][1]["session_id"] == "session-auip"
        assert "[AUIP" in state.history_response

        clean, expressions = clean_sentence_for_tts(
            '話す。[AUIP action="observe"]まだ話す。'
        )
        assert clean == "話す。まだ話す。"
        assert expressions == []

    asyncio.run(scenario())


def test_explicit_step_returns_blocked_reason_instead_of_silent_wait() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conversation-blocked-step")
        sid = registered["app_session_id"]
        controller_inputs: list[dict] = []
        requested: list[dict] = []
        updates: list[dict] = []

        async def controller(context: dict) -> dict:
            controller_inputs.append(context)
            return {
                "action": "blocked",
                "type": "",
                "payload": {},
                "private_note": (
                    "The current participant binding cannot take the first turn."
                ),
            }

        async def capture_action(_method: str, payload: dict) -> None:
            requested.append(payload)

        async def capture_update(_method: str, payload: dict) -> None:
            if isinstance(payload.get("operator_outcome"), dict):
                updates.append(payload)

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
        )
        bus.on(Method.AUIP_ACTION_REQUESTED, capture_action)
        bus.on(Method.AUIP_UPDATED, capture_update)
        try:
            engagement.request_step(
                app_session_id=sid,
                instruction="Take the first turn.",
            )
            await engagement.wait_for_idle(sid)

            assert controller_inputs[0]["action_required"] is True
            assert requested == []
            snapshot = runtime.get(sid)
            assert snapshot["operator_status"] == "error"
            assert snapshot["operator_error"] == "participant_blocked"
            assert snapshot["operator_error_detail"] == (
                "The current participant binding cannot take the first turn."
            )
            assert len(updates) == 1
            outcome = updates[0]["operator_outcome"]
            assert outcome["status"] == "blocked"
            assert outcome["instruction"] == "Take the first turn."
            assert outcome["reason"] == snapshot["operator_error_detail"]
            assert outcome["proposal_id"]
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture_action)
            bus.off(Method.AUIP_UPDATED, capture_update)
            await engagement.close()

    asyncio.run(scenario())


def test_assigned_participant_opportunity_never_fails_silently() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conversation-blocked-opportunity")
        sid = registered["app_session_id"]
        contexts: list[dict] = []
        outcomes: list[dict] = []

        async def controller(context: dict) -> dict:
            contexts.append(context)
            return {
                "action": "blocked",
                "type": "",
                "payload": {},
                "private_note": "The assigned role has no legal move in this state.",
            }

        async def capture(_method: str, payload: dict) -> None:
            outcome = payload.get("operator_outcome")
            if isinstance(outcome, dict):
                outcomes.append(outcome)

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
        )
        bus.on(Method.AUIP_UPDATED, capture)
        try:
            await engagement.set_mode(app_session_id=sid, mode="collaborate")
            opportunity = runtime.publish_event(
                app_session_id=sid,
                bridge_token=registered["bridge_token"],
                event_id="assigned-but-blocked",
                type="game.turn_ready",
                actor="app",
                revision=1,
                payload={"turn": "participant"},
            )
            await engagement.on_update(Method.AUIP_UPDATED, opportunity)
            await engagement.wait_for_idle(sid)

            assert contexts[0]["action_required"] is True
            assert len(outcomes) == 1
            assert outcomes[0]["status"] == "blocked"
            assert outcomes[0]["reason"] == (
                "The assigned role has no legal move in this state."
            )
            assert runtime.get(sid)["operator_error"] == "participant_blocked"
        finally:
            bus.off(Method.AUIP_UPDATED, capture)
            await engagement.close()

    asyncio.run(scenario())


def test_missing_app_receipt_becomes_an_unknown_outcome_instead_of_silence() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conversation-receipt-timeout")
        sid = registered["app_session_id"]
        outcomes: list[dict] = []

        async def controller(_context: dict) -> dict:
            return {
                "action": "act",
                "type": "game.move",
                "payload": {"position": 4},
            }

        async def capture(_method: str, payload: dict) -> None:
            outcome = payload.get("operator_outcome")
            if isinstance(outcome, dict):
                outcomes.append(outcome)

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
            receipt_timeout_s=0.02,
        )
        bus.on(Method.AUIP_UPDATED, capture)
        try:
            with patch("server.auip_runtime.PENDING_ACTION_TIMEOUT_S", 0.01):
                engagement.request_step(
                    app_session_id=sid,
                    instruction="Make the next move.",
                )
                await engagement.wait_for_idle(sid)
                receipt_watch = engagement._receipt_tasks[sid]
                await asyncio.wait_for(asyncio.shield(receipt_watch), timeout=1.0)

            assert len(outcomes) == 1
            assert "did not return an action receipt" in outcomes[0]["reason"]
            assert "unknown" in outcomes[0]["reason"]
            snapshot = runtime.get(sid)
            assert snapshot["pending_action"] is None
            assert snapshot["operator_error"] == "receipt_timeout"
            assert snapshot["latest_verified_self_action"] is None
        finally:
            bus.off(Method.AUIP_UPDATED, capture)
            await engagement.close()

    asyncio.run(scenario())


def test_explicit_step_is_bound_to_the_revision_where_consensus_was_formed() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = runtime.register(
            manifest=_manifest(),
            conversation_id="conv-revision-bound",
        )
        sid = registered["app_session_id"]
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=registered["bridge_token"],
            revision=1,
            state={"turn": "kurisu"},
        )
        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=lambda _context: {
                "action": "wait",
                "type": "",
                "payload": {},
            },
            role_authorizer=_approve,
        )
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=registered["bridge_token"],
            revision=2,
            state={"turn": "user"},
        )
        try:
            try:
                engagement.request_step(
                    app_session_id=sid,
                    instruction="Place at H7 as agreed.",
                    expected_revision=1,
                )
            except AuipProtocolError as exc:
                assert exc.code == "participant_revision_changed"
            else:
                raise AssertionError("stale negotiated step should fail closed")
        finally:
            await engagement.close()

    asyncio.run(scenario())


def test_revision_race_publishes_the_original_participant_error() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conv-revision-race")
        sid = registered["app_session_id"]
        updates: list[dict] = []
        requested: list[dict] = []

        async def capture_update(_method: str, payload: dict) -> None:
            updates.append(payload)

        async def capture_action(_method: str, payload: dict) -> None:
            requested.append(payload)

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=lambda _context: {"action": "wait", "type": "", "payload": {}},
            role_authorizer=_approve,
        )
        bus.on(Method.AUIP_UPDATED, capture_update)
        bus.on(Method.AUIP_ACTION_REQUESTED, capture_action)
        try:
            scheduled = engagement.request_step(
                app_session_id=sid,
                instruction="Make the agreed move.",
                expected_revision=1,
            )
            assert scheduled["scheduled"] is True
            # The task has been scheduled but has not yielded yet.  A real-time
            # app may publish one accepted boundary in this exact gap.
            runtime.publish_state(
                app_session_id=sid,
                bridge_token=registered["bridge_token"],
                revision=2,
                state={"turn": "user"},
            )
            await engagement.wait_for_idle(sid)

            snapshot = runtime.get(sid)
            assert snapshot["operator_error"] == "participant_revision_changed"
            assert requested == []
            outcome = next(
                update["operator_outcome"]
                for update in updates
                if isinstance(update.get("operator_outcome"), dict)
            )
            assert outcome["status"] == "blocked"
            assert outcome["reason"]
        finally:
            bus.off(Method.AUIP_UPDATED, capture_update)
            bus.off(Method.AUIP_ACTION_REQUESTED, capture_action)
            await engagement.close()

    asyncio.run(scenario())


def test_explicit_chat_directive_supersedes_only_an_unsubmitted_autonomous_proposal() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conv-directive-priority")
        sid = registered["app_session_id"]
        first_started = asyncio.Event()
        first_cancelled = asyncio.Event()
        contexts: list[dict] = []

        async def controller(context: dict) -> dict:
            contexts.append(context)
            if len(contexts) == 1:
                first_started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    first_cancelled.set()
                    raise
            return {
                "action": "wait",
                "type": "",
                "payload": {},
                "private_note": "directive observed",
            }

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
        )
        try:
            automatic = engagement.request_step(
                app_session_id=sid,
                instruction="Choose one appropriate action.",
                reason="collaborate_participant_opportunity",
            )
            assert automatic["scheduled"] is True
            await asyncio.wait_for(first_started.wait(), timeout=1.0)

            explicit = engagement.request_step(
                app_session_id=sid,
                instruction="Place at H7 as agreed.",
                reason="explicit_step",
                expected_revision=1,
            )
            assert explicit["scheduled"] is True
            assert explicit["superseded_in_flight"] is True
            await asyncio.wait_for(first_cancelled.wait(), timeout=1.0)
            await engagement.wait_for_idle(sid)

            assert len(contexts) == 2
            assert "Place at H7 as agreed." in contexts[1][
                "global_conversation_context"
            ]
            assert runtime.get(sid)["pending_action"] is None
        finally:
            await engagement.close()

    asyncio.run(scenario())


def test_silent_main_role_gate_sees_branch_state_and_can_block_execution() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conv-role-gate")
        sid = registered["app_session_id"]
        requested: list[dict] = []
        authorization_inputs: list[dict] = []

        async def controller(_context: dict) -> dict:
            return {
                "action": "act",
                "type": "game.move",
                "payload": {"position": 9},
            }

        async def authorizer(context: dict) -> dict:
            authorization_inputs.append(context)
            return {
                "decision": "reject",
                "reason": "The proposal does not follow the user's strategy.",
            }

        async def capture(_method: str, payload: dict) -> None:
            requested.append(payload)

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=authorizer,
            recent_chat=lambda _conversation: [
                {"role": "user", "content": "Follow my defensive shape."}
            ],
        )
        bus.on(Method.AUIP_ACTION_REQUESTED, capture)
        try:
            engagement.request_step(
                app_session_id=sid,
                instruction="Follow my defensive shape.",
            )
            await engagement.wait_for_idle(sid)
            assert requested == []
            assert authorization_inputs[0]["state"] == {"turn": "kurisu"}
            assert authorization_inputs[0]["proposal"]["payload"] == {
                "position": 9
            }
            assert "Follow my defensive shape." in authorization_inputs[0][
                "global_conversation_context"
            ]
            snapshot = runtime.get(sid)
            assert snapshot["operator_status"] == "error"
            assert snapshot["operator_error"] == "role_rejected_proposal"
            assert snapshot["operator_error_detail"] == (
                "The proposal does not follow the user's strategy."
            )
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)
            await engagement.close()

    asyncio.run(scenario())


def test_current_visible_role_refusal_reaches_the_silent_gate_before_action() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conv-current-role-refusal")
        sid = registered["app_session_id"]
        requested: list[dict] = []
        authorization_inputs: list[dict] = []
        operator_outcomes: list[dict] = []

        async def controller(_context: dict) -> dict:
            return {
                "action": "act",
                "type": "game.move",
                "payload": {"position": 7},
            }

        async def authorizer(context: dict) -> dict:
            authorization_inputs.append(context)
            global_context = json.loads(context["global_conversation_context"])
            if "あなたからどうぞ" in global_context["current_role_response"]:
                return {"decision": "reject", "reason": "role assigned the user first"}
            return {"decision": "approve", "reason": "role agreed"}

        async def capture(_method: str, payload: dict) -> None:
            requested.append(payload)

        async def capture_update(_method: str, payload: dict) -> None:
            if isinstance(payload.get("operator_outcome"), dict):
                operator_outcomes.append(payload["operator_outcome"])

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=authorizer,
        )
        bus.on(Method.AUIP_ACTION_REQUESTED, capture)
        bus.on(Method.AUIP_UPDATED, capture_update)
        try:
            engagement.request_step(
                app_session_id=sid,
                instruction="Take the first move now.",
                current_role_response="私は後手でいいわ。あなたからどうぞ。",
            )
            await engagement.wait_for_idle(sid)
            assert requested == []
            assert len(authorization_inputs) == 1
            assert authorization_inputs[0]["current_role_response"] == (
                "私は後手でいいわ。あなたからどうぞ。"
            )
            assert set(authorization_inputs[0]["proposal"]) == {
                "proposal_id",
                "action",
                "type",
                "payload",
            }
            assert authorization_inputs[0]["authorization_contract"][
                "speaker_roles"
            ] == {
                "current_role_response_speaker": "participant",
                "first_person": "participant",
                "second_person": "user",
                "proposal_actor": "participant",
            }
            snapshot = runtime.get(sid)
            assert snapshot["operator_error"] == "role_rejected_proposal"
            assert snapshot["operator_error_detail"] == "role assigned the user first"
            assert len(operator_outcomes) == 1
            assert operator_outcomes[0]["status"] == "blocked"
            assert operator_outcomes[0]["reason"] == "role assigned the user first"
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)
            bus.off(Method.AUIP_UPDATED, capture_update)
            await engagement.close()

    asyncio.run(scenario())


def test_role_quality_review_replans_once_and_invokes_only_the_approved_proposal() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conv-role-replan")
        sid = registered["app_session_id"]
        controller_inputs: list[dict] = []
        authorization_inputs: list[dict] = []
        requested: list[dict] = []

        async def controller(context: dict) -> dict:
            controller_inputs.append(context)
            position = 1 if len(controller_inputs) == 1 else 7
            return {
                "action": "act",
                "type": "game.move",
                "payload": {"position": position},
            }

        async def authorizer(context: dict) -> dict:
            authorization_inputs.append(context)
            if len(authorization_inputs) == 1:
                return {
                    "decision": "replan",
                    "reason": "The center is the materially stronger opening.",
                }
            return {"decision": "approve", "reason": "The correction is coherent."}

        async def capture(_method: str, payload: dict) -> None:
            requested.append(payload)

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=authorizer,
        )
        bus.on(Method.AUIP_ACTION_REQUESTED, capture)
        try:
            engagement.request_step(
                app_session_id=sid,
                instruction="Take one competent opening action.",
            )
            await engagement.wait_for_idle(sid)

            assert len(controller_inputs) == 2
            replan = json.loads(controller_inputs[1]["global_conversation_context"])
            assert replan["role_replan_feedback"]["rejected_proposal"][
                "payload"
            ] == {"position": 1}
            assert "center" in replan["role_replan_feedback"]["reason"]
            assert len(authorization_inputs) == 2
            assert len(requested) == 1
            assert requested[0]["action"]["payload"] == {"position": 7}
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)
            await engagement.close()

    asyncio.run(scenario())


def test_rejected_receipt_does_not_authorize_a_second_action() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = _registered(runtime, "conv-rejected-receipt-replan")
        sid = registered["app_session_id"]
        controller_inputs: list[dict] = []

        async def controller(context: dict) -> dict:
            controller_inputs.append(context)
            return {
                "action": "act",
                "type": "game.move",
                "payload": {"position": 4},
            }

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=_approve,
        )
        try:
            engagement.request_step(
                app_session_id=sid,
                instruction="Take one legal move.",
            )
            await engagement.wait_for_idle(sid)
            first = runtime.get(sid)["pending_action"]
            assert first and first["payload"] == {"position": 4}

            first_rejection = runtime.resolve_action(
                app_session_id=sid,
                bridge_token=registered["bridge_token"],
                action_id=first["action_id"],
                accepted=False,
                resulting_revision=1,
                reason="position 4 is already occupied",
            )
            await engagement.on_update(Method.AUIP_UPDATED, first_rejection)
            await engagement.wait_for_idle(sid)
            await asyncio.sleep(0)

            assert len(controller_inputs) == 1
            snapshot = runtime.get(sid)
            assert snapshot["pending_action"] is None
            assert snapshot["operator_error"] == "action_rejected"
            assert snapshot["operator_error_detail"] == (
                "position 4 is already occupied"
            )
        finally:
            await engagement.close()

    asyncio.run(scenario())


def _main() -> None:
    test_delegate_mode_schedules_declared_beats_but_not_kurisu_echoes()
    test_collaborate_schedules_only_declared_participant_opportunities()
    test_observe_invalidates_a_late_participant_proposal_and_leave_is_bounded()
    test_event_deduplication_is_scoped_to_each_appsession()
    test_host_controls_use_focused_appsession_and_do_not_create_work()
    test_participant_failure_surfaces_operator_error_without_requesting_an_action()
    test_explicit_step_returns_blocked_reason_instead_of_silent_wait()
    test_assigned_participant_opportunity_never_fails_silently()
    test_missing_app_receipt_becomes_an_unknown_outcome_instead_of_silence()
    test_inline_auip_control_interleaves_with_role_text_without_vts_or_delegate()
    test_explicit_step_is_bound_to_the_revision_where_consensus_was_formed()
    test_explicit_chat_directive_supersedes_only_an_unsubmitted_autonomous_proposal()
    test_silent_main_role_gate_sees_branch_state_and_can_block_execution()
    test_current_visible_role_refusal_reaches_the_silent_gate_before_action()
    test_role_quality_review_replans_once_and_invokes_only_the_approved_proposal()
    test_rejected_receipt_does_not_authorize_a_second_action()
    print("ok: AUIP engagement keeps control, scheduling, and execution truth separate")


if __name__ == "__main__":
    _main()
