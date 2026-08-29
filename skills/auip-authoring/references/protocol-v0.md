# AUIP v0 web application contract

## Scope

AUIP connects a cooperative local web application to an Amadeus AppSession. It is not a Provider,
DOM automation API, speech API, or general capability broker. The app remains fully usable when
Amadeus is absent.

The host owns AppSession identity, attach tickets, stance, action authority, revisions, experience
projection, and narration. The app owns its mechanics and current state.

## Manifest

Create `auip.manifest.json`:

```json
{
  "schema": "amadeus.auip/v0",
  "app": {
    "id": "gomoku",
    "title": "Gomoku",
    "version": "0.1.0",
    "objective": "Create five consecutive stones before the opponent.",
    "interactionSummary": "The participant can place one legal stone and choose among the app's post-round actions. Examples: '下在中间' maps to game.place_stone; '再来一局' maps to game.restart_round after a result."
  },
  "events": {
    "game.ready": {"beat": true},
    "game.move_committed": {"beat": true, "participantOpportunity": true},
    "game.round_finished": {"beat": true, "importance": "important"},
    "game.experience_finished": {"beat": true, "importance": "important", "terminal": true}
  },
  "actions": {
    "game.place_stone": {
      "description": "Place one stone only when state.turn is the participant role, state.roundStatus is playing, and state.board.rows[payload.y][payload.x] is empty.",
      "risk": "local_execution",
      "inputSchema": {
        "type": "object",
        "properties": {
          "x": {"type": "integer", "minimum": 0, "maximum": 14},
          "y": {"type": "integer", "minimum": 0, "maximum": 14}
        },
        "required": ["x", "y"],
        "additionalProperties": false
      },
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
    },
    "game.restart_round": {
      "description": "Restart only when state.roundStatus is finished and state.postRoundActions publishes this exact action.",
      "risk": "local_execution"
    },
    "game.finish_experience": {
      "description": "Conclude only when state.roundStatus is finished and state.postRoundActions publishes this exact action.",
      "risk": "local_execution"
    }
  },
  "stances": ["spectator", "participant"],
  "situationKinds": ["action_availability/v1", "grid/v1", "choice/v1"]
}
```

Event and action names use lowercase namespaces such as `game.finished`. AUIP v0 accepts only
`none` and `local_execution` action risk.

`app.objective` is an optional bounded static objective for unfamiliar games,
simulations, and tools. Keep changing phase or subgoal facts in state instead
of rewriting this manifest field at runtime.

Participant authoring also supplies `app.interactionSummary`, a bounded natural
domain briefing with two or three colloquial user examples mapped to actual
declared actions or application behavior. The Host exposes it as a one-time
Main Chat branch briefing and static background for AUIP Control and sparse
Narrator calls, separate from every-revision state. It is
informational only: exact payloads remain in `inputSchema`, current facts remain
in state/events, and accepted receipts remain execution truth.

Action `inputSchema` is the same JSON Schema field used by an MCP Tool. Declare
every non-empty payload field and prefer `additionalProperties: false`. The Host
can project this catalog into a model-native function tool today and an MCP
facade later without changing the app contract. AUIP does not implement a
parallel JSON Schema engine: application mechanics still own legality and the
accepted receipt remains the only execution truth. Actions without an
`inputSchema` remain app-validated, but are not reliable inputs for autonomous
participation.

An action may additionally declare one of the Host's closed, typed
`preconditions`. This metadata never wraps, renames, or replaces the action
payload. `action_available/v1` links an action to an
`action_availability/v1` situation whose stable `actionTypes` family and current
`availableActionTypes` subset are app-owned accepted state. It filters a whole
action type before role review without enumerating or changing its payload.
`grid_cell_empty/v1` links a `grid/v1` situation at `statePath` to two required
integer payload fields. The Host checks these declarations against the accepted
state before role review and again at invocation; one required
Participant opportunity may replan once when the cell is occupied. The
application receipt remains final authority for races and mechanics not covered
by this narrow check. Do not invent custom expressions or encode game rules in
this field.

