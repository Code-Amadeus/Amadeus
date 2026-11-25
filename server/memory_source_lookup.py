"""Join ranked saved notes and original Session records for an explicit read."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import time
from types import SimpleNamespace

from server.memory_service import MemoryRequestError


@dataclass(frozen=True)
class RankedLookup:
    references: tuple
    coverage: dict
    question_time: dict


def _merge(domains):
    # Native cosine is comparable for one query/model. Lexical scores are
    # local to their own indices, so preserve each lexical order on fallback.
    scored = [(ref, score, domain, rank) for domain, rows in enumerate(domains)
              for rank, (ref, score, _) in enumerate(rows) if score is not None]
    scored.sort(key=lambda row: (-row[1], row[2], row[3]))
    lexical = [[ref for ref, score, hit in rows if score is None and hit] for rows in domains]
    return tuple([row[0] for row in scored] + [
        rows[rank] for rank in range(max(map(len, lexical), default=0))
        for rows in lexical if rank < len(rows)
    ])


class SourceLookup:
    """Keep ranking separate from the Session-owned, budgeted delivery."""

    def __init__(self, host, delivery):
        self.host = host
        self.delivery = delivery
        self.store = host._store
        self.api = host._api
        self.evidence = self.api.EvidenceDelivery(self.store)
        self.stale = (MemoryRequestError, FileNotFoundError, self.api.MemoryConflict,
                      self.api.MemoryNotFound, self.api.MemoryValidationError)

    def rank(self, access, query, *, current, question_time):
        model = self.store._embedder
        version = model.version if model is not None else None
        dimensions = model.dimensions if model is not None else None
        vector, error = None, None

        def current_model():
            return self.store._embedder is model and (model is None or
                (model.version == version and model.dimensions == dimensions))

        def embedding(embedder, text, recent):
            nonlocal vector, error
            if embedder is not model or not current_model():
                raise ValueError("lookup_embedder_changed")
            if vector is None and error is None:
                try:
                    prepare = getattr(embedder, "prepare_query", None)
                    vector = tuple(embedder.embed_query(prepare(text, context=recent) if prepare else text))
                except Exception as exc:
                    error = exc
            if error is not None:
                raise error
            return vector

        raw = self.delivery.retrieval.lookup_sources(access, query, limit=16, candidate_limit=32,
            max_chars=131072, query_wait_ms=100, _query_embedding=embedding)

        def can_resolve(ref):
            try:
                self.evidence._plan(access, ref, "lookup", False, include_notes=True)
            except self.stale:
                return False
            return True

        notes = self.store.recall(access, query, now_ms=time.time_ns() // 1_000_000,
            ranking="semantic_first", purpose="lookup", limit=16, max_chars=131072,
            _query_embedding=embedding, max_vectors=4096, _reference_eligible=can_resolve)
        comparable = current_model()
        domains = (
            [(item.reference, item.semantic_score if comparable else None, item.lexical_match)
             for item in raw.items if item.source != current],
            [(self.api.RecallReference(item.memory_id, item.row_version),
              item.semantic_score if comparable else None, item.lexical_match) for item in notes.candidates],
        )
        coverage = asdict(notes.coverage)
        coverage["source_coverage"] = asdict(raw.coverage)
        coverage["budget_omitted"] += raw.coverage.budget_omitted
        return RankedLookup(_merge(domains), coverage, question_time)

    def require_reads(self, access, reads, view):
        """Check current Session entries even if replay has yet to reach SQLite."""
        outputs = (*reads.outputs, *(item.source for item in reads.sources))
        with self.store._lock:
            self.store._check()
            ancestry = self.store._output_dependencies(access, outputs, "lookup", candidates=reads.candidates)
        pins = (*reads.sources, *ancestry.sources)
        memories = {ref.memory_id for ref in (*reads.memories, *ancestry.memories)}
        identities = {(span.namespace, span.record_key) for span in (
            *outputs, *ancestry.outputs, *(item.source for item in pins))}
        policy = self.host._policy
        if not policy.ready or any(
                memories.intersection(pending["memory_ids"]) or
                any((source["namespace"], source["record_key"]) in identities
                    for source in pending["sources"])
                for pending in policy.value["pending"]):
            raise MemoryRequestError("conflict")
        signatures = view.signatures(pins)
        self.delivery.require_current(signatures)
        return signatures

    def assemble(self, access, ranked, *, counter, method):
        view = self.delivery.view(access)
        resolved = []
        unavailable = 0
        for ref in ranked.references:
            try:
                value = (view.pin(self.delivery.retrieval.detail(access, ref))
                         if isinstance(ref, self.api.RetrievalReference) else ref)
                resolved.append(value)
            except self.stale:
                unavailable += 1

        def read(pin, **limits):
            # A visible earlier answer may itself depend on a withdrawn
            # original. Omit that group before it takes a delivery slot.
            self.require_reads(access, self.api.OutputReadSet(sources=(pin,)), view)
            return view.read(pin, **limits)

        result = self.evidence.assemble(access, resolved, limits=self.api.EvidenceLimits(
            8, 8192, 8192, 1024, max_groups=8, max_references=32, read_sources=32,
            read_chars=262144, read_bytes=1048576),
            authorized=lambda: self.host._policy.ready, reader=SimpleNamespace(read=read), count_tokens=counter,
            now_ms=time.time_ns() // 1_000_000, include_notes=True)
        memories = tuple(group.reference for group in result.groups if group.status == "complete"
                         and isinstance(group.reference, self.api.RecallReference))
        reads = self.api.OutputReadSet(memories=memories, sources=result.read_set.sources)
        signatures = self.require_reads(access, reads, view)
        coverage = {**ranked.coverage, "context_budget": {
            "used_tokens": result.tokens, "used_bytes": result.utf8_bytes, "method": method}}
        coverage["budget_omitted"] += sum(group.status == "omitted" for group in result.groups)
        coverage["unavailable_groups"] = unavailable + sum(group.status == "unavailable" for group in result.groups)
        return reads, signatures, {**json.loads(result.payload), "coverage": coverage}
