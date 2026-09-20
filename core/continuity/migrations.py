"""SQLite schema migrations for the Continuity Runtime."""

from __future__ import annotations

SCHEMA_VERSION = 8


MIGRATION_1 = r"""
CREATE TABLE IF NOT EXISTS continuity_meta (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_clock (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    last_user_turn_at REAL,
    last_assistant_completed_at REAL,
    last_successful_chat_at REAL,
    last_app_started_at REAL,
    last_graceful_shutdown_at REAL,
    last_observed_wall_at REAL,
    wall_clock_high_water_at REAL,
    last_observed_local_date TEXT NOT NULL DEFAULT '',
    last_observed_utc_offset_minutes INTEGER,
    clock_adjusted INTEGER NOT NULL DEFAULT 0 CHECK (clock_adjusted IN (0, 1)),
    last_session_id TEXT NOT NULL DEFAULT '',
    updated_at REAL
);

INSERT OR IGNORE INTO conversation_clock(singleton_id) VALUES (1);

PRAGMA user_version = 1;
"""


MIGRATION_2 = r"""
-- C2 owns the durable memory write path.  Retrieval indexes, relationship, and
-- character-life state remain deferred to later migrations.
CREATE TABLE IF NOT EXISTS memory_items (
    id TEXT PRIMARY KEY,
    memory_key TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    kind TEXT NOT NULL CHECK (kind IN (
        'user_fact', 'preference', 'episodic', 'topic', 'open_loop',
        'relationship_event', 'temporary_context', 'host_fact_ref',
        'life_shared_event'
    )),
    subject TEXT NOT NULL DEFAULT '',
    predicate TEXT NOT NULL DEFAULT '',
    object_text TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5 CHECK (importance >= 0.0 AND importance <= 1.0),
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    future_value REAL NOT NULL DEFAULT 0.0 CHECK (future_value >= 0.0 AND future_value <= 1.0),
    relationship_value REAL NOT NULL DEFAULT 0.0 CHECK (relationship_value >= 0.0 AND relationship_value <= 1.0),
    priority_class TEXT NOT NULL DEFAULT 'P2' CHECK (priority_class IN ('P0', 'P1', 'P2', 'P3', 'P4')),
    mention_count INTEGER NOT NULL DEFAULT 1 CHECK (mention_count >= 0),
    recall_count INTEGER NOT NULL DEFAULT 0 CHECK (recall_count >= 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_mentioned_at REAL NOT NULL,
    last_recalled_at REAL,
    valid_from REAL,
    valid_to REAL,
    expires_at REAL,
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'superseded')),
    source_type TEXT NOT NULL CHECK (source_type IN (
        'user_asserted', 'user_confirmed', 'host_verified', 'work_ledger',
        'auip_verified', 'simulated_life_runtime'
    )),
    source_session_id TEXT NOT NULL DEFAULT '',
    source_turn_id TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    work_item_id TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_memory_items_key
    ON memory_items(scope, memory_key, state);
CREATE INDEX IF NOT EXISTS idx_memory_items_source_turn
    ON memory_items(source_session_id, source_turn_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_items_one_active_key
    ON memory_items(scope, memory_key)
    WHERE state = 'active';

CREATE TABLE IF NOT EXISTS memory_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_memory_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    to_memory_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    relation TEXT NOT NULL CHECK (relation IN (
        'supersedes', 'conflicts_with', 'supports', 'derived_from',
        'relates_to', 'continuation_of'
    )),
    created_at REAL NOT NULL,
    UNIQUE(from_memory_id, to_memory_id, relation)
);

CREATE TABLE IF NOT EXISTS memory_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL,
    source_hash TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(memory_id, session_id, turn_id)
);

CREATE TABLE IF NOT EXISTS memory_tombstones (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT 'global',
    memory_key TEXT NOT NULL,
    kind TEXT,
    created_at REAL NOT NULL,
    source_session_id TEXT NOT NULL DEFAULT '',
    source_turn_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT 'explicit_forget',
    cleared_at REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_tombstone_active
    ON memory_tombstones(scope, memory_key)
    WHERE cleared_at IS NULL;

-- This is a durable processing journal, not a second transcript.  It stores no
-- user/assistant message bodies.  Recovery re-reads the authoritative Session
-- JSON only for turns that were already observed by Continuity.
CREATE TABLE IF NOT EXISTS consolidation_turns (
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'observed', 'queued', 'processing', 'completed', 'failed', 'skipped'
    )),
    source TEXT NOT NULL DEFAULT '',
    user_created_at TEXT NOT NULL DEFAULT '',
    assistant_created_at TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT NOT NULL DEFAULT '',
    observed_at REAL,
    completed_at REAL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(session_id, turn_id)
);

CREATE INDEX IF NOT EXISTS idx_consolidation_turns_status
    ON consolidation_turns(status, updated_at);

PRAGMA user_version = 2;
"""


