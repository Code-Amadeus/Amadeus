from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from server.auip_action_candidates import compile_auip_action_candidates
from server.auip_engagement import AuipEngagementCoordinator
from server.auip_narration import AuipNarrationAdapter
from server.auip_participant import AuipParticipantCoordinator
from server.auip_participant_llm import decide_with_auip_participant
from server.auip_app_connection import AuipAppRequestHandler
from server.auip_runtime import AuipRuntime
from server.event_bus import bus
from server.handlers.auip_handler import AuipHandler
from server.protocol import Method


MANIFEST = {
    "schema": "amadeus.auip/v0",
    "app": {"id": "gomoku", "title": "Gomoku", "version": "0.1.0"},
    "events": {
        "game.move_committed": {"beat": True, "participantOpportunity": True},
        "game.finished": {"beat": True, "importance": "important", "terminal": True},
    },
    "actions": {
        "game.place_stone": {
            "description": "Place one legal stone.",
            "risk": "local_execution",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "minimum": 0, "maximum": 14},
                    "y": {"type": "integer", "minimum": 0, "maximum": 14},
                },
                "required": ["x", "y"],
                "additionalProperties": False,
            },
        }
    },
    "stances": ["spectator", "participant"],
}


def test_compact_choice_initial_state_and_b2_candidates_share_one_revision() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime(role_branch_mode="b2")
        app_handler = AuipAppRequestHandler(runtime)
        manifest = {
            "schema": "amadeus.auip/v0",
            "app": {
                "id": "compact-choice",
                "title": "Compact Choice",
                "interactionSummary": "Choose one currently available side.",
            },
            "events": {"app.ready": {"beat": True}},
            "actions": {
                "app.choose_side": {
                    "description": "Choose one available side.",
                    "risk": "local_execution",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "side": {"type": "string", "enum": ["left", "right"]}
                        },
                        "required": ["side"],
                        "additionalProperties": False,
                    },
                }
            },
            "stances": ["spectator", "participant"],
            "situationKinds": ["choice/v1"],
        }
        ticket = runtime.issue_attach_ticket(
            conversation_id="compact-choice-conversation",
            artifact_ref="artifact:compact-choice@1",
            engagement_mode="collaborate",
        )
        registered = await app_handler.handle(
            Method.AUIP_REGISTER,
            {"manifest": manifest, **ticket},
        )
        assert registered["revision"] == 0
        assert runtime.role_branch_active(registered["app_session_id"]) is False

        published = await app_handler.handle(
            Method.AUIP_STATE_PUBLISH,
            {
                "app_session_id": registered["app_session_id"],
                "bridge_token": registered["bridge_token"],
                "revision": 1,
                "state": {
                    "choice": {
                        "kind": "choice/v1",
                        "action": "app.choose_side",
                        "options": [
                            {"label": "Left", "payload": {"side": "left"}},
                            {"label": "Right", "payload": {"side": "right"}},
                        ],
                    }
                },
            },
        )
        assert published["ok"] is True
        assert published["revision"] == 1
        assert runtime.role_branch_active(registered["app_session_id"]) is True

        compiled = compile_auip_action_candidates(
            runtime,
            registered["app_session_id"],
        )
        assert {
            candidate.payload["side"] for candidate in compiled.candidates.values()
        } == {"left", "right"}
        assert {candidate.revision for candidate in compiled.candidates.values()} == {1}

    asyncio.run(scenario())