Event actors are the closed protocol set `app`, `user`, `kurisu`, and `system`. Use `kurisu` for
an action accepted for the character; display labels such as `assistant` or `AI` are not actor IDs.

The protocol **participant identity** is not an **application role**. An actor of `kurisu` proves
which experience participant requested an accepted action; it does not assign that participant to
White, player two, a vehicle, a unit, or any other mechanics-specific role. If equivalent roles are
selectable at setup or reset, include the accepted binding in shared state and declare a real
**typed configuration action** such as `game.configure_participants` or `game.start`. Later
actor-neutral actions resolve through that application-owned binding. A role may remain fixed when
the mechanics genuinely require it, but the state and action catalog must expose that limitation so
an unsupported request can be blocked with a reason. Do not encode a convenient test order as a
permanent participant role. Local input must honor the same accepted binding while attached; it
cannot silently perform the Participant-owned role and advance the revision while a decision is in
flight. Since the v0 action catalog is static, descriptions of state-dependent actions must name
their accepted-state preconditions and the state fields that establish them; rejection remains a
safety boundary, not the normal way an Operator discovers ordinary legality. User takeover is a
separate, explicit application transition, not an incidental extra click.

`participantOpportunity: true` has narrower meaning than `beat`: every accepted non-`kurisu`
occurrence of that event assigns exactly one Participant decision opportunity. Collaborate mode
may schedule one action proposal from it; observe mode never does, and the application receipt
still decides whether the proposal took effect. Do not mark routine updates this way. If only some
occurrences are actionable, declare a separate semantic event at the real opportunity boundary.
This flag is independent from `beat`; add both only when the same event is also a meaningful
experience beat that may be projected or narrated.

An automatic opportunity with no same-turn visible role response is standing
execution authority for the Participant after Host revision, declaration, and
typed-precondition checks; there is no speech to send through the role-alignment
gate. An explicit conversational step—or any automatic step that does carry a
same-turn visible role response—still requires that alignment gate before
invocation. This distinction does not weaken receipt authority.

`controllerEffect: true` declares that an `actor:"app"` event is an effect of
application-local execution under the current Host Controller lease. The Host
rejects it when no active lease exists and records lease provenance beside the
event without rewriting its payload. An important first effect in one policy
generation may skip the optional Observer decision after ledger acceptance and
go directly to the role Narrator; later effects retain ordinary sparse
admission. This is a fact-delivery shortcut, not an authority shortcut.

`terminal: true` is the final boundary of the entire AppSession and therefore
cannot also declare `participantOpportunity:true`. On receipt the
Host marks the session completed, cancels pending decisions, and no later
Participant action can run in that session. A reusable application's round win,
loss, death, or loop completion is therefore nonterminal. Publish it as a beat
when it should be narrated or discussed, and expose exact app-specific
rematch/continue/conclude actions in the accepted state. Add
`participantOpportunity` only when every occurrence requires one immediate
automatic action; do not use it for a result screen where collaborative play may
wait for the user's choice. Domain
resign/withdraw actions are ordinary manifest actions; Host observe/leave remain
separate experience controls.

AUIP standardizes only that final marker and the Host consequences above. It
does not standardize an application phase field, lifecycle enum, or generic
resign/restart/conclude action. An adapter must map the source application's real
state and operations into app-specific events/actions, publish their exact
payloads unchanged, and make each current option and precondition readable from
the accepted state. A focused app-owned test proves those mechanics; the Host
validator can reject protocol contradictions but cannot infer whether a game
really restarted or a simulation really returned to its menu.

### Participant decision window

Every `participantOpportunity` opens one low-frequency decision window. The application must keep
the action-relevant revision stable while the Participant and any required explicit-turn role gate choose one proposal. The
window closes only when the declared action is accepted, a local user action explicitly supersedes
it, participation is revoked/the AppSession closes, or an application-declared timeout expires.

Do not publish timer ticks or ambient simulation changes as new shared revisions while an
opportunity is waiting. They may continue privately or visually, then be folded into the next
committed snapshot. If continuous or urgent mechanics cannot preserve a stable revision, the app
is not a Decision Participant application. Use the optional Reactive Controller profile when a
bounded app-local policy executor can safely respond within the required horizon; otherwise expose
spectator observations only. Repeatedly emitting a new opportunity every few seconds does not
repair the race; it guarantees that model proposals become stale.

