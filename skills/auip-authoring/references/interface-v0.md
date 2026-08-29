# AUIP v0 authoring interface

This is the compact interface contract for application authors. The staged SDK
and validator implementations are opaque dependencies: reference or execute
them, but do not read their source unless an observed interface error cannot be
resolved from this document.

## Managed Web application

Load `managed-v0.js` before `auip-v0.js`. Load `situations-v0.js` before the
application code when using a standard situation projection. A Controller-capable
app also loads `controller-v0.js` before its application code.
For a Host-managed in-place application or export, the already materialized stable paths are
`sdk/auip-core/managed-v0.js`, `sdk/auip-core/situations-v0.js`,
`sdk/auip-core/controller-v0.js`, and `sdk/auip-web/auip-v0.js`, relative to the
application/delivery root (the Provider working directory for an in-place
integration). Reference those exact files directly; do not use root-level
aliases and do not create a second SDK copy.

For a browser entry, these are classic scripts, not ESM imports. Use this exact
order and the documented globals; there is no need to inspect or probe their
module shape:

```html
<script src="sdk/auip-core/managed-v0.js"></script>
<script src="sdk/auip-core/situations-v0.js"></script>
<!-- Controller apps only: -->
<script src="sdk/auip-core/controller-v0.js"></script>
<script src="sdk/auip-web/auip-v0.js"></script>
```

The corresponding globals are `AmadeusAUIPManaged`,
`AmadeusAUIPSituations`, `AmadeusAUIPController`, and `AmadeusAUIP`.

For a file-URL HTML entry, include one generated manifest slot; the Host fills
and verifies its content from `auip.manifest.json` after authoring:

```html
<script id="auip-manifest" type="application/json">
{}
</script>
```

```js
const auip = AmadeusAUIP.createManagedApp({
  manifest,                         // parsed auip.manifest.json
  snapshot: () => ({/* bounded current situation */}),
  initialEvents: () => [],          // optional declared events
  actions: {
    "namespace.action": (payload, tx) => tx.commit({
      mutate: () => appMutation(payload),
      effects: ({result, state}) => ({}),
      events: ({result, state}) => [],
    }),
  },
  onConnected: () => {},            // optional, synchronous
  onDiagnostic: diagnostic => {},   // optional presentation only
});

auip.start();
auip.commitLocal({actor: "user", mutate, effects, events});
auip.checkpoint({actor: "app", effects, events});
auip.checkpointIfDue({minimumIntervalMs: 2000});
auip.revision();
auip.snapshot();
```

`createManagedApp(...)` constructs the Managed Core and validates the first
snapshot synchronously; it does not wait for `start()`. Initialize every
app-owned field read by `snapshot()` before construction. Preserve progressive
enhancement at the same boundary: the original initial render and primary input
bindings must not depend on successful AUIP construction or transport startup.
Initialize the original app first, or contain the adapter bootstrap so a
failure leaves the standalone application usable.

Handlers and snapshot functions are synchronous. The exact manifest payload is
passed to its action handler unchanged and frozen. Conclude through
`tx.commit(...)` or `tx.reject(reason, code)`. The Managed Core owns revision,
expected-revision checks, actor validation, frozen envelopes, and publication.
Validate application preconditions before committing and conclude exactly once.
Do not catch an error from `tx.commit(...)` and then call `tx.reject(...)`; the
transaction may already be concluded. Convert an expected application failure
to `tx.reject(...)` before entering `tx.commit(...)`, and let unexpected Core
contract errors remain visible.
Put one bounded static `app.objective` in the manifest when the purpose is not
obvious; changing phase goals belong in the situation snapshot.
Participant apps also declare a bounded `app.interactionSummary`: one natural
domain briefing with two or three colloquial user examples mapped to real
declared actions or application behavior. The Host binds one branch-static
record to active Main Chat and makes the same knowledge available to AUIP Control
and sparse Narrator calls; it is not copied into each changing state projection. It helps resolve
elliptical language and terminology but never replaces `inputSchema`, grants
authority, or proves that an example occurred.

Write examples at the next-action boundary. If the accepted state requires a
declared prerequisite before the requested result, the example's good reply
must promise only that prerequisite and defer the downstream effect until a
later accepted receipt. If the current user proposal already authorizes the
prerequisite, the reply must not ask for the same confirmation again. Add a
negative contrast only when the application has a real ambiguity: for example,
a Controller policy that selects `attack` may not let Main Chat promise a named
target or manual aim. Describe the supported policy-level alternative while
leaving exact payload fields and enum values to Participant.

