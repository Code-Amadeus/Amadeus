"""Proposal-gated decomposition into grounded exact-source control clauses.

The inline transport still owns action existence. Once one role proposal
exists, this module asks whether the current user turn contains several
independently actionable clauses, validates source provenance, and runs the
existing ControlDecision pipeline on each clause in isolation. Zero/one clause
preserves the established single decision; multi-clause plans may be observed
in shadow or consumed by the separate Host authority policy. This module owns
no dispatcher.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Awaitable, Callable, Iterable, Literal, Mapping, Sequence

from server.control_decision import (
    CONTROL_REFERENCE_CANDIDATES_ATTR,
    DEFAULT_EXHAUSTIVE_CANDIDATE_LIMIT,
    reconcile_control_decision,
    resolve_control_decision,
)
from server.reference_catalog import TypedReferenceCandidate


CompoundPlanStatus = Literal["ok", "invalid", "unavailable", "incomplete"]
CompoundQueryPort = Callable[[list[dict[str, str]]], Awaitable[str]]
MAX_COMPOUND_OPERATIONS = 3
MAX_DECOMPOSITION_HISTORY_MESSAGES = 8


_DECOMPOSITION_SYSTEM = """[Compound control decomposition - FINAL]
You are a shadow semantic parser. You do not execute work and you do not choose
Project or WorkItem identities. One structured role proposal already proves
that this user turn may contain Host control; your only job is to separate the
current user's own affirmative control requests into independently actionable
clauses.

Return exactly one JSON object: {"clauses":["exact source substring", ...]}.

Rules:
- Copy every clause character-for-character as one contiguous substring of the
  current user message. Never paraphrase, translate, repair, or add words.
- Preserve source order. Return at most three clauses.
- Include only clauses that themselves ask or direct the system to execute,
  amend, report ledger state, retract work, switch context, or freshly observe
  external state. History may resolve pronouns but may not supply an action.
- An explicit request to tell, check, or summarize one existing task or
  Project's status, progress, or result is an independently actionable report
  clause. Keep it even when the same turn also requests execution on another
  object; it is not explanatory chat about that execution.
- Split only independently actionable operations. Requirements and details for
  one goal remain one clause. A context switch plus an operation at that same
  destination remains one clause because focus is a modifier, not a second
  operation.
- Ordinary chat, rationale, reaction, correction without a requested action,
  and desired or hypothetical actions are not clauses.
- If the current turn has one control operation, return one clause. If it has
  none, return an empty list.
