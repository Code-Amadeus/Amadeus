# Continuity Runtime Contract — C8 Freeze / Advanced Retrieval Complete

**Frozen:** 2026-09-18  
**Schema:** 7  
**Parent lineage:** C4.1 Candidate -> C5 Preflight -> C5 -> C6 -> C7  
**Historical C4.1 Candidate SHA-256:** `50e7811fc8f4376c52de77dce8a3c5ff370029379c434d3b9933897a78cee6f3`

## Purpose

This contract describes the Continuity authority and invariants implemented through C8. It supersedes the earlier C0/C1/C2/C3-only wording in this file; it does not retroactively change the historical C0-C4 implementation sequence.

The C4.1 Candidate remains historical acceptance evidence; the C8 package is the completed technical forward-development baseline. The historical pre-C4 production rollback artifact was not recoverable, so this document does not relabel the original strict G4 rollback gate as PASS.

## Ownership

Continuity is Host-owned. The LLM may propose text or classifications, but it is not durable-state authority.

- SQLite is the durable authority for Continuity state.
- `ChatRuntime` receives only bounded, read-only Continuity grounding by dependency injection.
- Character RAG remains a separate Canon/reference domain.
- Work Ledger remains the authority for Work lifecycle/completion.
- Provider Runtime and AUIP do not gain Continuity write or execution authority.
- C5 Relationship and C6 Character Life are derived-state subsystems and may not redefine user facts, Work truth, Canon, or permissions.

## Storage invariant

`memory_items` is the durable memory truth table. FTS rows, embeddings and FAISS structures are rebuildable accelerators.

The two memory dimensions remain orthogonal:

```text
logical fact state: active | superseded
retention tier:     hot | cold | archive
```

A fact may remain logically active while becoming cold or archive. Archive is not forget. Explicit forget is not retention decay.

## Schema 4 memory/retention authority

C4 migration adds, without changing the above fact semantics:

- `memory_items.retention_tier`
- `memory_items.retention_score`
- `memory_items.last_retention_at`
- `continuity_maintenance_runs`
- `continuity_work_events`

Schema 4 remains the historical memory/retention baseline. Schema 5 adds Relationship derived state. Schema 6 adds Character Life derived simulation state without changing memory truth.

## Fact provenance

Durable memory records keep source metadata such as session/turn/hash and source type. Source types remain bounded to the existing contract (`user_asserted`, `user_confirmed`, `host_verified`, `work_ledger`, `auip_verified`, `simulated_life_runtime`).

A generated assistant statement is not automatically a user fact. A derived-state subsystem may not erase or obscure its source evidence.

## Persona invariant

Continuity must not rewrite the Persona baseline. It can provide bounded context that modulates a turn, but it may not mutate Character RAG/Canon or make permission decisions.

## Durable memory record

C2-C4 memory records retain deterministic keys, provenance, logical state, priority/importance fields, timestamps and optional Work linkage. C4 adds retention metadata only.

`relationship_value` on a memory row is retention metadata for that memory item. It is **not** the C5 relationship snapshot and must not be used as such.

## Deterministic write semantics

Memory application is Host-controlled and deterministic:

- duplicate/reinforce/supersede decisions are resolved against SQLite authority;
- one active key remains authoritative under the existing unique-index contract;
- recall is not confirmation;
- recall/mention reinforcement is bounded/log-scaled where used by retention;
- current-turn generated content cannot silently become durable truth.

Passive capture (no explicit user command) is bounded and reviewable:

- stable facts are limited to the registered fact slots the structured recall
  path can query, plus preferences;
- every other **substantive** user utterance (>= 12 content characters, or >= 4
  when it carries a time/plan anchor) is kept verbatim (bounded to 400
  characters) as a conversation record: `EPISODIC` for statements and shared
 回忆, `OPEN_LOOP` for plans.  The thresholds are constants in
  `core/continuity/memory_extractor.py`, so capture breadth is auditable and
  testable rather than model-dependent;
- greetings, acknowledgements, and memory-control directives are never stored;
- assistant prose never becomes a user fact.

## Explicit remember / forget

Explicit forget is a hard semantic boundary:

1. matching durable source rows are deleted/invalidated according to the existing memory contract;
2. rebuildable derived retrieval state is removed;
3. a value-free tombstone/suppression record is installed;
4. old Session material may not re-extract the forgotten value.

Pinned/P0/retention/Work projection never overrides an active forget tombstone.

Beginning with C5, this rule extends through **forget closure**: a derived relationship/life state contribution that is supported only by forgotten source evidence must be invalidated and the derived snapshot recomputed without that contribution.

## Topic mute (user-requested quiet)

A user command such as "不要再提…" / "don't mention …" installs a **topic mute**,
which is deliberately not a forget:

- durable memory rows, mentions, anchors, and the Session transcript are
  untouched; retention tier progression continues;
