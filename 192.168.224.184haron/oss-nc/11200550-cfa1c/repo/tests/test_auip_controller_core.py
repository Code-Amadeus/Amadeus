from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "sdk" / "auip-core" / "controller-v0.js"


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if not node:
        print("ok: AUIP Controller Core test skipped (node unavailable)")
        return
    result = subprocess.run(
        [node, "-e", script, str(CORE)],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_controller_preserves_app_policy_and_enforces_rate_ceiling() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {createReactiveController} = require(process.argv[1]);
const sourcePolicy = {
  behavior:'orbit_and_screen',
  subject:{entityId:'blue-7'},
  clearanceMeters:18,
};
const applied = [];
const controller = createReactiveController({
  observe:()=>({threat:{entityId:'red-2'}, rangeMeters:42}),
  decide:({observation,policy,context})=>{
    assert.equal(Object.isFrozen(observation.threat), true);
    assert.equal(Object.isFrozen(policy.subject), true);
    assert.equal(context.lease.executor, 'app_controller');
    return {
      verb:policy.behavior,
      targetId:policy.subject.entityId,
      avoidId:observation.threat.entityId,
    };
  },
  apply:({command,policy})=>{
    assert.equal(Object.isFrozen(command), true);
    assert.equal(Object.isFrozen(policy), true);
    applied.push(command);
    return {accepted:true,effects:{queued:true}};
  },
  clearIntent:()=>{},
});
const activated = controller.activate({
  lease:{
    lease_id:'lease-1', principal:'kurisu', executor:'app_controller',
    generation:1, policy_revision:4, issued_at_ms:1000,
    expires_at_ms:5000, max_action_rate_hz:10, takeover:'immediate',
  },
  actionType:'vehicle.set_policy',
  policy:sourcePolicy,
  policySummary:'Orbit blue-7 while screening threats',
});
sourcePolicy.subject.entityId = 'tampered';
assert.equal(activated.accepted, true);
assert.deepEqual(controller.status(), {
  kind:'controller/v1', status:'active', policyRevision:4,
  policyAction:'vehicle.set_policy',
  policySummary:'Orbit blue-7 while screening threats',
});
const first = controller.step({nowMs:1000});
assert.equal(first.accepted, true);
assert.deepEqual(first.command, {
  verb:'orbit_and_screen', targetId:'blue-7', avoidId:'red-2',
});
assert.deepEqual(first.effects, {queued:true});
const limited = controller.step({nowMs:1050});
assert.equal(limited.accepted, false);
assert.equal(limited.code, 'controller_rate_limited');
assert.equal(applied.length, 1);
const second = controller.step({nowMs:1100});
assert.equal(second.accepted, true);
assert.equal(second.commandSequence, 2);
assert.equal(applied.length, 2);
"""
    )


def test_controller_requires_an_explicit_sustained_intent_cleanup_boundary() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {createReactiveController} = require(process.argv[1]);
assert.throws(
  ()=>createReactiveController({
    observe:()=>({}),
    decide:()=>({}),
    apply:()=>({accepted:true,effects:{}}),
  }),
  error=>error.code === 'controller_callback_required'
    && error.detail === 'clearIntent',
);
"""
    )


def test_controller_replacement_expiry_and_immediate_revoke_are_generation_safe() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {createReactiveController} = require(process.argv[1]);
let calls = 0;
const releases = [];
const controller = createReactiveController({
  observe:()=>({ready:true}),
  decide:()=>({operation:'pulse'}),
  apply:()=>{calls += 1; return {accepted:true,effects:{}};},
  clearIntent:context=>releases.push(context),
});
function activation(id,generation,expiresAt) {
  return {
    lease:{
      lease_id:id, principal:'kurisu', executor:'app_controller',
      generation, policy_revision:generation, issued_at_ms:0,
      expires_at_ms:expiresAt, max_action_rate_hz:20, takeover:'immediate',
    },
    actionType:'system.set_policy',
    policy:{strategy:'pulse_when_ready'},
    policySummary:'Pulse when ready',
  };
}
assert.equal(controller.activate(activation('lease-1',1,1000)).accepted, true);
assert.equal(controller.canActivate(activation('lease-1',1,1000).lease).accepted, false);
assert.equal(controller.canActivate(activation('lease-2',2,2000).lease).accepted, true);
const replacement = controller.activate(activation('lease-2',2,2000));
assert.equal(replacement.code, 'controller_policy_replaced');
assert.equal(replacement.replacedLeaseId, 'lease-1');
assert.equal(releases.length, 1);
assert.equal(releases[0].reason, 'replaced');
assert.equal(releases[0].lease.lease_id, 'lease-1');
const stale = controller.activate(activation('lease-old',1,3000));
assert.equal(stale.accepted, false);
assert.equal(stale.code, 'stale_controller_lease');
const staleRevoke = controller.requestRevoke({
  leaseId:'lease-1', generation:1, nowMs:100, reason:'old request',
});
assert.equal(staleRevoke.accepted, false);
assert.equal(controller.step({nowMs:100}).accepted, true);
const revoked = controller.requestRevoke({
  leaseId:'lease-2', generation:2, nowMs:150, reason:'user takeover',
});
assert.equal(revoked.code, 'controller_revoked');
assert.equal(releases.at(-1).reason, 'user takeover');
assert.equal(controller.step({nowMs:200}).code, 'controller_inactive');

