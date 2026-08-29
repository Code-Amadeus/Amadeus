---
name: auip-authoring
description: Add Amadeus AUIP v0 support to a local interactive web app so the character can observe semantic state and safely participate through typed actions. Use for games, simulations, or interactive tools when the user asks Amadeus/Kurisu to watch, comment, play, or operate them. Do not use for static pages, reports, ordinary sites, or unrelated code work.
---

# AUIP authoring

AUIP is progressive enhancement: preserve the original application's mechanics,
UI, and standalone behavior. The app remains the business authority when
Amadeus is absent.

## Read and build

1. Read the complete compact [authoring interface](references/interface-v0.md).
   It is the public API. Do not inspect SDK, parser, validator, or sync-tool
   implementations to rediscover it. Treat a preflight question that this
   interface cannot answer as a concrete authoring-interface defect; report the
   missing contract instead of searching for a second protocol source.
2. If and only if response horizon/effect lifetime requires a Reactive
   Controller, also read the complete
   [Controller interface](references/controller-v0.md) before implementing it.
3. Inspect the real application's state, handlers, mechanics, role binding, and
   full lifecycle. Start from [the manifest template](assets/auip.manifest.json)
   and declare only behavior the source application actually implements.
4. Reference Host-materialized `sdk/...` runtime assets in place. Do not open,
   copy, edit, regenerate, alias, or duplicate them, and never ship
   `.amadeus/runtime/authoring_inputs` or Attempt-private paths.
5. Keep `auip.manifest.json` as the only hand-edited schema source. Run the two
   Host-supplied opaque preflights—manifest validation, then manifest-to-entry
   synchronization—without reading or duplicating their implementations.

Use visible Provider progress at truthful transitions: chosen AUIP shape,
integration underway, validation underway, and completion or a concrete
blocker. A progress line is intermediate; continue the same turn until the
artifact and validation exist or a real blocker prevents further work.

## Choose the participation shape

- **Spectator:** semantic state/events only; no invented actions.
- **Decision Participant:** a low-frequency transaction model. One decision
  selects one bounded typed action against one stable revision.
- **Reactive Controller:** one accepted app-specific policy causes later local
  effects, or an urgent response horizon is shorter than model/gate latency.
  Policy is low-frequency; the app-local controller handles timely execution.

Choose by response horizon and lifetime of effects, not game genre, render
rate, or how rarely a policy setter is called. If there is neither a stable
decision window nor a feasible bounded Controller, remain spectator-only.
Never weaken revision checks, turn frames into decisions, or add a universal
mode/command schema.

Before settling on a participant shape, name the application's **core
participation outcome** and test whether the declared controls cover a coherent
useful part of the primary interactive loop. Start/pause/restart, menus,
upgrades, or other lifecycle choices alone are not credible participation when
the ordinary loop still requires a human to move, steer, aim, defend, collect,
or otherwise avoid its normal failure condition. The fact that enemies,
physics, timers, or animation continue by themselves is not app-local control
of the player side.

A Decision Participant is sufficient only when each bounded action itself
completes the promised core effect, or the existing application already carries
that intention autonomously until the next stable decision. If useful core
participation instead requires repeated/sustained actuator input or timely
reaction between stable boundaries, author a feasible Reactive Controller with
low-frequency policy and app-local execution. If that cannot be done safely,
declare spectator-only rather than overstating peripheral controls as
participation. Report this coverage reasoning in the first shape DESIGN
milestone so it remains reviewable.

## Domain contract

- `app.objective` is one bounded static objective; changing phase goals belong
  in state.
- A participant app provides one bounded `app.interactionSummary` after code
  inspection. Describe what Kurisu can perceive and intentionally control, with
  two or three short colloquial examples mapped to real public behavior. This
  branch-static summary is domain knowledge, not changing state, payload
  authority, execution evidence, or text to recite to the user.
- The summary and examples lead with the covered core participation outcome.
  Peripheral lifecycle actions may be included, but may not disguise a
  human-only primary loop as participant-capable.