- the Host filters the muted topic out of *proactive* surfacing: fast recall,
  the C8 cold/archive memory path, and C8 Session quoting, plus a bounded
  "do not raise these" note inside `[Continuity grounding]`;
- the mute is **lifted for the turn when the user's own words raise the topic**,
  so "还记得…那家甜点店吗" still recalls normally.  Only concrete topics qualify;
  deictic targets ("这个"/"it") are declined rather than guessed;
- matching is deterministic n-gram/containment similarity (no model call) and
  fails toward quiet when wording is indirect;
- mutes are Host-owned user controls exposed as `continuity.mute.list` /
  `continuity.mute.clear`; storage is bounded (<= 100 active topics) inside the
  existing `continuity_meta` key/value state, so no schema change is involved;
- a mute never replaces explicit forget: information that must be *erased* still
  requires forget (hard delete plus tombstone).


## Schema 5 Relationship authority

C5 adds:

```text
relationship_events
relationship_state
short_term_affect
```

`relationship_events` is a provenance-bearing derived-event ledger. `relationship_state` and `short_term_affect` are rebuildable snapshots, not independent facts. Long-term dimensions are `familiarity`, `trust`, `warmth`, `respect`, `closeness`; short-term affect dimensions are `irritation`, `embarrassment`, `tension`, `playfulness`.

The relationship commit path is Host-owned and enforces minimum confidence, allowed dimensions, deterministic source fingerprints, per-event caps, rolling-window caps, replay idempotency, deterministic event-time rebuild and policy versioning. Out-of-order arrival cannot change the final result for the same valid event set and policy.

Default relationship extraction uses only direct user-authored cues. Assistant text is excluded from relationship evidence hashing and cannot self-reinforce relationship state. Ordinary neutral chat is not stored as relationship state.

### Forget closure

`forget_memory()` invalidates relationship events linked by source memory ID and/or opaque source memory key before deleting the source memory, then rebuilds relationship/affect snapshots inside the same transaction. A memory-control utterance itself is excluded from semantic relationship extraction so `forget ...` cannot recreate the signal being removed. Public source-invalidation APIs also support future transcript/session deletion flows.

### Live projection

C5-B renders only qualitative bounded text inside `[Continuity grounding]`. Raw scalar values, event IDs, hashes and provenance are not normal prompt content. Relationship may modulate behavioral amplitude only and never changes Persona/Canon, facts, safety, Provider/AUIP permissions or Work state.

## Schema 6 Character Life authority

C6 adds:

```text
daily_schedules
schedule_items
life_threads
life_events
```

All C6 state is explicitly `SIMULATED_LIFE`. The scheduler is deterministic per character/local date and persists one shared plan across sessions/restarts. `life_threads` and `life_events` are derived simulation continuity, not Character RAG/Canon or external facts.

Long offline periods are handled with bounded coarse summaries rather than reconstructed detailed schedules. Activity outcomes are saved once with deterministic fingerprints, and chat interruption only pauses/resumes simulated activity; it never mutates real Work Ledger state.

Default C6 scheduling does not consume user-memory content. If a future life event is explicitly linked to a memory source, explicit forget invalidates that event before the source memory is deleted, extending forget closure through the C6 domain.

C6-B Live may expose only bounded qualitative `SIMULATED_LIFE` context. It may not expose private provenance or internal IDs/hashes/progress scalars and may not change Persona, Canon, user facts, permissions, Provider/AUIP authority, or Work state.

## Session-history and crash-recovery boundary

Session JSON remains the authoritative conversation-history artifact: it retains the complete accepted turn history across restarts, and Session reload/restart must never drop accepted turns. The bounded `max_rounds` rolling window is a model-prompt projection only (`ConversationHistory._prompt_window`), not a persistence or display policy. `consolidation_turns` is a processing journal and does not become a second transcript. Recovery may re-read only already-observed Session evidence and must still honor tombstones.

## C3 retrieval contract

### Structured path

Deterministic fact slots and kind-directed candidates cannot be lost behind a generic top-N window.

### FTS path

SQLite FTS5/trigram is a candidate generator. Host scoring performs the final bounded relevance decision.

### Semantic path

Semantic recall is optional and lazy. It uses local E5/FAISS-compatible components when enabled and must degrade to Structured + FTS when unavailable. SQLite remains authority.

### Scoring and exclusion

Only candidates with actual structured/lexical/semantic relevance may surface. Importance, priority, pinning and recency are support signals, not independent authority. Current-turn, expired, superseded and forgotten rows are excluded.

## C4 retention / maintenance contract

Retention maintenance is deterministic and auditable for a given clock/configuration.

- pinned/P0 active memories remain hot;
- non-protected hot records are subject to a deterministic hot working-set cap;
- normal operating envelope is at most 5,000 hot memories;
- cold records are durable but excluded from the default hot-only fast path unless a caller explicitly widens scope;
- archive remains durable authority but is excluded from normal FTS/fast recall;
- maintenance never resurrects superseded/forgotten content;
- scheduled maintenance stays outside first-response synchronous work.

