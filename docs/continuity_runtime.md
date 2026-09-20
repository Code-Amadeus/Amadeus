# Continuity Runtime — C1 through C8 Implementation

**Runtime schema:** 7  
**C8 parent:** `Amadeus_C7_UI_Diagnostics_User_Control_Source_Baseline.zip` (SHA-256 `9cf8e117a81ba1c42f4755306b84e9d9b44b450f31d87edb6e9d94bf2aeca163`)  
**Historical C4.1 Candidate SHA-256:** `50e7811fc8f4376c52de77dce8a3c5ff370029379c434d3b9933897a78cee6f3`

## Runtime modules

The implemented Continuity runtime is split across:

```text
core/continuity/clock.py
core/continuity/models.py
core/continuity/migrations.py
core/continuity/store.py
core/continuity/memory_extractor.py
core/continuity/memory_resolver.py
core/continuity/turn_ingest.py
core/continuity/retrieval_policy.py
core/continuity/memory_retriever.py
core/continuity/archive_recall.py
core/continuity/topic_mute.py
core/continuity/embedding_runtime.py
core/continuity/relationship_policy.py
core/continuity/relationship.py
core/continuity/life_policy.py
core/continuity/life_scheduler.py
core/continuity/life_runtime.py
core/continuity/context_renderer.py
core/continuity/service.py
```

`server.app` owns the service composition and injects read-only grounding into `ChatRuntime`.

## Schema version

`SCHEMA_VERSION = 7`.

C1 created clock/meta state. C2 added durable memory, links, mentions, tombstones and consolidation journal. C3 added rebuildable FTS/embedding state. C4 added retention and Work-projection metadata. C5 added provenance-bearing relationship events plus rebuildable long-term relationship and short-term affect snapshots. C6 adds deterministic Character Life schedules, items, threads, and provenance-bearing simulated-life events. C7 adds Host-owned UI/control surfaces without a schema bump. C8 adds content-free archive-forget provenance guards and bounded historical fallback.

C5 additions:

```text
relationship_events
relationship_state
short_term_affect
```

`memory_items` remains the durable memory truth. Relationship and Character Life are separate derived-state subsystems; `memory_items.relationship_value` remains retention metadata only. Character Life is explicitly classified `SIMULATED_LIFE`.

## ContinuityStore

`ContinuityStore` owns transactional SQLite access and migrations. Key C4.1 behaviors include:

- active/superseded logical truth;
- hard explicit forget + tombstone;
- hot/cold/archive retention tier;
- FTS and embeddings as rebuildable active accelerators;
- deterministic retention maintenance;
- bounded hot working set (`max_hot_memories`, normal contract 5,000);
- idempotent/stale-safe Work update projection;
- content-free Continuity diagnostics.

## Retrieval policy

Default recall remains Structured + FTS with optional semantic fusion. Normal fast recall uses the bounded hot working set. Callers may explicitly include cold rows for specific flows; archive is excluded from ordinary fast recall.

Retention support cannot make an unrelated memory relevant by itself.

## MemoryRetriever

### Structured recall

Known fact slots and kind-directed queries are resolved against SQLite authority and cannot be displaced solely by recency windows.

### Lexical recall

FTS5 trigram produces candidates from active, eligible retention tiers. Queries are escaped/quoted; raw user FTS operators are not treated as authority.

### Optional semantic recall

When enabled, `MemorySemanticIndex` lazily uses the local embedding/FAISS path. Model/cache failure degrades safely to Structured + FTS. No turn is allowed to download model weights implicitly.

## Host scoring and diversity

The Host combines structured/lexical/semantic relevance with bounded importance/priority/recency support. MMR-style dedupe and context budgets prevent memory dumps. `recall_count` is observability/retention support, not retrieval authority.

## C4 retention maintenance

`ContinuityStore.run_retention_maintenance()` computes bounded retention scores from existing metadata and age, then promotes/demotes/archives eligible active rows.

Important rules:

