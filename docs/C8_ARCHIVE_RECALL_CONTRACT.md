# C8 Archive Recall / Advanced Retrieval Contract

**Status:** Implemented / final Continuity upgrade stage  
**Parent:** `Amadeus_C7_UI_Diagnostics_User_Control_Source_Baseline.zip`  
**Parent SHA-256:** `9cf8e117a81ba1c42f4755306b84e9d9b44b450f31d87edb6e9d94bf2aeca163`  
**Schema:** 7

## 1. Purpose

C8 adds a bounded historical fallback for questions that explicitly ask about the past when the existing C3 fast memory recall is not confident enough. It does not make Session JSON a second memory database and it does not put Session scanning on the ordinary chat path.

The runtime order is:

```text
normal Structured / FTS / optional semantic recall
        ↓
explicit historical cue?
        ↓ yes
fast score below configured threshold?
        ↓ yes
active archive-tier memory summary recall
        +
bounded Session archive search anchored by active SQLite memory provenance
        ↓
high-fidelity quoted fallback evidence
```

## 2. Authority boundary

SQLite `memory_items` remains the durable Continuity memory authority.

Session JSON is authoritative only as the original conversation transcript. C8 may read it to recover high-fidelity wording for an already-authorized source turn, but a Session turn is not eligible merely because text exists in a JSON file.

A Session turn can enter archive fallback only when all of the following hold:

1. at least one currently active `memory_items` row points to the same `source_session_id` + `source_turn_id`;
2. the turn is not blocked by C8 explicit-forget provenance;
3. the user query contains an explicit historical cue;
4. fast recall is below the archive fallback confidence gate;
5. the bounded archive scorer selects the turn.

Archive fallback is Session-scoped: anchors are read from the current
dialogue's scope and quoting only ever opens that dialogue's own transcript
(memory scopes equal their owning session id; a scope whose transcript is
gone is reaped at startup), so one dialogue can never quote another.

Character RAG, Relationship, Character Life, Work Ledger, Provider Runtime and AUIP ownership do not change.

## 3. Explicit-forget closure

C8 migration 7 adds only content-free forget provenance:

```text
memory_tombstones.archive_guard_complete
memory_forget_sources(
    tombstone_id,
    source_session_id,
    source_turn_id,
    source_hash,
    forgotten_at
)
```

Before a memory row is deleted, C8 records the original source-turn identity. No forgotten summary, object text, or transcript body is copied into the guard table.

Once a source Session turn has been associated with an explicit forget, that historical turn remains blocked from Session fallback even if the key tombstone is later cleared to permit a new fact with the same memory key.

Databases upgraded from a pre-C8 schema may contain historical tombstones for which the original source turn is unknowable. Such a tombstone has `archive_guard_complete = 0`. C8 fails closed and disables Session-text fallback while any unmapped legacy tombstone exists. Archive-tier SQLite memory recall remains safe because forgotten `memory_items` rows have already been deleted.

## 4. Low-confidence gate

Archive fallback is not a normal-turn retrieval layer.

The default policy requires:

```text
explicit historical cue = true
AND
best fast-recall score < 0.38
```

Examples of historical cues include `还记得`, `上次`, `以前`, `去年`, `昨天`, `remember when`, `last time`, and equivalent forms.

A strong structured fact hit such as an exact birthday/name slot remains on the fast path and does not scan Session files even when the wording contains “还记得”.

## 5. Bounded archive search

Default bounds in `config/continuity_policy.json`:

```text
max_sessions                = 32
max_source_turns            = 120
max_hits                    = 3
max_excerpt_chars           = 1400
max_assistant_excerpt_chars = 1000
minimum_score               = 0.12
```

A selected side that exceeds its excerpt budget is semantically compressed as a whole
through the configured LLM (at most four compressions per query; the deterministic
whole-span sampler is the offline fallback) instead of being cut at the budget edge.

Source turns are ranked from active SQLite anchor summaries before filesystem access. Only selected Sessions are opened. Legacy Session messages without a `turn_id` cannot be safely joined to memory provenance and are therefore not used as C8 high-fidelity fallback evidence.

## 6. Time-aware query expansion

C8 parses bounded temporal hints from the query and applies them to source-turn ranking and Session-message timestamps. Supported classes include:

- absolute ISO/common dates;
- explicit years;
- `去年` / `前年` / `last year`;
- `昨天` / `前天`;
- `上周` / `上个月`;
- bounded `N天/周/月/年前` forms.

Temporal metadata narrows retrieval; it does not create facts.

## 7. High-fidelity projection

Historical excerpts are rendered inside the existing `[Continuity grounding]` block as quoted fallback data.

Both sides of a selected turn are quoted: the user transcript text is labeled as a
historical user statement, and the past assistant wording (semantically compressed
when over budget) is labeled non-authoritative quoted history, so details that only
the assistant voiced — a title, a name — stay recoverable.

A side that no longer matches the transcript word-for-word because it was condensed
for the budget is labeled `(condensed)` in the grounding block, so a condensation is
never presented as verbatim wording.

Session text is JSON-quoted and remains subject to the existing total Continuity context budget. It cannot inject new instructions or permissions.

## 8. Retrieval trace diagnostics

C8 keeps a non-durable in-memory ring of at most 32 content-free traces. The Host API `continuity.retrieval.trace` exposes bounded diagnostics such as:

- query fingerprint, never query text;
- fast hit count/top score and timing;
- gate reason;
- whether archive fallback ran;
- archive memory/session hit counts;
- Sessions opened and turns scored;
- temporal-filter class;
- forget-guard readiness;
- archive and total latency.

No transcript, memory summary, source hash, API key, Session id, or raw query is returned in these traces.

## 9. Failure policy

```text
archive disabled            -> existing fast recall only
Session directory missing   -> archive-tier SQLite memory only
legacy forget guard unknown -> no Session-text fallback
Session JSON unreadable      -> skip that Session
archive exception            -> retain fast recall only
```

C8 failure never fails Main Chat and never changes durable truth.
