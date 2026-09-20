"""C3 hybrid recall over Host-owned durable memory."""

from __future__ import annotations

import math
import re
import time
import unicodedata
from typing import Protocol, Sequence

from core.continuity.models import MemoryKind, MemoryRecord, MemoryRetrievalHit, RetentionTier
from core.continuity.retrieval_policy import ContinuityRetrievalPolicy
from core.continuity.store import ContinuityStore
from core.continuity.topic_mute import text_is_muted


class SemanticSearcher(Protocol):
    def search(
        self,
        query: str,
        records: Sequence[MemoryRecord],
        *,
        top_k: int,
    ) -> dict[str, float]: ...


_SLOT_QUERY_KEYS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("生日", "出生日期", "birthday", "birth date"), "user.fact.birth_date"),
    (("名字", "姓名", "name"), "user.fact.name"),
    (("职业", "工作是什么", "occupation", "job"), "user.fact.occupation"),
    (("住址", "地址", "居住地", "城市", "location", "where do i live", "city"), "user.fact.location"),
    (("喜欢的颜色", "最喜欢的颜色", "favorite color"), "user.fact.favorite_color"),
    (("喜欢的食物", "最喜欢的食物", "favorite food"), "user.fact.favorite_food"),
)

_PREFERENCE_META_MARKERS = (
    "我的喜好", "我的偏好", "我喜欢什么", "我不喜欢什么",
    "my preferences", "what do i like", "what i like",
)
_OPEN_LOOP_MARKERS = (
    "上次没", "还没完成", "继续", "待办", "下次", "之后再", "回头",
    "unfinished", "continue", "next time", "follow up", "todo",
)


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"\s+", " ", text)


def _fts_terms(query: str, *, limit: int = 14) -> list[str]:
    """Build safe trigram-compatible phrases without exposing FTS operators."""

    text = _normalize(query)
    terms: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = value.strip()
        if len(value) < 3 or value in seen:
            return
        seen.add(value)
        terms.append(value)

    for word in re.findall(r"[a-z0-9][a-z0-9_.-]{2,}", text):
        add(word)
    for sequence in re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]{3,}", text):
        if len(sequence) <= 8:
            add(sequence)
        for width in (4, 3):
            for index in range(0, max(0, len(sequence) - width + 1)):
                add(sequence[index:index + width])
                if len(terms) >= limit:
                    return terms
    return terms[:limit]


def build_fts_match_query(query: str) -> str:
    terms = _fts_terms(query)
    if not terms:
        return ""
    quoted = ['"' + term.replace('"', '""') + '"' for term in terms]
    return " OR ".join(quoted)


def _text_ngrams(value: str) -> set[str]:
    text = re.sub(r"\s+", "", _normalize(value))
    if not text:
        return set()
    if len(text) <= 3:
        return {text}
    return {text[index:index + 3] for index in range(len(text) - 2)}


def _similarity(a: MemoryRecord, b: MemoryRecord) -> float:
    left = _text_ngrams(a.summary)
    right = _text_ngrams(b.summary)
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def _recency_score(record: MemoryRecord, now: float) -> float:
    age_seconds = max(0.0, now - float(record.last_mentioned_at))
    age_days = age_seconds / 86400.0
    return 1.0 / (1.0 + age_days / 30.0)


def _priority_bonus(record: MemoryRecord) -> float:
    return {
        "P0": 1.0,
        "P1": 0.85,
        "P2": 0.65,
        "P3": 0.4,
        "P4": 0.2,
    }.get(record.priority_class.value, 0.5)


def _structured_targets(query: str) -> tuple[set[str], set[MemoryKind]]:
    normalized = _normalize(query)
    key_targets = {
        key
        for markers, key in _SLOT_QUERY_KEYS
        if any(marker in normalized for marker in markers)
    }
    kind_targets: set[MemoryKind] = set()
    if any(marker in normalized for marker in _PREFERENCE_META_MARKERS):
        kind_targets.add(MemoryKind.PREFERENCE)
    if any(marker in normalized for marker in _OPEN_LOOP_MARKERS):
        kind_targets.add(MemoryKind.OPEN_LOOP)
    return key_targets, kind_targets


def _record_is_retrievable(
    record: MemoryRecord,
    *,
    now: float,
    exclude_turn_id: str,
) -> bool:
    if record.expires_at is not None and float(record.expires_at) <= now:
        return False
    if exclude_turn_id and record.source_turn_id == exclude_turn_id:
        return False
    return True


def _memory_search_text(record: MemoryRecord) -> str:
    return f"{record.memory_key} {record.summary} {record.object_text}"


def _mute_allows(record: MemoryRecord, *, suppressed_topics: tuple[str, ...]) -> bool:
    if not suppressed_topics:
        return True
    return not text_is_muted(_memory_search_text(record), topics=suppressed_topics)


