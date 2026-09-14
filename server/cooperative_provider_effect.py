"""Typed Control-outbox ownership for cooperative Provider operations.

This module compiles already-decided cooperative actions into the existing
Control Ledger. It never chooses an action, creates a context or calls a Provider.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Callable, Literal, Mapping
import uuid

from server.control_ledger import (
    ControlEffect,
    ControlLedgerConflict,
    ControlLedgerStore,
    ReconciliationPolicy,
)
from server.turn_admission import TurnAdmissionRecord


ProviderEffectOperation = Literal["start", "append", "interrupt"]
_OPERATIONS = {"start", "append", "interrupt"}
_ID_NAMESPACE = uuid.UUID("69a2e98d-c2ae-4a4e-befe-936430045a2a")


def _required(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value


@dataclass(frozen=True, slots=True)
class CooperativeProviderEffectIntent:
    operation: ProviderEffectOperation
    session_id: str
    context_id: str
    binding_token: str
    source_utterance_id: str
    turn_id: str
    provider: str
    run_id: str = ""
    workspace: str = ""
    source_binding_context_id: str | None = None
    continuation_effect_id: str = ""

    def __post_init__(self):
        if self.operation not in _OPERATIONS:
            raise ValueError("unsupported cooperative Provider effect operation")
        for label in ("session_id", "context_id", "binding_token",
                "source_utterance_id", "turn_id", "provider"):
            _required(getattr(self, label), label)
        if not isinstance(self.workspace, str):
            raise ValueError("workspace must be a string")
        if self.source_binding_context_id is None:
            object.__setattr__(self, "source_binding_context_id", self.context_id)
        elif not isinstance(self.source_binding_context_id, str):
            raise ValueError("source binding context id must be a string")
        if self.operation == "start" and self.run_id:
            raise ValueError("start effect cannot predeclare a Provider run")
        if self.operation != "start":
            _required(self.run_id, "Provider run_id")
        if (not isinstance(self.continuation_effect_id, str)
                or (self.continuation_effect_id and self.operation != "start")):
            raise ValueError("only a start effect may continue a prior task effect")

    @property
    def target_key(self):
        digest = hashlib.sha256(
            "\0".join((self.session_id, self.binding_token, self.operation,
                self.run_id, self.source_utterance_id)).encode("utf-8")
        ).hexdigest()
        return "cooperative-operation:" + digest

    def to_payload(self):
        return {"operation":self.operation, "session_id":self.session_id,
            "context_id":self.context_id, "binding_token":self.binding_token,
            "source_utterance_id":self.source_utterance_id, "turn_id":self.turn_id,
            "provider":self.provider, "run_id":self.run_id,
            "workspace":self.workspace,
            "source_binding_context_id":self.source_binding_context_id,
            **({"continuation_effect_id":self.continuation_effect_id}
                if self.continuation_effect_id else {})}

    def external_id(self, run_id):
        clean_run_id = _required(run_id, "Provider run_id")
        if self.operation == "start":
            return clean_run_id
        if clean_run_id != self.run_id:
            raise ValueError("Provider effect run identity changed")
        return f"{clean_run_id}:{self.operation}:{self.source_utterance_id}"


class CooperativeProviderEffectLedger:
    """Compile and settle one cooperative operation through Control Ledger."""

    def __init__(self, ledger: ControlLedgerStore, *, owner="cooperative-provider",
                 lease_seconds=15.0, reconciliation_owner="cooperative-provider-reconcile",
                 max_probes=3, probe_interval_seconds=5.0, unknown_ttl_seconds=60.0):
        self.ledger = ledger
        self.owner = _required(owner, "effect owner")
        self.lease_seconds = lease_seconds
        self.reconciliation = ReconciliationPolicy(reconciliation_owner,
            max_probes=max_probes, interval_seconds=probe_interval_seconds,
            ttl_seconds=unknown_ttl_seconds)

    @staticmethod
    def _ids(root_id):
        root = _required(root_id, "admission root_id")
        return ("cooperative-plan-" + uuid.uuid5(_ID_NAMESPACE, "plan:" + root).hex,
            "cooperative-effect-" + uuid.uuid5(_ID_NAMESPACE, "effect:" + root).hex)

    @staticmethod
    def _match_admission(admission: TurnAdmissionRecord, intent: CooperativeProviderEffectIntent):
        if not isinstance(admission, TurnAdmissionRecord):
            raise TypeError("a captured Host TurnAdmissionRecord is required")
        if (admission.authority_mode != "turn_decision" or admission.chat_epoch is None
                or admission.session_id != intent.session_id
                or admission.utterance_id != intent.source_utterance_id
                or admission.turn_id != intent.turn_id):
            raise ControlLedgerConflict("cooperative Provider effect does not match its admitted turn")

    def accept_and_claim(self, admission: TurnAdmissionRecord,
                         intent: CooperativeProviderEffectIntent):
        accepted = self.accept(admission, intent)
        if accepted["replayed"]:
            return accepted
        claimed = self.claim_with_local_intent(
            accepted["effect"]["effect_id"], apply=lambda _cursor, _payload: {}
        )
        return {"accepted":accepted["accepted"], "effect":claimed["effect"],
            "replayed":False}

    def accept(self, admission: TurnAdmissionRecord,
               intent: CooperativeProviderEffectIntent):
        self._match_admission(admission, intent)
        plan_id, effect_id = self._ids(admission.root_id)
        with self.ledger._lock:
            if intent.continuation_effect_id:
                if intent.continuation_effect_id == effect_id:
                    raise ControlLedgerConflict("a Provider task cannot continue itself")
                self.task_root(intent.continuation_effect_id, session_id=intent.session_id,
                    context_id=intent.context_id, provider=intent.provider)
            accepted = self.ledger.accept(admission.root_id, chat_epoch=admission.chat_epoch,
                plan_id=plan_id, effects=(ControlEffect(effect_id, "provider",
                    intent.target_key, intent.to_payload()),),
                evidence={"owner":"cooperative_provider_effect", "turn_id":intent.turn_id})
        if accepted["replayed"]:
            return {"accepted":accepted, "effect":self.ledger.get_effect(effect_id),
                "replayed":True}
        return {"accepted":accepted, "effect":self.ledger.get_effect(effect_id),
            "replayed":False}

    def task_root(self, effect_id, *, session_id, context_id, provider, cursor=None):
        """Resolve only sealed continuation links, never shared context or prose."""
        with self.ledger._lock:
            reader = cursor if cursor is not None else self.ledger._db
            seen = set()
            while effect_id:
                if effect_id in seen:
                    raise ControlLedgerConflict("cyclic cooperative task continuation")
                seen.add(effect_id)
                row = reader.execute("""SELECT e.kind,e.payload_json,a.source_scope,
                        a.utterance_id,a.authority_mode,a.transcript_hash
                    FROM control_effect_outbox e JOIN control_admissions a ON a.root_id=e.root_id
                    WHERE e.effect_id=?""", (effect_id,)).fetchone()
                if row is None:
                    raise ControlLedgerConflict("cooperative task continuation is unavailable")
                payload = json.loads(row["payload_json"])
                if (row["kind"] != "provider" or payload.get("operation") != "start"
                        or row["authority_mode"] != "turn_decision" or not row["transcript_hash"]
                        or row["source_scope"] != "chat:" + session_id
                        or row["utterance_id"] != payload.get("source_utterance_id")
                        or any(payload.get(key) != value for key, value in {
                            "session_id":session_id, "context_id":context_id, "provider":provider}.items())):
                    raise ControlLedgerConflict("cooperative task continuation owner changed")
                predecessor = payload.get("continuation_effect_id", "")
                if not isinstance(predecessor, str):
                    raise ControlLedgerConflict("invalid cooperative task continuation")
                if not predecessor:
                    return effect_id
                effect_id = predecessor
        raise ControlLedgerConflict("cooperative task continuation is empty")

    def claim_with_local_intent(self, effect_id: str, *, apply: Callable):
        return self.ledger.claim_with_local_intent(effect_id, owner=self.owner,
            lease_seconds=self.lease_seconds, reconciliation=self.reconciliation,
            apply=apply)

    def accept_no_effect(self, admission: TurnAdmissionRecord, *, reason: str,
                         plan_evidence: Mapping | None = None, local_apply: Callable | None = None):
        if not isinstance(admission, TurnAdmissionRecord) or admission.authority_mode != "turn_decision":
            raise ControlLedgerConflict("no-effect decision does not match a TurnDecision admission")
        extra = dict(plan_evidence or {})
        if {"owner", "turn_id", "reason"}.intersection(extra):
            raise ControlLedgerConflict(
                "no-effect plan evidence cannot replace admission authority"
            )
        plan_id, _ = self._ids(admission.root_id)
        return self.ledger.accept(admission.root_id, chat_epoch=admission.chat_epoch,
            plan_id=plan_id, effects=(),
            evidence={"owner":"cooperative_provider_effect", "turn_id":admission.turn_id,
                "reason":str(reason or "no_action"), **extra}, local_apply=local_apply)

    def bind_start(self, claim: Mapping, intent: CooperativeProviderEffectIntent, *, run_id: str,
                   apply: Callable | None = None):
        if intent.operation != "start":
            raise ValueError("only a start effect binds a running Provider identity")
        if apply is None:
            self.ledger.bind_external(claim["effect_id"], claim_token=claim["claim_token"],
                external_id=intent.external_id(run_id))
            return None
        return self.ledger.bind_external_with_local(claim["effect_id"],
            claim_token=claim["claim_token"], external_id=intent.external_id(run_id),
            apply=apply)

    def settle(self, claim: Mapping, intent: CooperativeProviderEffectIntent, *, run_id: str,
               outcome: Literal["succeeded", "failed", "cancelled"], details: Mapping,
               apply: Callable | None = None):
        kwargs = dict(effect_id=claim["effect_id"], claim_token=claim["claim_token"],
            external_id=intent.external_id(run_id), outcome=outcome, details=dict(details))
        if apply is None:
            return self.ledger.record_receipt(**kwargs)
        return self.ledger.record_receipt_with_local(**kwargs, apply=apply)

    def settle_unstarted(self, claim: Mapping, intent: CooperativeProviderEffectIntent, *,
                         reason: str, apply: Callable | None = None):
        if intent.operation != "start":
            raise ValueError("only a start effect can be refused before Runtime identity")
        kwargs = dict(effect_id=claim["effect_id"], claim_token=claim["claim_token"],
            external_id="not-started:" + claim["effect_id"], outcome="failed",
            details={"state":"rejected", "reason":str(reason or "start_rejected")})
        if apply is None:
            return self.ledger.record_receipt(**kwargs)
        return self.ledger.record_receipt_with_local(**kwargs, apply=apply)

    def confirmed_interrupt(self, *, session_id: str, context_id: str, run_id: str) -> bool:
        """A settled Host stop owns the empty cancellation acknowledgement."""
        with self.ledger._lock:
            row = self.ledger._db.execute("""SELECT 1
                FROM control_effect_outbox e
                JOIN control_effect_receipts r ON r.effect_id=e.effect_id
                WHERE e.kind='provider' AND e.state='terminal'
                  AND json_extract(e.payload_json,'$.operation')='interrupt'
                  AND json_extract(e.payload_json,'$.session_id')=?
                  AND json_extract(e.payload_json,'$.context_id')=?
                  AND json_extract(e.payload_json,'$.run_id')=?
                  AND json_extract(r.receipt_json,'$.outcome')='succeeded'
                  AND json_extract(r.receipt_json,'$.details.provider_status')='cancelled'
                LIMIT 1""", (session_id, context_id, run_id)).fetchone()
        return row is not None

    def mark_unknown(self, claim: Mapping, intent: CooperativeProviderEffectIntent, *,
                     run_id: str, reason: str):
        return self.ledger.mark_unknown(claim["effect_id"], claim_token=claim["claim_token"],
            external_id=intent.external_id(run_id), reason=reason)
