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
- WorkItem, Attempt, workspace, permission, completion, and acceptance
  authority remain unchanged.

## 3. Rejected extremes

### Always send only the latest sentence

Rejected because anaphoric complaints, confirmations, corrections, and delayed
commitments may not contain the task object.

### Always replay a bounded snapshot

Safe for correctness, but a warm native session repeatedly receives facts it
already owns. In a long interaction this wastes context and can over-weight an
old constraint through repetition.

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

## 4. Current candidate: snapshot for correctness, delta for warm transport

The Host first builds one bounded parent-conversation checkpoint:

- most recent six `user` / `assistant` messages;
- at most 2000 characters total;
- at most 280 characters per message, retaining head and tail when truncated;
- explicit `User:` and `Main Chat:` role labels;
- current user wording excluded because it travels separately;
- executable/internal control markup removed before handoff.

Delivery mode is then selected by Host-owned continuity facts:

| Condition | Delivery |
| --- | --- |
| New Provider session/thread | `snapshot` |
| Provider switch or stateless Provider | `snapshot` |
| Same verified native session and one unique prior-source anchor | `delta` |
| No prior cursor, anchor rolled out, or anchor absent | `snapshot_fallback` |
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
attaches the same typed Provider Session. Previous Attempt metadata supplies
the last source user wording and turn audit identity. A cold continuation
receives a snapshot.

### Active Attempt steer

The active run record supplies the last accepted handoff cursor. The Host
computes the delta before creating `ProviderSteerRequest`. After the Provider
accepts the steer, Provider Runtime advances the run cursor. A rejected steer
does not advance it.

### Recovery

Recovery correctness never depends on delta. If a recovery lacks a verifiable
cursor or complete current checkpoint, it reuses a bounded snapshot/fallback
and the existing recovery task contract.

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
bounded snapshot establishes recoverability
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

Until then, the text anchor plus fail-safe snapshot is the smaller coherent
repair.

## 9. Experiment plan

| Experiment | Required result |
| --- | --- |
| E0 old failure baseline | Latest-only/recovery handoff demonstrably loses the multi-turn object |
| E1 cold/warm sequence | Turn 1 snapshot; later verified turns contain only new parent messages |
| E2 ambiguous/rolled cursor | Fail closed to bounded `snapshot_fallback` |
| E3 recovery/resend | Recovered delegate keeps bounded user and Main Chat evidence |
| E4 Work Ledger attach | Same typed Provider Session gets delta; cold continuation gets snapshot |
| E5 active steer | Only post-cursor parent messages reach steer; accepted steer advances cursor |
| E6 Provider matrix | Codex App Server, Direct Codex, OpenClaw, and Browser consume the same envelope |
| E7 authority guard | Control tags removed; prior Main Chat cannot become instruction/completion |
| E8 deterministic suite | Relevant suites and full repository runner pass in public and internal trees |

## 10. Experiment results

The deterministic experiment passed in the public working tree.

- E0 reproduced the defect before the repair: the old handoff retained only
  `这是学习使用，不是商业创作，你去找找看，然后更新。` and lost both the
  desktop Pokemon game object and Main Chat's commitment to update its files.
  The old recovery path could send no parent context.
- `python tools/probes/probe_provider_parent_context_handoff.py` passed. It
  preserved the multi-turn goal, free/public-domain asset constraint,
  and Main Chat commitment; removed control markup; produced `snapshot`, then
  exact warm `delta` payloads; and used `snapshot_fallback` for missing and
  repeated ambiguous anchors.
- The same probe rendered the authority guard through Codex, OpenClaw, Browser,
  and a future model-driven Provider without provider-name branching.
- Focused context, recovery, adapter, and active-steer suites: 122 passed.
  Focused Work Ledger continuation suites: 19 passed. The final
  unique/ambiguous-anchor regression group: 14 passed.
  After the final prompt-layout review, all nine modified test modules were
  rerun together: 94 passed.
- `git diff --check` and `python -m compileall -q agent_host core server
  tools/probes` exited zero. Git reported only the checkout's existing
  LF-to-CRLF conversion notices.
- Public full runner: `python tools/run_tests.py` -> 1680 passed, 1 skipped,
  1681 total, 506.43 seconds, exit zero.

No external paid-model run was used: this experiment tests Host checkpointing,
continuity selection, Provider request translation, and visible prompt payloads
deterministically. Model interpretation remains a downstream behavioral
variable, while the transport invariant is covered.

## 11. Non-goals

- no full conversation replay;
- no Provider-native history copied into the Work Ledger;
- no summary model or second action classifier;
- no database schema migration;
- no provider-name routing rule;
- no change to permissions, completion evidence, or user acceptance.