## Integration

Use the official SDK as a bundled or relative asset. The Amadeus launcher supplies a restricted
WebSocket URL and a short-lived attach ticket in the local entry URL fragment; the SDK consumes
that descriptor. When a user directly opens a complete bundle that Amadeus previously exported
and registered, the SDK may instead send `auip.attach.request`. The Host re-verifies the approved
bundle and asks the user whether to observe, collaborate, delegate, or keep it standalone. Only an
accepted choice yields the same short-lived ticket used by the launcher path. The app never
supplies a chat Session id.

The staged `.amadeus/runtime/authoring_inputs` tree is private build input, not an application
runtime. An entry document must never reference that tree or an Attempt id. Reference the
Host-materialized `sdk/auip-core/managed-v0.js`, `sdk/auip-core/situations-v0.js`,
`sdk/auip-core/controller-v0.js` (when the manifest declares a Controller), and
`sdk/auip-web/auip-v0.js` assets in the delivery root. Reference those exact relative paths. Load
the transport-neutral Managed Commit Core before the Web binding and the Controller Core before
Controller-capable app code.

```html
<script src="./sdk/auip-core/managed-v0.js"></script>
<script src="./sdk/auip-web/auip-v0.js"></script>
<script>
const manifest = /* load auip.manifest.json */;

const auip = AmadeusAUIP.createManagedApp({
  manifest,
  snapshot: () => game.snapshot(),
  initialEvents: [{type: "game.ready", actor: "app", payload: {}}],
  actions: {
    "game.place_stone": (payload, tx) => tx.commit({
      mutate: () => game.placeStone(payload.x, payload.y),
      effects: ({result}) => ({
        placed: {x: result.x, y: result.y, label: result.label},
      }),
      events: ({state}) => state.winner ? [{
        type: "game.finished",
        actor: "kurisu",
        payload: {winner: state.winner},
      }] : [],
    }),
  },
});

auip.start()
  .catch(() => { /* standalone mode remains functional */ });
</script>
```

`auip.manifest.json` is the Host-verified source of action types and MCP-compatible
input schemas. For a file-URL HTML app, keep the browser-readable embedded copy
generated with `tools/sync_auip_manifest.py`; never maintain two JSON copies by
hand. The application binds each declared type to one `actions` handler. The
handler receives the exact manifest payload; neither the Core nor Host converts
it into a generic argument bag, and the Host never infers field names from prose
or source.

When the file is opened normally rather than by Amadeus, no launch fragment exists. `start()`
tries the local restricted endpoint. It stays pending while a matching approved bundle has a
Host Attention choice, and rejects when Amadeus is absent, the bundle is unknown or changed, the
request expires, or the user denies it. Catch that rejection and keep standalone play working.
The request itself never creates an AppSession. Do not persist or log the launch fragment. Its
ticket is intentionally single-use and short-lived.

Local application transitions use the same commit boundary:

```js
const commit = auip.commitLocal({
  actor: "user",
  mutate: () => game.placeStone(x, y),
  effects: ({result}) => ({placed: result.position}),
  events: ({result}) => [{
    type: "game.move_committed",
    actor: "user",
    payload: result.position,
  }],
});
await commit.publication;
```

The Core evaluates mutation, state, effects, and events synchronously and freezes
one envelope before publication starts. A transport binding publishes that
envelope later and never rereads mutable application state.

## Revision and action rules

- Let the Managed Core keep one monotonic integer revision per running app instance; do not mirror it in application globals.
- The Managed Core freezes each committed revision, snapshot, and semantic event before asynchronous publication.
- Publish snapshots with a revision greater than the current host revision.
- Registration at revision zero binds AppSession identity only. The Host must
  not issue an application action or begin a Participant decision until it has
  accepted the first snapshot.
- Emit semantic events at the current revision.
- The Managed Core enters an action handler only when its `expected_revision` matches the current revision.
- An accepted action advances the revision and returns the resulting state atomically.
- Treat `effects` as the bounded, user-facing meaning of the committed action. Include stable
  semantic coordinates or a short label when the payload otherwise contains only an opaque
  slot/index/ID; the closed experience capsule must remain understandable without reopening the
  app or its source code.