MIGRATION_3 = r"""
-- C3 adds rebuildable retrieval structures only. SQLite memory_items remains
-- the sole authority; FTS rows and embeddings may be discarded and rebuilt.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    memory_id UNINDEXED,
    scope UNINDEXED,
    memory_key,
    kind UNINDEXED,
    subject,
    predicate,
    object_text,
    summary,
    tokenize = 'trigram'
);

CREATE TRIGGER IF NOT EXISTS memory_fts_after_insert
AFTER INSERT ON memory_items
WHEN NEW.state = 'active'
BEGIN
    INSERT INTO memory_fts(
        memory_id, scope, memory_key, kind, subject, predicate, object_text, summary
    ) VALUES (
        NEW.id, NEW.scope, NEW.memory_key, NEW.kind, NEW.subject,
        NEW.predicate, NEW.object_text, NEW.summary
    );
END;

CREATE TRIGGER IF NOT EXISTS memory_fts_after_update
AFTER UPDATE OF state, scope, memory_key, kind, subject, predicate, object_text, summary
ON memory_items
BEGIN
    DELETE FROM memory_fts WHERE memory_id = OLD.id;
    INSERT INTO memory_fts(
        memory_id, scope, memory_key, kind, subject, predicate, object_text, summary
    )
    SELECT
        NEW.id, NEW.scope, NEW.memory_key, NEW.kind, NEW.subject,
        NEW.predicate, NEW.object_text, NEW.summary
    WHERE NEW.state = 'active';
END;

CREATE TRIGGER IF NOT EXISTS memory_fts_after_delete
AFTER DELETE ON memory_items
BEGIN
    DELETE FROM memory_fts WHERE memory_id = OLD.id;
END;

-- Backfill when upgrading an existing C2 database. Re-running is safe because
-- migration 3 executes once under PRAGMA user_version.
DELETE FROM memory_fts;
INSERT INTO memory_fts(
    memory_id, scope, memory_key, kind, subject, predicate, object_text, summary
)
SELECT id, scope, memory_key, kind, subject, predicate, object_text, summary
FROM memory_items
WHERE state = 'active';

CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    dimension INTEGER NOT NULL CHECK (dimension > 0),
    vector_blob BLOB NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(memory_id, model_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model
    ON memory_embeddings(model_id, updated_at);

CREATE TRIGGER IF NOT EXISTS memory_embeddings_drop_inactive
AFTER UPDATE OF state ON memory_items
WHEN NEW.state <> 'active'
BEGIN
    DELETE FROM memory_embeddings WHERE memory_id = NEW.id;
END;

PRAGMA user_version = 3;
"""

