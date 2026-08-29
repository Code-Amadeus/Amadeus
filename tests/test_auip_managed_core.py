from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "sdk" / "auip-core" / "managed-v0.js"


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if not node:
        print("ok: AUIP managed core test skipped (node unavailable)")
        return
    result = subprocess.run(
        [node, "-e", script, str(CORE)],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="")
    assert result.returncode == 0


def test_managed_core_has_no_web_or_host_identity_dependency() -> None:
    source = CORE.read_text(encoding="utf-8")
    for forbidden in (
        "WebSocket",
        "document.",
        "window.",
        "attachTicket",
        "bridgeToken",
        "appSessionId",
    ):
        assert forbidden not in source


def test_managed_core_owns_revision_and_keeps_payload_app_specific() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {createManagedCore} = require(process.argv[1]);
const manifest = {
  schema:'amadeus.auip/v0',
  app:{id:'counter', title:'Counter', version:'0.1.0'},
  events:{'counter.changed':{beat:true}},
  actions:{
    'counter.increment':{
      description:'Increment by the exact payload amount.',
      risk:'local_execution',
      inputSchema:{
        type:'object',
        properties:{amount:{type:'integer'}, label:{type:'string'}},
        required:['amount', 'label'],
        additionalProperties:false,
      },
    },
  },
  stances:['participant'],
};
let value = 0;
let handlerCalls = 0;
let observedPayload = null;
const core = createManagedCore({
  manifest,
  snapshot: () => ({value}),
  actions:{
    'counter.increment': (payload, tx) => {
      handlerCalls += 1;
      observedPayload = payload;
      return tx.commit({
        mutate: () => {
          value += payload.amount;
          return {ok:true, amount:payload.amount, label:payload.label};
        },
        effects: ({result}) => ({increment:{amount:result.amount, label:result.label}}),
        events: ({state}) => [{
          type:'counter.changed',
          actor:'kurisu',
          payload:{value:state.value, label:payload.label},
        }],
      });
    },
  },
});

const payload = {amount:2, label:'two exact steps'};
const accepted = core.dispatchAction({
  action_id:'action-1',
  type:'counter.increment',
  expected_revision:0,
  payload,
});
assert.equal(accepted.accepted, true);
assert.equal(accepted.revision, 1);
assert.deepEqual(observedPayload, payload);
assert.equal(Object.isFrozen(observedPayload), true);
assert.deepEqual(accepted.effects, {increment:{amount:2, label:'two exact steps'}});
assert.deepEqual(accepted.events[0].payload, {value:2, label:'two exact steps'});

value = 99;
assert.deepEqual(accepted.state, {value:2}, 'commit envelope must be frozen at commit time');
assert.equal(Object.isFrozen(accepted.state), true);

const stale = core.dispatchAction({
  type:'counter.increment',
  expected_revision:0,
  payload:{amount:7, label:'stale'},
});
assert.equal(stale.accepted, false);
assert.equal(stale.code, 'stale_action_revision');
assert.equal(handlerCalls, 1, 'stale action must stop before the app handler');

const local = core.commitLocal({
  actor:'user',
  mutate:() => { value += 1; return {ok:true}; },
  effects:{source:'local button'},
  events:[{type:'counter.changed', payload:{value:100}}],
});
assert.equal(local.revision, 2);
assert.deepEqual(local.state, {value:100});
console.log('ok: managed core owns revision without abstracting action payloads');
"""
    )


def test_managed_core_fails_closed_on_contract_drift_and_async_commit_code() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {createManagedCore} = require(process.argv[1]);
const base = {
  schema:'amadeus.auip/v0',
  app:{id:'strict', title:'Strict', version:'0.1.0'},
  events:{'app.changed':{}},
  actions:{'app.change':{description:'Change.', risk:'local_execution'}},
  stances:['participant'],
};

assert.throws(
  () => createManagedCore({manifest:base, snapshot:()=>({}), actions:{}}),
  error => error.code === 'manifest_handler_mismatch'
);
assert.throws(
  () => createManagedCore({
    manifest:base,
    snapshot:()=>({}),
    actions:{'app.change':async (_payload, tx)=>tx.reject('no')},
  }),
  error => error.code === 'async_action_handler_not_allowed'
);
assert.throws(
  () => createManagedCore({
    manifest:base,
    snapshot:async ()=>({}),
    actions:{'app.change':(_payload, tx)=>tx.reject('no')},
  }),
  error => error.code === 'async_snapshot_not_allowed'
);

let state = {value:0};
const diagnostics = [];
const core = createManagedCore({
  manifest:base,
  projectionBudgetChars:8,
  onDiagnostic:item=>diagnostics.push(item),
  snapshot:()=>state,
  actions:{'app.change':(_payload, tx)=>tx.reject('not needed')},
});
assert.equal(diagnostics[0].code, 'projection_budget_exceeded');
assert.equal(core.revision(), 0);
assert.throws(
  () => core.commitLocal({actor:'kurisu', events:[]}),
  error => error.code === 'local_kurisu_authority_forbidden'
);
console.log('ok: managed core makes drift and async commit code observable');
"""
    )


