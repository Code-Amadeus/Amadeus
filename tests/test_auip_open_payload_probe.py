from server.auip_action_candidates import compile_auip_action_candidates
from server.auip_role_branch_experiment import AppSessionBranchProposal
from tools.probes.probe_auip_appsession_branch_abc import (
    InMemoryOpenPayloadReactor,
)


def test_open_reactor_exposes_locked_reset_and_uncovered_policy() -> None:
    fixture = InMemoryOpenPayloadReactor("test", 1)

    compiled = compile_auip_action_candidates(
        fixture.runtime,
        fixture.app_session_id,
    )

    assert compiled.uncovered_action_types == (
        "reactor.set_regulation_policy",
    )
    assert {
        (candidate.action_type, tuple(candidate.payload.items()))
        for candidate in compiled.candidates.values()
    } == {("reactor.reset_run", ())}


def test_open_reactor_stabilized_state_closes_to_reset_candidate() -> None:
    fixture = InMemoryOpenPayloadReactor("test", 1)
    fixture.prepare("stabilized")

    compiled = compile_auip_action_candidates(
        fixture.runtime,
        fixture.app_session_id,
    )

    assert compiled.complete is True
    assert {
        candidate.action_type for candidate in compiled.candidates.values()
    } == {"reactor.reset_run"}


def test_open_reactor_application_rejects_payload_outside_declared_shape() -> None:
    fixture = InMemoryOpenPayloadReactor("test", 1)

    receipt = fixture.apply(
        AppSessionBranchProposal(
            action="act",
            action_type="reactor.set_regulation_policy",
            payload={
                "targetTemperature": 50,
                "tolerance": 2,
                "invented": True,
            },
        )
    )

    assert receipt["accepted"] is False
    assert receipt["resulting_revision"] == 1
    assert receipt["reason"] == "invalid or unavailable regulation policy"