- Examples stop at one proposal boundary. If a prerequisite must commit first,
  promise only that prerequisite. If one ordinary intention can atomically
  validate and commit prerequisite plus immediate effect, expose one semantic
  action. Do not make adapter plumbing into conversational hesitation.
- When the role settles on a different declared action than the user's
  proposal, its visible reply gives the reason and the Participant payload
  matches that settled alternative exactly.
- Inventory independently meaningful controls—movement/heading, stable follow
  target, objective priority, pause, lifecycle choices—without forcing only
  bundled modes. Declare only dimensions the real app supports.
- Every non-empty payload keeps its exact MCP-compatible object JSON Schema,
  normally closed by `additionalProperties:false`. Do not wrap, flatten,
  rename, abbreviate, or abstract it into command/args/data. Preserve source
  action granularity: never replace repeated state transitions with a
  `solve`/`apply_plan` macro or split one payload into action names for size.
- Action descriptions name accepted-state preconditions and the state fields
  proving them. Rejection is a safety boundary, not ordinary legality discovery.
- For coordinate actions over `grid/v1`, declare the closed
  `grid_cell_empty/v1` action precondition so the Host can reject an occupied or
  out-of-bounds payload before role review without changing that payload.
- When whole action families become legal or illegal by phase, turn, binding,
  or another app-owned rule, publish `action_availability/v1` and add the
  `action_available/v1` precondition to each governed action. Keep its stable
  `actionTypes` family and current `availableActionTypes` subset explicit; this
  filters action types and never replaces or abstracts their payloads.

## State and authority

Use documented `action_availability/v1`, `grid/v1`, `choice/v1`, `scalars/v1`,
`sequence/v1`, and `controller/v1` shapes when applicable. Declare every
emitted kind in `situationKinds`.

- Keep shared state normally around or below 1024 characters, but this is advisory:
  exceed it rather than change action granularity, payload, lifecycle, or
  legality. Preserve readable rows, order, labels, trends, phase, role binding,
  and legality. Never flatten a grid.
- Shared state is not a cheat/debug channel. Keep hidden information, model
  reasoning, private utility/danger scores, raw frames, logs, pointer/aim
  intent, and continuous geometry local. Visible pressure may be mapped to
  app-owned qualitative bands when exact counts add no user value.
- A semantic event payload is evidence for that one event, not implicit current
  or cumulative state. If a user may ask for an exact running total or current
  value, publish it through accepted state at a real checkpoint; do not expect
  the Host or speaking model to aggregate repeated Controller-effect deltas.
- `choice/v1 available:true` promises that exact action and unchanged payload
  succeeds against that snapshot without setup. Pass the stable complete
  choice-governed family as `actionTypes` in every phase; compact single-action
  choice is the one exception because its root `action` is itself the complete
  one-item family. Publish the complete option set for represented payloads and
  use `available:false` for illegal phases. An absent option in a declared
  family is unavailable, never ungoverned. Retain unavailable stable options
  instead of an empty choice or whole-solution macro.
- When manifest action names already carry the portable option semantics, use
  `choiceSituation({actionAddressed:true,...})` to omit redundant app-local
  ids/labels from shared state while retaining exact action, payload, and
  availability.
- Use `actionAvailabilitySituation({actionTypes,availableActionTypes})` when
  payload enumeration would be large or is already governed by another typed
  shape such as `grid/v1`. It answers only whether an action type is currently
  available; coordinate and other payload legality remain in their own
  preconditions or exact `choice/v1` options.
- The accepted Decision snapshot may not drift privately while Participant and
  any required explicit-turn gate decide. A `participantOpportunity` is a stable required-action window:
  every non-`kurisu` occurrence requires one action now. A reusable win, loss,
  death, run, or round result needing role acknowledgement is a nonterminal
  `importance:"important"` beat, never an opportunity or normal-stride event.
- Keep participant identity separate from application role. `kurisu` does not
  mean White, player two, a vehicle, or a unit. Publish selectable binding and
  expose a real typed configuration action (or atomic composite) when needed.
  Attached local input follows the same binding.
  Takeover is an explicit application transition, not an incidental click.