Choose action granularity from the user's semantic outcome rather than internal
function boundaries. If one common request needs a local prerequisite and an
immediate effect that the app can validate and commit atomically, expose one
typed composite action and report both effects in its receipt. Keep two actions
only when the intermediate receipt matters for authority, confirmation, or
state-dependent choice. The Host does not fuse separate proposals on the app's
behalf. If a composite changes role, ownership, or control identity, the
role-facing example must speak from the resulting identity; describing the old
binding as still governing while promising the newly owned action is a
say/do conflict.

For a Node author test without a Host transport, load `managed-v0.js` with the
following UMD-compatible expression and instantiate the same manifest,
snapshot, initial-events, and action map:

```js
const managedModule = await import("./sdk/auip-core/managed-v0.js");
const managedApi = managedModule.default || globalThis.AmadeusAUIPManaged;
const core = managedApi.createManagedCore({
  manifest, snapshot, initialEvents: () => [], actions,
});
const result = core.dispatchAction({
  type: "namespace.action",
  payload: exactPayload,
  expected_revision: core.revision(),
});
```

The returned core exposes only `manifest`, `revision()`, `snapshot()`,
`commitLocal(spec)`, `checkpoint(spec)`, `checkpointIfChanged(spec)`,
`dispatchAction(envelope)`, and `healthy()`. `dispatchAction` takes that one
envelope object—never positional arguments or camelCase `expectedRevision`.
This is sufficient for direct Managed-Core receipt tests; do not probe SDK
internals to rediscover it.

## Interaction cadence and decision ownership

AUIP v0 Participant is a low-frequency transaction model. One model decision
selects one bounded action, and one accepted action produces one commit and one
receipt. The accepted snapshot used for the decision must remain valid through
ordinary Participant and authorization latency.

Rendering may run at any frequency, but action-relevant mechanics may not drift
privately behind an unchanged AUIP revision. Private animation is safe only when
it cannot change action selection, ordinary legality, or an accepted state fact
used by the decision. Timer-driven or externally advancing mechanics must reach
a bounded semantic `checkpoint(...)` before exposing the next decision window.
The Managed Core compares a fresh snapshot with the last committed/checkpointed
snapshot before dispatch and returns `state_changed_without_checkpoint` without
calling the application handler when they differ.

Do not point `snapshot()` at frame-mutated counters and rely on a later
`checkpointIfDue()` to make them safe: during the interval, the fresh snapshot
has already drifted. Either publish only stable/qualitative facts whose changes
receive an immediate checkpoint in the same JavaScript turn, or keep a separate
accepted projection cache and update that cache only at a commit/checkpoint
boundary. Exact timer, position, projectile, and per-frame health values normally
stay app-local.

`checkpointIfDue({minimumIntervalMs})` is an eventless background refresh (2000
ms default; normally 1000–5000). Before the interval it returns
`committed:false`, `code:"checkpoint_not_due"`. It accepts no semantic
effects/events; publish real boundaries immediately with `checkpoint(...)`.

An author test for every continuously advancing participant app must commit one
representative action, advance real timers/physics for less than the background
checkpoint interval, then dispatch the next published `available:true` action
against the still-accepted revision. It must either succeed or be preceded by a
real checkpoint; `state_changed_without_checkpoint` is a failed adapter, not an
expected retry path.

The shared projection is not a debug or cheat channel. Keep internal danger
scores, utility rankings, and private Controller telemetry local when the player
cannot legitimately perceive them; publish visible structure from which the
Participant can reason. Semantic event payloads should describe the smallest
user-meaningful outcome and must not copy a full metric snapshot merely for
narration. That payload proves only the individual event; it is not implicitly
current or cumulative state. Put any exact total or present value that users may
query into the accepted state projection at a real checkpoint instead of
requiring the Host or speaking model to aggregate repeated Controller-effect
deltas.