- Output JSON only.
[/Compound control decomposition - FINAL]"""


@dataclass(frozen=True, slots=True)
class SourceClause:
    text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class CompoundControlOperation:
    operation_index: int
    source_clause: str
    action: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CompoundControlPlan:
    status: CompoundPlanStatus
    operations: tuple[CompoundControlOperation, ...] = ()
    clauses: tuple[SourceClause, ...] = ()
    raw_reply: str = ""
    reason: str = ""
    decomposition_protocol_retries: int = 0
    decision_queries: int = 0
    candidate_verdict_queries: int = 0
    candidate_protocol_retries: int = 0


@dataclass(frozen=True, slots=True)
class CompoundControlShadowEvidence:
    """Payload-free runtime telemetry for one non-authoritative B arm."""

    turn_id: str
    session_id: str
    status: CompoundPlanStatus
    operations: tuple[CompoundControlOperation, ...]
    clauses: tuple[SourceClause, ...]
    reason: str
    latency_ms: int
    decomposition_protocol_retries: int
    decision_queries: int
    candidate_verdict_queries: int
    candidate_protocol_retries: int

    @property
    def decision_status(self) -> CompoundPlanStatus:
        return self.status

    @property
    def outcome(self) -> str:
        return "diverge" if len(self.operations) > 1 else "agree"

    @property
    def canonical_actions(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(operation.action for operation in self.operations)

    @property
    def notes(self) -> tuple[str, ...]:
        return ()

    def as_log_record(self) -> dict[str, Any]:
        return {
            "turnId": self.turn_id,
            "sessionId": self.session_id,
            "status": self.status,
            "operationCount": len(self.operations),
            "clauseCount": len(self.clauses),
            "clauseEvidence": [
                {
                    "index": index,
                    "chars": len(clause.text),
                    "sha256": hashlib.sha256(clause.text.encode("utf-8")).hexdigest(),
                }
                for index, clause in enumerate(self.clauses)
            ],
            "operations": [
                {
                    "operationIndex": operation.operation_index,
                    "control": operation_control_view(operation),
                    "references": _reference_tokens(operation.action),
                }
                for operation in self.operations
            ],
            "reason": self.reason,
            "latencyMs": self.latency_ms,
            "decompositionProtocolRetries": self.decomposition_protocol_retries,
            "decisionQueries": self.decision_queries,
            "candidateVerdictQueries": self.candidate_verdict_queries,
            "candidateProtocolRetries": self.candidate_protocol_retries,
        }


def build_decomposition_messages(
    messages: Sequence[Mapping[str, str]],
    *,
    protocol_repair: bool = False,
) -> list[dict[str, str]]:
    """Keep bounded history while replacing every competing system contract."""

    if not messages or str(messages[0].get("role") or "") != "system":
        raise ValueError("compound decomposition requires a leading system message")
    current_user = _current_user_text(messages)
    if not current_user:
        raise ValueError("compound decomposition requires a current user message")
    prior = [
        {
            "role": str(message.get("role") or ""),
            "content": str(message.get("content") or ""),
        }
        for message in messages[1:-1]
        if str(message.get("role") or "") in {"user", "assistant"}
        and str(message.get("content") or "")
    ][-MAX_DECOMPOSITION_HISTORY_MESSAGES:]
    # Recent dialogue helps discourse parsing, but an unbounded transcript
    # made long-session enumeration unstable. Exact-substring validation still
    # prevents this bounded context from supplying an action absent from the
    # current turn.
    cloned = [
        {"role": "system", "content": _DECOMPOSITION_SYSTEM},
        *prior,
        {"role": "user", "content": current_user},
    ]
    if protocol_repair:
        cloned.append(
            {
                "role": "user",
                "content": (
                    "[Compound decomposition protocol repair]\n"
                    "The previous reply was malformed. Re-evaluate the same current "
                    "user turn and return exactly one JSON object with only a clauses "
                    "array. Every value must be an exact contiguous substring.\n"
                    "[/Compound decomposition protocol repair]"
                ),
            }
        )
    return cloned


def parse_decomposition_reply(reply: str, *, source_user_text: str) -> tuple[SourceClause, ...]:
    """Validate source provenance and derive order without trusting model indexes."""

    raw = str(reply or "").strip()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"decomposition reply is not exact JSON: {exc}") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"clauses"}:
        raise ValueError("decomposition root must contain only clauses")
    values = parsed.get("clauses")
    if not isinstance(values, list) or len(values) > MAX_COMPOUND_OPERATIONS:
        raise ValueError(
            f"clauses must be a list of at most {MAX_COMPOUND_OPERATIONS} items"
        )
    source = str(source_user_text or "")
    clauses: list[SourceClause] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError("every clause must be one non-empty trimmed string")
        if value in seen:
            raise ValueError("duplicate source clause")
        start = source.find(value)
        if start < 0 or source.find(value, start + 1) >= 0:
            raise ValueError("clause is not one uniquely occurring exact source substring")
        end = start + len(value)
        clauses.append(SourceClause(value, start, end))
        seen.add(value)
    clauses.sort(key=lambda clause: clause.start)
    for previous, current in zip(clauses, clauses[1:]):
        if current.start < previous.end:
            raise ValueError("source clauses overlap")
    return tuple(clauses)


def _current_user_text(messages: Sequence[Mapping[str, str]]) -> str:
    return next(
        (
            str(message.get("content") or "")
            for message in reversed(messages)
            if str(message.get("role") or "") == "user"
        ),
        "",
    )


def _messages_for_clause(
    messages: Sequence[Mapping[str, str]],
    clause: SourceClause,
) -> list[dict[str, str]]:
    cloned = [
        {
            "role": str(message.get("role") or ""),
            "content": str(message.get("content") or ""),
        }
        for message in messages
    ]
    current_index = next(
        (
            index
            for index in range(len(cloned) - 1, 0, -1)
            if cloned[index]["role"] == "user"
        ),
        -1,
    )
    if current_index < 0:
        raise ValueError("compound plan requires a current user message")
    cloned[current_index]["content"] = clause.text
    return cloned


async def resolve_compound_control_plan(
    messages: Sequence[Mapping[str, str]],
    proposals: Sequence[Mapping[str, Any]],
    candidates: Sequence[TypedReferenceCandidate],
    *,
    complete: bool,
    query: CompoundQueryPort,
    provider_ids: Iterable[str],
    candidate_limit: int = DEFAULT_EXHAUSTIVE_CANDIDATE_LIMIT,
    proposal_controls: Sequence[Mapping[str, Any]] = (),
) -> CompoundControlPlan:
    """Produce a non-executable plan behind the existing proposal gate."""

    if not proposals:
        return CompoundControlPlan(status="ok")
    if len(proposals) != 1:
        return CompoundControlPlan(
            status="incomplete",
            reason="compound shadow currently requires one sealed proposal gate",
        )
    if not complete:
        return CompoundControlPlan(
            status="incomplete",
            reason="host could not prove the typed reference catalog was complete",
        )
    source_user_text = _current_user_text(messages)
    try:
        raw_reply = await query(build_decomposition_messages(messages))
    except Exception as exc:
        return CompoundControlPlan(
            status="unavailable",
            reason=f"decomposition query unavailable: {type(exc).__name__}: {exc}",
        )
    retries = 0
    try:
        clauses = parse_decomposition_reply(
            raw_reply,
            source_user_text=source_user_text,
        )
    except ValueError:
        retries = 1
        try:
            raw_reply = await query(
                build_decomposition_messages(messages, protocol_repair=True)
            )
            clauses = parse_decomposition_reply(
                raw_reply,
                source_user_text=source_user_text,
            )
        except Exception as exc:
            return CompoundControlPlan(
                status="invalid",
                raw_reply=str(raw_reply or ""),
                reason=f"decomposition protocol invalid: {exc}",
                decomposition_protocol_retries=retries,
            )

    frozen_provider_ids = tuple(str(provider_id) for provider_id in provider_ids)

    # B exists only to expand a proven multi-operation turn. When enumeration
    # finds zero or one clause, preserve the established A path by construction
    # instead of asking an isolated clause classifier to reinterpret a valid
    # focus modifier, placement constraint, or negative turn.
    if len(clauses) <= 1:
        decision = await resolve_control_decision(
            messages,
            proposals,
            candidates,
            complete=True,
            query=query,
            candidate_limit=candidate_limit,
            proposal_controls=proposal_controls,
        )
        actions: list[dict[str, Any]] = []
        notes: list[str] = []
        if decision.status == "ok":
            actions, notes = reconcile_control_decision(
                proposals,
                decision,
                provider_ids=frozen_provider_ids,
                proposal_controls=proposal_controls,
                source_user_text=source_user_text,
            )
        if decision.status != "ok":
            return CompoundControlPlan(
                status=decision.status,
                clauses=clauses,
                raw_reply=str(raw_reply or ""),
                reason=f"single-path decision failed: {decision.reason}",
                decomposition_protocol_retries=retries,
                decision_queries=1 + int(decision.decision_protocol_retries),
                candidate_verdict_queries=decision.candidate_verdict_queries,
                candidate_protocol_retries=decision.candidate_protocol_retries,
            )
        if len(actions) > 1:
            return CompoundControlPlan(
                status="invalid",
                clauses=clauses,
                raw_reply=str(raw_reply or ""),
                reason="single-path ControlDecision returned multiple actions",
                decomposition_protocol_retries=retries,
                decision_queries=1 + int(decision.decision_protocol_retries),
                candidate_verdict_queries=decision.candidate_verdict_queries,
                candidate_protocol_retries=decision.candidate_protocol_retries,
            )
        effective_clauses = clauses
        if actions and not effective_clauses:
            effective_clauses = (
                SourceClause(source_user_text, 0, len(source_user_text)),
            )
        operations = tuple(
            CompoundControlOperation(
                operation_index=index,
                source_clause=(
                    effective_clauses[index].text
                    if index < len(effective_clauses)
                    else source_user_text
                ),
                action=action,
            )
            for index, action in enumerate(actions)
        )
        return CompoundControlPlan(
            status="ok",
            operations=operations,
            clauses=effective_clauses,
            raw_reply=str(raw_reply or ""),
            reason="; ".join(notes),
            decomposition_protocol_retries=retries,
            decision_queries=1 + int(decision.decision_protocol_retries),
            candidate_verdict_queries=decision.candidate_verdict_queries,
            candidate_protocol_retries=decision.candidate_protocol_retries,
        )

    async def resolve_clause(clause: SourceClause):
        clause_messages = _messages_for_clause(messages, clause)
        clause_payload = ({"task": clause.text},)
        decision = await resolve_control_decision(
            clause_messages,
            clause_payload,
            candidates,
            complete=True,
            query=query,
            candidate_limit=candidate_limit,
            same_turn_reference_context=(
                source_user_text
                if clause.text != source_user_text
                else ""
            ),
        )
        actions: list[dict[str, Any]] = []
        if decision.status == "ok":
            actions, _notes = reconcile_control_decision(
                clause_payload,
                decision,
                provider_ids=frozen_provider_ids,
                source_user_text=clause.text,
            )
        return clause, decision, actions

    # Clause semantics are independent; resolve them concurrently, then retain
    # source order for the eventual (still hypothetical) serial dispatcher.
    clause_results = await asyncio.gather(
        *(resolve_clause(clause) for clause in clauses)
    )
    decision_queries = sum(
        1 + int(decision.decision_protocol_retries)
        for _clause, decision, _actions in clause_results
    )
    candidate_queries = sum(
        int(decision.candidate_verdict_queries)
        for _clause, decision, _actions in clause_results
    )
    candidate_retries = sum(
        int(decision.candidate_protocol_retries)
        for _clause, decision, _actions in clause_results
    )
    operations: list[CompoundControlOperation] = []
    seen_context_actions: set[str] = set()
    for clause_index, (clause, decision, actions) in enumerate(clause_results):
        if decision.status != "ok":
            return CompoundControlPlan(
                status=decision.status,
                clauses=clauses,
                raw_reply=str(raw_reply or ""),
                reason=f"clause decision failed: {decision.reason}",
                decomposition_protocol_retries=retries,
                decision_queries=decision_queries,
                candidate_verdict_queries=candidate_queries,
                candidate_protocol_retries=candidate_retries,
            )
        if len(actions) > 1:
            return CompoundControlPlan(
                status="invalid",
                clauses=clauses,
                raw_reply=str(raw_reply or ""),
                reason="one source clause produced more than one canonical action",
                decomposition_protocol_retries=retries,
                decision_queries=decision_queries,
                candidate_verdict_queries=candidate_queries,
                candidate_protocol_retries=candidate_retries,
            )
        if not actions:
            continue
        action = dict(actions[0])
        if str(action.get("intent") or "").strip().lower() == "focus":
            # Context-only focus never owns Provider payload. If discourse
            # decomposition repeats the same destination constraint, collapse
            # the idempotent control rather than publishing two confirmations.
            for field in ("task", "url", "query", "text"):
                action.pop(field, None)
            context_key = json.dumps(
                {
                    key: value
                    for key, value in action.items()
                    if not str(key).startswith("_host_")
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if context_key in seen_context_actions:
                continue
            seen_context_actions.add(context_key)
        else:
            action["task"] = clause.text
            action["_host_payload_source"] = "exact_current_user_clause"
        operations.append(
            CompoundControlOperation(
                operation_index=clause_index,
                source_clause=clause.text,
                action=action,
            )
        )

    return CompoundControlPlan(
        status="ok",
        operations=tuple(operations),
        clauses=clauses,
        raw_reply=str(raw_reply or ""),
        decomposition_protocol_retries=retries,
        decision_queries=decision_queries,
        candidate_verdict_queries=candidate_queries,
        candidate_protocol_retries=candidate_retries,
    )


def operation_control_view(operation: CompoundControlOperation) -> dict[str, Any]:
    """Stable, payload-free comparison view for A/B reports."""

    hidden = {"task", "url", "query", "text"}
    return {
        key: value
        for key, value in operation.action.items()
        if key not in hidden and not str(key).startswith("_host_")
    }


def _reference_tokens(action: Mapping[str, Any]) -> list[str]:
    references = action.get(CONTROL_REFERENCE_CANDIDATES_ATTR)
    if not isinstance(references, tuple):
        return []
    return [
        candidate.token
        for candidate in references
        if isinstance(candidate, TypedReferenceCandidate)
    ]