## Actions, lifecycle, and receipts

Bind manifest keys exactly in `createManagedApp({actions})`. Handlers are
synchronous, receive the unchanged frozen payload, validate before mutation,
and call exactly one `tx.commit(...)` or `tx.reject(...)`. Put the real mutation
inside `commit({mutate})`. Never catch a `tx.commit` error and then reject.

Use `commitLocal` for local semantic transitions, `checkpoint` for externally
advancing semantic boundaries, and eventless `checkpointIfDue` for background
refresh. Frames/local commands alone do not create revisions; real semantic
events publish immediately. Only an accepted receipt proves Kurisu acted.
Effects retain enough bounded meaning for later recall.

`terminal:true` ends the whole AppSession. A reusable win, loss, death, run, or
round result is nonterminal and keeps app-owned phase plus exact
restart/continue/conclude choices. Host `observe` stops Kurisu while the user
continues; Host `leave` leaves the owned surface. Neither substitutes for an
application action or claims arbitrary process closure. Do not invent a
universal lifecycle enum or unsupported resignation/restart path.

## Controller boundary

When selected, follow [Controller interface](references/controller-v0.md)
exactly. The Host/Core own principal, lease, generation, expiry, rate ceiling,
replacement, and takeover. The app owns exact policy, observation, command,
actuator intent, local AI/rules, safe point, and semantic effects.

One policy drives local continuous control; do not publish policies, snapshots,
commands, or geometry per frame. The Controller must operate original mechanics
rather than parallel counters, cover a coherent useful subset including the
ordinary failure condition, clear every sustained actuator on all lease-ending
paths, and emit at least one sparse real `controllerEffect:true` outcome under
the active Host lease.

## Verify and hand off

Verify the application/Managed-Core boundary, not only manifest shape:

- execute the completed entry top-to-bottom both without AUIP and with the
  exact Host-materialized runtime scripts; a capture stub that only records
  `createManagedApp(...)` options is not a boot test;
- standalone behavior remains intact, including initial render and primary
  input binding. AUIP construction/start must not be the only path to original
  application initialization, so an adapter bootstrap error cannot blank or
  disable the standalone app;
- initialize every app-owned field read by `snapshot()` before constructing
  the Managed app: `createManagedApp(...)` validates its first snapshot
  synchronously, before `start()`;
- the declared participant controls exercise a coherent primary-loop effect;
  for a continuously controlled app, prove that effect without hidden human
  input rather than stopping at a start/menu/lifecycle receipt;
- every representative `available:true` option and primary control dimension
  reaches the real handler with the exact payload;
- every representative lifecycle state preserves the same complete
  `choice/v1.actionTypes` family (or the same compact root `action`), so omitted
  or `available:false` options cannot become apparently legal through static
  capability prose;
- every `action_availability/v1` surface preserves its complete `actionTypes`
  family, and unavailable governed actions are absent from
  `availableActionTypes` before role choice;
- an absent API must never be hidden by prose;
- state-dependent legality is readable before dispatch;
- after real timers/physics advance below the background checkpoint interval,
  the next published available action does not fail with
  `state_changed_without_checkpoint`;
- receipts, effects, events, snapshot shape/size, role binding, and reusable
  lifecycle transitions are correct;
- one post-result choice works while the AppSession remains active, and only a
  true app-owned conclusion emits a terminal event;
- Controller tests, when applicable, use the shared Core and prove real
  movement/combat/pickup or equivalent mechanics, 120 stable frames without
  checkpoint noise, and no drift after revoke/replacement/expiry. Drive the
  exact production mechanics (or one production module shared by the app and
  test), never a simplified surrogate whose physics, actuator magnitude, or
  timing differs. For coupled numeric policies such as target plus tolerance,
  test an interior value and feasible boundary combinations; reject a
  combination before commit when the real controller cannot preserve the
  declared objective or safety invariant.

Author tests may remain Attempt-private. The Host owns real transport, attach
tickets, launch, permission, bundle integrity, and post-validation. Report only
the integration and validations that actually passed; a request or policy
receipt alone is not execution proof.