When the required response horizon is shorter than the model/gate round trip,
AUIP may expose a Reactive Controller policy action. AUIP adaptation may author a
thin application-local rule system or AI for those declared policies. The local
Controller owns app-specific reactions; Chat and Decision Participant do not
choose each tick. Reuse `controller-v0.js` for leases, generation replacement,
expiry, rate ceilings and takeover. The adapter still owns the exact policy,
observation, command, effect and safe-point semantics. Do not add a universal
follow/attack/hold enum or a generic command/args/data payload. If the app has
neither a stable decision boundary nor a feasible safe local Controller, author
it as spectator-only and report that participation is unsupported.

Apply a core-participation coverage check before choosing that boundary. Name
the primary user-controlled outcome and its ordinary failure condition. A
Decision Participant is credible only when one bounded action completes a useful
core effect, or existing app-local automation carries the accepted intention to
the next stable decision without human input. Timers, enemies, physics, and
animation advancing while player actuators remain idle do not satisfy this
condition. If the primary loop still needs repeated movement, steering, aiming,
defence, collection, or comparable input, lifecycle/menu actions such as start,
pause, upgrade, continue, restart, or reset are supplementary; they cannot by
themselves justify a participant stance. Use a feasible Reactive Controller for
that coherent useful core subset, or remain spectator-only.

Inventory independent user-facing controls. One-shot displacement may be a
Decision action; sustained heading/navigation/follow may be an app-specific
Controller policy. Preserve movement/target/objective dimensions instead of
only bundled modes; use one atomic app payload when they interact, never Host
merge rules, keys, or frames. Cooperative follow needs stable app-owned role and
target identity/availability—not proximity inference—and clears or blocks on
target loss, phase change, takeover, revoke, or expiry.

One language example equals one proposal boundary. Multi-primitive behavior
such as aim then fire needs a real composite/Controller action or separate
receipt-bound steps; `actionA + actionB` is not one proved action.

Classify by the lifetime of effects, not by how rarely a policy setter is
called. If one accepted Participant action can cause later application effects
without another Participant action, it is a Controller policy and must end or
transfer with the Host lease. Do not expose it as ordinary Decision
configuration that keeps acting after observe/leave. Put all fields needed for
one continuing policy into one exact atomic payload; threshold, target, response,
and similar app semantics must not require several Decision calls to assemble.
The first shape-design report states the core participation outcome, whether
the application already executes it autonomously between decisions, and why the
selected response horizon covers (or cannot cover) the ordinary failure path.

The ordinary legality and meaning of a Controller policy payload must remain
stable while fast telemetry advances. The Host can then rebind a policy proposal
to the latest data-plane revision when the decision generation is unchanged;
ordinary Decision actions remain strictly revision-bound, and any `choice/v1`
whitelist covering that policy is rechecked at the latest state. If policy
legality itself changes with telemetry, expose a stable control boundary or do
not claim Controller support.

Host mode `collaborate` means both human and Participant may act according to
the application's accepted mechanics. It does not create alternating turns,
player roles, or input locks. Those are application state only when the app
already implements and publishes them.

Audit the full application lifecycle before writing events or actions. Keep the
following ownership boundary exact:

| Meaning | AUIP/Host owns | Adapter/application owns |
| --- | --- | --- |
| One accepted domain change | revision, actor, receipt truth | action name, exact payload, legality, mutation, effects |
| A new low-frequency decision is due | `participantOpportunity` scheduling semantics | the app event that marks the real stable boundary |
| A round/run/phase ended but the app can continue | ordinary nonterminal beat processing; required scheduling only when the event is a true opportunity | result event name and payload, readable phase state, exact restart/continue/conclude choices |
| The whole attached experience is over | `terminal:true` completes the AppSession and cancels later decisions | the app event emitted only when its own conclude operation commits |
| Kurisu stops acting while the user continues | Host `observe` | no substitute app action |
| The Host-owned surface is left | Host `leave` | no claim that an arbitrary OS process was closed |

Manifest `terminal:true` is therefore not a synonym for one round's win, loss,
death, or loop completion. When the same application can continue, publish a
nonterminal app-authored result event, expose the current post-round choices in
state, and declare exact application actions such as that app's own restart,
continue, or conclude operations. Add `participantOpportunity:true` only when
every occurrence requires one immediate automatic action; leave it off when
collaborative play may comment, ask the user, or wait for a short instruction.
Delegate mode can still react to an important result beat. If the source
supports resigning or withdrawing during an active round, declare its real typed
action and preconditions. The adapter may call its phase field `lifecycle`,
`screen`, `runStatus`, or anything else already natural to the app; AUIP does not
standardize that field or its values. Do not add a universal lifecycle enum,
action name, or `resign`/`restart` payload.