- pinned/P0 stay hot;
- tombstones and superseded facts are never resurrected;
- archive is durable historical storage, not a logical state;
- hot overflow is deterministically demoted rather than deleted;
- one audit row is written to `continuity_maintenance_runs`;
- scheduled service maintenance runs outside the first-response synchronous path.

## Work lifecycle linkage

`ContinuityService` consumes normalized Work snapshots/events and forwards per-item changes to `ContinuityStore.apply_work_update()`.

The store records `continuity_work_events` and applies lifecycle projection only to linked `open_loop` memories. Duplicate IDs are no-ops; older observations cannot overwrite newer state; terminal status lowers future value/tier while active/reopen status may return the loop to hot.

Work Ledger remains the completion authority.


## C5 Relationship Runtime

`relationship.py` provides a conservative user-authored proposal extractor, runtime facade and qualitative renderer. `relationship_policy.py` loads versioned Host-owned confidence/cap/decay/projection policy from `config/continuity_policy.json`.

The Host commit path enforces:

- deterministic source fingerprints and replay idempotency;
- per-event caps and same-sign rolling-window caps;
- deterministic event-time rebuild for out-of-order arrival;
- separate slow relationship and short-lived affect dimensions;
- source provenance without storing user text in the relationship ledger;
- explicit-forget closure via source memory ID/opaque source key invalidation;
- content-free relationship diagnostics.

Default extraction is deliberately sparse: ordinary neutral turns are not durable relationship events. Assistant-generated friendliness is never relationship evidence.

Affect is stored as a small snapshot at an `as_of` time and decays analytically on reads, so Main Chat never scans event history.

## C6 Character Life Runtime

`life_policy.py`, `life_scheduler.py`, and `life_runtime.py` implement the C6 simulation domain. `character_continuity/kurisu.json` contains character-specific activity pools and continuity-thread templates while the scheduler remains generic and deterministic.

Schema 6 adds:

```text
daily_schedules
schedule_items
life_threads
life_events
```

One schedule is persisted per character/local date. Startup/resume and an independent midnight rollover task call `ensure_today()`. Late-start and offline catch-up are bounded: missing days are not backfilled with detailed fabricated schedules.

Completed activity outcomes use deterministic fingerprints and are saved once. Ongoing life threads carry bounded progress across days. Chat interruption pauses/resumes only simulated schedule items and never owns Work state.

Default scheduling does not read user-memory content. Source-linked life events support invalidation during explicit forget, preserving forget closure for future C6 extensions.

C6-A Shadow is enabled by default while C6-B Main Chat projection remains opt-in. Live output is qualitative, explicitly labeled `SIMULATED_LIFE`, and denied Canon/fact/permission/Work authority.

## Context rendering

`context_renderer.py` emits one bounded Host projection. User memory summaries are JSON-quoted data. IDs, scores, FTS rank, vectors and SQLite implementation details are not exposed to the model.

## Main Chat integration

C5 preserves the existing dependency-injection shape and adds Relationship only inside the same bounded Continuity envelope when Live is enabled:

```text
Character RAG reference --------\
                                  +--> turn-local grounding --> existing Main Chat provider
Continuity Memory/Reality --------+
Relationship qualitative context /
Character Life qualitative context /
```

The two domains remain separate until the current turn. `ChatRuntime` does not own the Continuity DB.

## RealityClock

RealityClock uses persisted wall-clock/high-water state for cross-restart intervals and avoids negative/falsely precise elapsed time when the system clock rolls back.

## Diagnostics

`ContinuityStore.continuity_diagnostics()` exposes content-free hot/cold/archive counts plus latest maintenance timing for development and later C7 UI. Diagnostics do not grant UI direct SQLite ownership.

## Feature flags

```text
CONTINUITY_ENABLED
CONTINUITY_MEMORY_ENABLED
CONTINUITY_RELATIONSHIP_ENABLED
CONTINUITY_RELATIONSHIP_LIVE_ENABLED
CONTINUITY_LIFE_ENABLED
CONTINUITY_LIFE_LIVE_ENABLED
CONTINUITY_SEMANTIC_RECALL_ENABLED
CONTINUITY_ARCHIVE_RECALL_ENABLED
AMADEUS_CONTINUITY_DB_PATH
```