class MemoryRetriever:
    def __init__(
        self,
        store: ContinuityStore,
        *,
        policy: ContinuityRetrievalPolicy | None = None,
        semantic_searcher: SemanticSearcher | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or ContinuityRetrievalPolicy()
        self.semantic_searcher = semantic_searcher

    @staticmethod
    def _structured_scores(query: str, records: Sequence[MemoryRecord]) -> dict[str, float]:
        normalized = _normalize(query)
        key_targets, kind_targets = _structured_targets(query)
        scores: dict[str, float] = {}
        for record in records:
            score = 0.0
            if record.memory_key in key_targets:
                score = 1.0
            if MemoryKind.PREFERENCE in kind_targets and record.kind is MemoryKind.PREFERENCE:
                score = max(score, 0.9)
            if MemoryKind.OPEN_LOOP in kind_targets and record.kind is MemoryKind.OPEN_LOOP:
                score = max(score, 0.95)
            # Stable slot names may be intentionally used by tests/dev tools.
            if record.memory_key.casefold() in normalized:
                score = max(score, 1.0)
            if score:
                scores[record.id] = score
        return scores

    def retrieve(
        self,
        query: str,
        *,
        scope: str = "global",
        now: float | None = None,
        exclude_turn_id: str = "",
        suppressed_topics: tuple[str, ...] = (),
    ) -> list[MemoryRetrievalHit]:
        clean_query = str(query or "").strip()
        if not clean_query:
            return []
        observed_at = float(time.time() if now is None else now)
        records = self.store.list_retrievable_memories(
            scope=scope,
            now=observed_at,
            exclude_turn_id=exclude_turn_id,
            limit=max(self.policy.candidate_limit * 4, 50),
        )
        record_by_id = {record.id: record for record in records}

        # Deterministic slot/kind recall must not depend on whether a memory
        # happened to survive the generic top-N candidate window.
        key_targets, kind_targets = _structured_targets(clean_query)
        for memory_key in key_targets:
            record = self.store.get_active_memory(memory_key, scope=scope)
            if record is not None and _record_is_retrievable(
                record, now=observed_at, exclude_turn_id=exclude_turn_id
            ):
                record_by_id[record.id] = record
        for kind in kind_targets:
            for record in self.store.list_retrievable_memories(
                scope=scope,
                now=observed_at,
                exclude_turn_id=exclude_turn_id,
                kinds=(kind,),
                limit=self.policy.candidate_limit,
            ):
                record_by_id[record.id] = record
        if suppressed_topics:
            records = [
                record for record in record_by_id.values()
                if _mute_allows(record, suppressed_topics=suppressed_topics)
            ]
            record_by_id = {record.id: record for record in records}
        else:
            records = list(record_by_id.values())
        if not records:
            return []
        structured = self._structured_scores(clean_query, records)

        lexical: dict[str, float] = {}
        match_query = build_fts_match_query(clean_query)
        if match_query:
            fts_hits = self.store.search_memory_fts(
                match_query,
                scope=scope,
                now=observed_at,
                exclude_turn_id=exclude_turn_id,
                limit=self.policy.candidate_limit,
            )
            # FTS bm25 absolute magnitude is corpus-dependent. Rank-normalize
            # here and keep SQLite as the lexical candidate generator.
            count = len(fts_hits)
            for position, (record, _rank) in enumerate(fts_hits):
                lexical[record.id] = 1.0 - (position / max(1, count)) * 0.35
                record_by_id.setdefault(record.id, record)

        semantic: dict[str, float] = {}
        if self.semantic_searcher is not None:
            # Semantic recall is opt-in and may scan a wider bounded corpus.
            # This prevents old but highly relevant memories from being hidden
            # behind a recency/importance pre-window. C4 retention will further
            # constrain long-lived corpus size.
            semantic_records = self.store.list_retrievable_memories(
                scope=scope,
                now=observed_at,
                exclude_turn_id=exclude_turn_id,
                limit=1000,
            )
            if suppressed_topics:
                semantic_records = [
                    record for record in semantic_records
                    if _mute_allows(record, suppressed_topics=suppressed_topics)
                ]
            for record in semantic_records:
                record_by_id.setdefault(record.id, record)
            semantic = self.semantic_searcher.search(
                clean_query,
                semantic_records,
                top_k=min(self.policy.semantic_candidate_limit, len(semantic_records)),
            )

        candidate_ids = set(structured) | set(lexical) | set(semantic)
        if not candidate_ids:
            return []

        weighted: list[MemoryRetrievalHit] = []
        for memory_id in candidate_ids:
            record = record_by_id.get(memory_id)
            if record is None:
                continue
            structured_score = structured.get(memory_id, 0.0)
            lexical_score = lexical.get(memory_id, 0.0)
            semantic_score = semantic.get(memory_id, 0.0)
            recency = _recency_score(record, observed_at)
            importance = max(float(record.importance), _priority_bonus(record) * 0.8)
            score = (
                self.policy.structured_weight * structured_score
                + self.policy.lexical_weight * lexical_score
                + self.policy.semantic_weight * semantic_score
                + self.policy.importance_weight * importance
                + self.policy.recency_weight * recency
            )
            # Pinned/open-loop memories remain easy to surface when there is
            # actual lexical/semantic/structured relevance, never by themselves.
            if record.pinned:
                score += 0.03
            if record.kind is MemoryKind.OPEN_LOOP:
                score += 0.03
            score = max(0.0, min(1.0, score))
            if score < self.policy.minimum_score:
                continue
            reasons: list[str] = []
            if structured_score:
                reasons.append("structured")
            if lexical_score:
                reasons.append("lexical")
            if semantic_score:
                reasons.append("semantic")
            weighted.append(
                MemoryRetrievalHit(
                    memory=record,
                    score=score,
                    structured_score=structured_score,
                    lexical_score=lexical_score,
                    semantic_score=semantic_score,
                    recency_score=recency,
                    reasons=tuple(reasons),
                )
            )

        weighted.sort(key=lambda hit: (-hit.score, -hit.memory.importance, -hit.memory.last_mentioned_at))
        selected: list[MemoryRetrievalHit] = []
        remaining = weighted[: self.policy.candidate_limit]
        open_loops = 0
        while remaining and len(selected) < self.policy.max_items:
            best_index = 0
            best_value = -math.inf
            for index, hit in enumerate(remaining):
                if hit.memory.kind is MemoryKind.OPEN_LOOP and open_loops >= self.policy.max_open_loops:
                    continue
                redundancy = max(
                    (_similarity(hit.memory, chosen.memory) for chosen in selected),
                    default=0.0,
                )
                value = self.policy.mmr_lambda * hit.score - (1.0 - self.policy.mmr_lambda) * redundancy
                if value > best_value:
                    best_value = value
                    best_index = index
            if best_value == -math.inf:
                break
            chosen = remaining.pop(best_index)
            selected.append(chosen)
            if chosen.memory.kind is MemoryKind.OPEN_LOOP:
                open_loops += 1
        return selected


    def retrieve_archive(
        self,
        query: str,
        *,
        scope: str = "global",
        now: float | None = None,
        exclude_turn_id: str = "",
        max_items: int = 3,
        suppressed_topics: tuple[str, ...] = (),
    ) -> list[MemoryRetrievalHit]:
        """C8 bounded search over cold/archive-tier memory summaries.

        This path is never called by ordinary fast recall. It exists only after
        the explicit historical/low-confidence gate and therefore does not put
        cold or archive rows back into the normal FTS working set.  Cold rows
        are included so the demoted-but-durable window still answers explicit
        historical questions.
        """

        clean_query = str(query or "").strip()
        if not clean_query:
            return []
        observed_at = float(time.time() if now is None else now)
        records = [
            record
            for tier in (RetentionTier.ARCHIVE, RetentionTier.COLD)
            for record in self.store.list_memories_by_tier(tier, scope=scope)
            if _record_is_retrievable(record, now=observed_at, exclude_turn_id=exclude_turn_id)
            and _mute_allows(record, suppressed_topics=suppressed_topics)
        ][:1000]
        if not records:
            return []
        structured = self._structured_scores(clean_query, records)
        query_grams = _text_ngrams(clean_query)
        semantic: dict[str, float] = {}
        if self.semantic_searcher is not None:
            semantic = self.semantic_searcher.search(
                clean_query,
                records,
                top_k=min(self.policy.semantic_candidate_limit, len(records)),
            )
        weighted: list[MemoryRetrievalHit] = []
        for record in records:
            target = f"{record.memory_key} {record.summary} {record.object_text}"
            target_grams = _text_ngrams(target)
            lexical = (
                len(query_grams & target_grams) / max(1, min(len(query_grams), 24))
                if query_grams and target_grams
                else 0.0
            )
            structured_score = structured.get(record.id, 0.0)
            semantic_score = semantic.get(record.id, 0.0)
            if not structured_score and not lexical and not semantic_score:
                continue
            importance = max(float(record.importance), _priority_bonus(record) * 0.8)
            recency = _recency_score(record, observed_at)
            score = (
                0.40 * structured_score
                + 0.38 * lexical
                + 0.16 * semantic_score
                + 0.04 * importance
                + 0.02 * recency
            )
            if record.pinned:
                score += 0.02
            score = max(0.0, min(1.0, score))
            if score < min(self.policy.minimum_score, 0.14):
                continue
            reasons = ["archive"]
            if structured_score:
                reasons.append("structured")
            if lexical:
                reasons.append("archive_lexical")
            if semantic_score:
                reasons.append("semantic")
            weighted.append(
                MemoryRetrievalHit(
                    memory=record,
                    score=score,
                    structured_score=structured_score,
                    lexical_score=lexical,
                    semantic_score=semantic_score,
                    recency_score=recency,
                    reasons=tuple(reasons),
                )
            )
        weighted.sort(key=lambda hit: (-hit.score, -hit.memory.importance, -hit.memory.last_mentioned_at))
        return weighted[: max(1, min(int(max_items), 5))]
