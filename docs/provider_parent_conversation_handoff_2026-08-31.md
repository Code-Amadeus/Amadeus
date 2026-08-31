# Provider parent-conversation handoff: hybrid checkpoint and delta

Date: 2026-08-31
Status: validated candidate contract; not current product semantics until merged
Scope: Main Chat -> Host -> execution Provider handoff for new runs, attached continuations, recovery, and active steer
Tracking: https://github.com/Code-Amadeus/Amadeus/issues/17

## 1. Defect classification

This is a provider-neutral delegation-boundary defect, not a Codex-specific
failure.

The Host owns the parent conversation, while a Provider owns only its native
execution thread/session. Before this change, the generic handoff retained the
current user wording but reduced prior context to at most one earlier user
message. The recovery/resend path could omit even that message. A goal spread
across several user/assistant turns therefore arrived at the Provider as a
short complaint or confirmation with no recoverable object.

Codex Desktop exposed the defect clearly because its persisted handoff turn is
visible to the user. The same missing fact can affect any model-driven Provider.

## 2. Authority boundary

The handoff must preserve these non-equivalences:

```text
authorized Provider task
  != current user wording
  != prior user reference context
  != prior Main Chat commitment
  != attachable Provider Session
  != acknowledged context delivery
  != Provider completion fact
```

- The accepted `ProviderRunRequest.task` remains the execution task.
- Exact current user wording is intent evidence for actors, interaction mode,
  destination, exclusions, and references.
- Prior user messages may resolve a goal, object, constraint, or reference.
- Prior Main Chat messages are conversation evidence about what the role said
  or committed to do. They are not Provider instructions, receipts, or
  completion facts.
- Prior context cannot independently authorize another action.
- A typed Provider Session proves only that a native thread may be attached.
  It does not prove that the current Attempt delivered its prompt.
- A context cursor advances only after the adapter observes the native request
  acceptance boundary and emits the canonical `context.delivered` event.
- WorkItem, Attempt, workspace, permission, completion, and acceptance
  authority remain unchanged.

### Provider-visible data and behavior boundary

This feature is a bounded product-semantic change, not only an internal cursor
repair. A model-driven Provider now receives the exact current user wording
plus up to six recent `User` / `Main Chat` messages from the owning dialogue
source. If the selected Provider is external, that bounded text leaves the
Host along with the authorized Work request. This may change Provider model
behavior, prompt tokens, latency, cost, and data disclosure compared with the
previous latest-only handoff.

The envelope labels prior Main Chat text as evidence and Host authority gates
remain unchanged, but prompt wording cannot prove how a real model will
interpret that evidence. Deterministic tests cover transport and authority
enforcement only; they are not live-model behavioral evidence.

## 3. Rejected extremes

### Always send only the latest sentence

Rejected because anaphoric complaints, confirmations, corrections, and delayed
commitments may not contain the task object.

### Always replay a bounded snapshot

Conservative against an unsafe cursor, but only best-effort for recoverability:
the six-message/2000-character window may already have omitted an older task
object. A warm native session also repeatedly receives facts it already owns,
wasting context and potentially over-weighting an old constraint.

### Always send only a delta

Rejected because a missing cursor, Provider switch, legacy Attempt, rolled
conversation window, or ambiguous repeated wording could permanently remove
necessary context.

### Generate a model summary

Rejected because it adds another model pass and lets a lossy translation decide
which goals, constraints, and commitments survive.

### Introduce durable message sequencing immediately

This is the theoretical end state, but current user history does not yet have a
complete monotonic message identity. Adding it now would expand this repair into
conversation persistence, migration, branch, and recovery semantics.

## 4. Current candidate: bounded snapshot fallback, delta for warm transport

The Host first builds one bounded parent-conversation checkpoint:

- most recent six `user` / `assistant` messages;
- at most 2000 characters total;
- at most 280 characters per message, retaining head and tail when truncated;
- explicit `User:` and `Main Chat:` role labels;
- current user wording excluded because it travels separately;
- executable/internal control markup removed before handoff.

The checkpoint comes from the dialogue source that actually owns the turn:

- ordinary or independent Work uses `chat:<session_id>`;
- an A1-scoped application turn uses `auip:<app_session_id>` and only that
  AppSession role branch;
- an empty A1 branch never falls back to unrelated parent Chat.

`source_context_scope` isolates handoff provenance and delta calculation; it
is not a native Provider-history confidentiality boundary. Provider Sessions
remain WorkItem-scoped. Continuing the same WorkItem from another Chat or an
A1 branch may attach the same native thread, whose earlier source history is
still present. The Host sends a new bounded `snapshot_fallback` from the current
source instead of calculating a cross-source delta. Strict native-history
isolation would require a new Provider Session and is not part of this repair.