When one `choice/v1` projection governs a lifecycle action family, pass the same
complete family as `actionTypes` at each phase and mark currently represented
illegal options `available:false`. An action type listed in `actionTypes` but
absent from current options is closed as unavailable; it is never reinterpreted
as an ungoverned always-addressable action. The author test must exercise the
same Managed Core action map through the
application's real mechanics: reach one app-owned round/run result and prove the
AppSession is still active; invoke one currently published continuation or
conclusion choice and prove its exact receipt/state transition; then, when the
app exposes a true conclude action, prove its declared terminal event completes
the AppSession and rejects later actions. Test optional resign/withdraw only
when the source application genuinely implements it. Static validation can
reject contradictions such as `terminal:true` plus
`participantOpportunity:true`, but it cannot infer game rules or certify that
an app-specific lifecycle action is real.

## Manifest authoring limits

The manifest must be valid JSON and no larger than 64 KiB when serialized.
Use `amadeus.auip/v0`; declare at least one semantic event; use only
`spectator` and `participant` stances; and use only `none` or
`local_execution` action risk. Identifiers and semantic types use lowercase
ASCII. Limits are: `app.id` 80 characters, `app.title` 120, `app.version` 40,
`app.objective` 240, `app.interactionSummary` 640, each event/action type 120, and each action description
240. Every non-empty action input is an MCP-compatible JSON Schema whose root
`type` is `object`. An authored manifest that declares `participant` must also
retain the lower-authority `spectator` stance, and must declare a non-empty top-level
`situationKinds` array using `action_availability/v1`, `grid/v1`, `choice/v1`,
`scalars/v1`, `sequence/v1`, and/or `controller/v1`; every declared kind must occur in each published state (it may
be nested beside additional business state). A `participant` manifest must
declare at least one real typed application action; an empty catalog is a
spectator surface regardless of descriptive prose. Run the opaque manifest validation and embed-sync preflight
paths supplied by the Host against the completed manifest and HTML entry; do
not read their implementations. The Host reruns the authoritative checks after
Provider completion.

## Reactive Controller profile

Use a Controller only after the response-horizon/effect-lifetime decision in
this interface selects it. Then read the complete
[Controller interface](controller-v0.md) before implementation. It contains the
exact manifest profile, callback envelopes, lease test shape, loop cadence,
intent cleanup, sparse outcome, governance projection, and causal author-test
contract. Decision-only and spectator integrations do not need that reference.

## Coordinate grid situation

Use this pattern for a dense coordinate-addressed surface whose row/column
structure matters to choosing an action:

```js
const board = AmadeusAUIPSituations.gridSituation({
  width: 15,
  height: 15,
  empty: ".",
  legend: {b: "black", w: "white"},
  cell: (x, y) => game.board[y][x] || ".",
});
```

It returns `{kind:"grid/v1", width, height, empty, legend, rows}` and fails if
dimensions, symbols, row widths, or legend membership drift. A coordinate
action must name all ordinary preconditions and the direct evidence path, for
example `state.board.rows[payload.y][payload.x] == state.board.empty`. Do not
flatten rows into one string or require a Participant to count cells.

Declare the same mechanical link in the action manifest without changing the
handler payload:

```json
"preconditions": [{
  "kind": "grid_cell_empty/v1",
  "statePath": "board",
  "xField": "x",
  "yField": "y"
}]
```

The referenced coordinate fields must be required integers in the unchanged
MCP-compatible `inputSchema`, and `situationKinds` must include `grid/v1`.

## Action-family availability situation

Use this shape when a phase, turn, participant binding, or other app-owned rule
makes an entire action type available or unavailable, while its payload space is
too large for `choice/v1` or is already governed by another typed shape:

```js
const actionAvailability = AmadeusAUIPSituations.actionAvailabilitySituation({
  actionTypes: ["game.place_stone", "game.take_first_move"],
  availableActionTypes: canTakeFirst
    ? ["game.take_first_move"]
    : canPlace ? ["game.place_stone"] : [],
});
```