def test_managed_core_rejects_state_drift_before_a_low_frequency_action() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {createManagedCore} = require(process.argv[1]);
const manifest = {
  schema:'amadeus.auip/v0',
  app:{id:'reactive-scalar', title:'Reactive scalar', version:'0.1.0'},
  events:{'app.checkpoint':{beat:true}},
  actions:{'app.adjust':{description:'Adjust one stable value.', risk:'local_execution'}},
  stances:['participant'],
};
let value = 85;
let handlerCalls = 0;
const diagnostics = [];
const core = createManagedCore({
  manifest,
  snapshot:()=>({value}),
  onDiagnostic:item=>diagnostics.push(item),
  actions:{
    'app.adjust':(_payload, tx)=>{
      handlerCalls += 1;
      return tx.commit({mutate:()=>{value -= 5;}, events:[]});
    },
  },
});

value = 92; // timer/external mechanics changed action-relevant state privately
const rejected = core.dispatchAction({
  type:'app.adjust', expected_revision:0, payload:{},
});
assert.equal(rejected.accepted, false);
assert.equal(rejected.code, 'state_changed_without_checkpoint');
assert.equal(handlerCalls, 0, 'drift must stop before application mutation');
assert.equal(diagnostics.at(-1).code, 'state_changed_without_checkpoint');

const checkpoint = core.checkpoint({
  actor:'app',
  events:[{type:'app.checkpoint', payload:{value}}],
});
assert.equal(checkpoint.revision, 1);
assert.deepEqual(checkpoint.state, {value:92});
const accepted = core.dispatchAction({
  type:'app.adjust', expected_revision:1, payload:{},
});
assert.equal(accepted.accepted, true);
assert.equal(accepted.revision, 2);
assert.deepEqual(accepted.state, {value:87});
assert.equal(handlerCalls, 1);
console.log('ok: low-frequency actions reject private state drift until checkpointed');
"""
    )


def test_managed_core_changed_checkpoint_suppresses_only_unchanged_projections() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {createManagedCore} = require(process.argv[1]);
const manifest = {
  schema:'amadeus.auip/v0', app:{id:'cadenced-core'},
  events:{'app.observed':{beat:false}}, actions:{}, stances:['spectator'],
};
let phase = 'paused';
const core = createManagedCore({
  manifest,
  snapshot:context=>({phase, projectedRevision:context.revision}),
  actions:{},
});

const unchanged = core.checkpointIfChanged({actor:'app', events:[]});
assert.equal(unchanged.committed, false);
assert.equal(unchanged.code, 'checkpoint_unchanged');
assert.equal(core.revision(), 0);

phase = 'playing';
const changed = core.checkpointIfChanged({actor:'app', events:[]});
assert.equal(changed.committed, true);
assert.equal(changed.revision, 1);
assert.deepEqual(changed.state, {phase:'playing', projectedRevision:1});

const stableAgain = core.checkpointIfChanged({actor:'app', events:[]});
assert.equal(stableAgain.committed, false);
assert.equal(core.revision(), 1);

const explicitEvent = core.checkpoint({
  actor:'app',
  events:[{type:'app.observed', actor:'app', payload:{phase}}],
});
assert.equal(explicitEvent.committed, true);
assert.equal(explicitEvent.revision, 2);
console.log('ok: changed checkpoints suppress only background projection no-ops');
"""
    )


def test_post_mutation_projection_failure_poisoned_core_cannot_claim_more_actions() -> None:
    _run_node(
        r"""
const assert = require('assert');
const {createManagedCore} = require(process.argv[1]);
let value = 0;
let snapshotFails = false;
const manifest = {
  schema:'amadeus.auip/v0', app:{id:'poison'}, events:{'app.changed':{}},
  actions:{'app.change':{description:'Change.', risk:'local_execution'}},
  stances:['participant'],
};
const core = createManagedCore({
  manifest,
  snapshot:()=>{
    if (snapshotFails) throw new Error('projection unavailable');
    return {value};
  },
  actions:{
    'app.change':(_payload, tx)=>tx.commit({
      mutate:()=>{ value += 1; snapshotFails = true; return {ok:true}; },
      events:[],
    }),
  },
});
assert.throws(
  () => core.dispatchAction({type:'app.change', expected_revision:0, payload:{}}),
  error => error.code === 'post_mutation_projection_failed' && error.afterMutation === true
);
assert.equal(value, 1);
assert.equal(core.healthy(), false);
assert.throws(() => core.snapshot(), error => error.code === 'managed_core_desynchronized');
console.log('ok: an unprojectable committed mutation fails closed');
"""
    )


if __name__ == "__main__":
    test_managed_core_has_no_web_or_host_identity_dependency()
    test_managed_core_owns_revision_and_keeps_payload_app_specific()
    test_managed_core_fails_closed_on_contract_drift_and_async_commit_code()
    test_managed_core_rejects_state_drift_before_a_low_frequency_action()
    test_post_mutation_projection_failure_poisoned_core_cannot_claim_more_actions()