MIGRATION_4 = r"""
-- C4 retention is deliberately orthogonal to logical fact state.  A valid
-- active fact can be hot, cold, or archived; superseded remains a fact state.
ALTER TABLE memory_items ADD COLUMN retention_tier TEXT NOT NULL DEFAULT 'hot'
    CHECK (retention_tier IN ('hot', 'cold', 'archive'));
ALTER TABLE memory_items ADD COLUMN retention_score REAL NOT NULL DEFAULT 0.5
    CHECK (retention_score >= 0.0 AND retention_score <= 1.0);
ALTER TABLE memory_items ADD COLUMN last_retention_at REAL;

CREATE INDEX IF NOT EXISTS idx_memory_items_retention
    ON memory_items(scope, state, retention_tier, retention_score, last_mentioned_at);
CREATE INDEX IF NOT EXISTS idx_memory_items_work
    ON memory_items(work_item_id, state);

CREATE TABLE IF NOT EXISTS continuity_maintenance_runs (
    run_id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    completed_at REAL NOT NULL,
    now_at REAL NOT NULL,
    scanned INTEGER NOT NULL DEFAULT 0,
    promoted INTEGER NOT NULL DEFAULT 0,
    demoted INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT 'scheduled'
);

CREATE TABLE IF NOT EXISTS continuity_work_events (
    event_id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL,
    status TEXT NOT NULL,
    observed_at REAL NOT NULL,
    payload_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_continuity_work_events_item
    ON continuity_work_events(work_item_id, observed_at);

-- Keep the derived FTS accelerator bounded to hot/cold active rows. Archive
-- remains durable authority but is reserved for explicit historical fallback.
DROP TRIGGER IF EXISTS memory_fts_after_update;
CREATE TRIGGER memory_fts_after_update
AFTER UPDATE OF state, retention_tier, scope, memory_key, kind, subject, predicate, object_text, summary
ON memory_items
BEGIN
    DELETE FROM memory_fts WHERE memory_id = OLD.id;
    INSERT INTO memory_fts(
        memory_id, scope, memory_key, kind, subject, predicate, object_text, summary
    )
    SELECT NEW.id, NEW.scope, NEW.memory_key, NEW.kind, NEW.subject,
           NEW.predicate, NEW.object_text, NEW.summary
    WHERE NEW.state = 'active' AND NEW.retention_tier IN ('hot', 'cold');
END;
DELETE FROM memory_fts;
INSERT INTO memory_fts(memory_id, scope, memory_key, kind, subject, predicate, object_text, summary)
SELECT id, scope, memory_key, kind, subject, predicate, object_text, summary
FROM memory_items WHERE state = 'active' AND retention_tier IN ('hot', 'cold');

PRAGMA user_version = 4;
"""


MIGRATION_5 = r"""
-- C5 stores relationship continuity as provenance-bearing derived state.  The
-- event ledger is durable evidence metadata; snapshots remain rebuildable and
-- never become Persona, user-fact, Work, or permission authority.
CREATE TABLE IF NOT EXISTS relationship_events (
    event_id TEXT PRIMARY KEY,
    source_session_id TEXT NOT NULL DEFAULT '',
    source_turn_id TEXT NOT NULL DEFAULT '',
    source_memory_id TEXT REFERENCES memory_items(id) ON DELETE SET NULL,
    source_memory_key TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    source_fingerprint TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    state_class TEXT NOT NULL CHECK (state_class IN ('relationship', 'affect')),
    dimension TEXT NOT NULL CHECK (dimension IN (
        'familiarity', 'trust', 'warmth', 'respect', 'closeness',
        'irritation', 'embarrassment', 'tension', 'playfulness'
    )),
    proposed_delta REAL NOT NULL,
    bounded_delta REAL NOT NULL DEFAULT 0.0,
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    occurred_at REAL NOT NULL,
    created_at REAL NOT NULL,
    invalidated_at REAL,
    invalidation_reason TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relationship_events_dimension
    ON relationship_events(state_class, dimension, occurred_at, event_id);
CREATE INDEX IF NOT EXISTS idx_relationship_events_source_memory
    ON relationship_events(source_memory_id, source_memory_key);
CREATE INDEX IF NOT EXISTS idx_relationship_events_source_turn
    ON relationship_events(source_session_id, source_turn_id, source_hash);
CREATE INDEX IF NOT EXISTS idx_relationship_events_valid
    ON relationship_events(invalidated_at, state_class, dimension, occurred_at);

CREATE TABLE IF NOT EXISTS relationship_state (
    dimension TEXT PRIMARY KEY CHECK (dimension IN (
        'familiarity', 'trust', 'warmth', 'respect', 'closeness'
    )),
    value REAL NOT NULL CHECK (value >= 0.0 AND value <= 1.0),
    event_count INTEGER NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    updated_at REAL NOT NULL,
    policy_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS short_term_affect (
    dimension TEXT PRIMARY KEY CHECK (dimension IN (
        'irritation', 'embarrassment', 'tension', 'playfulness'
    )),
    value REAL NOT NULL CHECK (value >= 0.0 AND value <= 1.0),
    event_count INTEGER NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    as_of REAL NOT NULL,
    policy_version TEXT NOT NULL
);

INSERT OR IGNORE INTO relationship_state(dimension, value, event_count, updated_at, policy_version)
VALUES
    ('familiarity', 0.5, 0, 0.0, 'c5-v1'),
    ('trust', 0.5, 0, 0.0, 'c5-v1'),
    ('warmth', 0.5, 0, 0.0, 'c5-v1'),
    ('respect', 0.5, 0, 0.0, 'c5-v1'),
    ('closeness', 0.5, 0, 0.0, 'c5-v1');

INSERT OR IGNORE INTO short_term_affect(dimension, value, event_count, as_of, policy_version)
VALUES
    ('irritation', 0.0, 0, 0.0, 'c5-v1'),
    ('embarrassment', 0.0, 0, 0.0, 'c5-v1'),
    ('tension', 0.0, 0, 0.0, 'c5-v1'),
    ('playfulness', 0.0, 0, 0.0, 'c5-v1');

PRAGMA user_version = 5;
"""


