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
    current: object | None


@dataclass(frozen=True)
class LookupPack:
    groups: tuple
    records: tuple
    current: object | None


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
        # Trusted Host configuration (policy id, revision, formation pipeline).
        self.ordered_change_policy = None
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
        return RankedLookup(_merge(domains), coverage, question_time, current)

    def search_originals(self, access, query):
        return self.delivery.retrieval.lookup_sources(access, query, limit=16, candidate_limit=32,
            max_chars=131072, query_wait_ms=100).items

    def require_reads(self, access, reads, view, *, purpose="lookup", record_watches=None):
        """Check current Session entries even if replay has yet to reach SQLite."""
        outputs = (*reads.outputs, *(item.source for item in reads.sources))
        current_outputs = set()
        with self.store._lock:
            self.store._check()
            ancestry = self.store._output_dependencies(
                access, outputs, purpose, candidates=reads.candidates, current_outputs=current_outputs)
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
        states = {output: view.output_state(output) for output in current_outputs}
        signatures = view.signatures(pins, output_states=states.values())
        self.delivery.require_current(signatures)
        if record_watches is not None:
            record_watches((pin.source, pin.state_version) for pin in pins)
            record_watches((output, position) for output, (_, position) in states.items())
        return signatures

    def capture(self, access, reads, *, purpose="lookup"):
        return self.store.capture_read_set(access, memories=reads.memories, sources=reads.sources,
            outputs=reads.outputs, candidates=reads.candidates, now_ms=time.time_ns() // 1_000_000,
            purpose=purpose)

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

        result = self._pack(access, resolved, view, counter)
        coverage = dict(ranked.coverage)
        coverage["budget_omitted"] += sum(group.status == "omitted" for group in result.groups)
        coverage["unavailable_groups"] = unavailable + sum(group.status == "unavailable" for group in result.groups)
        return self._package(access, ranked.current, result, view, coverage, counter, method)

    def _pack(self, access, resolved, view, counter):
        def read(pin, **limits):
            # A visible earlier answer may itself depend on a withdrawn
            # original. Omit that group before it takes a delivery slot.
            self.require_reads(access, self.api.OutputReadSet(sources=(pin,)), view)
            return view.read(pin, **limits)

        return self.evidence.assemble(access, resolved, limits=self.api.EvidenceLimits(
            8, 8192, 8192, 1024, max_groups=8, max_references=32, read_sources=32,
            read_chars=262144, read_bytes=1048576),
            authorized=lambda: self.host._policy.ready, reader=SimpleNamespace(read=read), count_tokens=counter,
            now_ms=time.time_ns() // 1_000_000, include_notes=True)

    def _package(self, access, current, result, view, coverage, counter, method, *,
                 prior=None, visited=(), state_change=None):
        memories = tuple(group.reference for group in result.groups if group.status == "complete"
                         and isinstance(group.reference, self.api.RecallReference))
        payload, tokens, size, position_reads = self._source_order(access, result, view, counter)
        if state_change is not None:
            aliases = {pin.source: row["source_id"]
                       for row, pin in zip(payload["records"], result.record_inputs, strict=True)}
            order = {}
            for label, excerpt in zip(("before", "after"), (state_change.left, state_change.right), strict=True):
                whole = self.api.SourceSpan(excerpt.namespace, excerpt.record_key, excerpt.revision)
                order[label] = [aliases[whole], excerpt.start_cp, excerpt.end_cp]
            payload = {**payload, "state_change": order}
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            tokens, size = counter(raw), len(raw.encode("utf8"))
            if tokens > 1024 or size > 8192:
                return None
        if self.ordered_change_policy is not None and result.record_inputs:
            coverage = {**coverage, "ordered_state_lookup": True}
        before = prior or self.api.OutputReadSet()
        reads = self.capture(access, self.api.OutputReadSet(
            memories=(*before.memories, *memories),
            sources=(*before.sources, *result.record_inputs, *position_reads, *visited),
            outputs=before.outputs, candidates=before.candidates))
        signatures = self.require_reads(access, reads, view)
        package = LookupPack(tuple(group for group in result.groups if group.status == "complete"),
                             result.record_inputs, current)
        return reads, signatures, {
            **payload, "coverage": {**coverage, "context_budget": {
                "used_tokens":tokens, "used_bytes":size, "method":method}},
        }, package

    def _source_order(self, access, result, view, counter):
        payload = json.loads(result.payload)
        locations = {}
        for row, pin in zip(payload["records"], result.record_inputs, strict=True):
            owner, position = view.position(pin)
            locations.setdefault(owner, []).append((position, row["source_id"], pin))
        edges, dependencies = [], []
        for positions in locations.values():
            positions.sort()
            for (i, left, anchor), (j, right, target) in zip(positions, positions[1:]):
                if j - i not in (1, 2):
                    continue
                try:
                    if view.relative(anchor, j - i)[0] != target:
                        continue
                    if j - i == 2:
                        bridge, _ = view.relative(anchor, 1)
                        self.require_reads(access, self.api.OutputReadSet(sources=(bridge,)), view)
                        dependencies.append(bridge)
                except self.stale:
                    continue
                edges.append([left, right, j - i])
        if edges:
            ordered = {**payload, "source_order":edges}
            raw = json.dumps(ordered, ensure_ascii=False, sort_keys=True)
            tokens, size = counter(raw), len(raw.encode("utf8"))
            if tokens <= 1024 and size <= 8192:
                return ordered, tokens, size, tuple(dependencies)
        return payload, result.tokens, result.utf8_bytes, ()

    def supplement(self, access, package, prior, action, ranked, *, payload, counter, method,
                   required_originals=None):
        """Apply one supplemental read. Host-selected originals must all fit."""
        view = self.delivery.view(access)
        prior = self.capture(access, prior)
        signatures = self.require_reads(access, prior, view)
        roots = tuple(group.reference for group in package.groups)
        anchor, visited, state_change = None, (), None
        if required_originals is not None:
            # Each nomination comes from the existing authorized source lookup,
            # independently of the role action and its serializable parameters.
            if {"record", "change_from"}.intersection(action) or len(required_originals) != 2:
                raise MemoryRequestError("invalid_request")
            try:
                additions = tuple(view.pin(item) for item in required_originals)
                self.require_reads(access, self.api.OutputReadSet(sources=additions), view)
            except self.stale:
                return None, signatures
            at = len(roots)
        elif "change_from" in action or "record" in action:
            is_state_change = "change_from" in action
            label = action["change_from" if is_state_change else "record"]
            anchor = self._visible_record(label, package, payload)
            if anchor.source == package.current:
                return None, signatures
            if is_state_change:
                ordered = self._ordered_change_reference(access, anchor, label, action, payload)
                if ordered is None:
                    return None, signatures
                addition, state_change = ordered
                additions, at = (addition,), len(roots)
            else:
                addition, visited = self._neighbor(access, view, anchor, action["direction"])
                at = next(i for i, group in enumerate(package.groups) if anchor.source in group.sources)
                at += action["direction"] == "next"
                additions = (addition,) if addition is not None else ()
        else:
            addition = self._new_original(access, view, package, ranked)
            at = len(roots)
            additions = (addition,) if addition is not None else ()
        originals = tuple(pin for pin in additions if isinstance(pin, self.api.SourceInput))
        if (not additions or set(additions).intersection(roots)
                or len({pin.source for pin in originals}) != len(originals)
                or any(pin.source == package.current or pin in package.records for pin in originals)):
            return None, signatures

        release = self._release(package, action.get("release_group"), anchor)
        attempts = (None, release) if release is not None else (None,)
        for discarded in attempts:
            proposed = list(roots)
            proposed[at:at] = additions
            if discarded is not None:
                proposed.remove(discarded)
            if len(proposed) > 8:
                continue
            result = self._pack(access, proposed, view, counter)
            selected = tuple(group.reference for group in result.groups if group.status == "complete")
            if selected != tuple(proposed):
                continue
            if state_change is not None:
                added = {pin.source for pin in result.record_inputs} - {pin.source for pin in package.records}
                if not added or package.current in added:
                    continue
            change = self._package(access, package.current, result, view, payload["coverage"], counter, method,
                                   prior=prior, visited=visited, state_change=state_change)
            if change is not None:
                return change, change[1]
        return None, signatures

    def _ordered_change_reference(self, access, anchor, label, action, payload):
        if self.ordered_change_policy is None:
            return None
        body, = (row["text"] for row in payload["records"] if row["source_id"] == label)
        try:
            excerpt = self.api.locate_source_quote(anchor.source, body, action["quote"])
        except self.api.MemoryValidationError:
            return None
        match_end = {"previous": "right", "next": "left"}.get(action["direction"])
        if match_end is None:
            return None
        found = self.api.Comparisons(self.store).find(access, [excerpt], purpose="lookup")
        if found.budget_omitted:
            return None
        matches = (row for row in found.items if row.comparison.kind == "reported_change"
                   and excerpt in (row.comparison.left, row.comparison.right))
        selected = []
        with self.store._lock:
            self.store._check()
            for row in matches:
                ref = self.api.RecallReference(row.memory.memory_id, row.memory.row_version)
                policy = self.store._db.execute(
                    "SELECT a.policy_id,a.policy_version,a.pipeline_version "
                    "FROM memories m JOIN derived_authorizations a ON a.authorization_id=m.authorization_id "
                    "WHERE m.memory_id=? AND m.row_version=? AND a.enabled=1 "
                    "AND m.authorization_generation=a.generation AND m.state='active' AND m.invalid_reason IS NULL",
                    (ref.memory_id, ref.row_version),
                ).fetchone()
                if not policy or tuple(policy) != self.ordered_change_policy:
                    return None
                if getattr(row.comparison, match_end) == excerpt:
                    selected.append((ref, row.comparison))
        return selected[0] if len(selected) == 1 else None

    @staticmethod
    def _visible_record(label, package, payload):
        for row, pin in zip(payload["records"], package.records, strict=True):
            if row["source_id"] == label:
                return pin
        raise MemoryRequestError("invalid_request")

    def _neighbor(self, access, view, anchor, direction):
        step = 1 if direction == "next" else -1
        visited = []
        for distance in (step, 2 * step):
            try:
                pin, role = view.relative(anchor, distance)
                self.require_reads(access, self.api.OutputReadSet(sources=(pin,)), view)
            except self.stale:
                break
            if role == "user":
                return pin, tuple(visited)
            if role != "assistant":
                break
            visited.append(pin)
        return None, ()

    def _new_original(self, access, view, package, ranked):
        seen = {pin.source for pin in package.records}
        for item in ranked:
            if item.source in seen or item.source == package.current:
                continue
            try:
                pin = view.pin(item)
                self.require_reads(access, self.api.OutputReadSet(sources=(pin,)), view)
                return pin
            except self.stale:
                continue
        return None

    def _release(self, package, label, anchor):
        for group in package.groups:
            pin = group.reference
            if (group.group_id == label and isinstance(pin, self.api.SourceInput)
                    and group.sources == (pin.source,) and not group.highlights and pin != anchor
                    and not any(other is not group and pin.source in other.sources for other in package.groups)):
                return pin
        return None
