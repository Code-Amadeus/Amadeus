# AUIP v0 Reactive Controller interface

Read this complete reference only when the application's response horizon or
effect lifetime requires a Reactive Controller.

## Profile and ownership

Declare the optional profile only for a bounded app-local policy executor:

```json
{
  "controller": {
    "policyActions": ["vehicle.set_navigation_policy"],
    "leaseDurationMs": 30000,
    "maxActionRateHz": 12,
    "takeover": "immediate"
  }
}
```

Both `participant` and `spectator` are required. Every policy action exists,
has a non-empty object `inputSchema` closed by `additionalProperties:false`,
and receives its exact unchanged payload. An accepted receipt activates the
Host-supplied lease beside that payload; rejection activates nothing.

The Host/Core own principal, lease, generation, expiry, rate ceiling,
replacement, and takeover. The app owns exact policy, observation, command,
actuator intent, local rule system/AI, safe point, and semantic effects. There
is no shared attack/follow/hold enum or command/args/data payload.

## Exact callback ABI

Define one synchronous callback object and reuse that same object in the app and
author test:

```js
const controllerCallbacks = {
  observe: () => app.controllerObservation(),
  decide: ({observation, policy, context}) =>
    appPolicy.decide(observation, policy),
  apply: ({command, context}) => app.applyControllerCommand(command),
  clearIntent: ({reason, policy}) => app.clearControllerIntent(reason),
  policySummary: ({policy}) => app.describePolicy(policy),
  onStatus: status => app.renderControllerStatus(status),
};

const auip = AmadeusAUIP.createManagedApp({
  manifest,
  snapshot: ({controller}) => ({
    /* bounded app state */,
    controller: AmadeusAUIPSituations.controllerSituation(controller),
  }),
  actions: { /* policy plus any Decision actions */ },
  controller: controllerCallbacks,
});
```

All callbacks are synchronous; policy, observation, command, and effects are
frozen exact JSON. `apply` returns `{accepted:true,effects:{...}}` only after
application mechanics accept the command. `onStatus` is presentation-only.

`clearIntent` synchronously neutralizes every sustained actuator on
replacement, immediate revoke, expiry, callback failure, and completed
safe-point takeover. Clear actuators, not durable policy configuration: a
replacement may already have committed its new policy when the old lease ends.
Failure blocks observably instead of leaving old input active.

## Application cadence

Call `auip.controllerStep()` from an app-owned bounded decision loop. The Web
binding supplies Unix epoch time; do not pass `requestAnimationFrame` time or
`performance.now()`. Calling without an active lease safely returns
`controller_inactive`.

The Web binding also reconciles lease time before a managed Host action, local
commit, or checkpoint. This is a governance safety net for a phase that paused
or stopped its ordinary loop: an elapsed lease clears sustained intent before
the next semantic snapshot and never executes one extra Controller command.
It does not replace `controllerStep()` while the application is running.

For continuous applications, the decision loop updates app-specific sustained
intent while the existing render/physics loop consumes it every frame.
`maxActionRateHz` is a ceiling, not a desired frame rate or Host timer. One
low-frequency policy drives local commands. Publish phase, legality, visible
qualitative bands, and sparse outcomes—not frames, raw observations, commands,
aim/actuator intent, or continuous geometry.

The Controller must operate original mechanics, not parallel counters. Its
useful supported subset must include the ordinary failure condition; otherwise
state the partial boundary in `interactionSummary` or remain spectator-only.
Stable follow targets use app-owned identity and availability, never nearest
entity inference; target loss and every takeover/lease-ending path clear or
block the actuator.

Approved policy may rebind to the latest telemetry revision only while policy
meaning/legality and decision generation remain stable. Ordinary Decision,
unleased, or future-revision actions receive no exception.

## Sparse outcomes and governance

Every profile declares at least one event with `controllerEffect:true` and emits
it only after real application-local execution under the current lease. The
first meaningful result may be `importance:"important"` for the post-ledger
Narrator fast lane; routine effects remain sparse. A policy receipt, apply
counter, DOM label, or locally invented boolean is not a mechanics outcome.

The wrapper supplies synthetic active governance status to `snapshot` and
rejects an adapter that discards it. Pass it through exactly:

```js
snapshot: ({controller}) => ({
  controller: AmadeusAUIPSituations.controllerSituation(controller),
  /* app state */
})
```

Do not replace it with an adapter-local idle object. For
`takeover:"safe_point"`, call `auip.acknowledgeControllerSafePoint()` only after
the real app reaches its safe point. Internal Controller commands are app
execution, not self-authored `actor:"kurisu"` events.

## Transport-free author test

Instantiate the shipped Core with the exact callback object; do not invoke
callbacks directly or fake `controllerStep`:

```js
const controllerModule = await import("./sdk/auip-core/controller-v0.js");
const controllerApi = controllerModule.default || globalThis.AmadeusAUIPController;
const core = controllerApi.createReactiveController(controllerCallbacks);
const lease = {
  lease_id: "author-test-1",
  principal: "kurisu",
  executor: "app_controller",
  issued_at_ms: 1000,
  expires_at_ms: 31000,
  max_action_rate_hz: 10,
  takeover: "immediate", // or "safe_point"
  generation: 1,
  policy_revision: 1,
};
core.activate({
  lease,
  actionType: "namespace.set_policy",
  policy: exactPolicyPayload,
  policySummary: "bounded test policy",
});
core.step({nowMs: 1100});
core.requestRevoke({
  leaseId: lease.lease_id,
  generation: lease.generation,
  nowMs: 1200,
  reason: "author_test_stop",
});
// Safe-point takeover only, after the app's real safe point:
core.acknowledgeSafePoint({
  leaseId: lease.lease_id,
  generation: lease.generation,
  nowMs: 1300,
});
core.status();
```

Test every policy dimension through that Core and the exact production
mechanics. Extract one mechanics module for both the app and test when a browser
shell cannot be driven directly; do not substitute a smaller model whose
physics, actuator magnitude, decision cadence, or timing differs. Prove
persistent finite movement/entity state, real projectile/collision or
equivalent causality, pickup/score/health or equivalent objective effects, and
the ordinary failure response. For coupled numeric fields such as target plus
tolerance, exercise an interior value and feasible boundary combinations under
the real loop. The action handler must reject before commit any combination the
controller cannot keep within its declared objective or safety invariant, and
the action description must state that relationship so the Participant can
choose it. Run 120 stable render/physics frames plus several decisions and prove
frames/local commands alone do not checkpoint. Prove replacement, revoke,
expiry, callback failure, and safe-point completion clear intent with no later
drift.