MIGRATION_6 = r"""
-- C6 Character Life is a Host-owned simulated derived-state domain. It is
-- intentionally separate from durable user facts, Character RAG, Work, AUIP,
-- Provider authority, and C5 relationship evidence.
CREATE TABLE IF NOT EXISTS daily_schedules (
    schedule_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    local_date TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    seed_hash TEXT NOT NULL,
    generated_at REAL NOT NULL,
    source_class TEXT NOT NULL DEFAULT 'simulated_life'
        CHECK (source_class = 'simulated_life'),
    UNIQUE(character_id, local_date)
);
CREATE INDEX IF NOT EXISTS idx_daily_schedules_character_date
    ON daily_schedules(character_id, local_date);

CREATE TABLE IF NOT EXISTS schedule_items (
    item_id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES daily_schedules(schedule_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    starts_at REAL NOT NULL,
    ends_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned', 'running', 'paused', 'completed', 'skipped')),
    thread_key TEXT NOT NULL DEFAULT '',
    interrupted_by_session_id TEXT NOT NULL DEFAULT '',
    interrupted_by_turn_id TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    UNIQUE(schedule_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_schedule_items_schedule_time
    ON schedule_items(schedule_id, starts_at, ends_at, ordinal);
CREATE INDEX IF NOT EXISTS idx_schedule_items_status
    ON schedule_items(status, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS life_threads (
    thread_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'completed')),
    progress REAL NOT NULL DEFAULT 0.0 CHECK (progress >= 0.0 AND progress <= 1.0),
    source_class TEXT NOT NULL DEFAULT 'simulated_life'
        CHECK (source_class = 'simulated_life'),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_event_at REAL,
    UNIQUE(character_id, thread_key)
);
CREATE INDEX IF NOT EXISTS idx_life_threads_character_status
    ON life_threads(character_id, status, updated_at);

CREATE TABLE IF NOT EXISTS life_events (
    event_id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    local_date TEXT NOT NULL,
    schedule_item_id TEXT REFERENCES schedule_items(item_id) ON DELETE SET NULL,
    thread_id TEXT REFERENCES life_threads(thread_id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    feeling TEXT NOT NULL DEFAULT '',
    follow_up TEXT NOT NULL DEFAULT '',
    source_class TEXT NOT NULL DEFAULT 'simulated_life'
        CHECK (source_class = 'simulated_life'),
    source_session_id TEXT NOT NULL DEFAULT '',
    source_turn_id TEXT NOT NULL DEFAULT '',
    source_memory_id TEXT REFERENCES memory_items(id) ON DELETE SET NULL,
    source_memory_key TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    source_fingerprint TEXT NOT NULL UNIQUE,
    occurred_at REAL NOT NULL,
    created_at REAL NOT NULL,
    invalidated_at REAL,
    invalidation_reason TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_life_events_character_date
    ON life_events(character_id, local_date, invalidated_at, occurred_at);
CREATE INDEX IF NOT EXISTS idx_life_events_source_memory
    ON life_events(source_memory_id, source_memory_key);
CREATE INDEX IF NOT EXISTS idx_life_events_schedule_item
    ON life_events(schedule_item_id, invalidated_at, occurred_at);

PRAGMA user_version = 6;
"""