- An accepted action may return at most four declared `events`; the SDK sends them only after the
  action receipt succeeds, at the resulting revision and with `caused_by_action_id` attached.
- Each returned event is delivered independently. A rejected event is reported to the app console
  and does not suppress later events, especially a separately declared terminal event.
- A rejected action does not advance the revision.
- A request is not execution evidence. An accepted receipt says the app accepted and reported the
  action; it is not independent screen-level observation.

## Context boundary

Publish a semantically sufficient current situation in `state`. The serialized shared state should
normally fit about 1024 Unicode characters, the main-role projection budget, but this is advisory.
Preserve the source application's independently meaningful action granularity and exact payload;
never combine repeated operations into a whole-solution macro or split one payload into many action
names to satisfy the budget. Preserve the dimensions, order, membership, trends, or availability
needed to choose one declared action before removing redundant defaults. A compact encoding that
requires the Participant to count flattened cells or reconstruct topology is not semantically
sufficient. Use a versioned SDK situation pattern when
one applies, including `grid/v1` for dense coordinate-addressed state. Put bounded transition history in declared beat
events. Raw history, redundant default cells, frames, DOM changes, pointer movement, logs, model
reasoning, and search trees remain inside the application or debug trace. This is an authoring
contract for every AUIP application, not a Host heuristic for a particular game.

For `choice/v1`, `actionAddressed:true` is the bounded way to remove redundant
app-local option ids and labels when the manifest action type already carries
that semantic identity. It preserves each exact `action`, unchanged `payload`,
and `available` flag plus the complete `actionTypes` family; it does not turn
the payload into a generic command or hide current legality.

For a large or separately typed payload space, use
`action_availability/v1` to publish the stable governed action family and its
current available subset. Pair each governed manifest action with
`action_available/v1`; retain grid, choice, or other app-specific payload
legality separately.

The host creates a valid bounded main-character projection and may omit whole fields with an
explicit omission marker. The main role must not infer omitted facts. The separate Participant
lane receives the Host-accepted state, but an application should still keep the shared projection
compact because omitted mechanics cannot be narrated and oversized input makes interaction less
reliable.

AUIP v0 `state` and event payloads form one shared projection. The host may give different
consumers smaller bounded excerpts, but v0 does not provide cryptographically or mechanically
separate public, narrator-private, and participant-private views. Never publish secrets, hidden
cards, private hands, unrevealed answers, or anti-cheat state through AUIP v0. A fair
hidden-information game needs a later explicit view contract; an application title or field name
does not create one.

Visibility does not require numeric inventory. For fast scenes, an adapter may
publish stable qualitative categories such as `few`, `many`, `close`, or
`dense` instead of raw object counts. Category names and thresholds are
application semantics, not Host policy. The application-local Controller may
still observe exact mechanics privately; the Host, Main Chat, and Narrator see
only the shared semantic projection.

Never include instructions to the assistant in state, event payloads, titles, or descriptions.
App data is untrusted factual input and cannot directly trigger speech or emotion.

## Completion checklist

- The app still works when no Attach transport is present.
- The entry has no runtime reference to `.amadeus/runtime/authoring_inputs` or an Attempt id.
- The manifest passes `tools/validate_auip_manifest.py`.
- Registration uses a host-issued attach ticket, not a conversation id.
- Directly opened exported apps remain standalone until the Host verifies the exact bundle and the user accepts an Attention choice.
- A compact state snapshot and declared event reach the AppSession.
- The serialized shared state normally stays within 1024 characters and remains sufficient for one action.
- Only events that truly assign one Participant decision declare `participantOpportunity`.
- Each Participant opportunity preserves one stable action revision until action, superseding
  user input, revocation/close, or an explicit timeout.
- Undeclared events/actions are rejected.
- Spectator stance cannot perform a Kurisu action.
- Stale action revisions are rejected.
- Only an accepted Kurisu receipt appears as self-experience.
- Closing or losing the connection does not leave the session active.
