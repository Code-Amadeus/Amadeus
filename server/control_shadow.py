"""Runtime ControlDecision resolution and evidence capture.

This module owns no dispatch path. It captures host facts synchronously at the
completed-proposal boundary, runs one control decision plus bounded independent
candidate verdicts, and records how that result differs from the role proposal.
The caller decides whether to observe the evidence in shadow mode or apply it
through the separate authority rollout policy.
Disagreement is deliberately called a divergence rather than a correction:
only an expected-case matrix or later user outcome can supply ground truth.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import time
from typing import Any, Awaitable, Callable, Iterable, Mapping

from server.compound_control_shadow import (
    CompoundControlShadowEvidence,
    resolve_compound_control_plan,
)
from server.control_decision import (
    CONTROL_REFERENCE_CANDIDATES_ATTR,
    CONTROL_FIELDS,
    DEFAULT_EXHAUSTIVE_CANDIDATE_LIMIT,
    ControlDecision,
    reconcile_control_decision,
    resolve_control_decision,
)
from server.control_proposal import ControlProposalBatch
from server.reference_catalog import (
    TypedReferenceCandidate,
    candidate_catalog_from_coordinator,
)


logger = logging.getLogger(__name__)

ControlShadowQueryPort = Callable[[list[dict[str, str]]], Awaitable[str]]
ControlShadowSink = Callable[["ControlShadowEvidence"], Any]
CompoundControlShadowSink = Callable[[CompoundControlShadowEvidence], Any]


@dataclass(frozen=True, slots=True)
class ControlShadowContext:
    messages: tuple[Mapping[str, str], ...]
    candidates: tuple[TypedReferenceCandidate, ...]
    catalog_complete: bool
    exhaustive_candidate_limit: int
    provider_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class ControlShadowEvidence:
    turn_id: str
    session_id: str
    transport: str
    commit_point: str
    decision_status: str
    outcome: str
    raw_controls: tuple[Mapping[str, Any], ...]
    raw_references: tuple[tuple[str, ...] | None, ...]
    canonical_controls: tuple[Mapping[str, Any], ...]
    canonical_references: tuple[tuple[str, ...] | None, ...]
    # Full payload-bearing actions are available only to explicit in-process
    # probes. They are intentionally omitted from routine log records.
    canonical_actions: tuple[Mapping[str, Any], ...]
    notes: tuple[str, ...]
    reason: str
    decision_reply: str
    latency_ms: int
    candidate_count: int
    decision_protocol_retries: int
    candidate_verdict_queries: int
    candidate_protocol_retries: int
    # Explicit sinks may inspect malformed output; routine log records omit it.
    candidate_failure_reply: str
    exhaustive_candidate_limit: int
    catalog_complete: bool

    def as_log_record(self) -> dict[str, Any]:
        return {
            "turnId": self.turn_id,
            "sessionId": self.session_id,
            "transport": self.transport,
            "commitPoint": self.commit_point,
            "decisionStatus": self.decision_status,
            "outcome": self.outcome,
            "rawControls": [dict(item) for item in self.raw_controls],
            "rawReferences": [
                list(tokens) if tokens is not None else None
                for tokens in self.raw_references
            ],
            "canonicalControls": [dict(item) for item in self.canonical_controls],
            "canonicalReferences": [
                list(tokens) if tokens is not None else None
                for tokens in self.canonical_references
            ],
            "notes": list(self.notes),
            "reason": self.reason,
            "latencyMs": self.latency_ms,
            "candidateCount": self.candidate_count,
            "decisionProtocolRetries": self.decision_protocol_retries,
            "candidateVerdictQueries": self.candidate_verdict_queries,
            "candidateProtocolRetries": self.candidate_protocol_retries,
            "exhaustiveCandidateLimit": self.exhaustive_candidate_limit,
            "catalogComplete": self.catalog_complete,
        }


def _control_view(action: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in action.items()
        if key in CONTROL_FIELDS and value not in (None, "", False)
    }


def _comparison_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip()
    if text.lower() in {"true", "yes", "on", "1"}:
        return True
    if text.lower() in {"false", "no", "off", "0"}:
        return False
    return text


def _comparison_controls(items: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            key: _comparison_value(value)
            for key, value in sorted(_control_view(item).items())
        }
        for item in items
    )


def _reference_view(action: Mapping[str, Any]) -> tuple[str, ...] | None:
    candidates = action.get(CONTROL_REFERENCE_CANDIDATES_ATTR)
    if candidates is None:
        return None
    if not isinstance(candidates, tuple) or any(
        not isinstance(candidate, TypedReferenceCandidate)
        for candidate in candidates
    ):
        return ()
    return tuple(candidate.token for candidate in candidates)


def _raw_reference_view(action: Mapping[str, Any]) -> tuple[str, ...] | None:
    references: list[str] = []
    project_id = str(action.get("project_id") or "").strip()
    if project_id:
        references.append(f"project:{project_id}")
    workspace_ref = str(action.get("workspace_ref") or "").strip()
    if workspace_ref:
        references.append(f"work_item:{workspace_ref}")
    return tuple(references) if references else None


class ControlDecisionAdjudicator:
    """Run and record one immutable ControlDecision without dispatching it."""

    def __init__(
        self,
        *,
        query: ControlShadowQueryPort,
        sink: ControlShadowSink | None = None,
        compound_sink: CompoundControlShadowSink | None = None,
    ) -> None:
        self._query = query
        self._sink = sink
        self._compound_sink = compound_sink

    async def observe(
        self,
        batch: ControlProposalBatch,
        context: ControlShadowContext,
    ) -> ControlShadowEvidence:
        decision: ControlDecision = await resolve_control_decision(
            context.messages,
            batch.decision_payloads(),
            context.candidates,
            complete=context.catalog_complete,
            query=self._query,
            candidate_limit=context.exhaustive_candidate_limit,
            proposal_controls=batch.proposals,
        )
        canonical, notes = reconcile_control_decision(
            batch.decision_payloads(),
            decision,
            provider_ids=context.provider_ids,
            proposal_controls=batch.proposals,
            source_user_text=batch.user_text,
        )
        raw_controls = tuple(_control_view(item) for item in batch.proposals)
        raw_references = tuple(_raw_reference_view(item) for item in batch.proposals)
        canonical_controls = tuple(_control_view(item) for item in canonical)
        canonical_references = tuple(_reference_view(item) for item in canonical)
        if decision.status != "ok":
            outcome = decision.status
        elif not canonical_controls:
            outcome = "suppressed"
        elif (
            _comparison_controls(raw_controls) == _comparison_controls(canonical_controls)
            and raw_references == canonical_references
        ):
            outcome = "agree"
        else:
            outcome = "diverge"
        evidence = ControlShadowEvidence(
            turn_id=batch.turn_id,
            session_id=batch.session_id,
            transport=batch.transport,
            commit_point=batch.commit_point,
            decision_status=decision.status,
            outcome=outcome,
            raw_controls=raw_controls,
            raw_references=raw_references,
            canonical_controls=canonical_controls,
            canonical_references=canonical_references,
            canonical_actions=tuple(dict(item) for item in canonical),
            notes=tuple(notes),
            reason=decision.reason,
            # Available to explicit evidence sinks for reproducible probes,
            # but omitted from as_log_record(): malformed output is not
            # guaranteed to obey the payload-free decision contract.
            decision_reply=decision.raw_reply,
            latency_ms=max(
                0,
                int(round((time.monotonic() - batch.sealed_at_monotonic) * 1000)),
            ),
            candidate_count=len(context.candidates),
            decision_protocol_retries=decision.decision_protocol_retries,
            candidate_verdict_queries=decision.candidate_verdict_queries,
            candidate_protocol_retries=decision.candidate_protocol_retries,
            candidate_failure_reply=decision.candidate_failure_reply,
            exhaustive_candidate_limit=context.exhaustive_candidate_limit,
            catalog_complete=context.catalog_complete,
        )
        await self._publish(evidence)
        return evidence

    async def _publish(self, evidence: ControlShadowEvidence) -> None:
        if self._sink is None:
            logger.info(
                "[CONTROL-DECISION] %s",
                json.dumps(evidence.as_log_record(), ensure_ascii=False, separators=(",", ":")),
            )
            return
        result = self._sink(evidence)
        if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
            await result

    async def observe_compound(
        self,
        batch: ControlProposalBatch,
        context: ControlShadowContext,
    ) -> CompoundControlShadowEvidence:
        """Run B beside the shipping decision and expose no executable handle."""

        started = time.monotonic()
        plan = await resolve_compound_control_plan(
            context.messages,
            batch.decision_payloads(),
            context.candidates,
            complete=context.catalog_complete,
            query=self._query,
            provider_ids=context.provider_ids,
            candidate_limit=context.exhaustive_candidate_limit,
            proposal_controls=batch.proposals,
        )
        evidence = CompoundControlShadowEvidence(
            turn_id=batch.turn_id,
            session_id=batch.session_id,
            status=plan.status,
            operations=plan.operations,
            clauses=plan.clauses,
            reason=plan.reason,
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            decomposition_protocol_retries=plan.decomposition_protocol_retries,
            decision_queries=plan.decision_queries,
            candidate_verdict_queries=plan.candidate_verdict_queries,
            candidate_protocol_retries=plan.candidate_protocol_retries,
        )
        if self._compound_sink is None:
            logger.info(
                "[COMPOUND-CONTROL-SHADOW] %s",
                json.dumps(
                    evidence.as_log_record(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        else:
            result = self._compound_sink(evidence)
            if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                await result
        return evidence


class RuntimeControlDecisionResolver:
    """Capture runtime history and typed entity facts before any dispatch."""

    def __init__(
        self,
        *,
        coordinator,
        query: ControlShadowQueryPort,
        sink: ControlShadowSink | None = None,
        compound_shadow: bool = False,
        compound_sink: CompoundControlShadowSink | None = None,
        project_limit: int = 200,
        work_item_limit: int = 200,
        exhaustive_candidate_limit: int = DEFAULT_EXHAUSTIVE_CANDIDATE_LIMIT,
    ) -> None:
        self._coordinator = coordinator
        self._observer = ControlDecisionAdjudicator(
            query=query,
            sink=sink,
            compound_sink=compound_sink,
        )
        self._compound_shadow = bool(compound_shadow)
        self._project_limit = max(1, int(project_limit))
        self._work_item_limit = max(1, int(work_item_limit))
        self._exhaustive_candidate_limit = max(
            1, int(exhaustive_candidate_limit)
        )

    def capture(self, batch: ControlProposalBatch):
        """Return an awaitable after synchronously freezing every host fact."""

        return self._observer.observe(batch, self._capture_context(batch))

    def capture_compound_shadow(self, batch: ControlProposalBatch):
        """Return an independent B-arm awaitable, or nothing while disabled."""

        if not self._compound_shadow:
            return None
        return self._observer.observe_compound(batch, self._capture_context(batch))

    def _capture_context(self, batch: ControlProposalBatch) -> ControlShadowContext:
        """Freeze one catalog/history view shared by A and B."""

        from llm.prompts import get_structured_control_prompt, registered_provider_ids
        from server.work_context import augment_system_prompt_for_control_decision

        candidates, complete, _catalog_reason = candidate_catalog_from_coordinator(
            self._coordinator,
            batch.session_id,
            project_limit=self._project_limit,
            work_item_limit=self._work_item_limit,
        )
        system_prompt = augment_system_prompt_for_control_decision(
            get_structured_control_prompt(),
            session_id=batch.session_id,
        )
        messages = (
            {"role": "system", "content": system_prompt},
            *(
                {
                    "role": str(message.get("role") or ""),
                    "content": str(message.get("content") or ""),
                }
                for message in batch.prior_messages
            ),
            {"role": "user", "content": batch.user_text},
        )
        return ControlShadowContext(
            messages=messages,
            candidates=candidates,
            catalog_complete=complete,
            exhaustive_candidate_limit=self._exhaustive_candidate_limit,
            provider_ids=frozenset(registered_provider_ids()),
        )


# Compatibility names for probes and tests written while this resolver was
# shadow-only. New production wiring uses the neutral names above.
ControlDecisionShadowObserver = ControlDecisionAdjudicator
RuntimeControlDecisionShadow = RuntimeControlDecisionResolver
