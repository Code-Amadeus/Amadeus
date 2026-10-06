# Chat runtime convergence

Cooperative is the sole production Chat orchestrator. The default remains
professional; `COOPERATIVE_WORK_PLANNER_ENABLED=false` selects basic. A failed
decision never falls back to a different strategy. Host permissions, durable
Work identity, replay fences and execution acceptance remain authoritative.

`core/chat_runtime.py` now contains shared presentation only: text filtering,
sentence splitting, expressions and TTS delivery. It cannot generate a turn or
dispatch a DELEGATE. Original Chat generation, its separate adjudicator,
decomposition query, recovery/resend chain and direct Browser/AUIP entry points
are retired. Shared source/reference validation and `parse_decomposition_reply`
remain; professional decoding still hands off at the first DELEGATE.

## Upgrading settings

`COOPERATIVE_CHAT_ENABLED` is a retired setting. Explicit false produces a
warning and starts Cooperative. The old Off value selected Original; it never
prohibited Work. Existing Work permission policy is unchanged.

Desktop Settings keeps a saved false through ordinary reads, unrelated saves
and backup recovery. Its migration notice explains the change; confirmation
durably deletes the old key. A failed save retains the notice. Environment or
`.env` values are read-only explanations: remove the obsolete assignment from
that source and restart. The backend exposes these non-sensitive facts through
`retired_settings`; they do not authorize execution.

The transitional reader is retained for the first release containing this
convergence. Before removing it in a later release, retain a file-format
migration for users skipping releases. Rollback requires the previous software
version; there is no hidden Original runtime switch in this version. Existing
historical authority records are retained and do not become fresh effect grants.

After acknowledging the desktop migration, rolling back does not restore the
deleted false value: the older version uses its default until you explicitly
set its old route option. Do not restore an old settings file over later edits
or an old database snapshot over newer receipts. Stop ingress and drain/stop
owned work before a version change; an unknown Provider state remains unknown.
[Storage compatibility checks](evidence/chat-runtime-rollback-2026-10-07.json)
cover only the recorded current/baseline commits on test copies, not arbitrary
releases or external process recovery.

## Local and Hybrid profiles

For pure-local Chat, use `LOCAL_LLM_TYPE=llama_server`. The managed default
context is 16384; an explicitly saved 4096 is preserved and may need increasing.
The messages boundary measures the actual server slot and rendered input plus
output budget, and rejects insufficient capacity before generation. Larger
context consumes additional device memory.

The managed Hybrid launcher keeps its 4096 default. An explicit
`LOCAL_LLM_CLI_CONTEXT` overrides both launch profiles; leave it unset to use
their separate defaults. The pure-local `llama_server` backend requires the
server's `/props`, `/apply-template` and `/tokenize` APIs, in addition to chat
completions. Older llama.cpp builds or OpenAI-only proxies without those APIs
are rejected because their capacity cannot be verified before generation.

Persistent `cli` sessions cannot satisfy independent messages requests and are
rejected with a migration hint. Point the executable setting at `llama-server`
from the same llama.cpp package. LM Studio and Ollama endpoint capacity and
overflow behavior remain **pending live acceptance**; no endpoint was available
in this environment.

`hybrid*` now actually requests its configured local HTTP head plus the existing
remote role model. This is an intentional behavior change for users who had
selected Hybrid while Cooperative previously used only the remote model. No new
response-mode setting is introduced. Head configuration is independent of
`LOCAL_LLM_TYPE`; the existing Hybrid BAT launch profile is preserved.

The delivery lock stays inside the user turn. Streamed remote replies, and
non-streamed replies with no Host action, share one history entry with the head.
For a non-streamed Host action the head is committed separately and the lock is
released before the action. An already-ready remote reply cancels an unused
head; pending choices never inherit its lock.

RAG uses the shared process index and adds a per-turn reference to the role
system prompt. Display/voice formatting cannot create execution authority.

## Evidence and remaining acceptance

The linked evidence records its tested revisions and retained failures.
Historical routing scores remain historical, including results for the
now-retired Original route.

The corrected 30-pair ordinary Chat comparison measured send-to-first-text
medians of 1.039 s before and 1.047 s after the capability migration. This is a
pre-retirement comparison, not a new performance claim for every later edit.
The earlier 3.9 s result included input-readiness wait and is invalid for this
metric. First model content, first Chat text and first audio-device write are
different boundaries; device writes do not prove acoustic arrival.

External local endpoints and acoustic loopback remain unverified. The full
current routing/persona corpus was observed in a new controlled context;
[results retain all mismatches](evidence/chat-runtime-hybrid-semantics-2026-10-07.json).
Local heads now retain a complete sentence across commas, but copied input
language and repetitive openings remain model-quality limitations. No extra
model pass or language classifier was added. The user explicitly deferred model
quality tuning from this retirement after reviewing these observations. A
bounded pure-local Host probe also retained basic's missed task handoff and
professional's unrequested desktop target; neither is relabeled as a fully
accepted local model outcome. See the [quality disposition](evidence/chat-runtime-quality-deferred-2026-10-07.json)
and [local Host evidence](evidence/chat-runtime-local-host-2026-10-07.json).
AUIP J7 uses the shipping entry with
a seeded application and real action receipts. Parent review found wrong
actor/winner narration after resignation; the candidate catalog now preserves
declared action semantics, and B2 receives the original user wording instead of
a lossy derived instruction. Both strategies' repeated journeys correctly
attributed resignation and the winner through follow-up dialogue. See the
[bounded repair evidence](evidence/chat-runtime-auip-source-repair-2026-10-07.json).
Gomoku does not cover open-payload actions or continuous controllers. Separate
[bounded journeys](evidence/chat-runtime-auip-extended-2026-10-07.json) now cover
both, with the open case explicitly using a declaration variant of an existing
app. Local [cancellation and fresh-loop checks](evidence/chat-runtime-local-lifecycle-2026-10-07.json)
cover transport lifecycle, not durable desktop restoration or general model
quality. The old private-runtime harness is not retained as a compatibility path.

Browser is limited to regression observation and issue recording in this
retirement, by the user's explicit scope decision. The live cross-domain
journey retained seven failed checks; it is **not** marked accepted. The
workspace-less OpenClaw amendment restriction predates this work; other
Project/steering mismatches remain unattributed. They are deferred for the
future Browser decision, while regressions proven to be introduced by this
convergence remain in scope. See the [retained observations and baseline
evidence](evidence/chat-runtime-browser-deferred-2026-10-07.json).

Real native Codex approval was checked in both directions: allow continued the
same run and wrote the requested file; deny durably recorded refusal and left
the target absent. Both closed the permission card and retained one WorkItem
and Attempt. These are direct provider/Host checks, not GUI permission-button
or natural-language routing checks. See [permission evidence](evidence/chat-runtime-host-permissions-2026-10-07.json).

A real [interruption/restart journey](evidence/chat-runtime-recovery-2026-10-07.json)
preserved failed status, workspace and focus across backend restart. Explicit
retry added one Attempt to the same WorkItem and produced the exact recovery
artifact; read-only queries added no Work. This is bounded headless
professional/DeepSeek/Codex evidence, with TTS off.

Provider handoff continues to prefer the selected operation's source clause.
The experimental expansion to the full current message has been withdrawn
from this change; surrounding-constraint propagation needs separate validation
against multi-operation scope. Earlier execution evidence that used that
experiment is not proof of constraint delivery in the final implementation.