Delivery mode is then selected by Host-owned continuity facts:

| Condition | Delivery |
| --- | --- |
| New Provider session/thread | `snapshot` |
| Provider switch or stateless Provider | `snapshot` |
| Same verified native session, same dialogue source, acknowledged delivery, and one unique prior-source anchor | `delta` |
| No delivery receipt, changed dialogue source, anchor rolled out, or anchor absent | `snapshot_fallback` |
| Repeated identical anchor makes the position ambiguous | `snapshot_fallback` |

A delta contains only parent-conversation messages after the last user wording
that was already delivered to the same native Provider session. The new current
user wording still travels separately.

### Multi-turn example

```text
cold turn 1
  snapshot: A initial goal + B role response
  current:  C first request

warm turn 2
  delta:    D new role response after C
  current:  E second request

warm turn 3
  delta:    F new role response after E
  current:  G third request
```

The Provider's native thread/session keeps A/B/C and later execution history.
The Host does not copy native Provider history back into Main Chat or the
Ledger.

## 5. Continuation and steer

### Terminal Attempt followed by an amendment

The Work Ledger may use delta only after it verifies that the new Attempt
attaches the same typed Provider Session and finds the latest acknowledged
delivery for that exact Session. A later Attempt that inherited the handle but
failed before native acceptance is skipped; it cannot move the cursor. A cold
or unverifiable continuation receives a bounded snapshot.

### Active Attempt steer

The active run record supplies the last acknowledged handoff cursor. The Host
computes the delta before creating `ProviderSteerRequest`. Adapter acceptance
that means only "queued" does not advance the cursor. Provider Runtime advances
it only after the native boundary reports `context.delivered`; rejected,
coalesced-before-send, or interrupted steers leave it unchanged.

### Recovery

Recovery transport safety never depends on delta. If a recovery lacks a
verifiable cursor or complete current checkpoint, it reuses a bounded
snapshot/fallback and the existing recovery task contract. That window is
best-effort context, not a guarantee that an older task object remains present.

## 6. Provider translation

The semantic envelope is provider-neutral:

```text
ChatRuntime checkpoint
  -> DelegateDispatch / ProviderSteerRequest metadata
  -> with_parent_conversation_context
       -> Codex App Server
       -> Direct Codex
       -> OpenClaw
       -> Browser planner
```

Codex-specific code owns only Desktop presentation: thread title, visible
current-request/prior-context layout, and takeover guidance.

Structured MCP operations do not receive this prose envelope because their
typed arguments are already the complete operation contract and no execution
model resolves conversational references there.

## 7. Why delta is an optimization, not a truth source

The canonical safety rule is:

```text
bounded snapshot provides best-effort recoverability
verified delta removes repetition
```

If delta calculation fails, the system may send extra bounded context but must
not send less context based on a guessed cursor. `source_context_mode` records
`snapshot`, `delta`, `snapshot_fallback`, or `none` so fallback frequency
and transport behavior remain observable.

## 8. Theoretical optimal design

The long-term exact form is:

```text
conversation_id
+ dialogue_source_scope
+ monotonic message_seq
+ last_delivered_seq per typed Provider Session
= exact parent-conversation delta
```

Every parent message would have a durable sequence independent of its text.
The Host would persist the last sequence delivered to each verified Provider
Session and send `messages where seq > last_delivered_seq`. Snapshot would
remain mandatory for cold start, Provider switch, cursor loss, or incompatible
history version.

This replaces text anchoring only; it does not change the authority contract or
Provider adapter interface.

### Upgrade triggers

Adopt stable message sequencing when at least one of these is evidenced:

- repeated or clipped user wording causes material `snapshot_fallback` volume;
- bounded snapshot replay creates measured token/latency cost;
- a conversation-history migration already introduces durable message identity;
- cross-device/session synchronization requires exact acknowledgement cursors.

Until then, the text anchor plus fail-closed bounded fallback is the smaller
coherent repair. A clipped user message cannot match its full receipt anchor
and therefore intentionally uses `snapshot_fallback`; measured fallback volume
and token/latency savings remain future evidence work.

## 9. Experiment plan

