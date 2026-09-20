"""Stable data contracts for Host-owned continuity state.

C0-C4 established RealityClock, durable memory, retrieval, retention and Work
projection contracts. C5 adds provenance-bearing relationship events plus
rebuildable relationship/affect snapshots. C6 adds simulated Character Life schedules, threads, and events; C8 adds
bounded archive-recall contracts without changing fact or execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ContinuityFactAuthority(StrEnum):
    """Authority/provenance classes that continuity facts must preserve."""

    CANON = "canon"
    USER_SHARED = "user_shared"
    HOST_VERIFIED = "host_verified"
    SIMULATED_LIFE = "simulated_life"
    EXTERNAL_VERIFIED = "external_verified"


class ContinuityFactSource(StrEnum):
    """Permitted source labels for durable continuity facts."""

    USER_ASSERTED = "user_asserted"
    USER_CONFIRMED = "user_confirmed"
    HOST_VERIFIED = "host_verified"
    WORK_LEDGER = "work_ledger"
    AUIP_VERIFIED = "auip_verified"
    SIMULATED_LIFE_RUNTIME = "simulated_life_runtime"


class MemoryKind(StrEnum):
    USER_FACT = "user_fact"
    PREFERENCE = "preference"
    EPISODIC = "episodic"
    TOPIC = "topic"
    OPEN_LOOP = "open_loop"
    RELATIONSHIP_EVENT = "relationship_event"
    TEMPORARY_CONTEXT = "temporary_context"
    HOST_FACT_REF = "host_fact_ref"
    LIFE_SHARED_EVENT = "life_shared_event"


class MemoryPriorityClass(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class MemoryState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class RetentionTier(StrEnum):
    """Retrieval tier, orthogonal to logical fact validity."""

    HOT = "hot"
    COLD = "cold"
    ARCHIVE = "archive"


class MemoryDirectiveAction(StrEnum):
    REMEMBER = "remember"
    FORGET = "forget"
    MUTE = "mute"


class RelationshipStateClass(StrEnum):
    RELATIONSHIP = "relationship"
    AFFECT = "affect"


class LifeScheduleStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class LifeThreadStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class ConsolidationStatus(StrEnum):
    OBSERVED = "observed"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ConversationClockState:
    """Persisted singleton clock state owned by the Host."""

    last_user_turn_at: float | None = None
    last_assistant_completed_at: float | None = None
    last_successful_chat_at: float | None = None
    last_app_started_at: float | None = None
    last_graceful_shutdown_at: float | None = None
    last_observed_wall_at: float | None = None
    wall_clock_high_water_at: float | None = None
    last_observed_local_date: str = ""
    last_observed_utc_offset_minutes: int | None = None
    clock_adjusted: bool = False
    last_session_id: str = ""
    updated_at: float | None = None


@dataclass(frozen=True, slots=True)
class RealitySnapshot:
    """Current reality-clock projection for later Main Chat grounding."""

    now: datetime
    last_user_turn_at: datetime | None
    last_assistant_completed_at: datetime | None
    last_successful_chat_at: datetime | None
    last_app_started_at: datetime | None
    last_graceful_shutdown_at: datetime | None
    elapsed_since_last_successful_chat_seconds: float | None
    process_elapsed_seconds: float | None
    clock_adjusted: bool
    local_date: str
    utc_offset_minutes: int
    last_session_id: str


@dataclass(frozen=True, slots=True)
class TurnEvidence:
    """One accepted user/assistant turn supplied to memory consolidation.

    The transcript remains owned by Session JSON.  This object is ephemeral and
    exists only long enough for C2 extraction/resolution; it is not persisted as
    a second full transcript.
    """

    session_id: str
    turn_id: str
    user_text: str
    assistant_text: str = ""
    source: str = ""
    user_created_at: str = ""
    assistant_created_at: str = ""
    observed_at: float | None = None


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """Structured fact candidate emitted by a MemoryExtractor."""

    memory_key: str
    kind: MemoryKind
    summary: str
    subject: str = "user"
    predicate: str = ""
    object_text: str = ""
    scope: str = "global"
    importance: float = 0.5
    confidence: float = 1.0
    future_value: float = 0.0
    relationship_value: float = 0.0
    priority_class: MemoryPriorityClass = MemoryPriorityClass.P2
    pinned: bool = False
    explicit_keep: bool = False
    source_type: ContinuityFactSource = ContinuityFactSource.USER_ASSERTED
    valid_from: float | None = None
    expires_at: float | None = None
    work_item_id: str = ""


@dataclass(frozen=True, slots=True)
class MemoryDirective:
    """High-confidence explicit user remember/forget/mute command."""

    action: MemoryDirectiveAction
    target_key: str = ""
    target_kind: MemoryKind | None = None
    target_text: str = ""
    candidate: MemoryCandidate | None = None


@dataclass(frozen=True, slots=True)
class MemoryMute:
    """User-requested topic suppression (stop proactively raising a topic).

    A mute is a Host-owned presentation/retrieval filter, not a memory fact:
    matching durable memories stay stored and can still be recalled when the
    user raises the topic themselves.
    """

    id: str
    scope: str
    topic: str
    created_at: float
    cleared_at: float | None = None
    source_session_id: str = ""
    source_turn_id: str = ""
    reason: str = "user_mute"


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    memory_key: str
    scope: str
    kind: MemoryKind
    subject: str
    predicate: str
    object_text: str
    summary: str
    importance: float
    confidence: float
    future_value: float
    relationship_value: float
    priority_class: MemoryPriorityClass
    mention_count: int
    recall_count: int
    created_at: float
    updated_at: float
    last_mentioned_at: float
    last_recalled_at: float | None
    valid_from: float | None
    valid_to: float | None
    expires_at: float | None
    pinned: bool
    state: MemoryState
    source_type: ContinuityFactSource
    source_session_id: str
    source_turn_id: str
    source_hash: str
    work_item_id: str
    retention_tier: RetentionTier = RetentionTier.HOT
    retention_score: float = 0.5
    last_retention_at: float | None = None




@dataclass(frozen=True, slots=True)
class RelationshipProposal:
    """Host-validated candidate for one bounded C5 relationship event.

    Proposals are evidence, not authority.  Store policy decides confidence,
    caps, idempotency, and the durable delta.
    """

    event_type: str
    state_class: RelationshipStateClass
    dimension: str
    delta: float
    confidence: float = 1.0
    source_memory_id: str = ""
    source_memory_key: str = ""
    occurred_at: float | None = None


@dataclass(frozen=True, slots=True)
class RelationshipEvent:
    event_id: str
    source_session_id: str
    source_turn_id: str
    source_memory_id: str
    source_memory_key: str
    source_hash: str
    source_fingerprint: str
    event_type: str
    state_class: RelationshipStateClass
    dimension: str
    proposed_delta: float
    bounded_delta: float
    confidence: float
    occurred_at: float
    created_at: float
    invalidated_at: float | None
    invalidation_reason: str
    policy_version: str


@dataclass(frozen=True, slots=True)
class RelationshipDimensionState:
    dimension: str
    value: float
    event_count: int
    updated_at: float
    policy_version: str


@dataclass(frozen=True, slots=True)
class AffectDimensionState:
    dimension: str
    value: float
    event_count: int
    as_of: float
    policy_version: str


@dataclass(frozen=True, slots=True)
class RelationshipSnapshot:
    relationship: tuple[RelationshipDimensionState, ...] = ()
    affect: tuple[AffectDimensionState, ...] = ()
    as_of: float = 0.0

    def relationship_value(self, dimension: str, default: float = 0.5) -> float:
        for item in self.relationship:
            if item.dimension == dimension:
                return item.value
        return float(default)

    def affect_value(self, dimension: str, default: float = 0.0) -> float:
        for item in self.affect:
            if item.dimension == dimension:
                return item.value
        return float(default)


@dataclass(frozen=True, slots=True)
class LifeScheduleItemPlan:
    ordinal: int
    category: str
    title: str
    starts_at: float
    ends_at: float
    thread_key: str = ""
    thread_title: str = ""


@dataclass(frozen=True, slots=True)
class LifeSchedule:
    schedule_id: str
    character_id: str
    local_date: str
    profile_version: str
    seed_hash: str
    generated_at: float
    source_class: ContinuityFactAuthority


@dataclass(frozen=True, slots=True)
class LifeScheduleItem:
    item_id: str
    schedule_id: str
    ordinal: int
    category: str
    title: str
    starts_at: float
    ends_at: float
    status: LifeScheduleStatus
    thread_key: str
    interrupted_by_session_id: str
    interrupted_by_turn_id: str
    updated_at: float


@dataclass(frozen=True, slots=True)
class LifeThread:
    thread_id: str
    character_id: str
    thread_key: str
    title: str
    status: LifeThreadStatus
    progress: float
    source_class: ContinuityFactAuthority
    created_at: float
    updated_at: float
    last_event_at: float | None


@dataclass(frozen=True, slots=True)
class LifeEvent:
    event_id: str
    character_id: str
    local_date: str
    schedule_item_id: str
    thread_id: str
    event_type: str
    summary: str
    feeling: str
    follow_up: str
    source_class: ContinuityFactAuthority
    source_session_id: str
    source_turn_id: str
    source_memory_id: str
    source_memory_key: str
    source_hash: str
    source_fingerprint: str
    occurred_at: float
    created_at: float
    invalidated_at: float | None
    invalidation_reason: str
    policy_version: str


@dataclass(frozen=True, slots=True)
class LifeSnapshot:
    character_id: str
    local_date: str
    schedule: LifeSchedule | None = None
    items: tuple[LifeScheduleItem, ...] = ()
    current_item: LifeScheduleItem | None = None
    next_item: LifeScheduleItem | None = None
    recent_events: tuple[LifeEvent, ...] = ()
    active_threads: tuple[LifeThread, ...] = ()
    as_of: float = 0.0




@dataclass(frozen=True, slots=True)
class MemoryTombstone:
    id: str
    scope: str
    memory_key: str
    kind: MemoryKind | None
    created_at: float
    source_session_id: str
    source_turn_id: str
    reason: str
    cleared_at: float | None


@dataclass(frozen=True, slots=True)
class ConsolidationTurnState:
    session_id: str
    turn_id: str
    status: ConsolidationStatus
    source: str
    user_created_at: str
    assistant_created_at: str
    source_hash: str
    attempts: int
    last_error: str
    observed_at: float | None
    completed_at: float | None
    updated_at: float


@dataclass(frozen=True, slots=True)
class MemoryRetrievalHit:
    """One C3 recall candidate selected from Host-owned durable memory."""

    memory: MemoryRecord
    score: float
    structured_score: float = 0.0
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    recency_score: float = 0.0
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArchiveRetrievalHit:
    """One bounded C8 high-fidelity Session archive fallback hit."""

    session_id: str
    turn_id: str
    user_text: str = ""
    assistant_text: str = ""
    created_at: str = ""
    score: float = 0.0
    lexical_score: float = 0.0
    temporal_score: float = 0.0
    anchor_memory_ids: tuple[str, ...] = ()
    # True when that side no longer matches the transcript word-for-word
    # (it was condensed to fit the excerpt budget); the renderer must not
    # present a condensation as verbatim wording.
    user_condensed: bool = False
    assistant_condensed: bool = False


@dataclass(frozen=True, slots=True)
class ContinuityGrounding:
    """Bounded C3-C8 projection passed to Main Chat for one turn."""

    text: str = ""
    memory_count: int = 0
    recalled_memory_ids: tuple[str, ...] = ()
    semantic_used: bool = False
    archive_used: bool = False
    archive_hit_count: int = 0