assert.equal(controller.activate(activation('lease-3',3,300)).accepted, true);
const expired = controller.step({nowMs:300});
assert.equal(expired.code, 'controller_lease_expired');
assert.equal(controller.status().status, 'idle');
assert.equal(controller.status().reason, 'expired');
assert.equal(releases.at(-1).reason, 'expired');
assert.equal(calls, 1);
"""
    )


def test_safe_point_takeover_stops_new_commands_until_app_acknowledges() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {createReactiveController} = require(process.argv[1]);
let calls = 0;
const controller = createReactiveController({
  observe:()=>({phase:'moving'}),
  decide:()=>({throttle:0}),
  apply:()=>{calls += 1; return {accepted:true,effects:{}};},
  clearIntent:()=>{calls += 100;},
});
controller.activate({
  lease:{
    lease_id:'safe-1', principal:'kurisu', executor:'app_controller',
    generation:8, policy_revision:2, issued_at_ms:0,
    expires_at_ms:5000, max_action_rate_hz:30, takeover:'safe_point',
  },
  actionType:'vehicle.set_navigation_policy',
  policy:{destination:{dock:'A-12'}, arrival:'soft_capture'},
  policySummary:'Dock at A-12 with soft capture',
});
const stopping = controller.requestRevoke({
  leaseId:'safe-1', generation:8, nowMs:100, reason:'user takeover',
});
assert.equal(stopping.code, 'controller_safe_point_requested');
assert.equal(controller.status().status, 'stopping');
assert.equal(controller.step({nowMs:120}).code, 'controller_stopping');
assert.equal(calls, 0);
const wrong = controller.acknowledgeSafePoint({
  leaseId:'wrong', generation:8, nowMs:150,
});
assert.equal(wrong.code, 'stale_controller_lease');
const stopped = controller.acknowledgeSafePoint({
  leaseId:'safe-1', generation:8, nowMs:160,
});
assert.equal(stopped.code, 'controller_safe_point_reached');
assert.equal(controller.status().status, 'idle');
assert.equal(calls, 100);
"""
    )


def test_controller_reconcile_expires_a_paused_safe_point_without_applying() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {createReactiveController} = require(process.argv[1]);
let applied = 0;
const releases = [];
const controller = createReactiveController({
  observe:()=>({phase:'paused'}),
  decide:()=>({throttle:1}),
  apply:()=>{applied += 1; return {accepted:true,effects:{}};},
  clearIntent:context=>releases.push(context.reason),
});
controller.activate({
  lease:{
    lease_id:'paused-1', principal:'kurisu', executor:'app_controller',
    generation:1, policy_revision:1, issued_at_ms:0,
    expires_at_ms:200, max_action_rate_hz:30, takeover:'safe_point',
  },
  actionType:'vehicle.set_navigation_policy',
  policy:{destination:'A'},
  policySummary:'Navigate to A',
});
const current = controller.reconcile({nowMs:100});
assert.equal(current.accepted,true);
assert.equal(current.changed,false);
assert.equal(applied,0);
controller.requestRevoke({
  leaseId:'paused-1', generation:1, nowMs:120, reason:'safe point pending',
});
assert.equal(controller.status().status,'stopping');
const expired = controller.reconcile({nowMs:200});
assert.equal(expired.accepted,true);
assert.equal(expired.changed,true);
assert.equal(expired.code,'controller_lease_expired');
assert.equal(controller.status().status,'idle');
assert.equal(controller.status().reason,'expired');
assert.deepEqual(releases,['expired']);
assert.equal(applied,0);
"""
    )


def test_controller_blocks_after_callback_failure_without_claiming_execution() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {createReactiveController} = require(process.argv[1]);
let applied = 0;
const statuses = [];
const controller = createReactiveController({
  observe:()=>({event:'urgent'}),
  decide:()=>{throw new Error('missing app rule');},
  apply:()=>{applied += 1; return {accepted:true,effects:{}};},
  clearIntent:()=>statuses.push({released:true}),
  onStatus:value=>statuses.push(value),
});
controller.activate({
  lease:{
    lease_id:'blocked-1', principal:'kurisu', executor:'app_controller',
    generation:1, policy_revision:1, issued_at_ms:0,
    expires_at_ms:5000, max_action_rate_hz:12, takeover:'immediate',
  },
  actionType:'alarm.set_response_policy',
  policy:{onDetection:{severityAtLeast:4,response:'isolate_zone'}},
  policySummary:'Isolate zones for severity 4 or higher',
});
const failed = controller.step({nowMs:100});
assert.equal(failed.accepted, false);
assert.equal(failed.code, 'controller_decision_failed');
assert.equal(applied, 0);
assert.equal(controller.status().status, 'blocked');
assert.equal(controller.step({nowMs:200}).code, 'controller_blocked');
assert.equal(statuses.at(-1).status, 'blocked');
"""
    )


def test_controller_fails_closed_when_sustained_intent_cannot_be_cleared() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {createReactiveController} = require(process.argv[1]);
const controller = createReactiveController({
  observe:()=>({ready:true}),
  decide:()=>({throttle:1}),
  apply:()=>({accepted:true,effects:{}}),
  clearIntent:()=>{throw new Error('actuator unavailable');},
});
controller.activate({
  lease:{
    lease_id:'release-1', principal:'kurisu', executor:'app_controller',
    generation:1, policy_revision:1, issued_at_ms:0,
    expires_at_ms:5000, max_action_rate_hz:12, takeover:'immediate',
  },
  actionType:'vehicle.set_policy',
  policy:{mode:'follow'},
  policySummary:'Follow the player',
});
assert.equal(controller.step({nowMs:100}).accepted, true);
const revoked = controller.requestRevoke({
  leaseId:'release-1', generation:1, nowMs:200, reason:'observe',
});
assert.equal(revoked.accepted, false);
assert.equal(revoked.code, 'controller_clear_intent_failed');
assert.equal(controller.status().status, 'blocked');
assert.equal(controller.step({nowMs:300}).code, 'controller_blocked');
"""
    )