Relationship and Character Life processing default enabled while both Live projections default disabled, allowing Shadow observation before Main Chat modulation. The default semantic-off path remains complete and supported.

## State backup and Session reaping

Server startup refreshes one fixed backup set under `runtime/backup/` (`continuity.sqlite3`, `work_ledger.sqlite3`, `sessions/` mirror) from the previous run's durable state, then reaps Session scopes whose dialogue has neither a live transcript nor a backup transcript. All paths are resolved relative to the project root, so a renamed project folder changes nothing. The slot is never timestamped and never stacks; it exists to *recover* deleted or damaged state, so it never deletes: a Session transcript removed from the live directory stays recoverable from the mirror (same-name files are overwritten, missing files are kept), and a database is only refreshed while the source passes `PRAGMA quick_check` — a missing or damaged source keeps the previous good backup untouched. Reaping runs after the backup refresh so the pre-reap snapshot always exists; it is transactional per scope, removes that Session's memories, tombstones/forget-sources, topic mutes, relationship/affect state and consolidation journal, and exempts `__unsessioned__` plus the inert legacy `global` placeholder. Failures are logged and skipped without blocking startup. An explicitly overridden state layout (`AMADEUS_SESSION_DIR`, `AMADEUS_CONTINUITY_DB_PATH`, `AMADEUS_WORK_LEDGER_PATH`; CI / e2e / tooling isolation) opts out of both steps instead of clobbering the real slot.

## Failure behavior

The runtime fails open to a narrower truthful context. Rebuildable index failures do not redefine memory truth; failed semantic retrieval cannot fail Main Chat; retention/maintenance failure must not corrupt the current turn.

## C4.1 acceptance state

Recorded final acceptance:

```text
full pytest: 1900 passed / 12 skipped / 4 warnings / 0 failed
source hygiene: forbidden artifacts 0
uv lock --check: PASS in formal acceptance environment
release policy: 0 issues
candidate clean extraction/hash: PASS
Continuity candidate tests: PASS
24h soak: PASS (reported in final C4 execution report)
```

The strict historical pre-C4 rollback evidence item remains unavailable; it is an audit gap, not a reason to rewrite current source quality evidence.

## C7 UI / Diagnostics / User Control

C7 is implemented without a schema bump. Electron receives bounded DTOs through `ContinuityHandler`; it never opens Continuity SQLite. The surface provides active-memory inspection, pin/unpin, explicit forget, derived-index rebuild, bounded maintenance, Relationship diagnostics, Life diagnostics and a `SIMULATED_LIFE` schedule inspector. Explicit forget continues to use the existing tombstone/provenance closure transaction.

## C8 Archive Recall / Advanced Retrieval

C8 is implemented in `archive_recall.py` and the existing retrieval/service composition. The normal Structured/FTS/optional-semantic path remains first. Only an explicit historical cue plus low fast score enables cold/archive-tier summary recall and bounded Session archive search.

Session turns are eligible only when anchored by active SQLite memory source provenance. Schema 7 records forgotten source turns without storing forgotten text; those turns remain permanently blocked from Session fallback. Legacy tombstones lacking source closure disable Session-text fallback rather than risk resurrection.

Time-aware filtering supports bounded absolute/relative dates, and high-fidelity excerpts are rendered as quoted transcript data under the existing Continuity context budget; selected turns quote both the user statement (up to `archive_recall.max_excerpt_chars`) and the past assistant wording (up to `archive_recall.max_assistant_excerpt_chars`, explicitly non-authoritative), and a side over its budget is semantically compressed as a whole through the configured LLM — bounded per query, with deterministic whole-span sampling as the offline fallback — rather than cut at the edge; a condensed side is labeled `(condensed)` so it is never presented as verbatim wording. Topics the user muted ("不要再提…") are skipped unless the current turn raises them itself. The Host exposes content-free recent retrieval traces through `continuity.retrieval.trace`.
