"""Compatibility imports plus the retained joint-owner ablation."""

from dataclasses import replace

from server import control_decision
from server.reference_catalog import validate_candidate_catalog

from server.whole_turn_control import (
    JOINT_MARKER,
    build_joint_messages,
    parse_joint_reply,
)


async def joint_control_reference_owner(messages, proposals, candidates, *, complete,
        query, candidate_limit=64, proposal_controls=(),
        same_turn_reference_context=""):
    if not proposals:
        return control_decision.ControlDecision(status="ok")
    if (len(proposals) != 1 or not complete or len(candidates) > candidate_limit
            or same_turn_reference_context):
        return control_decision.ControlDecision(
            status="incomplete", reason="outside joint diagnostic scope")
    error = validate_candidate_catalog(candidates)
    if error:
        return control_decision.ControlDecision(status="invalid", reason=error)
    for attempt in range(2):
        try:
            raw = await query(build_joint_messages(
                messages, proposals, candidates, protocol_repair=bool(attempt)))
        except Exception as exc:
            return control_decision.ControlDecision(status="unavailable",
                reason=f"{type(exc).__name__}: {exc}", decision_protocol_retries=attempt)
        result = replace(parse_joint_reply(raw, candidates),
            decision_protocol_retries=attempt)
        if result.status != "invalid":
            if any(entry.control.get("intent") not in {"focus", "amend"}
                    for entry in result.entries):
                return replace(result, status="incomplete", entries=(),
                    reason="unsupported operation in joint diagnostic slice")
            return result
    return result

__all__ = (
    "JOINT_MARKER",
    "build_joint_messages",
    "joint_control_reference_owner",
    "parse_joint_reply",
)
