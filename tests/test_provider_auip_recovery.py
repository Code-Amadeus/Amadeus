"""Typed AUIP recovery remains one bounded successor of authorized Work."""

from __future__ import annotations

import json

import pytest

from agent_host.provider_types import ProviderRecoveryContext


def test_progress_recovery_shape_remains_byte_compatible_without_feedback() -> None:
    recovery = ProviderRecoveryContext(
        reason=" progress_only_completion ",
        root_attempt_id=" attempt-root ",
        predecessor_attempt_id=" attempt-previous ",
    )
    assert recovery.feedback == ""
    assert recovery.to_dict() == {
        "reason":"progress_only_completion",
        "root_attempt_id":"attempt-root",
        "predecessor_attempt_id":"attempt-previous",
        "ordinal":1,
    }


def test_auip_recovery_requires_bounded_feedback_and_preserves_typed_lineage() -> None:
    feedback = "App preflight failed: expected one registered action."
    recovery = ProviderRecoveryContext(
        reason=" AUIP_VALIDATION_FAILED ",
        root_attempt_id="attempt-root",
        predecessor_attempt_id="attempt-previous",
        ordinal=1,
        feedback="  " + feedback + "  ",
    )
    assert recovery.reason == "auip_validation_failed"
    assert recovery.feedback == feedback
    assert recovery.to_dict() == {
        "reason":"auip_validation_failed",
        "root_attempt_id":"attempt-root",
        "predecessor_attempt_id":"attempt-previous",
        "ordinal":1,
        "feedback":feedback,
    }
    assert json.loads(json.dumps(recovery.to_dict())) == recovery.to_dict()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reason":"auip_validation_failed", "feedback":""},
        {"reason":"auip_validation_failed", "feedback":"x" * 4097},
        {"reason":"auip_validation_failed", "feedback":{"error":"failed"}},
        {"reason":"auip_validation_failed", "feedback":"failed", "ordinal":2},
        {"reason":"progress_only_completion", "feedback":"not allowed"},
        {"reason":"unknown", "feedback":"failed"},
    ],
)
def test_recovery_rejects_unbounded_or_cross_contract_shapes(kwargs) -> None:
    with pytest.raises((TypeError, ValueError)):
        ProviderRecoveryContext(
            root_attempt_id="attempt-root",
            predecessor_attempt_id="attempt-previous",
            **kwargs,
        )


def test_auip_feedback_accepts_exact_maximum_length() -> None:
    recovery = ProviderRecoveryContext(
        reason="auip_validation_failed",
        root_attempt_id="attempt-root",
        predecessor_attempt_id="attempt-previous",
        feedback="x" * 4096,
    )
    assert len(recovery.feedback) == 4096
