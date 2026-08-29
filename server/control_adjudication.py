"""Stable runtime entry point for ControlDecision adjudication.

The implementation originated as a shadow observer. Re-exporting its neutral
types here keeps rollout mode out of callers and leaves the historical module
available to existing probes.
"""

from server.control_shadow import (
    ControlShadowContext as ControlDecisionContext,
    ControlShadowEvidence as ControlDecisionEvidence,
    ControlDecisionAdjudicator,
    RuntimeControlDecisionResolver,
)

__all__ = [
    "ControlDecisionAdjudicator",
    "ControlDecisionContext",
    "ControlDecisionEvidence",
    "RuntimeControlDecisionResolver",
]