MIGRATION_7 = r"""
-- C8 Archive Recall adds content-free provenance guards for explicit forget.
-- Session JSON remains the authoritative transcript archive; this table stores
-- only source identity metadata needed to fail closed when a forgotten turn
-- could otherwise be surfaced by historical fallback.
ALTER TABLE memory_tombstones ADD COLUMN archive_guard_complete INTEGER NOT NULL DEFAULT 0
    CHECK (archive_guard_complete IN (0, 1));

CREATE TABLE IF NOT EXISTS memory_forget_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tombstone_id TEXT NOT NULL REFERENCES memory_tombstones(id) ON DELETE CASCADE,
    source_session_id TEXT NOT NULL DEFAULT '',
    source_turn_id TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    forgotten_at REAL NOT NULL,
    UNIQUE(tombstone_id, source_session_id, source_turn_id, source_hash)
);
CREATE INDEX IF NOT EXISTS idx_memory_forget_sources_turn
    ON memory_forget_sources(source_session_id, source_turn_id, tombstone_id);

PRAGMA user_version = 7;
"""


MIGRATION_8 = r"""
-- 2026-09 Session isolation: remembered facts, tombstones, topic mutes and
-- relationship/affect state belong to their owning chat Session.  ``scope`` is
-- the storage form of that Session id; 'global' is a legacy placeholder that
-- is backfilled from recorded provenance below.  Character Life (C6) and the
-- RealityClock intentionally stay character-/host-global.
ALTER TABLE relationship_events ADD COLUMN scope TEXT NOT NULL DEFAULT 'global';
CREATE INDEX IF NOT EXISTS idx_relationship_events_scope
    ON relationship_events(scope, occurred_at, event_id);

-- The two snapshot tables are derived caches that can always be recomputed
-- from scoped events (heal-on-read keeps them current), so they are re-created
-- with the scope key instead of migrated row by row.
DROP TABLE IF EXISTS relationship_state;
CREATE TABLE relationship_state (
    scope TEXT NOT NULL,
    dimension TEXT NOT NULL CHECK (dimension IN (
        'familiarity', 'trust', 'warmth', 'respect', 'closeness'
    )),
    value REAL NOT NULL CHECK (value >= 0.0 AND value <= 1.0),
    event_count INTEGER NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    updated_at REAL NOT NULL,
    policy_version TEXT NOT NULL,
    PRIMARY KEY(scope, dimension)
);

DROP TABLE IF EXISTS short_term_affect;
CREATE TABLE short_term_affect (
    scope TEXT NOT NULL,
    dimension TEXT NOT NULL CHECK (dimension IN (
        'irritation', 'embarrassment', 'tension', 'playfulness'
    )),
    value REAL NOT NULL CHECK (value >= 0.0 AND value <= 1.0),
    event_count INTEGER NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    as_of REAL NOT NULL,
    policy_version TEXT NOT NULL,
    PRIMARY KEY(scope, dimension)
);

-- Ownership backfill from recorded provenance; rows without a source Session
-- keep the inert 'global' placeholder and are never matched by a real scope.
UPDATE memory_items SET scope = source_session_id
 WHERE scope = 'global' AND source_session_id <> '';
UPDATE memory_tombstones SET scope = source_session_id
 WHERE scope = 'global' AND source_session_id <> '';
UPDATE relationship_events SET scope = source_session_id
 WHERE scope = 'global' AND source_session_id <> '';

PRAGMA user_version = 8;
"""


MIGRATIONS: dict[int, str] = {
    1: MIGRATION_1,
    2: MIGRATION_2,
    3: MIGRATION_3,
    4: MIGRATION_4,
    5: MIGRATION_5,
    6: MIGRATION_6,
    7: MIGRATION_7,
    8: MIGRATION_8,
}
