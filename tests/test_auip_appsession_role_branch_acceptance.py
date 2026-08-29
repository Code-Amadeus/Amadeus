from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from core.chat_runtime import ChatRuntime, _TurnState
from server.auip_control_decision import AuipControlDecision
from server.auip_engagement import AuipEngagementCoordinator
from server.auip_runtime import AuipRuntime
from server.event_bus import bus
from server.protocol import Method


def _manifest() -> dict:
    return {
        "schema": "amadeus.auip/v0",
        "app": {
            "id": "branch-acceptance",
            "title": "Branch Acceptance",
            "interactionSummary": (
                "Move one step left or right when a turn opportunity is accepted."
            ),
        },
        "events": {
            "game.turn_ready": {"participantOpportunity": True},
            "game.moved": {"beat": True},
        },
        "actions": {
            "game.move": {
                "description": "Move one legal step.",
                "risk": "local_execution",
            }
        },
        "stances": ["spectator", "participant"],
    }


def test_a1_full_lifecycle_keeps_short_downlink_and_automatic_turns_local() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime(role_branch_mode="a1")
        registered = runtime.register(
            manifest=_manifest(),
            conversation_id="branch-acceptance-chat",
        )
        app_session_id = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.publish_state(
            app_session_id=app_session_id,
            bridge_token=token,
            revision=1,
            state={"turn": "kurisu", "position": 0},
        )
        runtime.set_engagement_mode(
            app_session_id=app_session_id,
            mode="collaborate",
        )

        controller_contexts: list[dict] = []
        gate_contexts: list[dict] = []

        async def controller(context: dict) -> dict:
            controller_contexts.append(context)
            direction = "right" if len(controller_contexts) == 1 else "left"
            return {
                "action": "act",
                "type": "game.move",
                "payload": {"direction": direction},
                "private_note": "one bounded legal move",
            }

        async def authorize(context: dict) -> dict:
            gate_contexts.append(context)
            return {"decision": "approve", "reason": "same visible commitment"}

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=controller,
            role_authorizer=authorize,
            recent_chat=lambda conversation_id: (
                runtime.recent_role_branch_messages(conversation_id, limit=6) or []
            ),
        )
        requested: list[dict] = []

        async def capture(_method: str, payload: dict) -> None:
            requested.append(payload)

        bus.on(Method.AUIP_ACTION_REQUESTED, capture)
        try:
            # This is the same ordering used by ChatRuntime: the visible role
            # turn is staged before AUIP dispatch so a same-turn leave can still
            # be part of the close capsule.
            runtime.record_role_branch_turn(
                conversation_id="branch-acceptance-chat",
                app_session_id=app_session_id,
                user_text="往右。",
                assistant_text="いいわ、右へ行く。",
            )
            engagement.request_step(
                app_session_id=app_session_id,
                instruction="往右。",
                current_role_response="いいわ、右へ行く。",
            )
            await engagement.wait_for_idle(app_session_id)

            first_pending = runtime.get(app_session_id)["pending_action"]
            assert first_pending["payload"] == {"direction": "right"}
            first_global = json.loads(
                controller_contexts[0]["global_conversation_context"]
            )
            assert first_global["instruction"] == "往右。"
            assert first_global["current_role_response"] == "いいわ、右へ行く。"
            assert first_global["recent_chat"][-1]["content"] == "往右。"
            assert all(
                row["content"] != "いいわ、右へ行く。"
                for row in first_global["recent_chat"]
            )
            runtime.resolve_action(
                app_session_id=app_session_id,
                bridge_token=token,
                action_id=first_pending["action_id"],
                accepted=True,
                resulting_revision=2,
                state={"turn": "user", "position": 1},
            )
            runtime.record_delivered_narration(
                app_session_id=app_session_id,
                text="右へ動いたわ。",
            )

            # A later application opportunity is sufficient authority. No new
            # user message or "该你了" heartbeat is required.
            ready = runtime.publish_event(
                app_session_id=app_session_id,
                bridge_token=token,
                event_id="turn-ready-2",
                type="game.turn_ready",
                actor="user",
                revision=2,
                payload={"turn": "kurisu"},
            )
            await engagement.on_update(Method.AUIP_UPDATED, ready)
            await engagement.wait_for_idle(app_session_id)

            second_pending = runtime.get(app_session_id)["pending_action"]
            assert second_pending["payload"] == {"direction": "left"}
            second_global = json.loads(
                controller_contexts[1]["global_conversation_context"]
            )
            assert "accepted participant opportunity" in second_global[
                "instruction"
            ]
            assert "往右" not in second_global["instruction"]
            assert any(
                "Verified AUIP receipt" in row["content"]
                for row in second_global["recent_chat"]
            )
            assert any(
                row["content"] == "右へ動いたわ。"
                for row in second_global["recent_chat"]
            )
            runtime.resolve_action(
                app_session_id=app_session_id,
                bridge_token=token,
                action_id=second_pending["action_id"],
                accepted=True,
                resulting_revision=3,
                state={"turn": "user", "position": 0},
            )

            # Independent Provider Work remains parent-scoped even while the
            # AppSession is focused.
            work_turn = _TurnState(
                gui_callback=None,
                turn_id="independent-work",
                question="帮我查一下 Paxos 论文。",
                session_id="branch-acceptance-chat",
            )
            work_turn.full_response = "調べてみるわ。"
            work_turn.auip_decision_result = AuipControlDecision(
                status="ok",
                action="none",
                work_relation="independent",
            )
            before = runtime.recent_role_branch_messages(
                "branch-acceptance-chat"
            )
            with patch("server.auip_runtime.runtime", runtime):
                ChatRuntime._record_auip_role_branch_turn(work_turn)
            assert work_turn.auip_role_branch_isolated is False
            assert runtime.recent_role_branch_messages(
                "branch-acceptance-chat"
            ) == before

            closed = runtime.close(
                app_session_id=app_session_id,
                bridge_token=token,
                reason="acceptance_complete",
            )
            branch_capsule = closed["experience_capsule"]["role_branch"]
            assert branch_capsule["close_reason"] == "acceptance_complete"
            assert len(branch_capsule["verified_actions"]) == 2
            assert runtime.recent_role_branch_messages(
                "branch-acceptance-chat"
            ) is None
            assert len(gate_contexts) == 1
            assert len(requested) == 2
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)
            await engagement.close()

    asyncio.run(scenario())
