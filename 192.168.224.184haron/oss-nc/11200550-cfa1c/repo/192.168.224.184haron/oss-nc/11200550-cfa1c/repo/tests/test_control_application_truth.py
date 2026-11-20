"""Control application uncertainty is not a no-execution receipt.

The real dispatcher schedules a fake Host handler; no Provider/model is invoked.
Failures are injected on either side of that actual scheduling boundary.
"""

from __future__ import annotations


def test_application_uncertainty_keeps_the_observer_failure_latch_after_ring_eviction():
    from server.turn_decision_shadow import TurnDecisionShadowObserver

    shadow = TurnDecisionShadowObserver(enabled=True)
    shadow.admit_turn(utterance_id="input", turn_id="application-trace", session_id="test", transcript="build", dialogue_source_scope="chat:test", chat_epoch=1)
    shadow.record_event("application-trace", stage="control_authority_resolved",
        origin_kind="host_action_dispatcher", origin_id="application_uncertain",
        payload={"disposition":"failed_closed", "decision_status":"invalid",
                 "execution_uncertain":True})
    event = shadow.snapshot()["recent"][0]["events"][-1]
    assert event["origin_kind"] == "host_action_dispatcher"
    assert event["payload"]["execution_uncertain"] is True
    for i in range(110):
        shadow.record_event("application-trace", stage="bounded_noise", origin_kind="test", origin_id=str(i))
    assert not any(item["stage"] == "control_authority_resolved" for item in shadow.snapshot()["recent"][0]["events"])
    decision = shadow.observe_settlement("application-trace", effective_actions=({"type": "DELEGATE", "attrs": {"intent": "execute", "task": "build"}},))
    assert decision.status == "failed_closed"
    assert len(decision.effects) == 1  # Known request retained, never a no-effect receipt.