def test_specialist_action_receipt_and_branch_capsule_cross_the_real_boundaries() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        host_handler = AuipHandler(runtime, current_session_id=lambda: "conversation-gomoku")
        app_handler = AuipAppRequestHandler(runtime)
        coordinator = AuipParticipantCoordinator(runtime)
        app_requests: list[dict] = []

        async def capture(_method: str, payload: dict) -> None:
            app_requests.append(payload)

        bus.on(Method.AUIP_ACTION_REQUESTED, capture)
        try:
            ticket = runtime.issue_attach_ticket(
                conversation_id="conversation-gomoku",
                artifact_ref="artifact:gomoku@1234",
            )
            registered = await app_handler.handle(
                Method.AUIP_REGISTER,
                {"manifest": MANIFEST, **ticket},
            )
            assert registered
            sid = registered["app_session_id"]
            token = registered["bridge_token"]
            await app_handler.handle(
                Method.AUIP_STATE_PUBLISH,
                {
                    "app_session_id": sid,
                    "bridge_token": token,
                    "revision": 1,
                    "state": {"turn": "kurisu", "board": []},
                },
            )
            await host_handler.handle(
                Method.AUIP_STANCE_SET,
                {"app_session_id": sid, "stance": "participant"},
            )

            async def specialist(context: dict) -> dict:
                assert context["global_conversation_context"] == "Block the open four."
                return {
                    "type": "game.place_stone",
                    "payload": {"x": 3, "y": 4},
                    "private_note": "private candidate search",
                }

            specialist_proposal = await coordinator.propose(
                app_session_id=sid,
                controller=specialist,
                controller_id="gomoku-agent",
                global_context="Block the open four.",
            )
            assert app_requests == []
            proposal = await coordinator.invoke(specialist_proposal)
            assert app_requests[-1]["action"]["action_id"] == proposal["action"]["action_id"]
            assert "private candidate search" not in runtime.render_main_chat_context(
                "conversation-gomoku"
            )

            resolved = await app_handler.handle(
                Method.AUIP_ACTION_RESULT,
                {
                    "app_session_id": sid,
                    "bridge_token": token,
                    "action_id": proposal["action"]["action_id"],
                    "accepted": True,
                    "resulting_revision": 2,
                    "state": {
                        "turn": "user",
                        "board": [{"x": 3, "y": 4, "actor": "kurisu"}],
                    },
                    "effects": {"placed": {"x": 3, "y": 4}},
                },
            )
            assert resolved and resolved["latest_verified_self_action"]["type"] == "game.place_stone"
            runtime.record_delivered_narration(
                app_session_id=sid,
                text="I blocked that line; your turn.",
            )
            await app_handler.handle(
                Method.AUIP_EVENT_PUBLISH,
                {
                    "app_session_id": sid,
                    "bridge_token": token,
                    "event_id": "finished-1",
                    "event_type": "game.finished",
                    "actor": "app",
                    "revision": 2,
                    "payload": {"winner": "user"},
                },
            )
            retained = runtime.render_main_chat_context("conversation-gomoku")
            assert "I blocked that line; your turn." in retained
            assert "game.finished" in retained
            assert "private candidate search" not in retained
            assert '"board"' not in retained
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)

    asyncio.run(scenario())


def test_natural_gomoku_step_reaches_an_accepted_receipt_and_new_revision() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        app_handler = AuipAppRequestHandler(runtime)
        app_requests: list[dict] = []
        captured_request: dict = {}

        class Completions:
            @staticmethod
            def create(**kwargs):
                captured_request.update(kwargs)
                user_payload = json.loads(kwargs["messages"][1]["content"])
                assert "你来下先手吧" in user_payload["global_conversation_context"]
                action_tool = next(
                    item
                    for item in kwargs["tools"]
                    if item["function"]["name"] == "auip_action_0"
                )
                payload_schema = action_tool["function"]["parameters"]
                assert payload_schema["required"] == ["x", "y"]
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                tool_calls=[
                                    SimpleNamespace(
                                        function=SimpleNamespace(
                                            name="auip_action_0",
                                            arguments='{"x":7,"y":7}',
                                        )
                                    )
                                ]
                            )
                        )
                    ]
                )

        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

        async def capture(_method: str, payload: dict) -> None:
            app_requests.append(payload)

        ticket = runtime.issue_attach_ticket(
            conversation_id="conversation-natural-gomoku",
            artifact_ref="artifact:gomoku@natural",
        )
        registered = await app_handler.handle(
            Method.AUIP_REGISTER,
            {"manifest": MANIFEST, **ticket},
        )
        assert registered
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        await app_handler.handle(
            Method.AUIP_STATE_PUBLISH,
            {
                "app_session_id": sid,
                "bridge_token": token,
                "revision": 1,
                "state": {"turn": "kurisu", "board": []},
            },
        )
        runtime.set_stance(app_session_id=sid, stance="participant")
        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=decide_with_auip_participant,
            role_authorizer=lambda _context: {
                "decision": "approve",
                "reason": "assembly policy",
            },
        )
        bus.on(Method.AUIP_ACTION_REQUESTED, capture)
        try:
            with (
                patch("server.auip_narration_llm.has_auip_model_config", return_value=True),
                patch("server.auip_narration_llm._provider", return_value="deepseek"),
                patch("server.auip_narration_llm._model", return_value="test-model"),
                patch("server.auip_narration_llm._client", return_value=client),
                patch(
                    "server.auip_participant_llm.settings.AUIP_ACTION_PROVIDER",
                    "deepseek",
                ),
                patch(
                    "server.auip_participant_llm.settings.AUIP_ACTION_MODEL",
                    "test-model",
                ),
                patch(
                    "server.auip_participant_llm.settings.AUIP_ACTION_REASONING_EFFORT",
                    "none",
                ),
            ):
                engagement.request_step(
                    app_session_id=sid,
                    instruction="你来下先手吧",
                )
                await engagement.wait_for_idle(sid)

            assert captured_request["tool_choice"] == "required"
            assert captured_request["extra_body"] == {
                "thinking": {"type": "disabled"}
            }
            assert "reasoning_effort" not in captured_request
            assert len(app_requests) == 1
            action = app_requests[0]["action"]
            assert action["type"] == "game.place_stone"
            assert action["payload"] == {"x": 7, "y": 7}
            assert runtime.get(sid)["revision"] == 1

            resolved = await app_handler.handle(
                Method.AUIP_ACTION_RESULT,
                {
                    "app_session_id": sid,
                    "bridge_token": token,
                    "action_id": action["action_id"],
                    "accepted": True,
                    "resulting_revision": 2,
                    "state": {
                        "turn": "user",
                        "board": [{"x": 7, "y": 7, "actor": "kurisu"}],
                    },
                    "effects": {"placed": {"x": 7, "y": 7}},
                },
            )
            assert resolved and resolved["revision"] == 2
            assert resolved["latest_verified_self_action"]["type"] == "game.place_stone"
            assert runtime.get(sid)["pending_action"] is None
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)
            await engagement.close()

    asyncio.run(scenario())


