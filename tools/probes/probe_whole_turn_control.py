"""Compatibility imports plus the retained single-owner ablation."""

from server import compound_control as compound, control_decision
from server.whole_turn_control import (
    WHOLE_TURN_MARKER,
    _scope_failure,
    build_whole_turn_messages,
    parse_whole_turn_reply,
    whole_turn_owner,
)


async def whole_input_single_owner(messages, proposals, candidates, *, complete, query,
        provider_ids, candidate_limit=64, proposal_controls=()):
    """Ablation only: existing one-slot decision without Compound decomposition."""
    failure = _scope_failure(messages, proposals, candidates, complete, candidate_limit)
    if failure is not None:
        return failure
    decision = await control_decision.resolve_control_decision(
        messages, proposals, candidates, complete=complete, query=query,
        candidate_limit=candidate_limit, proposal_controls=proposal_controls)
    if decision.status != "ok":
        return compound.CompoundControlPlan(status=decision.status, reason=decision.reason,
            decision_queries=1 + decision.decision_protocol_retries,
            candidate_verdict_queries=decision.candidate_verdict_queries,
            candidate_protocol_retries=decision.candidate_protocol_retries)
    source = compound._current_user_text(messages)
    actions, notes = control_decision.reconcile_control_decision(
        proposals, decision, provider_ids=provider_ids,
        proposal_controls=proposal_controls, source_user_text=source)
    operations = []
    for action in actions:
        action = dict(action)
        if (str(action.get("intent") or "").strip().lower() != "focus"
                and control_decision.CONTROL_PAYLOAD_GROUNDING_ATTR not in action):
            action["task"] = source
            action["_host_payload_source"] = "exact_current_user_clause"
        operations.append(compound.CompoundControlOperation(
            len(operations), source, action))
    return compound.CompoundControlPlan(status="ok", operations=tuple(operations),
        clauses=(compound.SourceClause(source, 0, len(source)),) if actions else (),
        reason="; ".join(notes),
        decision_queries=1 + decision.decision_protocol_retries,
        candidate_verdict_queries=decision.candidate_verdict_queries,
        candidate_protocol_retries=decision.candidate_protocol_retries)


__all__ = (
    "WHOLE_TURN_MARKER",
    "build_whole_turn_messages",
    "parse_whole_turn_reply",
    "whole_turn_owner",
    "whole_input_single_owner",
)
