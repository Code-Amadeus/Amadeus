"""Observe one user-turn decision lineage without changing runtime authority.

This is the Phase-A/early-shadow boundary for the unified turn-decision
experiment.  Existing source-specific witnesses remain authoritative:

* Main Chat's sealed proposal witnesses Work action existence;
* AUIP's source-local decision witnesses application action existence;
* Browser/B2 direct branches keep their existing structural contracts.

The observer records how those witnesses converge and compiles a read-only
``ShadowTurnDecision``.  It never dispatches, retries, grounds an identity,
creates a WorkItem, or treats an absent proposal as proof of no user intent.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from itertools import islice
import json
import logging
from threading import RLock
import time
from typing import Any, Iterable, Literal, Mapping, Sequence
import uuid


from server.turn_admission import (
    TurnAdmissionRecord,
    capture_turn_admission,
    _bounded_json,
    _digest,
    _stable_id,
    _PAYLOAD_COLLECTION_CAP,
    _PAYLOAD_STRING_CAP,
    _PAYLOAD_NODE_CAP,
    _PAYLOAD_CHAR_CAP,
)


logger = logging.getLogger("turn_decision_shadow")

_DECISION_NAMESPACE = uuid.UUID("5373d989-3731-4d99-82af-f32cccfbaae6")
_EFFECT_NAMESPACE = uuid.UUID("5652ced5-56c2-465e-87ec-9aa19ee4beb6")
_GOAL_NAMESPACE = uuid.UUID("45a595b9-0838-4ad1-b58b-10f31f875e72")

_ROOT_CAP = 64
_EVENT_CAP = 96
_EFFECT_CAP = 40
_INVARIANT_VIOLATION_CAP = 32
_TURN_ALIAS_CAP = 7
_LOG_LINE_CHAR_CAP = 16_384
_STALE_EFFECT_STAGES = frozenset(
    {
        "shadow_turn_decision_observed",
        "shadow_turn_decision_mutated",
        "legacy_dispatch_accepted",
        "work_attempt_admitted",
        "provider_run_created",
        "auip_action_requested",
        "routing_scope_lease_consumed",
        "direct_branch_decision_observed",
        "direct_branch_decision_mutated",
    }
)
_STALE_LIFECYCLES = frozenset({"superseded", "discarded", "expired"})
_TERMINAL_LIFECYCLES = _STALE_LIFECYCLES | {"completed", "failed", "cancelled"}
_MILESTONE_STAGES = {
    "role_request_sent": "role_request_sent",
    "first_sentence_enqueued": "first_sentence_enqueued",
    "control_proposal_sealed": "proposal_sealed",
    "shadow_turn_decision_observed": "decision_settled",
    "direct_branch_decision_observed": "decision_settled",
    "legacy_dispatch_accepted": "dispatch_accepted",
    "provider_run_created": "provider_run_created",
    "provider_adapter_started": "provider_adapter_started",
    "provider_terminal_published": "provider_terminal_published",
    "auip_action_requested": "auip_action_requested",
    "auip_action_receipt": "auip_action_receipt",
    "terminal_disposition_observed": "terminal_disposition",
}
_MILESTONE_NAMES = frozenset(_MILESTONE_STAGES.values()) | {
    "first_audio_write_completed",
}

TurnDecisionShadowStatus = Literal[
    "observed_no_effect",
    "observed_attention",
    "observed_effects",
    "failed_closed",
]
EffectAxis = Literal["work", "auip", "browser", "attention", "focus"]
DependencyCondition = Literal[
    "requires_acceptance",
    "requires_dispatch",
    "requires_run_created",
    "requires_terminal",
    "requires_success",
    "requires_verified_outcome",
]


def _shadow_enabled() -> bool:
    try:
        from config.settings import TURN_DECISION_SHADOW_ENABLED

        return bool(TURN_DECISION_SHADOW_ENABLED)
    except Exception:
        return False


@dataclass(frozen=True, slots=True)
class ShadowEffect:
    effect_id: str
    axis: EffectAxis
    operation: str
    witness_kind: str
    witness_id: str
    ordinal: int
    goal_group_id: str | None = None
    target_kind: str = ""
    target_id: str = ""
    payload_digest: str = ""
    admission_status: str = "observed"


@dataclass(frozen=True, slots=True)
class ExecutionDependency:
    """The only relation family that forms an executable DAG."""

    parent_effect_id: str
    child_effect_id: str
    condition_kind: DependencyCondition
    condition: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SemanticConstraint:
    """Non-topological semantic relation such as subsumption or conflict."""

    source_effect_id: str
    target_effect_id: str
    kind: Literal["subsumes", "conflicts"]
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EffectLineage:
    """Cross-decision correction/supersession; never a DAG dependency."""

    effect_id: str
    prior_effect_id: str
    kind: Literal["supersedes", "corrects"]


@dataclass(frozen=True, slots=True)
class TargetAction:
    """A cancel/retract operation against an existing target."""

    effect_id: str
    target_kind: str
    target_id: str
    action: Literal["cancel", "retract"]


@dataclass(frozen=True, slots=True)
class ShadowTurnDecision:
    """Read-only projection of the already-effective legacy axes."""

    decision_id: str
    decision_lineage_id: str
    root_id: str
    turn_id: str
    status: TurnDecisionShadowStatus
    planner_source: str
    schema_version: str
    effects: tuple[ShadowEffect, ...] = ()
    execution_dependencies: tuple[ExecutionDependency, ...] = ()
    semantic_constraints: tuple[SemanticConstraint, ...] = ()
    lineage: tuple[EffectLineage, ...] = ()
    target_actions: tuple[TargetAction, ...] = ()
    notes: tuple[str, ...] = ()
    observed_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return _bounded_json(asdict(self))


@dataclass(frozen=True, slots=True)
class TurnTraceEvent:
    sequence: int
    root_id: str
    turn_id: str
    stage: str
    origin_kind: str
    origin_id: str
    arrived_at: float
    arrived_at_monotonic: float
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ShadowTerminalDisposition:
    """Immutable observation closure, not an executable Plan or acceptance."""

    terminal_id: str
    lifecycle: str
    disposition: str
    decision_id: str | None
    observed_effect_count: int | None
    reason: str
    finalized_at: float
    finalized_at_monotonic: float


@dataclass(slots=True)
class _ObservedTurn:
    admission: TurnAdmissionRecord
    events: deque[TurnTraceEvent] = field(
        default_factory=lambda: deque(maxlen=_EVENT_CAP)
    )
    decision: ShadowTurnDecision | None = None
    lifecycle: str = "open"
    terminal: ShadowTerminalDisposition | None = None
    milestones: dict[str, float] = field(default_factory=dict)
    authority_failed_closed: bool = False
    invariant_violations: deque[str] = field(
        default_factory=lambda: deque(maxlen=_INVARIANT_VIOLATION_CAP)
    )
    invariant_violation_count: int = 0
    turn_aliases: deque[str] = field(
        default_factory=lambda: deque(maxlen=_TURN_ALIAS_CAP)
    )


def _public_control(attrs: Mapping[str, Any]) -> dict[str, Any]:
    public: dict[str, Any] = {}
    # Host-private reference candidates can be numerous. Bound the raw scan as
    # well as the retained public projection so excluded fields cannot turn an
    # observe-only hook into unbounded work.
    for key, value in islice(attrs.items(), _PAYLOAD_COLLECTION_CAP + 1):
        if str(key).startswith("_host_reference_candidates"):
            continue
        if len(public) >= _PAYLOAD_COLLECTION_CAP:
            break
        public[str(key)[:120]] = value
    projected = _bounded_json(public)
    return dict(projected) if isinstance(projected, Mapping) else {}


def _effect_target(attrs: Mapping[str, Any]) -> tuple[str, str]:
    for key, kind in (
        ("work_item_id", "work_item"),
        ("workspace_ref", "work_item"),
        ("project_id", "project"),
        ("projectId", "project"),
        ("app_session_id", "app_session"),
        ("artifact_id", "artifact"),
    ):
        value = str(attrs.get(key) or "").strip()
        if value:
            return kind, value[:240]
    return "", ""


def _make_effect(
    admission: TurnAdmissionRecord,
    *,
    axis: EffectAxis,
    operation: str,
    witness_kind: str,
    witness_id: str,
    ordinal: int,
    payload: Mapping[str, Any] | None = None,
    target_kind: str = "",
    target_id: str = "",
    admission_status: str = "observed",
) -> ShadowEffect:
    safe_payload = _bounded_json(payload or {})
    payload_digest = _digest(safe_payload)
    effect_id = _stable_id(
        _EFFECT_NAMESPACE,
        admission.root_id,
        axis,
        witness_kind,
        witness_id,
        str(ordinal),
        payload_digest,
    )
    return ShadowEffect(
        effect_id=effect_id,
        axis=axis,
        operation=str(operation or "unknown")[:120],
        witness_kind=str(witness_kind or "unknown")[:120],
        witness_id=str(witness_id or "")[:240],
        ordinal=int(ordinal),
        target_kind=str(target_kind or "")[:120],
        target_id=str(target_id or "")[:240],
        payload_digest=payload_digest,
        admission_status=str(admission_status or "observed")[:120],
    )


def _goal_id(admission: TurnAdmissionRecord, label: str) -> str:
    return _stable_id(_GOAL_NAMESPACE, admission.root_id, label)


def _work_effects(
    admission: TurnAdmissionRecord,
    actions: Iterable[Mapping[str, Any]],
) -> tuple[list[ShadowEffect], list[TargetAction], list[str]]:
    effects: list[ShadowEffect] = []
    target_actions: list[TargetAction] = []
    notes: list[str] = []
    for source_index, action in enumerate(actions):
        if str(action.get("type") or "").strip().upper() != "DELEGATE":
            continue
        supplied = action.get("attrs")
        attrs = supplied if isinstance(supplied, Mapping) else {}
        operation = str(attrs.get("intent") or "execute").strip().lower() or "execute"
        axis: EffectAxis = "focus" if operation == "focus" else "work"
        target_kind, target_id = _effect_target(attrs)
        witness_id = str(
            attrs.get("_host_proposal_id")
            or attrs.get("_host_dispatch_source")
            or f"effective-control:{source_index}"
        )
        effect = _make_effect(
            admission,
            axis=axis,
            operation=operation,
            witness_kind="main_chat_control_proposal",
            witness_id=witness_id,
            ordinal=len(effects),
            payload=_public_control(attrs),
            target_kind=target_kind,
            target_id=target_id,
            admission_status="legacy_effective_control",
        )
        effects.append(effect)
        if operation in {"retract", "cancel"}:
            if target_id:
                target_actions.append(
                    TargetAction(
                        effect_id=effect.effect_id,
                        target_kind=target_kind or "work_item",
                        target_id=target_id,
                        action="retract" if operation == "retract" else "cancel",
                    )
                )
            else:
                notes.append("Work retract/cancel effect has no grounded target in shadow evidence")
    return effects, target_actions, notes


def _auip_effect(
    admission: TurnAdmissionRecord,
    decision: Any,
    *,
    ordinal: int,
    dispatched: bool | None,
) -> tuple[ShadowEffect | None, str, str, list[str]]:
    notes: list[str] = []
    if decision is None:
        return None, "", "", notes
    status = str(getattr(decision, "status", "") or "").strip().lower()
    attrs: Mapping[str, Any] = {}
    control_attrs = getattr(decision, "control_attrs", None)
    if callable(control_attrs):
        try:
            candidate = control_attrs()
            if isinstance(candidate, Mapping):
                attrs = candidate
        except Exception as exc:
            notes.append(f"AUIP control attrs unavailable: {type(exc).__name__}")
    action = str(
        attrs.get("action") or getattr(decision, "action", "") or "none"
    ).strip().lower()
    timing = str(
        attrs.get("after") or getattr(decision, "timing", "") or ""
    ).strip().lower()
    relation = str(getattr(decision, "work_relation", "") or "").strip().lower()
    raw_read_facets = getattr(decision, "read_facets", ()) or ()
    read_facets = tuple(islice(raw_read_facets, _PAYLOAD_COLLECTION_CAP))
    ambiguity = str(getattr(decision, "ambiguity", "") or "").strip()

    if ambiguity:
        effect = _make_effect(
            admission,
            axis="attention",
            operation="clarify",
            witness_kind="auip_source_local_decision",
            witness_id=str(getattr(decision, "proposal_id", "") or "auip-ambiguity"),
            ordinal=ordinal,
            payload={"ambiguity": ambiguity, "status": status},
            admission_status="attention_required",
        )
        return effect, timing, relation, notes
    if action in {"", "none"} and not read_facets:
        if status not in {"", "ok", "unavailable"}:
            notes.append(f"AUIP decision status={status} produced no observable effect")
        return None, timing, relation, notes
    if action in {"", "none"}:
        action = "read"
        public_attrs = _public_control(attrs)
        public_attrs["read_facets"] = list(
            islice(read_facets, _PAYLOAD_COLLECTION_CAP)
        )
    else:
        public_attrs = _public_control(attrs)
    app_session_id = str(getattr(decision, "app_session_id", "") or "").strip()
    witness_id = str(
        getattr(decision, "proposal_id", "")
        or getattr(decision, "action_id", "")
        or f"auip-decision:{action}"
    )
    effect = _make_effect(
        admission,
        axis="auip",
        operation=action,
        witness_kind="auip_source_local_decision",
        witness_id=witness_id,
        ordinal=ordinal,
        payload={
            "status": status,
            "action": action,
            "timing": timing,
            "work_relation": relation,
            "attrs": public_attrs,
        },
        target_kind="app_session" if app_session_id else "",
        target_id=app_session_id,
        admission_status=(
            "host_read_projection"
            if action == "read"
            else "legacy_dispatched"
            if dispatched is True
            else "witness_only"
        ),
    )
    return effect, timing, relation, notes


def _direct_branch_effect(
    admission: TurnAdmissionRecord,
    result: Mapping[str, Any],
) -> tuple[ShadowEffect | None, list[str]]:
    route_kind = str(result.get("route_kind") or "").strip().lower()
    provider = str(result.get("provider") or "").strip().lower()
    source = str(result.get("source") or "").strip().lower()
    notes: list[str] = []
    if result.get("execution_uncertain") is True:
        return None, ["Direct branch execution state is unknown; no accepted effect can be inferred"]
    if route_kind == "browser_continuation_blocked":
        return None, ["Browser continuation was blocked; no continuation effect was accepted"]
    if route_kind == "auip_b2_blocked":
        notes.append(
            "Direct AUIP branch was handled but no application effect was accepted"
        )
        return None, notes
    if route_kind.startswith("auip_") or source == "auip_b2":
        axis: EffectAxis = "auip"
        receipt = result.get("receipt") if isinstance(result.get("receipt"), Mapping) else {}
        operation = str(receipt.get("type") or "step").strip().lower() or "step"
        target_kind = "app_session"
        target_id = str(result.get("app_session_id") or result.get("branch_id") or "")
        witness_id = str(
            result.get("action_id") or result.get("proposal_id") or "auip-direct"
        )
    elif provider == "browser" or result.get("branch_id"):
        axis = "browser"
        operation = "continue"
        target_kind = "interaction_branch"
        target_id = str(result.get("branch_id") or "")
        run = result.get("run") if isinstance(result.get("run"), Mapping) else {}
        witness_id = str(run.get("run_id") or target_id or "browser-direct")
    else:
        notes.append("Handled direct branch has no recognized shadow effect axis")
        return None, notes
    return (
        _make_effect(
            admission,
            axis=axis,
            operation=operation,
            witness_kind="host_structural_branch",
            witness_id=witness_id,
            ordinal=0,
            payload={
                "route_kind": route_kind,
                "provider": provider,
                "source": source,
                "proposal_id": str(result.get("proposal_id") or ""),
                "action_id": str(result.get("action_id") or ""),
            },
            target_kind=target_kind,
            target_id=target_id,
            admission_status=(
                "receipt_accepted" if axis == "auip" else "legacy_direct_branch"
            ),
        ),
        notes,
    )


def compile_shadow_turn_decision(
    admission: TurnAdmissionRecord,
    *,
    effective_actions: Sequence[Mapping[str, Any]] = (),
    auip_decision: Any = None,
    auip_dispatched: bool | None = None,
    direct_branch_result: Mapping[str, Any] | None = None,
    authority_failed_closed: bool = False,
) -> ShadowTurnDecision:
    """Compile existing accepted witnesses into a non-executable decision view."""

    # Reserve one bounded slot for the source-local/direct axis. A production
    # turn should never approach this limit; truncation is diagnostic evidence,
    # not permission to execute an omitted effect.
    work_input_cap = max(0, _EFFECT_CAP - 1)
    bounded_actions = tuple(islice(effective_actions, work_input_cap))
    evidence_truncated = len(effective_actions) > len(bounded_actions)
    work_effects, target_actions, notes = _work_effects(
        admission, bounded_actions
    )
    if evidence_truncated:
        notes.append(
            "Effective action observation truncated at "
            f"{work_input_cap} inputs (received {len(effective_actions)})"
        )
    effects = list(work_effects)
    dependencies: list[ExecutionDependency] = []
    constraints: list[SemanticConstraint] = []

    if direct_branch_result is not None:
        direct_effect, direct_notes = _direct_branch_effect(
            admission, direct_branch_result
        )
        notes.extend(direct_notes)
        if direct_effect is not None:
            effects.append(direct_effect)
    else:
        auip, timing, relation, auip_notes = _auip_effect(
            admission,
            auip_decision,
            ordinal=len(effects),
            dispatched=auip_dispatched,
        )
        notes.extend(auip_notes)
        if auip is not None:
            effects.append(auip)
            actionable_work = [item for item in work_effects if item.axis == "work"]
            if evidence_truncated:
                notes.append(
                    "Effect relationships omitted because Work witness evidence was truncated"
                )
            elif timing in {"work", "after_work"}:
                if len(actionable_work) == 1:
                    group = _goal_id(admission, "work-auip")
                    parent = replace(actionable_work[0], goal_group_id=group)
                    work_index = effects.index(actionable_work[0])
                    effects[work_index] = parent
                    auip = replace(auip, goal_group_id=group)
                    effects[-1] = auip
                    dependencies.append(
                        ExecutionDependency(
                            parent_effect_id=parent.effect_id,
                            child_effect_id=auip.effect_id,
                            condition_kind="requires_verified_outcome",
                            condition={
                                "predicate": "launchable_auip_delivery_exists"
                            },
                        )
                    )
                else:
                    notes.append(
                        "AUIP after_work has no unique Work effect owner in shadow evidence"
                    )
            elif relation == "independent":
                auip = replace(auip, goal_group_id=_goal_id(admission, "auip"))
                effects[-1] = auip
            elif relation == "subsumed" and actionable_work:
                for item in actionable_work:
                    constraints.append(
                        SemanticConstraint(
                            source_effect_id=auip.effect_id,
                            target_effect_id=item.effect_id,
                            kind="subsumes",
                            evidence={"source": "auip_work_relation"},
                        )
                    )

    ungrouped_work = [
        item for item in effects if item.axis == "work" and not item.goal_group_id
    ]
    if evidence_truncated and ungrouped_work:
        notes.append(
            "Work goal ownership omitted because witness evidence was truncated"
        )
    elif len(ungrouped_work) == 1:
        only = ungrouped_work[0]
        effects[effects.index(only)] = replace(
            only,
            goal_group_id=_goal_id(admission, "work"),
        )
    elif len(ungrouped_work) > 1:
        notes.append(
            "Multiple Work effects remain goal-group unresolved; shadow does not invent independence"
        )

    attention_only = bool(effects) and all(item.axis == "attention" for item in effects)
    if authority_failed_closed:
        notes.append("Existing control authority failed closed for a proposal batch")
    status: TurnDecisionShadowStatus
    if evidence_truncated or authority_failed_closed or (
        direct_branch_result is not None
        and direct_branch_result.get("execution_uncertain") is True
    ):
        # This status belongs only to the observer. Legacy execution is not
        # changed, but a partial/uncertain witness set must not look like a complete
        # shadow decision to a machine consumer.
        status = "failed_closed"
    elif not effects:
        # This is deliberately not ``sealed_no_action``: the Host observed that
        # no effect was accepted, but an optional proposal could have omitted
        # the user's intended action.
        status = "observed_no_effect"
    elif attention_only:
        status = "observed_attention"
    else:
        status = "observed_effects"

    notes = notes[:_PAYLOAD_COLLECTION_CAP]
    decision_shape = {
        "root_id": admission.root_id,
        "status": status,
        "effects": [asdict(item) for item in effects],
        "execution_dependencies": [asdict(item) for item in dependencies],
        "semantic_constraints": [asdict(item) for item in constraints],
        "target_actions": [asdict(item) for item in target_actions],
        "notes": notes,
    }
    lineage_id = _stable_id(_DECISION_NAMESPACE, admission.root_id, "lineage")
    decision_id = _stable_id(
        _DECISION_NAMESPACE,
        lineage_id,
        _digest(decision_shape),
    )
    return ShadowTurnDecision(
        decision_id=decision_id,
        decision_lineage_id=lineage_id,
        root_id=admission.root_id,
        turn_id=admission.turn_id,
        status=status,
        planner_source="existing_source_specific_witnesses",
        schema_version="amadeus.shadow-turn-decision.v1",
        effects=tuple(effects),
        execution_dependencies=tuple(dependencies),
        semantic_constraints=tuple(constraints),
        target_actions=tuple(target_actions),
        notes=tuple(notes),
        observed_at=time.time(),
    )


class TurnDecisionShadowObserver:
    """Bounded in-memory trace indexed by origin identity, not arrival windows."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        root_cap: int = _ROOT_CAP,
    ) -> None:
        self._enabled = _shadow_enabled() if enabled is None else bool(enabled)
        self._root_cap = min(_ROOT_CAP, max(1, int(root_cap)))
        self._lock = RLock()
        self._turns: OrderedDict[tuple[str, str], _ObservedTurn] = OrderedDict()
        self._key_by_turn_id: dict[str, tuple[str, str]] = {}
        self._sequence = 0
        self._counters: dict[str, int] = {
            "admitted": 0,
            "replayed_admission": 0,
            "events": 0,
            "decisions": 0,
            "decision_replays": 0,
            "decision_mutations": 0,
            "invariant_violations": 0,
            "stale_application_events": 0,
            "terminal_dispositions": 0,
            "terminal_missing_settlement": 0,
            "terminal_replays": 0,
            "late_settlements": 0,
            "evicted_without_terminal": 0,
        }

    @property
    def enabled(self) -> bool:
        return self._enabled

    def clear(self) -> None:
        """Testing/diagnostic reset; never called by the product path."""

        with self._lock:
            self._turns.clear()
            self._key_by_turn_id.clear()
            self._sequence = 0
            for key in self._counters:
                self._counters[key] = 0

    def admit_turn(
        self,
        *,
        utterance_id: str,
        turn_id: str,
        session_id: str,
        transcript: str,
        dialogue_source_scope: str = "",
        input_source: str = "",
        chat_epoch: int | None = None,
        pending: bool = False,
        authority_mode: str = "source_witness_v1",
        source_evidence: Mapping[str, Any] | None = None,
    ) -> TurnAdmissionRecord | None:
        if not self.enabled:
            return None
        return self.observe_admission(capture_turn_admission(
            utterance_id=utterance_id, turn_id=turn_id, session_id=session_id,
            transcript=transcript, dialogue_source_scope=dialogue_source_scope,
            input_source=input_source, chat_epoch=chat_epoch, pending=pending,
            authority_mode=authority_mode, source_evidence=source_evidence,
        ))

    def observe_admission(
        self, captured: TurnAdmissionRecord | None,
    ) -> TurnAdmissionRecord | None:
        """Retain a captured ingress record without recapturing time or scope."""

        if not self.enabled or captured is None:
            return None
        incoming = deepcopy(captured)
        clean_turn = incoming.turn_id
        clean_utterance = incoming.utterance_id
        key = incoming.acceptance_key
        transcript_hash = incoming.transcript_hash
        event: TurnTraceEvent
        with self._lock:
            existing = self._turns.get(key)
            if existing is not None:
                self._counters["replayed_admission"] += 1
                self._turns.move_to_end(key)
                # A transport retry may allocate a fresh presentation turn id
                # while retaining the same stable utterance identity. Keep all
                # late events joined to the original provenance root.
                self._bind_turn_alias_locked(existing, key, clean_turn)
                if existing.admission.transcript_hash != transcript_hash:
                    detail = "same source/utterance identity arrived with different transcript"
                    self._record_violation_locked(existing, detail)
                    event = self._append_event_locked(
                        existing,
                        stage="admission_identity_collision",
                        origin_kind="transport_utterance",
                        origin_id=clean_utterance,
                        payload={
                            "existing_transcript_hash": existing.admission.transcript_hash,
                            "replayed_transcript_hash": transcript_hash,
                        },
                    )
                else:
                    event = self._append_event_locked(
                        existing,
                        stage="admission_replayed",
                        origin_kind="transport_utterance",
                        origin_id=clean_utterance,
                        payload={"chat_epoch": incoming.chat_epoch},
                    )
                admission = existing.admission
            else:
                admission = incoming
                observed = _ObservedTurn(admission=admission)
                self._turns[key] = observed
                turn_id_bound = True
                mapped = self._key_by_turn_id.get(clean_turn)
                if mapped is not None and mapped != key:
                    turn_id_bound = False
                    self._record_violation_locked(
                        observed,
                        "turn id is already bound to a different provenance root",
                    )
                else:
                    self._key_by_turn_id[clean_turn] = key
                self._counters["admitted"] += 1
                event = self._append_event_locked(
                    observed,
                    stage="turn_admitted",
                    origin_kind="transport_utterance",
                    origin_id=clean_utterance,
                    payload={
                        "chat_epoch": incoming.chat_epoch,
                        "pending": incoming.pending,
                        "input_source": incoming.input_source,
                        "turn_id_bound": turn_id_bound,
                    },
                )
                self._evict_locked()
        self._log_event(event)
        return deepcopy(admission)

    def _bind_turn_alias_locked(
        self,
        observed: _ObservedTurn,
        key: tuple[str, str],
        turn_id: str,
    ) -> None:
        mapped = self._key_by_turn_id.get(turn_id)
        if mapped is not None and mapped != key:
            self._record_violation_locked(
                observed,
                "turn id is already bound to a different provenance root",
            )
            return
        if turn_id == observed.admission.turn_id:
            self._key_by_turn_id[turn_id] = key
            return
        try:
            observed.turn_aliases.remove(turn_id)
        except ValueError:
            if len(observed.turn_aliases) == observed.turn_aliases.maxlen:
                stale = observed.turn_aliases.popleft()
                if self._key_by_turn_id.get(stale) == key:
                    self._key_by_turn_id.pop(stale, None)
        observed.turn_aliases.append(turn_id)
        self._key_by_turn_id[turn_id] = key

    def _record_violation_locked(
        self,
        observed: _ObservedTurn,
        detail: str,
        *,
        stale_application: bool = False,
    ) -> None:
        observed.invariant_violations.append(
            str(detail or "unknown invariant violation")[:_PAYLOAD_STRING_CAP]
        )
        observed.invariant_violation_count += 1
        self._counters["invariant_violations"] += 1
        if stale_application:
            self._counters["stale_application_events"] += 1

    def _evict_locked(self) -> None:
        while len(self._turns) > self._root_cap:
            key, observed = self._turns.popitem(last=False)
            if observed.terminal is None:
                self._counters["evicted_without_terminal"] += 1
            for turn_id in (
                observed.admission.turn_id,
                *tuple(observed.turn_aliases),
            ):
                if self._key_by_turn_id.get(turn_id) == key:
                    self._key_by_turn_id.pop(turn_id, None)

    def _observed_locked(self, turn_id: str) -> _ObservedTurn | None:
        key = self._key_by_turn_id.get(str(turn_id or "").strip())
        if key is None:
            return None
        observed = self._turns.get(key)
        if observed is not None:
            self._turns.move_to_end(key)
        return observed

    def admission_for_turn(self, turn_id: str) -> TurnAdmissionRecord | None:
        """Return the existing origin record without creating a replay event."""

        if not self.enabled:
            return None
        with self._lock:
            observed = self._observed_locked(turn_id)
            return deepcopy(observed.admission) if observed is not None else None

    def _append_event_locked(
        self,
        observed: _ObservedTurn,
        *,
        stage: str,
        origin_kind: str,
        origin_id: str,
        payload: Mapping[str, Any] | None = None,
    ) -> TurnTraceEvent:
        self._sequence += 1
        event = TurnTraceEvent(
            sequence=self._sequence,
            root_id=observed.admission.root_id,
            turn_id=observed.admission.turn_id,
            stage=str(stage or "unknown")[:160],
            origin_kind=str(origin_kind or "unknown")[:120],
            origin_id=str(origin_id or "")[:240],
            arrived_at=time.time(),
            arrived_at_monotonic=time.monotonic(),
            payload=_bounded_json(payload or {}),
        )
        observed.events.append(event)
        self._counters["events"] += 1
        milestone = _MILESTONE_STAGES.get(event.stage)
        if milestone is not None and not event.payload.get("after_terminal"):
            observed.milestones.setdefault(milestone, event.arrived_at_monotonic)
        return event

    @staticmethod
    def _log_event(event: TurnTraceEvent) -> None:
        """Emit diagnostic I/O after releasing the observer's global lock."""

        try:
            record = {
                "sequence": event.sequence,
                "root_id": event.root_id,
                "turn_id": event.turn_id,
                "stage": event.stage,
                "origin_kind": event.origin_kind,
                "origin_id": event.origin_id,
                "arrived_at": event.arrived_at,
                "arrived_at_monotonic": event.arrived_at_monotonic,
                "payload": event.payload,
            }
            rendered = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if len(rendered) > _LOG_LINE_CHAR_CAP:
                record["payload"] = {
                    "_shadow_payload_truncated": True,
                    "payload_digest": _digest(event.payload),
                }
                rendered = json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            logger.info("[TURN-DECISION-SHADOW] %s", rendered)
        except Exception:
            # Observation must never become availability authority.
            logger.debug("turn decision event logging failed", exc_info=True)

    def record_event(
        self,
        turn_id: str,
        *,
        stage: str,
        origin_kind: str,
        origin_id: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> TurnTraceEvent | None:
        if not self.enabled:
            return None
        with self._lock:
            observed = self._observed_locked(turn_id)
            if observed is None:
                return None
            event_payload: Mapping[str, Any] = payload or {}
            if (
                stage == "control_authority_resolved"
                and event_payload.get("disposition") == "failed_closed"
            ):
                # Preserve the actual authority outcome independently of the
                # event ring. Zero witnesses must not erase a known failure.
                observed.authority_failed_closed = True
            if (
                str(stage or "") in _STALE_EFFECT_STAGES
                and observed.lifecycle in _STALE_LIFECYCLES
            ):
                detail = (
                    f"{str(stage or 'effect event')} arrived after turn lifecycle "
                    f"became {observed.lifecycle}"
                )
                self._record_violation_locked(
                    observed,
                    detail,
                    stale_application=True,
                )
                bounded_payload = _bounded_json(event_payload)
                event_payload = {
                    "stale_origin_lifecycle": observed.lifecycle,
                    **(
                        dict(bounded_payload)
                        if isinstance(bounded_payload, Mapping)
                        else {}
                    ),
                }
            event = self._append_event_locked(
                observed,
                stage=stage,
                origin_kind=origin_kind,
                origin_id=origin_id,
                payload=event_payload,
            )
        self._log_event(event)
        return deepcopy(event)

    def observe_proposal_batch(self, batch: Any) -> None:
        if not self.enabled:
            return
        raw_proposals = getattr(batch, "proposals", ()) or ()
        try:
            proposal_count: int | None = len(raw_proposals)
        except TypeError:
            proposal_count = None
        sampled = tuple(
            islice(raw_proposals, _PAYLOAD_COLLECTION_CAP + 1)
        )
        truncated = len(sampled) > _PAYLOAD_COLLECTION_CAP
        proposals = sampled[:_PAYLOAD_COLLECTION_CAP]
        payloads = []
        for proposal in proposals:
            attrs = proposal if isinstance(proposal, Mapping) else {}
            payloads.append(
                {
                    "intent": str(attrs.get("intent") or ""),
                    "provider": str(attrs.get("provider") or ""),
                    "payload_digest": _digest(_public_control(attrs)),
                }
            )
        self.record_event(
            str(getattr(batch, "turn_id", "") or ""),
            stage="control_proposal_sealed",
            origin_kind="main_chat_role",
            origin_id=str(getattr(batch, "commit_point", "") or ""),
            payload={
                "transport": str(getattr(batch, "transport", "") or ""),
                "proposal_count": (
                    proposal_count if proposal_count is not None else len(proposals)
                ),
                "proposal_count_known": proposal_count is not None,
                "proposal_observed_count": len(proposals),
                "proposal_observation_truncated": bool(
                    truncated
                    or (
                        proposal_count is not None
                        and proposal_count > len(proposals)
                    )
                ),
                "proposals": payloads,
            },
        )

    def observe_settlement(
        self,
        turn_id: str,
        *,
        effective_actions: Sequence[Mapping[str, Any]] = (),
        auip_decision: Any = None,
        auip_dispatched: bool | None = None,
    ) -> ShadowTurnDecision | None:
        if not self.enabled:
            return None
        with self._lock:
            observed = self._observed_locked(turn_id)
            if observed is None:
                return None
            decision = compile_shadow_turn_decision(
                observed.admission,
                effective_actions=effective_actions,
                auip_decision=auip_decision,
                auip_dispatched=auip_dispatched,
                authority_failed_closed=observed.authority_failed_closed,
            )
            previous = observed.decision
            event_stage = "shadow_turn_decision_observed"
            if previous is None and observed.terminal is None:
                self._counters["decisions"] += 1
            elif previous is not None and previous.decision_id == decision.decision_id:
                event_stage = "shadow_turn_decision_replayed"
                self._counters["decision_replays"] += 1
            elif previous is not None:
                event_stage = "shadow_turn_decision_mutated"
                detail = (
                    "one turn's shadow decision shape changed after first settlement"
                )
                self._record_violation_locked(observed, detail)
                self._counters["decision_mutations"] += 1
            if observed.terminal is None:
                observed.decision = decision
            else:
                self._counters["late_settlements"] += 1
            event_payload: dict[str, Any] = {
                "status": decision.status,
                "effect_count": len(decision.effects),
                "effect_axes": [item.axis for item in decision.effects],
                "dependency_count": len(decision.execution_dependencies),
                "constraint_count": len(decision.semantic_constraints),
                "notes": list(decision.notes),
            }
            if observed.terminal is not None:
                event_payload["after_terminal"] = True
            if previous is not None:
                event_payload["previous_decision_id"] = previous.decision_id
            if observed.lifecycle in _STALE_LIFECYCLES:
                detail = (
                    "shadow settlement arrived after turn lifecycle became "
                    f"{observed.lifecycle}"
                )
                self._record_violation_locked(
                    observed,
                    detail,
                    stale_application=True,
                )
                event_payload["stale_origin_lifecycle"] = observed.lifecycle
            event = self._append_event_locked(
                observed,
                stage=event_stage,
                origin_kind="host_settlement",
                origin_id=decision.decision_id,
                payload=event_payload,
            )
        self._log_event(event)
        return deepcopy(decision)

    def observe_direct_branch(
        self,
        turn_id: str,
        result: Mapping[str, Any],
    ) -> ShadowTurnDecision | None:
        if not self.enabled:
            return None
        with self._lock:
            observed = self._observed_locked(turn_id)
            if observed is None:
                return None
            decision = compile_shadow_turn_decision(
                observed.admission,
                direct_branch_result=result,
                authority_failed_closed=observed.authority_failed_closed,
            )
            previous = observed.decision
            event_stage = "direct_branch_decision_observed"
            if previous is None and observed.terminal is None:
                self._counters["decisions"] += 1
            elif previous is not None and previous.decision_id == decision.decision_id:
                event_stage = "direct_branch_decision_replayed"
                self._counters["decision_replays"] += 1
            elif previous is not None:
                event_stage = "direct_branch_decision_mutated"
                self._record_violation_locked(
                    observed,
                    "one direct branch decision shape changed for the same origin turn",
                )
                self._counters["decision_mutations"] += 1
            if observed.terminal is None:
                observed.decision = decision
            else:
                self._counters["late_settlements"] += 1
            event_payload = {
                "status": decision.status,
                "effect_count": len(decision.effects),
                "route_kind": str(result.get("route_kind") or ""),
                "provider": str(result.get("provider") or ""),
            }
            if observed.terminal is not None:
                event_payload["after_terminal"] = True
            if previous is not None:
                event_payload["previous_decision_id"] = previous.decision_id
            event = self._append_event_locked(
                observed,
                stage=event_stage,
                origin_kind="host_structural_branch",
                origin_id=str(
                    result.get("action_id")
                    or result.get("branch_id")
                    or result.get("route_kind")
                    or "direct"
                ),
                payload=event_payload,
            )
        self._log_event(event)
        return deepcopy(decision)

    def mark_lifecycle(
        self, turn_id: str, status: str, *, reason: str = "", only_if_open: bool = False,
    ) -> None:
        if not self.enabled:
            return
        with self._lock:
            observed = self._observed_locked(turn_id)
            if observed is None:
                return
            if only_if_open and observed.terminal is not None:
                return
            observed.lifecycle = str(status or "unknown")[:120]
            event = self._append_event_locked(
                observed,
                stage="turn_lifecycle",
                origin_kind="turn_coordinator",
                origin_id=str(turn_id or ""),
                payload={"status": observed.lifecycle, "reason": str(reason or "")},
            )
            terminal_event = None
            if observed.lifecycle in _TERMINAL_LIFECYCLES:
                if observed.terminal is not None:
                    self._counters["terminal_replays"] += 1
                else:
                    decision = observed.decision
                    disposition = (
                        decision.status if decision is not None else "missing_settlement"
                    ) if observed.lifecycle == "completed" else observed.lifecycle
                    terminal = ShadowTerminalDisposition(
                        terminal_id=_stable_id(
                            _DECISION_NAMESPACE, observed.admission.root_id, "terminal"
                        ),
                        lifecycle=observed.lifecycle,
                        disposition=disposition,
                        decision_id=decision.decision_id if decision is not None else None,
                        observed_effect_count=len(decision.effects) if decision is not None else None,
                        reason=str(reason or "")[:_PAYLOAD_STRING_CAP],
                        finalized_at=event.arrived_at,
                        finalized_at_monotonic=event.arrived_at_monotonic,
                    )
                    observed.terminal = terminal
                    self._counters["terminal_dispositions"] += 1
                    if disposition == "missing_settlement":
                        self._counters["terminal_missing_settlement"] += 1
                    terminal_event = self._append_event_locked(
                        observed,
                        stage="terminal_disposition_observed",
                        origin_kind="host_observation_closure",
                        origin_id=terminal.terminal_id,
                        payload=asdict(terminal),
                    )
        self._log_event(event)
        if terminal_event is not None:
            self._log_event(terminal_event)

    def snapshot(self, *, limit: int = 8) -> dict[str, Any]:
        audio_write_times: dict[str, float] = {}
        if self.enabled:
            # Playback owns bounded raw evidence. Join only exact identities;
            # server imports and diagnostic I/O stay off its writer thread.
            from core.turn_coordinator import get_turn_coordinator

            audio_write_times = get_turn_coordinator().first_audio_write_times()
        with self._lock:
            for observed in self._turns.values():
                writes = [
                    audio_write_times[turn_id]
                    for turn_id in (observed.admission.turn_id, *observed.turn_aliases)
                    if turn_id in audio_write_times
                ]
                if writes:
                    observed.milestones.setdefault("first_audio_write_completed", min(writes))
            recent = list(self._turns.values())[-max(1, int(limit)) :]
            captured = [
                (
                    observed.admission,
                    observed.lifecycle,
                    observed.decision,
                    tuple(observed.invariant_violations),
                    observed.invariant_violation_count,
                    tuple(observed.events)[-24:],
                    tuple(observed.turn_aliases),
                    observed.terminal,
                    dict(observed.milestones),
                )
                for observed in reversed(recent)
            ]
            counters = dict(self._counters)
            retained = {
                "roots": len(self._turns),
                "turn_ids": len(self._key_by_turn_id),
                "events": sum(len(observed.events) for observed in self._turns.values()),
                "invariant_details": sum(
                    len(observed.invariant_violations)
                    for observed in self._turns.values()
                ),
                "without_terminal": sum(
                    observed.terminal is None for observed in self._turns.values()
                ),
            }
        rows: list[dict[str, Any]] = []
        for (
            admission,
            lifecycle,
            decision,
            invariant_violations,
            invariant_violation_count,
            events,
            turn_aliases,
            terminal,
            milestones,
        ) in captured:
            rows.append(
                {
                    "admission": _bounded_json(asdict(admission)),
                    "lifecycle": lifecycle,
                    "decision": decision.as_dict() if decision is not None else None,
                    "terminal": _bounded_json(asdict(terminal)) if terminal is not None else None,
                    "timing": {
                        "clock": "process_monotonic",
                        "clock_resolution_ms": time.get_clock_info("monotonic").resolution * 1000,
                        "plan_freeze_supported": False,
                        "elapsed_ms": {
                            **{
                                name: round((milestones[name] - admission.admitted_at_monotonic) * 1000, 3)
                                if name in milestones else None
                                for name in sorted(_MILESTONE_NAMES)
                            },
                            "plan_frozen": None,
                        },
                    },
                    "invariant_violations": list(invariant_violations),
                    "invariant_violation_count": invariant_violation_count,
                    "invariant_details_truncated": (
                        invariant_violation_count > len(invariant_violations)
                    ),
                    "turn_alias_count": len(turn_aliases),
                    "events": [_bounded_json(asdict(event)) for event in events],
                }
            )
        return {
            "enabled": self.enabled,
            "mode": "observe_only",
            "authority": False,
            "schema_version": "amadeus.shadow-turn-decision.v1",
            "limits": {
                "roots": self._root_cap,
                "events_per_root": _EVENT_CAP,
                "turn_aliases_per_root": _TURN_ALIAS_CAP,
                "invariant_details_per_root": _INVARIANT_VIOLATION_CAP,
                "effects_per_decision": _EFFECT_CAP,
                "payload_collection_items": _PAYLOAD_COLLECTION_CAP,
                "payload_nodes": _PAYLOAD_NODE_CAP,
                "payload_chars": _PAYLOAD_CHAR_CAP,
                "log_line_chars": _LOG_LINE_CHAR_CAP,
                "milestones_per_root": len(_MILESTONE_NAMES),
            },
            "retained": retained,
            "counters": counters,
            "recent": rows,
        }


observer = TurnDecisionShadowObserver()


def get_turn_decision_shadow_observer() -> TurnDecisionShadowObserver:
    return observer


def get_enabled_turn_decision_shadow_observer() -> TurnDecisionShadowObserver | None:
    """Return the observer only when the startup feature switch is enabled."""

    return observer if observer.enabled else None


__all__ = [
    "EffectLineage",
    "ExecutionDependency",
    "SemanticConstraint",
    "ShadowEffect",
    "ShadowTurnDecision",
    "TargetAction",
    "TurnAdmissionRecord",
    "capture_turn_admission",
    "TurnDecisionShadowObserver",
    "TurnTraceEvent",
    "compile_shadow_turn_decision",
    "get_enabled_turn_decision_shadow_observer",
    "get_turn_decision_shadow_observer",
]