def test_user_move_then_explicit_chat_strategy_replaces_private_auto_proposal() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        app_handler = AuipAppRequestHandler(runtime)
        first_started = asyncio.Event()
        first_cancelled = asyncio.Event()
        calls: list[dict] = []
        authorizations: list[dict] = []
        requests: list[dict] = []

        async def participant(context: dict) -> dict:
            calls.append(context)
            if len(calls) == 1:
                first_started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    first_cancelled.set()
                    raise
            return {
                "action": "act",
                "type": "game.place_stone",
                "payload": {"x": 7, "y": 8},
            }

        async def authorizer(context: dict) -> dict:
            authorizations.append(context)
            return {"decision": "approve", "reason": "matches H8"}

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=participant,
            role_authorizer=authorizer,
            recent_chat=lambda _conversation: [
                {"role": "user", "content": "按我们说的，下 H8。"}
            ],
        )
        host = AuipHandler(
            runtime,
            current_session_id=lambda: "conversation-three-move",
            engagement=engagement,
        )

        async def capture(_method: str, payload: dict) -> None:
            requests.append(payload)

        ticket = runtime.issue_attach_ticket(
            conversation_id="conversation-three-move",
            artifact_ref="artifact:gomoku@three-move",
            engagement_mode="collaborate",
        )
        registered = await app_handler.handle(
            Method.AUIP_REGISTER,
            {"manifest": MANIFEST, **ticket},
        )
        sid = str(registered["app_session_id"])
        token = str(registered["bridge_token"])
        await app_handler.handle(
            Method.AUIP_STATE_PUBLISH,
            {
                "app_session_id": sid,
                "bridge_token": token,
                "revision": 2,
                "state": {
                    "turn": "kurisu",
                    "board": [
                        {"x": 7, "y": 7, "actor": "kurisu"},
                        {"x": 8, "y": 7, "actor": "user"},
                    ],
                },
            },
        )
        opportunity = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="user-second-stone",
            type="game.move_committed",
            actor="user",
            revision=2,
            payload={"x": 8, "y": 7},
        )
        bus.on(Method.AUIP_ACTION_REQUESTED, capture)
        try:
            await engagement.on_update(Method.AUIP_UPDATED, opportunity)
            await asyncio.wait_for(first_started.wait(), timeout=1.0)

            routed = await host.route_control(
                {"action": "step", "instruction": "Place at H8 as agreed."},
                session_id="conversation-three-move",
                user_text="就按我们说的下 H8。",
                turn_id="turn-third-stone",
            )
            assert routed and routed["superseded_in_flight"] is True
            await asyncio.wait_for(first_cancelled.wait(), timeout=1.0)
            await engagement.wait_for_idle(sid)
            assert len(requests) == 1
            action = requests[0]["action"]
            assert action["payload"] == {"x": 7, "y": 8}
            assert action["proposal_id"].startswith("proposal_")
            assert authorizations[0]["state"]["board"][-1]["actor"] == "user"
            assert authorizations[0]["proposal"]["payload"] == {"x": 7, "y": 8}

            accepted = await app_handler.handle(
                Method.AUIP_ACTION_RESULT,
                {
                    "app_session_id": sid,
                    "bridge_token": token,
                    "action_id": action["action_id"],
                    "accepted": True,
                    "resulting_revision": 3,
                    "state": {
                        "turn": "user",
                        "board": [
                            {"x": 7, "y": 7, "actor": "kurisu"},
                            {"x": 8, "y": 7, "actor": "user"},
                            {"x": 7, "y": 8, "actor": "kurisu"},
                        ],
                    },
                    "effects": {"placed": {"x": 7, "y": 8}},
                },
            )
            assert accepted and accepted["revision"] == 3
            assert accepted["latest_verified_self_action"]["proposal_id"] == action[
                "proposal_id"
            ]
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)
            await engagement.close()

    asyncio.run(scenario())