The tier ages and the promote/cold/archive **score thresholds** are part of the
reviewable retrieval policy (`config/continuity_policy.json`); the store only
executes them.  The configured product ladder is: half-life 30 days, hot until
`cold_after_days = 180`, archive after `archive_after_days = 730`, with score
thresholds 0.62 / 0.55 / 0.55.  The archive threshold must stay reachable for
ordinary rows (their decayed score floor is ~0.40), otherwise archive recall is
dead code.

`continuity_maintenance_runs` records scan/promote/demote/archive counts and timing metadata without storing user memory bodies.

## Work Ledger projection

Continuity consumes normalized `work.updated` lifecycle events but never owns Work completion.

- duplicate event IDs are idempotent;
- stale/out-of-order events are rejected;
- legitimate reopen is a new lifecycle change, not a stale running event;
- terminal Work downgrades linked open loops to cold except protected P0/pinned cases;
- active/reopened Work may keep linked open loops hot;
- Work projection cannot bypass forget.

## Grounding contract

The renderer exposes a bounded data projection, not raw SQLite metadata. Memory text is quoted/escaped as data and cannot inject grounding envelope structure. IDs, scores, vectors and internal index metadata are not prompt content.

Character RAG and Continuity are prepared as separate domains and join only in the current-turn projection.

## RealityClock grounding

Host time is authoritative. Cross-restart intervals use persisted wall-clock state; rollback/high-water protection prevents negative or falsely precise elapsed-time claims.

## Feature flags

The existing feature-disable semantics remain authoritative:

```text
CONTINUITY_ENABLED=true
CONTINUITY_MEMORY_ENABLED=true
CONTINUITY_RELATIONSHIP_ENABLED=true
CONTINUITY_RELATIONSHIP_LIVE_ENABLED=false
CONTINUITY_LIFE_ENABLED=true
CONTINUITY_LIFE_LIVE_ENABLED=false
CONTINUITY_SEMANTIC_RECALL_ENABLED=false
CONTINUITY_ARCHIVE_RECALL_ENABLED=true
AMADEUS_CONTINUITY_DB_PATH=...
```

Global disable prevents Continuity mutations and yields no Continuity grounding. Memory, Relationship, and Character Life have independent feature switches; Relationship Live and Life Live each have separate projection switches. Semantic disable preserves Structured + FTS.

## Failure policy

Continuity fails toward narrower truthful context, not invented state:

```text
semantic unavailable -> structured + FTS
FTS failure           -> structured recall
retrieval failure     -> RealityClock-only / no memory projection
Continuity failure    -> Main Chat can continue without Continuity
```

Derived-index corruption never redefines durable truth.

## C4.1 performance / acceptance boundary

The finalized C4.1 acceptance tree completed the recorded full regression with `1900 passed / 12 skipped / 4 deprecation warnings / 0 failed`. The candidate package also passed clean-extraction/hash, Continuity tests and lock validation in the formal acceptance environment.

The historical pre-C4 production rollback artifact remains an evidence gap. This is preserved as an audit fact and does not authorize fabricating a strict G4 rollback PASS.

## C7 implemented control boundary

C7 uses bounded Host APIs for Memory listing, pin/unpin, explicit forget, topic mute list/clear, index rebuild, maintenance, Relationship diagnostics and Character Life schedule inspection. Electron/UI code does not obtain direct SQLite ownership. Displayed Character Life remains explicitly `SIMULATED_LIFE` and cannot be promoted to Canon or external truth.

## C8 Archive Recall / Advanced Retrieval

C8 is implemented as a historical low-confidence fallback after the existing fast path. Cold and archive-tier memory rows remain outside ordinary FTS recall and are reachable only through this explicit fallback. Session JSON is read only for explicit historical questions with low fast-recall confidence, and only for turns anchored by currently-active SQLite memory provenance.

Selected historical turns quote both sides of the exchange - the user statement plus a bounded, explicitly non-authoritative past-assistant excerpt - so details that only the assistant voiced (a title, a name) remain recoverable. This supersedes the earlier C8 wording that included past assistant text only when the query asked about it or assistant-side matching dominated.

Muted topics are excluded from cold/archive memory recall and Session quoting unless the current user turn raises the topic itself.

Schema 7 adds content-free `memory_forget_sources` plus `memory_tombstones.archive_guard_complete`. A forgotten source turn remains blocked from Session fallback even if the key tombstone is later cleared for a new fact. Any legacy pre-C8 tombstone whose original source turn cannot be proven causes Session fallback to fail closed.

C8 time-aware filtering and high-fidelity excerpts do not create new facts. Retrieval traces are non-durable and content-free. Ordinary chat retains the existing bounded fast path.