| Experiment | Required result |
| --- | --- |
| E0 old failure baseline | Latest-only/recovery handoff demonstrably loses the multi-turn object |
| E1 cold/warm sequence | Turn 1 snapshot; later verified turns contain only new parent messages |
| E2 ambiguous/rolled cursor | Fail closed to bounded `snapshot_fallback` |
| E3 recovery/resend | Recovered delegate keeps bounded user and Main Chat evidence |
| E4 Work Ledger attach | Same typed Provider Session plus acknowledged receipt gets delta; cold/unverified continuation gets snapshot |
| E5 active steer | Only post-cursor parent messages reach steer; queue acceptance alone does not advance the cursor |
| E6 Provider matrix | Codex App Server, Direct Codex, OpenClaw, and Browser consume the same envelope |
| E7 authority guard | Control tags removed and Host authority gates unchanged; envelope labels prior Main Chat as evidence |
| E8 deterministic suite | Relevant suites and the full public runner pass; any later internal sync must rerun its own gates |
| E9 failed-before-delivery | A prepared/failed Attempt cannot cut context from its successor |
| E10 source transport isolation | Cross-Chat and A1 branch changes force a bounded snapshot from the correct source while preserving WorkItem-scoped native continuity |
| E11 public projection | Receipt event and cursor metadata are absent from `provider.list`, `provider.result`, terminal events, and Electron Work projections |
| E12 non-activity persistence | Persisting a receipt leaves Attempt and WorkItem visible timestamps unchanged |
| E13 clipped anchor | A clipped prior user message predictably falls back to the bounded snapshot |

## 10. Experiment results

The deterministic experiment passed in an isolated public repair candidate
built from current `main` plus PR #18. The original PR worktree remained
unchanged.

- E0 reproduced the defect before the repair: the old handoff retained only
  `这是学习使用，不是商业创作，你去找找看，然后更新。` and lost both the
  desktop Pokemon game object and Main Chat's commitment to update its files.
  The old recovery path could send no parent context.
- `python tools/probes/probe_provider_parent_context_handoff.py` passed its
  A/B/C comparison. The pre-PR baseline lost the multi-turn object. PR #18
  improved the normal warm path, but a failed-before-delivery Attempt sent only
  the failure sentence on the third turn, and a same-WorkItem switch from
  `chat-A` to `chat-B` sent only the final `chat-B` constraint. The repaired
  candidate used the last acknowledged cursor for the failed-Attempt case and
  a complete bounded `chat-B` `snapshot_fallback` for the source-switch case.
  It also preserved the goal, free/public-domain asset constraint, and Main
  Chat commitment; removed control markup; produced `snapshot`, then exact
  verified warm `delta` payloads; and used `snapshot_fallback` for missing,
  repeated ambiguous, rolled, or cross-source anchors.
- The same probe rendered the authority guard through Codex, OpenClaw, Browser,
  and a future model-driven Provider without provider-name branching.
- Independent-audit counterexamples first reproduced both residual defects:
  receipt data appeared in public Runtime projections and receipt persistence
  advanced Attempt/WorkItem timestamps. After the targeted repair, eight key
  cross-layer test files passed 82 tests spanning adapters, Provider event
  ingestion, Work Ledger continuity, Runtime Activity, WebSocket projection,
  and recovery.
- Electron `npm test` passed two receipt-visibility projection tests, and
  `npm run build` passed TypeScript and the production Vite build.
- `git diff --check` and `python -m compileall -q agent_host core server
  tools/probes` exited zero. Git reported only the checkout's existing
  LF-to-CRLF conversion notices.
- Final public full suite in the project's `cu124` environment:
  `python -m pytest -q` -> 1689 passed, 1 skipped, 1690 total, 139.73 seconds,
  exit zero.

The receipt is deliberately an internal control-plane event. It is not stored
in the public Runtime event list; cursor metadata is removed from public
`provider.list`, `provider.result`, and terminal event projections; the client
WebSocket and Electron Work projection independently reject it. It does not
create Work Activity, enter the Provider Activity journal, extend the Browser
interaction branch lifetime, advance Attempt/WorkItem visible timestamps, or
claim progress, completion, permission, or outcome. Its bounded operational
cost is one hidden Ledger metadata write per natively accepted initial prompt
or steer. An unverified, clipped-anchor, or cross-source continuation may
repeat the bounded snapshot rather than risk omitting window content. Verified
same-source warm continuation is delta-only only while its unique full-text
anchor remains present.

No external paid-model run was used: this experiment tests Host checkpointing,
continuity selection, Provider request translation, and visible prompt payloads
deterministically. Model interpretation remains a downstream behavioral
variable. No claim is made yet that real Providers always treat Main Chat
commitments only as evidence or that delta produces a measured token/latency
improvement.

## 11. Non-goals

- no full conversation replay;
- no Provider-native history copied into the Work Ledger;
- no per-Chat/A1 isolation of one WorkItem's native Provider history;
- no summary model or second action classifier;
- no database schema migration;
- no provider-name routing rule;
- no live-model interpretation or token/cost benchmark;
- no change to permissions, completion evidence, or user acceptance.
