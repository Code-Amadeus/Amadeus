"""C5 bounded relationship derived-state runtime.

The relationship runtime never owns user facts, Persona, permissions, or Work
truth.  It accepts conservative user-authored proposals, commits them through
Host policy, and renders only a qualitative bounded projection.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from typing import Protocol, Sequence, TYPE_CHECKING

from core.continuity.memory_extractor import parse_explicit_memory_directive, stable_freeform_memory_key
from core.continuity.models import (
    RelationshipProposal,
    RelationshipSnapshot,
    RelationshipStateClass,
    TurnEvidence,
)
from core.continuity.relationship_policy import RelationshipPolicy

if TYPE_CHECKING:  # pragma: no cover
    from core.continuity.store import ContinuityStore


def _norm(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    return re.sub(r"\s+", " ", text)


def relationship_evidence_hash(evidence: TurnEvidence) -> str:
    """Hash only user-authored relationship evidence plus stable turn identity."""

    payload = "\x1f".join((
        str(evidence.session_id or ""),
        str(evidence.turn_id or ""),
        _norm(evidence.user_text),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RelationshipExtractor(Protocol):
    async def extract(self, evidence: TurnEvidence) -> Sequence[RelationshipProposal]:
        """Propose relationship events from accepted evidence.

        Implementations must not treat assistant wording as evidence for a new
        durable relationship change.
        """


class DeterministicRelationshipExtractor:
    """Conservative user-only baseline for C5.

    Durable relationship events are emitted only for direct/high-confidence
    user expressions addressed to the assistant. Ordinary chat is deliberately
    not journaled as relationship state. The Host still applies all confidence,
    delta, window, and replay policy.
    """

    async def extract(self, evidence: TurnEvidence) -> Sequence[RelationshipProposal]:
        text = _norm(evidence.user_text)
        if not text:
            return ()
        source_key = stable_freeform_memory_key(text)
        proposals: list[RelationshipProposal] = []
        # Memory-control commands are control-plane text.  In particular, a
        # request such as "forget that I trust you" must not be re-read as a
        # fresh positive relationship signal on chat.complete.
        if parse_explicit_memory_directive(text) is not None:
            return ()
        lowered = text.casefold()

        def add(event_type: str, state_class: RelationshipStateClass, dimension: str, delta: float, confidence: float = 0.95) -> None:
            proposals.append(
                RelationshipProposal(
                    event_type=event_type,
                    state_class=state_class,
                    dimension=dimension,
                    delta=delta,
                    confidence=confidence,
                    source_memory_key=source_key,
                    occurred_at=evidence.observed_at,
                )
            )

        # Direct trust / distrust.
        if re.search(r"(?:我(?:很)?信任你|我相信你|i\s+trust\s+you|i\s+believe\s+you)", lowered):
            add("user_expressed_trust", RelationshipStateClass.RELATIONSHIP, "familiarity", 0.010, 0.96)
            add("user_expressed_trust", RelationshipStateClass.RELATIONSHIP, "trust", 0.04, 0.99)
            add("user_expressed_trust", RelationshipStateClass.RELATIONSHIP, "closeness", 0.015, 0.94)
        if re.search(r"(?:我不信任你|我不相信你|i\s+(?:do\s+not|don't)\s+trust\s+you)", lowered):
            add("user_expressed_distrust", RelationshipStateClass.RELATIONSHIP, "familiarity", 0.005, 0.94)
            add("user_expressed_distrust", RelationshipStateClass.RELATIONSHIP, "trust", -0.05, 0.99)
            add("user_expressed_distrust", RelationshipStateClass.AFFECT, "tension", 0.10, 0.98)

        # Explicit appreciation directed at the assistant.
        if re.search(r"(?:谢谢你|多亏你|你帮了我(?:很大|大)?的?忙|thank\s+you|thanks\s+to\s+you|you\s+really\s+helped\s+me)", lowered):
            add("user_appreciation", RelationshipStateClass.RELATIONSHIP, "familiarity", 0.006, 0.92)
            add("user_appreciation", RelationshipStateClass.RELATIONSHIP, "warmth", 0.025, 0.97)
            add("user_appreciation", RelationshipStateClass.RELATIONSHIP, "respect", 0.015, 0.93)

        # Direct enjoyment of the relationship/conversation.
        if re.search(r"(?:我(?:很)?喜欢(?:和|跟)你聊天|跟你聊天(?:很)?开心|和你聊天(?:很)?开心|i\s+(?:really\s+)?like\s+(?:talking|chatting)\s+(?:to|with)\s+you|i\s+enjoy\s+(?:talking|chatting)\s+(?:to|with)\s+you)", lowered):
            add("user_enjoys_interaction", RelationshipStateClass.RELATIONSHIP, "familiarity", 0.010, 0.96)
            add("user_enjoys_interaction", RelationshipStateClass.RELATIONSHIP, "warmth", 0.025, 0.98)
            add("user_enjoys_interaction", RelationshipStateClass.RELATIONSHIP, "closeness", 0.025, 0.98)

        # Direct disappointment or annoyance must mention the assistant/"you".
        if re.search(r"(?:我对你(?:很)?失望|你让我(?:很)?失望|i(?:'m|\s+am)\s+disappointed\s+(?:in|with)\s+you)", lowered):
            add("user_disappointment", RelationshipStateClass.RELATIONSHIP, "trust", -0.03, 0.98)
            add("user_disappointment", RelationshipStateClass.AFFECT, "tension", 0.08, 0.97)
        if re.search(r"(?:你让我(?:很)?生气|我在生你的气|你惹我生气了|you\s+(?:made|make)\s+me\s+(?:angry|mad)|i(?:'m|\s+am)\s+angry\s+with\s+you)", lowered):
            add("user_anger", RelationshipStateClass.AFFECT, "irritation", 0.16, 0.99)
            add("user_anger", RelationshipStateClass.AFFECT, "tension", 0.08, 0.97)
            add("user_anger", RelationshipStateClass.RELATIONSHIP, "warmth", -0.015, 0.91)

        # Explicit repair/forgiveness can reduce temporary negative affect, but
        # does not automatically restore long-term trust.
        if re.search(r"(?:我原谅你|我不生你的气了|这次我原谅你|i\s+forgive\s+you|we(?:'re|\s+are)\s+okay\s+now)", lowered):
            add("user_repair", RelationshipStateClass.AFFECT, "irritation", -0.14, 0.96)
            add("user_repair", RelationshipStateClass.AFFECT, "tension", -0.10, 0.94)

        return tuple(proposals)


class RelationshipRuntime:
    """Small Host-owned facade over relationship extraction/store policy."""

    def __init__(
        self,
        store: "ContinuityStore",
        *,
        policy: RelationshipPolicy | None = None,
        extractor: RelationshipExtractor | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or RelationshipPolicy()
        self.extractor = extractor or DeterministicRelationshipExtractor()

    async def process_turn(self, evidence: TurnEvidence, *, as_of: float | None = None):
        proposals = tuple(await self.extractor.extract(evidence))
        if not proposals:
            return ()
        return await asyncio.to_thread(
            self.store.apply_relationship_proposals,
            evidence,
            proposals,
            policy=self.policy,
            as_of=as_of,
        )

    def snapshot(self, *, now: float, scope: str = "global") -> RelationshipSnapshot:
        return self.store.get_relationship_snapshot(now=now, scope=scope, policy=self.policy)

    def render_projection(self, *, now: float, scope: str = "global") -> str:
        return render_relationship_projection(
            self.snapshot(now=now, scope=scope),
            policy=self.policy,
        )


def _level(value: float) -> str:
    value = max(0.0, min(1.0, float(value)))
    if value < 0.20:
        return "very low"
    if value < 0.40:
        return "low"
    if value < 0.60:
        return "moderate"
    if value < 0.80:
        return "moderately high"
    return "high"


def _affect_level(value: float) -> str:
    value = max(0.0, min(1.0, float(value)))
    if value < 0.08:
        return "minimal"
    if value < 0.25:
        return "low"
    if value < 0.50:
        return "moderate"
    if value < 0.75:
        return "elevated"
    return "high"


def render_relationship_projection(snapshot: RelationshipSnapshot, *, policy: RelationshipPolicy | None = None) -> str:
    """Render qualitative C5-B context without scalars or provenance IDs."""

    policy = policy or RelationshipPolicy()
    relationship = {item.dimension: item.value for item in snapshot.relationship}
    affect = {item.dimension: item.value for item in snapshot.affect}
    # A neutral untouched database should not create artificial relationship
    # narration.  Require at least one contributing event.
    if not any(item.event_count for item in snapshot.relationship) and not any(item.event_count for item in snapshot.affect):
        return ""

    candidates: list[str] = []
    relationship_has_evidence = any(item.event_count for item in snapshot.relationship)
    familiarity = relationship.get("familiarity", policy.long_term_baseline)
    trust = relationship.get("trust", policy.long_term_baseline)
    warmth = relationship.get("warmth", policy.long_term_baseline)
    closeness = relationship.get("closeness", policy.long_term_baseline)
    if relationship_has_evidence:
        candidates.append(f"- Familiarity is {_level(familiarity)}.")
        if abs(trust - policy.long_term_baseline) >= 0.015:
            candidates.append(f"- Trust is {_level(trust)}.")
        if abs(warmth - policy.long_term_baseline) >= 0.015:
            candidates.append(f"- Warmth is {_level(warmth)}.")
        if abs(closeness - policy.long_term_baseline) >= 0.015:
            candidates.append(f"- Closeness is {_level(closeness)}.")
    for dimension in ("irritation", "tension", "playfulness", "embarrassment"):
        value = affect.get(dimension, policy.affect_baseline)
        if value >= 0.08:
            candidates.append(f"- Current {dimension} is {_affect_level(value)}.")

    if not candidates:
        return ""
    lines = candidates[: policy.projection_max_lines]
    prefix = (
        "Use this only to modulate behavioral intensity within the existing Persona; "
        "do not change identity, facts, permissions, Canon, or Work state."
    )
    text = "\n".join((prefix, *lines))
    if len(text) > policy.projection_max_chars:
        text = text[: policy.projection_max_chars].rstrip()
    return text


__all__ = [
    "DeterministicRelationshipExtractor",
    "RelationshipExtractor",
    "RelationshipRuntime",
    "relationship_evidence_hash",
    "render_relationship_projection",
]
