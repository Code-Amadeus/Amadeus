"""Local, versioned review of formation suggestions using native evidence."""
from dataclasses import replace
import json
from time import time_ns

from core import session_source_journal as journal
from server.memory_service import MemoryRequestError

_SAVABLE = frozenset(("fact", "preference", "instruction", "event"))


class CandidateReview:
    def __init__(self, lookup):
        self.lookup = lookup
        self.api = lookup.api
        self.candidates = self.api.Candidates(lookup.store)

    @staticmethod
    def _summary(candidate):
        return {
            "candidate_id": candidate.candidate_id,
            "row_version": candidate.row_version,
            "scope": candidate.scope.kind,
            "kind": "comparison" if candidate.comparison else candidate.kind,
            "created_at_ms": candidate.created_at_ms,
            "savable": not candidate.comparison and candidate.kind in _SAVABLE,
        }

    def list(self, access):
        # Only the selected suggestion exposes a body or opens Session sources.
        return [self._summary(candidate) for candidate in self.candidates.list(access)]

    def _evidence(self, access, candidate_id, version):
        policy = self.lookup.host._policy
        policy_value = policy.value
        if not policy.ready or policy_value["pending"]:
            raise MemoryRequestError("conflict")
        reference = self.api.CandidateReference(candidate_id, version)
        candidate = self.candidates.detail(access, candidate_id)
        if candidate.state != "pending" or candidate.row_version != version:
            raise MemoryRequestError("conflict")
        view = self.lookup.delivery.view(access)
        # This is a local review surface. The cap bounds source reads and UI
        # payload size without spending the role's chat-context budget.
        result = self.lookup.evidence.assemble(
            access, (reference,), review_pending=True,
            limits=self.api.EvidenceLimits(8, 65_536, 262_144, 65_536, read_sources=8,
                                           read_chars=65_536, read_bytes=262_144),
            reader=view, authorized=lambda: policy.ready, count_tokens=len,
            now_ms=time_ns() // 1_000_000,
        )
        if result.groups[0].status != "complete":
            raise MemoryRequestError("conflict")
        reads = replace(result.read_set, candidates=(reference,))
        signatures = self.lookup.require_reads(access, reads, view)
        if not policy.ready or policy.value is not policy_value:
            raise MemoryRequestError("conflict")
        return candidate, result.payload, signatures, policy_value

    def detail(self, access, candidate_id, version):
        candidate, serialized, _, _ = self._evidence(access, candidate_id, version)
        evidence = json.loads(serialized)
        records = {row["source_id"]: row for row in evidence["records"]}
        quotes = []
        for edge in evidence["groups"][0]["highlights"]:
            record = records[edge["source_id"]]
            quotes.append({"speaker_role": record["speaker_role"], "role": edge["role"],
                           "text": record["text"][edge["start_cp"]:edge["end_cp"]]})
        return {**self._summary(candidate), "text": candidate.text, "condition": candidate.condition,
                "quotes": quotes}

    def decide(self, access, params, *, now_ms):
        candidate_id, version = params.get("candidate_id"), params.get("expected_version")
        common = {"operation_id": params.get("operation_id"), "item_id": params.get("item_id"),
                  "now_ms": now_ms}
        decision = params.get("decision")
        if decision == "dismiss":
            return self.candidates.reject(access, candidate_id, version, **common)
        kind = params.get("kind")
        if decision != "save" or not isinstance(kind, str) or kind not in _SAVABLE:
            raise MemoryRequestError("invalid_request")

        def accept():
            return self.candidates.accept(access, candidate_id, version, memory_kind=kind, **common)

        # Let the SDK verify the exact operation replay, including after the
        # accepted proposal is no longer pending or the saved note was erased.
        if self.lookup.store.operation_status(access, operation_id=common["operation_id"],
                                               item_id=common["item_id"]) is not None:
            return accept()
        candidate, _, signatures, policy_value = self._evidence(access, candidate_id, version)
        if candidate.comparison is not None or candidate.kind != kind:
            raise MemoryRequestError("invalid_request")
        with journal.commit_lock:
            policy = self.lookup.host._policy
            if not policy.ready or policy.value is not policy_value:
                raise MemoryRequestError("conflict")
            self.lookup.delivery.require_current(signatures)
            return accept()
