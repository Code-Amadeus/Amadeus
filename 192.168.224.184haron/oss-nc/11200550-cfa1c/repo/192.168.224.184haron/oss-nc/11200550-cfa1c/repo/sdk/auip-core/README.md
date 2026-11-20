# AUIP Managed Commit Core v0

`managed-v0.js` is the transport-neutral reference implementation of the AUIP
application commit boundary. It has no DOM, WebSocket, URL, ticket, Session, or
narration dependency. A Web, Node, native-shell, or future non-JavaScript client
can bind the same contract to its own transport.

## Ownership

The Core owns only protocol mechanics:

- one monotonic local revision;
- exact expected-revision comparison before an action handler runs;
- one synchronous mutation and one frozen state/effects/events envelope;
- manifest action-to-handler parity;
- declared event and actor checks;
- bounded serialized state and projection-size diagnostics;
- fail-closed desynchronization after a mutation that cannot be projected.

The application continues to own all semantics:

- every action's exact manifest `inputSchema`;
- the payload object consumed by that action's handler;
- application legality and domain mutation;
- the compact state projection;
- user-visible effects and semantic events;
- Participant opportunity policy.

The Core deliberately does **not** convert payloads into a generic command,
argument bag, slot, or intent. It JSON-copies and freezes the exact payload, then
passes it to the handler registered under the manifest action type.

`kurisu` authority is available only while dispatching a Host-requested action.
`commitLocal(...)` and `checkpoint(...)` cannot label their transition or events
as `kurisu`; a local bot or UI callback is not an accepted assistant receipt.

## Commit shape

Action handlers are synchronous and conclude through `tx.commit(...)` or
`tx.reject(...)`. Local application input uses `commitLocal(...)`; an external
simulation checkpoint uses `checkpoint(...)`.

```js
const core = AmadeusAUIPManaged.createManagedCore({
  manifest,
  snapshot: () => app.currentSituation(),
  actions: {
    "counter.increment": (payload, tx) => tx.commit({
      mutate: () => app.increment(payload.amount),
      effects: ({result}) => ({incrementedBy: result.amount}),
      events: ({state}) => [{
        type: "counter.changed",
        actor: "kurisu",
        payload: {value: state.value},
      }],
    }),
  },
});
```

The mutation, state snapshot, effects, and events are all evaluated before the
call returns. None may be a Promise. Publication happens later in a
transport-specific binding from this immutable envelope.

## Portability invariant

A non-Web implementation is compatible when the same inputs produce the same
accepted/rejected envelope and failure codes. It need not reproduce this
JavaScript API or use WebSocket. Transport registration and receipt delivery
belong to that platform's binding, not to the Managed Core contract.