def test_rejected_app_receipt_reaches_role_narration_without_becoming_action_truth() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        app_handler = AuipAppRequestHandler(runtime)
        coordinator = AuipParticipantCoordinator(runtime)
        delivered: list[dict] = []
        narrator_facts: list[str] = []
        ticket = runtime.issue_attach_ticket(
            conversation_id="conversation-rejected-receipt",
            artifact_ref="artifact:role-bound-app@1",
        )
        registered = await app_handler.handle(
            Method.AUIP_REGISTER,
            {"manifest": MANIFEST, **ticket},
        )
        sid = str(registered["app_session_id"])
        token = str(registered["bridge_token"])
        await app_handler.handle(
            Method.AUIP_STATE_PUBLISH,
            {
                "app_session_id": sid,
                "bridge_token": token,
                "revision": 1,
                "state": {
                    "turn": "black",
                    "roleBindings": {"participant": "white", "user": "black"},
                },
            },
        )
        runtime.set_stance(app_session_id=sid, stance="participant")

        async def participant(_context: dict) -> dict:
            return {
                "action": "act",
                "type": "game.place_stone",
                "payload": {"x": 7, "y": 7},
            }

        proposal = await coordinator.propose(
            app_session_id=sid,
            controller=participant,
            controller_id="assembly-participant",
            action_required=True,
        )
        requested = await coordinator.invoke(proposal)
        narration = AuipNarrationAdapter(
            runtime=runtime,
            observer=lambda _payload: (_ for _ in ()).throw(
                AssertionError("Host rejection must not be reinterpreted as an app event")
            ),
            narrator=lambda payload: narrator_facts.append(payload["fact_brief"])
            or {
                "display_text": "今は私の手番じゃないから、その手は打てなかったわ。",
                "emotion": "thinking",
            },
            sink=lambda payload: delivered.append(payload) or {"status": "queued"},
            display_language=lambda: "japanese",
            role_prompt=lambda: "CANONICAL MAIN ROLE",
        )
        narration_callback = narration.enqueue_update
        bus.on(Method.AUIP_UPDATED, narration_callback)
        try:
            rejected = await app_handler.handle(
                Method.AUIP_ACTION_RESULT,
                {
                    "app_session_id": sid,
                    "bridge_token": token,
                    "action_id": requested["action"]["action_id"],
                    "accepted": False,
                    "resulting_revision": 1,
                    "reason": "it is not the protocol participant's bound turn",
                },
            )
            await narration.wait_for_idle()

            assert rejected and rejected["operator_error"] == "action_rejected"
            assert rejected["latest_verified_self_action"] is None
            assert len(delivered) == 1
            assert delivered[0]["source"] == "auip_operator_outcome"
            assert "No accepted execution receipt" in narrator_facts[0]
            assert "bound turn" in narrator_facts[0]
        finally:
            bus.off(Method.AUIP_UPDATED, narration_callback)
            await narration.close()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_specialist_action_receipt_and_branch_capsule_cross_the_real_boundaries()
    test_natural_gomoku_step_reaches_an_accepted_receipt_and_new_revision()
    test_user_move_then_explicit_chat_strategy_replaces_private_auto_proposal()
    test_rejected_app_receipt_reaches_role_narration_without_becoming_action_truth()
    print("ok: AUIP assembly preserves specialist, execution, and memory ownership")
