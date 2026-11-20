from __future__ import annotations

import asyncio

from server.auip_participant import AuipParticipantCoordinator
from server.auip_participant_llm import AUIP_PARTICIPANT_SYSTEM_PROMPT
from server.auip_runtime import AuipRuntime
from server.event_bus import bus
from server.protocol import Method


def _manifest() -> dict:
    return {
        "schema": "amadeus.auip/v0",
        "app": {"id": "gomoku", "title": "Gomoku", "version": "0.1.0"},
        "events": {"game.move_committed": {"beat": True}},
        "actions": {
            "game.place_stone": {
                "description": "Place one stone.",
                "risk": "local_execution",
            }
        },
        "stances": ["spectator", "participant"],
    }


def test_specialist_controller_proposes_but_does_not_own_execution_truth() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        registered = runtime.register(manifest=_manifest(), conversation_id="game-chat")
        sid = registered["app_session_id"]
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=registered["bridge_token"],
            revision=1,
            state={"turn": "kurisu", "board": []},
        )
        runtime.set_stance(app_session_id=sid, stance="participant")
        coordinator = AuipParticipantCoordinator(runtime)
        requested: list[dict] = []

        async def capture(_method: str, payload: dict) -> None:
            requested.append(payload)

        bus.on(Method.AUIP_ACTION_REQUESTED, capture)

        async def specialist(context: dict) -> dict:
            assert context["state"]["turn"] == "kurisu"
            assert context["global_conversation_context"] == "The user asked for a defensive move."
            return {
                "type": "game.place_stone",
                "payload": {"x": 7, "y": 7},
                "private_note": "long hidden search tree that must stay out of role context",
            }

        try:
            proposal = await coordinator.propose(
                app_session_id=sid,
                controller=specialist,
                controller_id="gomoku-specialist",
                global_context="The user asked for a defensive move.",
            )
            assert requested == []
            proposed = await coordinator.invoke(proposal)
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, capture)
        assert requested[-1]["action"]["type"] == "game.place_stone"
        assert requested[-1]["action"]["proposal_id"] == proposal.proposal_id
        context_before_receipt = runtime.render_main_chat_context("game-chat")
        assert "latest_verified_self_action=" not in context_before_receipt
        assert "long hidden search tree" not in context_before_receipt
        trace = coordinator.debug_trace(proposal.trace_id)
        assert trace and "long hidden search tree" in trace["private_note"]

        runtime.resolve_action(
            app_session_id=sid,
            bridge_token=registered["bridge_token"],
            action_id=proposed["action"]["action_id"],
            accepted=True,
            resulting_revision=2,
            state={"turn": "user", "board": [{"x": 7, "y": 7, "actor": "kurisu"}]},
            effects={"placed": {"x": 7, "y": 7}},
        )
        context_after_receipt = runtime.render_main_chat_context("game-chat")
        assert "latest_verified_self_action=" in context_after_receipt
        assert proposal.proposal_id in context_after_receipt
        assert "long hidden search tree" not in context_after_receipt

    asyncio.run(scenario())


def test_model_contract_sequences_prerequisites_and_treats_choices_as_closed() -> None:
    assert "propose that prerequisite first" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "Never substitute a prerequisite" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "Never skip it" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "complete\nsmall legal set for the action types represented" in (
        AUIP_PARTICIPANT_SYSTEM_PROMPT
    )
    assert "Other supplied actions" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "never a new authority" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "interactionSummary" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "actionTypes" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "no current option is unavailable" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "plausible mappings rather than commands" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "cannot actually produce" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "not manifest-type authority" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "you still own" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "supplied tool schema is the sole exact payload surface" in (
        AUIP_PARTICIPANT_SYSTEM_PROMPT
    )
    assert "Never copy unsupported" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "concrete supported\nalternative is settled" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "role visibly gave its reason" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "Do not reduce\nthis rule to obedience" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "nonterminal round result" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "resign, withdraw, or stop action" in AUIP_PARTICIPANT_SYSTEM_PROMPT
    assert "Host observe/leave" in AUIP_PARTICIPANT_SYSTEM_PROMPT


def _main() -> None:
    test_specialist_controller_proposes_but_does_not_own_execution_truth()
    test_model_contract_sequences_prerequisites_and_treats_choices_as_closed()
    print("ok: AUIP specialist controller is separate from role memory and execution truth")


if __name__ == "__main__":
    _main()