It returns
`{kind:"action_availability/v1",actionTypes,availableActionTypes}`.
`actionTypes` is the stable complete family governed by this surface;
`availableActionTypes` is its current subset and may be empty. The helper rejects
duplicates, unknown action names, or an available action outside the family.

Each governed manifest action declares the same current-state link:

```json
"preconditions": [
  {
    "kind": "action_available/v1",
    "statePath": "actionAvailability"
  },
  {
    "kind": "grid_cell_empty/v1",
    "statePath": "board",
    "xField": "x",
    "yField": "y"
  }
]
```

The Host filters an unavailable action type before role choice and checks it
again at invocation. This shape does not enumerate, rename, or wrap payloads:
cell occupancy still belongs to `grid_cell_empty/v1`, and a small exact payload
set still belongs to `choice/v1`.

## Small choice situation

Use `AmadeusAUIPSituations.choiceSituation({actionTypes,options})` for a small
action space. `actionTypes` is the stable complete set governed across phases;
each option is exact `{id,label,action,payload,available}`. The helper clones and
freezes payloads unchanged and returns `{kind:"choice/v1",actionTypes,options}`.

Use `actionAddressed:true` when manifest action names already carry the portable
meaning. It validates ids/labels but projects only
`{action,payload,available}` plus `actionTypes`. `available:true` promises that
exact pair succeeds against this snapshot without setup; publish legality before
selection, not through rejection. One independently meaningful source
transition is atomic even if its UI needs several clicks. Repeated placement,
connection, selection, or movement is not a whole objective: never replace it
with `solve`/`apply_plan` or split its payload into action names for size.

The whitelist is scoped by action type. Options contain the complete current
payload set for each represented action. A governed action with no current option is unavailable.
Other actions may use another standard shape. Never publish only a convenient
payload subset.

When all options are available and share one action, use
`choiceSituation({compact:true, action:"namespace.action", options})`; the root
action is the stable one-item family and repeated wire fields are omitted, while
labels and exact payloads remain. Mixed or unavailable options use the full
form. The 1024-character projection target is advisory: exceed it rather than
alter granularity, payload, lifecycle, or legality. Retain the stable option
family with `available:false` instead of an empty choice or macro.

## Numeric scalar situation

Use `AmadeusAUIPSituations.scalarSituation({metrics})` for labeled numeric
state. Each metric is `{id,label,value,unit,trend,safe:[low,high]}`, where trend
is `rising`, `falling`, or `steady`. It returns
`{kind:"scalars/v1",metrics}` and rejects duplicate ids, non-finite values, or
invalid safe ranges. Preserve the current value, direction, and accepted range
needed to choose an action.

## Linear sequence situation

Use `AmadeusAUIPSituations.sequenceSituation({steps, completedCount})` when an
application has one fixed required order. `steps` is an ordered array of
`{id,label}` with unique ids; `completedCount` is the number of leading steps
already completed. It returns
`{kind:"sequence/v1",completedCount,nextStepId,steps}`. `nextStepId` is derived
from the ordered steps and becomes `null` only when the sequence is complete.
This makes the next ordinary precondition directly readable without encoding
order as a delimiter string or requiring receipt rejection to discover it.
Publish exact action payloads separately through `choiceSituation(...)` when
the available action space is small.

## Controller status situation

Use `controllerSituation({status,policyRevision,policyAction,policySummary,reason})`
for the bounded governance projection. Status is `idle`, `active`, `stopping`,
or `blocked`. Idle has null policy revision/action and an empty summary; every
other status names the exact manifest policy action and its Host policy revision.
The application-specific policy payload remains application state when it is
safe and useful to expose; the helper never renames or wraps it.

## Ownership boundary

- The application owns mechanics, action-specific payload fields, legality,
  objective, situation mapping, effects, and events.
- The Core owns commit protocol mechanics only.
- The Web binding owns Attach transport only.
- Host-materialized runtime assets must be referenced in place, not copied,
  opened, edited, or regenerated by the coding Provider.
- Host post-validation checks final packaging and protocol invariants. Application-
  specific behavior and action semantics still require focused author tests.
  For Web applications, that author test must execute the completed entry
  top-to-bottom once without AUIP and once with the exact materialized runtime
  scripts. Merely replacing `createManagedApp(...)` with a configuration-capture
  stub does not prove initial snapshot validity, initial rendering, or handler
  binding.
