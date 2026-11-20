from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "sdk" / "auip-web" / "auip-v0.js"


def test_sdk_registers_publishes_and_resolves_actions() -> None:
    node = shutil.which("node")
    if not node:
        print("ok: AUIP Web SDK test skipped (node unavailable)")
        return
    script = r"""
const assert = require('assert');
const sdk = require(process.argv[1]);
const requests = [];
let eventListener = null;
const transport = {
  async request(method, params) {
    requests.push({method, params});
    if (method === 'auip.register') return {ok:true, app_session_id:'app_1', bridge_token:'secret', revision:0};
    if (method === 'auip.state.publish') return {ok:true, revision:params.revision};
    if (method === 'auip.action.result') return {ok:true, revision:params.resulting_revision};
    if (method === 'auip.event.publish') return {ok:true, revision:params.revision};
    if (method === 'auip.session.close') return {ok:true};
    throw new Error(method);
  },
  onEvent(listener) { eventListener = listener; return () => { eventListener = null; }; },
  close() {},
};
let state = {value: 0};
const app = sdk.createApp({
  manifest: {schema:'amadeus.auip/v0'},
  attachTicket: 'ticket-1',
  transport,
  getState: () => state,
  onAction: async (action) => {
    assert.equal(action.type, 'counter.increment');
    state = {value: state.value + 1};
    return {
      accepted:true,
      revision:2,
      state,
      effects:{value:1},
      events:[{type:'counter.changed', payload:{value:1}, actor:'kurisu'}],
    };
  },
});
(async () => {
  await app.start();
  await app.publishState(1);
  await app.emit('counter.changed', {value:0}, {actor:'app'});
  eventListener('auip.action.requested', {app_session_id:'app_1', action:{action_id:'a1', type:'counter.increment', expected_revision:1, payload:{amount:1}}});
  await new Promise(resolve => setTimeout(resolve, 0));
  const receipt = requests.find(item => item.method === 'auip.action.result');
  assert(receipt);
  assert.equal(receipt.params.bridge_token, 'secret');
  assert.equal(receipt.params.accepted, true);
  assert.equal(receipt.params.resulting_revision, 2);
  assert.deepEqual(receipt.params.state, {value:1});
  const causedEvent = requests.find(item =>
    item.method === 'auip.event.publish' && item.params.caused_by_action_id === 'a1'
  );
  assert(causedEvent);
  assert.equal(requests.indexOf(receipt) < requests.indexOf(causedEvent), true);
  assert.equal(causedEvent.params.revision, 2);
  const registration = requests.find(item => item.method === 'auip.register');
  assert.equal(registration.params.attach_ticket, 'ticket-1');
  assert.equal(registration.params.conversation_id, undefined);
  console.log('ok: AUIP Web SDK keeps transport and app mechanics separate');
})().catch(error => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        [node, "-e", script, str(SDK)],
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


def test_rejected_action_event_does_not_suppress_later_terminal_event() -> None:
    node = shutil.which("node")
    if not node:
        return
    script = r"""
const assert = require('assert');
const sdk = require(process.argv[1]);
const requests = [];
const loggedErrors = [];
const originalConsoleError = console.error;
let eventListener = null;
let publishedEvents = 0;
const transport = {
  async request(method, params) {
    requests.push({method, params});
    if (method === 'auip.register') return {ok:true, app_session_id:'app_1', bridge_token:'secret', revision:0};
    if (method === 'auip.action.result') return {ok:true, revision:params.resulting_revision};
    if (method === 'auip.event.publish') {
      publishedEvents += 1;
      if (publishedEvents === 1) throw new Error('invalid actor');
      return {ok:true, revision:params.revision};
    }
    throw new Error(method);
  },
  onEvent(listener) { eventListener = listener; return () => { eventListener = null; }; },
  close() {},
};
const app = sdk.createApp({
  manifest: {schema:'amadeus.auip/v0'},
  attachTicket: 'ticket-1',
  transport,
  getState: () => ({winner:'O'}),
  onAction: async () => ({
    accepted:true,
    revision:2,
    state:{winner:'O'},
    events:[
      {type:'game.move_committed', actor:'invalid-actor', payload:{mark:'O'}},
      {type:'game.finished', actor:'app', payload:{winner:'O'}},
    ],
  }),
});
(async () => {
  console.error = (...args) => loggedErrors.push(args);
  await app.start();
  eventListener('auip.action.requested', {
    app_session_id:'app_1',
    action:{action_id:'a1', type:'game.place_mark', expected_revision:1, payload:{row:1, column:0}},
  });
  await new Promise(resolve => setTimeout(resolve, 20));
  console.error = originalConsoleError;

  const receiptIndex = requests.findIndex(item => item.method === 'auip.action.result');
  const events = requests.filter(item => item.method === 'auip.event.publish');
  assert(receiptIndex >= 0);
  assert.equal(events.length, 2);
  assert.equal(events[0].params.event_type, 'game.move_committed');
  assert.equal(events[1].params.event_type, 'game.finished');
  assert.equal(requests.indexOf(events[0]) > receiptIndex, true);
  assert.equal(requests.indexOf(events[1]) > requests.indexOf(events[0]), true);
  assert.equal(loggedErrors.length, 1);
  console.log('ok: one rejected semantic event cannot suppress a later terminal event');
})().catch(error => { console.error = originalConsoleError; originalConsoleError(error); process.exit(1); });
"""
    result = subprocess.run(
        [node, "-e", script, str(SDK)],
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


def test_websocket_transport_uses_the_restricted_request_envelope() -> None:
    node = shutil.which("node")
    if not node:
        return
    script = r"""
const assert = require('assert');
const sdk = require(process.argv[1]);
class FakeSocket {
  constructor() { this.readyState = 0; this.listeners = {}; this.sent = []; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  emit(name, data) { (this.listeners[name] || []).forEach(fn => fn(data)); }
  send(raw) {
    const message = JSON.parse(raw);
    this.sent.push(message);
    this.emit('message', {data: JSON.stringify({
      type:'res', id:message.id, method:'', params:{ok:true, echoed:message.method}
    })});
  }
  close() { this.emit('close', {}); }
}
const socket = new FakeSocket();
const transport = sdk.createWebSocketTransport({
  url:'ws://127.0.0.1:17777/auip/ws',
  webSocketFactory: () => socket,
});
socket.readyState = 1;
socket.emit('open', {});
(async () => {
  const result = await transport.request('auip.register', {attach_ticket:'ticket'});
  assert.equal(result.echoed, 'auip.register');
  assert.equal(socket.sent[0].type, 'req');
  assert.equal(socket.sent[0].params.attach_ticket, 'ticket');
  transport.close();
  console.log('ok: AUIP WebSocket transport uses the restricted envelope');
})().catch(error => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        [node, "-e", script, str(SDK)],
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


def test_sdk_consumes_the_launcher_fragment_without_app_owned_session_identity() -> None:
    node = shutil.which("node")
    if not node:
        return
    script = r"""
const assert = require('assert');
const sdk = require(process.argv[1]);
const descriptor = {
  schema:'amadeus.auip/launch-v0',
  webSocketUrl:'ws://127.0.0.1:17777/auip/ws',
  attachTicket:'ticket-from-host',
  expiresAt:Date.now() / 1000 + 60,
};
const encoded = Buffer.from(JSON.stringify(descriptor), 'utf8')
  .toString('base64url');
const replaced = [];
const launch = sdk.readLaunchConfig({
  hash:'#amadeus-auip=' + encoded,
  location:{hash:'#amadeus-auip=' + encoded, href:'file:///game.html#amadeus-auip=' + encoded},
  history:{replaceState(_state, _title, value) { replaced.push(value); }},
});
assert.equal(launch.webSocketUrl, descriptor.webSocketUrl);
assert.equal(launch.attachTicket, descriptor.attachTicket);
assert.deepEqual(replaced, ['file:///game.html']);
assert.equal(launch.conversationId, undefined);
console.log('ok: AUIP SDK consumes the host launch fragment');
"""
    result = subprocess.run(
        [node, "-e", script, str(SDK)],
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


def test_sdk_self_attach_asks_before_using_the_existing_registration_path() -> None:
    node = shutil.which("node")
    if not node:
        return
    script = r"""
const assert = require('assert');
const sdk = require(process.argv[1]);
const requests = [];
const manifest = {schema:'amadeus.auip/v0', app:{id:'counter'}};
const transport = {
  async request(method, params) {
    requests.push({method, params});
    if (method === 'auip.attach.request') {
      return {ok:true, attach_ticket:'approved-ticket', expires_at:Date.now()/1000+60};
    }
    if (method === 'auip.register') {
      return {ok:true, app_session_id:'app_self', bridge_token:'secret', revision:0};
    }
    throw new Error(method);
  },
  onEvent() { return () => {}; },
  close() {},
};
const app = sdk.createApp({
  manifest,
  transport,
  entryUrl:'file:///C:/Users/user/Desktop/Counter/counter.html',
  instanceId:'instance-counter-1',
});
(async () => {
  const session = await app.start();
  assert.equal(session.appSessionId, 'app_self');
  assert.deepEqual(requests.map(item => item.method), [
    'auip.attach.request',
    'auip.register',
  ]);
  assert.equal(requests[0].params.instance_id, 'instance-counter-1');
  assert.equal(requests[0].params.entry_url, 'file:///C:/Users/user/Desktop/Counter/counter.html');
  assert.equal(requests[0].params.conversation_id, undefined);
  assert.equal(requests[1].params.attach_ticket, 'approved-ticket');
  console.log('ok: AUIP self-attach remains an ask before ordinary ticket registration');
})().catch(error => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        [node, "-e", script, str(SDK)],
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


def test_managed_web_app_serializes_host_receipt_before_later_local_commit() -> None:
    node = shutil.which("node")
    if not node:
        return
    script = r"""
const assert = require('assert');
const sdk = require(process.argv[1]);
const requests = [];
let eventListener = null;
let releaseReceipt;
let receiptStartedResolve;
const receiptStarted = new Promise(resolve => { receiptStartedResolve = resolve; });
const transport = {
  async request(method, params) {
    requests.push({method, params});
    if (method === 'auip.register') {
      return {ok:true, app_session_id:'managed-app', bridge_token:'secret', revision:0};
    }
    if (method === 'auip.state.publish') return {ok:true, revision:params.revision};
    if (method === 'auip.action.result') {
      receiptStartedResolve();
      await new Promise(resolve => { releaseReceipt = resolve; });
      return {ok:true, revision:params.resulting_revision};
    }
    if (method === 'auip.event.publish') return {ok:true, revision:params.revision};
    if (method === 'auip.session.close') return {ok:true};
    throw new Error(method);
  },
  onEvent(listener) { eventListener = listener; return () => { eventListener = null; }; },
  close() {},
};
const manifest = {
  schema:'amadeus.auip/v0', app:{id:'managed-counter'},
  events:{'app.ready':{}, 'counter.changed':{}},
  actions:{
    'counter.increment':{
      description:'Increment using the exact amount payload.', risk:'local_execution',
      inputSchema:{type:'object', properties:{amount:{type:'integer'}}, required:['amount'], additionalProperties:false},
    },
  },
  stances:['participant'],
};
let value = 0;
let observedPayload = null;
const app = sdk.createManagedApp({
  manifest,
  attachTicket:'ticket',
  transport,
  snapshot:()=>({value}),
  initialEvents:[{type:'app.ready', actor:'app', payload:{value:0}}],
  actions:{
    'counter.increment':(payload, tx)=>{
      observedPayload = payload;
      return tx.commit({
        mutate:()=>{ value += payload.amount; return {ok:true}; },
        effects:{source:'host action'},
        events:({state})=>[{type:'counter.changed', actor:'kurisu', payload:{value:state.value}}],
      });
    },
  },
});
(async () => {
  await app.start();
  assert.equal(app.revision(), 1);
  eventListener('auip.action.requested', {
    app_session_id:'managed-app',
    action:{action_id:'host-1', type:'counter.increment', expected_revision:1, payload:{amount:2}},
  });
  await receiptStarted;
  assert.deepEqual(observedPayload, {amount:2});

  const local = app.commitLocal({
    actor:'user',
    mutate:()=>{ value += 5; return {ok:true}; },
    effects:{source:'local button'},
    events:({state})=>[{type:'counter.changed', actor:'user', payload:{value:state.value}}],
  });
  assert.equal(local.revision, 3);
  releaseReceipt();
  await local.publication;
  await app.settled();

  const receiptIndex = requests.findIndex(item =>
    item.method === 'auip.action.result' && item.params.action_id === 'host-1'
  );
  const hostEventIndex = requests.findIndex(item =>
    item.method === 'auip.event.publish'
    && item.params.caused_by_action_id === 'host-1'
  );
  const localStateIndex = requests.findIndex((item, index) =>
    index > receiptIndex
    && item.method === 'auip.state.publish'
    && item.params.revision === 3
  );
  assert(receiptIndex >= 0 && hostEventIndex > receiptIndex);
  assert(localStateIndex > hostEventIndex, 'later local publish must wait for host receipt events');
  assert.equal(requests[hostEventIndex].params.revision, 2);
  assert.deepEqual(requests[receiptIndex].params.state, {value:2});
  assert.deepEqual(requests[localStateIndex].params.state, {value:7});
  console.log('ok: managed web binding serializes receipt events before later local state');
})().catch(error => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        [node, "-e", script, str(SDK)],
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


def test_managed_web_app_closes_registration_when_initial_state_is_rejected() -> None:
    node = shutil.which("node")
    if not node:
        return
    script = r"""
const assert = require('assert');
const sdk = require(process.argv[1]);
const requests = [];
const transport = {
  async request(method, params) {
    requests.push({method, params});
    if (method === 'auip.register') {
      return {ok:true, app_session_id:'managed-invalid', bridge_token:'secret', revision:0};
    }
    if (method === 'auip.state.publish') {
      const error = new Error('choice_action_family_required');
      error.code = 'choice_action_family_required';
      throw error;
    }
    if (method === 'auip.session.close') return {ok:true};
    throw new Error(method);
  },
  onEvent() { return () => {}; },
  close() {},
};
const manifest = {
  schema:'amadeus.auip/v0', app:{id:'managed-invalid'},
  events:{},
  actions:{'game.choose':{description:'Choose.',risk:'local_execution'}},
  stances:['participant'],
};
const app = sdk.createManagedApp({
  manifest,
  attachTicket:'ticket',
  transport,
  snapshot:()=>({phase:'active'}),
  actions:{'game.choose':(_payload, tx)=>tx.reject('unused')},
});
(async () => {
  await assert.rejects(app.start(), /choice_action_family_required/);
  assert.deepEqual(requests.map(item => item.method), [
    'auip.register',
    'auip.state.publish',
    'auip.session.close',
  ]);
  assert.equal(requests[2].params.reason, 'initial_state_publish_failed');
  assert.equal(app.session().active, false);
  console.log('ok: rejected initial state closes the registered AppSession');
})().catch(error => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        [node, "-e", script, str(SDK)],
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


def test_managed_web_background_checkpoints_are_cadenced_without_delaying_events() -> None:
    node = shutil.which("node")
    if not node:
        return
    script = r"""
const assert = require('assert');
const sdk = require(process.argv[1]);
const requests = [];
let nowMs = 10000;
Date.now = () => nowMs;
const transport = {
  async request(method, params) {
    requests.push({method, params});
    if (method === 'auip.register') {
      return {ok:true, app_session_id:'cadence-app', bridge_token:'secret', revision:0};
    }
    if (method === 'auip.state.publish') return {ok:true, revision:params.revision};
    if (method === 'auip.event.publish') return {ok:true, revision:params.revision};
    if (method === 'auip.session.close') return {ok:true};
    throw new Error(method);
  },
  onEvent() { return () => {}; },
  close() {},
};
const manifest = {
  schema:'amadeus.auip/v0',app:{id:'cadenced-simulation'},
  events:{'simulation.phase_changed':{beat:true}},
  actions:{},stances:['spectator'],situationKinds:[],
};
let phase = 'running';
let privateFrame = 0;
const app = sdk.createManagedApp({
  manifest,attachTicket:'ticket',transport,
  snapshot:()=>({phase,pressure:'stable'}),
  initialEvents:[],actions:{},
});
(async()=>{
  await app.start();
  assert.equal(app.revision(),1);
  for (let frame=0;frame<120;frame+=1) {
    privateFrame += 1;
    nowMs = 10000 + frame * 8;
    const skipped = app.checkpointIfDue({minimumIntervalMs:1000});
    assert.equal(skipped.committed,false);
    assert.equal(skipped.code,'checkpoint_not_due');
  }
  assert.equal(privateFrame,120);
  assert.equal(app.revision(),1,'render frames must not create shared revisions');
  assert.equal(requests.filter(item=>item.method==='auip.state.publish').length,1);

  nowMs = 11000;
  const background = app.checkpointIfDue({minimumIntervalMs:1000});
  assert.equal(background.committed,false);
  assert.equal(background.code,'checkpoint_unchanged');
  await background.publication;
  assert.equal(app.revision(),1,'an unchanged due probe must not create a revision');
  assert.equal(requests.filter(item=>item.method==='auip.state.publish').length,1);

  nowMs = 11999;
  assert.equal(
    app.checkpointIfDue({minimumIntervalMs:1000}).code,
    'checkpoint_not_due'
  );
  nowMs = 12000;
  phase = 'under-pressure';
  const changedBackground = app.checkpointIfDue({minimumIntervalMs:1000});
  assert.equal(changedBackground.committed,true);
  await changedBackground.publication;
  assert.equal(app.revision(),2,'a changed projection publishes when the cadence is due');

  assert.throws(
    ()=>app.checkpointIfDue({
      minimumIntervalMs:1000,
      events:[{type:'simulation.phase_changed',actor:'app',payload:{phase:'paused'}}],
    }),
    /cannot carry semantic events/
  );
  nowMs = 12010;
  phase = 'paused';
  const important = app.checkpoint({
    actor:'app',effects:{phase},
    events:[{type:'simulation.phase_changed',actor:'app',payload:{phase}}],
  });
  await important.publication;
  assert.equal(app.revision(),3,'semantic events publish immediately');

  nowMs = 13009;
  assert.equal(
    app.checkpointIfDue({minimumIntervalMs:1000}).code,
    'checkpoint_not_due'
  );
  nowMs = 13010;
  const nextBackground = app.checkpointIfDue({minimumIntervalMs:1000});
  await nextBackground.publication;
  assert.equal(nextBackground.committed,false);
  assert.equal(nextBackground.code,'checkpoint_unchanged');
  assert.equal(app.revision(),3);

  nowMs = 13020;
  const sameStateEvent = app.checkpoint({
    actor:'app',effects:{},
    events:[{type:'simulation.phase_changed',actor:'app',payload:{phase}}],
  });
  await sameStateEvent.publication;
  assert.equal(sameStateEvent.committed,true);
  assert.equal(app.revision(),4,'an explicit same-state semantic event still commits');
  assert.equal(requests.filter(item=>item.method==='auip.state.publish').length,4);
})().catch(error=>{console.error(error);process.exit(1);});
"""
    result = subprocess.run(
        [node, "-e", script, str(SDK)],
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


def test_managed_web_controller_activates_after_receipt_and_reports_takeover() -> None:
    node = shutil.which("node")
    if not node:
        return
    script = r"""
const assert = require('assert');
const sdk = require(process.argv[1]);
const requests = [];
const diagnostics = [];
const unhandledRejections = [];
process.on('unhandledRejection',error=>unhandledRejections.push(String(error && error.message || error)));
let eventListener = null;
let failNextControllerStatus = false;
const transport = {
  async request(method, params) {
    requests.push({method, params});
    if (method === 'auip.register') {
      return {ok:true, app_session_id:'controller-app', bridge_token:'secret', revision:0};
    }
    if (method === 'auip.state.publish') return {ok:true, revision:params.revision};
    if (method === 'auip.action.result') {
      return {
        ok:true,
        revision:params.resulting_revision,
        receipt:{accepted:params.accepted,effects:params.effects || {}},
      };
    }
    if (method === 'auip.controller.status.publish') {
      if (failNextControllerStatus) {
        failNextControllerStatus = false;
        throw new Error('stale_controller_lease');
      }
      return {ok:true};
    }
    if (method === 'auip.session.close') return {ok:true};
    throw new Error(method);
  },
  onEvent(listener) { eventListener = listener; return () => { eventListener = null; }; },
  close() {},
};
const manifest = {
  schema:'amadeus.auip/v0',
  app:{id:'reactive-vehicle',title:'Reactive vehicle'},
  events:{'vehicle.ready':{beat:true}},
  actions:{
    'vehicle.set_navigation_policy':{
      description:'Set the exact navigation policy.',risk:'local_execution',
      inputSchema:{
        type:'object',
        properties:{destination:{type:'string'},arrival:{type:'string'}},
        required:['destination','arrival'],additionalProperties:false,
      },
    },
    'vehicle.reset':{
      description:'Reset the local vehicle state.',risk:'local_execution',
      inputSchema:{type:'object',properties:{},additionalProperties:false},
    },
  },
  stances:['spectator','participant'],
  situationKinds:['controller/v1'],
  controller:{
    policyActions:['vehicle.set_navigation_policy'],
    leaseDurationMs:30000,maxActionRateHz:12,takeover:'safe_point',
  },
};
let destination = '';
const applied = [];
const releases = [];
const observedNow = [];
let wallNow = 900;
Date.now = () => wallNow;
const app = sdk.createManagedApp({
  manifest, attachTicket:'ticket', transport,
  onDiagnostic:item=>diagnostics.push(item),
  controller:{
    observe:context=>{observedNow.push(context.nowMs);return {obstacleId:'debris-9'};},
    decide:({observation,policy})=>({
      navigateTo:policy.destination,
      arrival:policy.arrival,
      avoid:observation.obstacleId,
    }),
    apply:({command})=>{
      applied.push(command);
      return {accepted:true,effects:{queued:true}};
    },
    clearIntent:context=>{releases.push(context.reason);},
    policySummary:({policy})=>`Navigate to ${policy.destination} using ${policy.arrival}`,
  },
  snapshot:context=>({destination,controller:context.controller}),
  initialEvents:[],
  actions:{
    'vehicle.set_navigation_policy':(payload,tx)=>tx.commit({
      mutate:()=>{destination=payload.destination; return {accepted:true};},
      effects:{destination:payload.destination},events:[],
    }),
    'vehicle.reset':(_payload,tx)=>tx.commit({
      mutate:()=>{destination=''; return {reset:true};},
      effects:{reset:true},events:[],
    }),
  },
});
const lease = {
  lease_id:'lease-9',principal:'kurisu',executor:'app_controller',
  generation:9,policy_revision:3,issued_at_ms:1000,expires_at_ms:31000,
  max_action_rate_hz:12,takeover:'safe_point',
};
(async()=>{
  await app.start();
  const idleStep = app.controllerStep({nowMs:999999});
  assert.equal(idleStep.accepted,false);
  assert.equal(idleStep.code,'controller_inactive');
  assert.deepEqual(applied,[]);
  assert.equal(app.controllerStatus().status,'idle');
  const ambient1 = app.checkpoint({actor:'app',events:[]});
  const ambient2 = app.checkpoint({actor:'app',events:[]});
  await Promise.all([ambient1.publication,ambient2.publication]);
  assert.equal(app.revision(),3);
  eventListener('auip.action.requested',{
    app_session_id:'controller-app',
    action:{
      action_id:'unleased-policy',type:'vehicle.set_navigation_policy',
      actor:'kurisu',expected_revision:1,
      payload:{destination:'must-not-apply',arrival:'unsafe'},
    },
  });
  await new Promise(resolve=>setTimeout(resolve,0));
  const unleasedReceipt = requests.find(item=>
    item.method==='auip.action.result'
    && item.params.action_id==='unleased-policy'
  );
  assert(unleasedReceipt);
  assert.equal(unleasedReceipt.params.accepted,false);
  assert.equal(destination,'');
  eventListener('auip.action.requested',{
    app_session_id:'controller-app',
    action:{
      action_id:'unleased-policy-exact',type:'vehicle.set_navigation_policy',
      actor:'kurisu',expected_revision:app.revision(),
      payload:{destination:'must-still-not-apply',arrival:'unsafe'},
    },
  });
  await new Promise(resolve=>setTimeout(resolve,0));
  await app.settled();
  const exactUnleasedReceipt = requests.find(item=>
    item.method==='auip.action.result'
    && item.params.action_id==='unleased-policy-exact'
  );
  assert(exactUnleasedReceipt);
  assert.equal(exactUnleasedReceipt.params.accepted,false);
  assert.match(exactUnleasedReceipt.params.reason,/controller_lease_required/);
  assert.equal(destination,'');
  destination = 'private-telemetry-drift';
  eventListener('auip.action.requested',{
    app_session_id:'controller-app',
    action:{
      action_id:'policy-1',type:'vehicle.set_navigation_policy',
      actor:'kurisu',
      expected_revision:1,
      payload:{destination:'dock-A12',arrival:'soft_capture'},
      controller_lease:lease,
    },
  });
  for (let i=0;i<20 && app.controllerStatus().status!=='active';i+=1) {
    await new Promise(resolve=>setTimeout(resolve,0));
  }
  await app.settled();
  assert.equal(app.controllerStatus().status,'active');
  assert(diagnostics.some(item=>
    item.code==='controller_policy_private_telemetry_checkpoint'
    && item.revision===4
  ));
  assert(diagnostics.some(item=>
    item.code==='controller_policy_locally_rebased'
    && item.fromRevision===1
    && item.toRevision===4
  ));
  const receipt = requests.find(item=>
    item.method==='auip.action.result'
    && item.params.action_id==='policy-1'
  );
  assert.deepEqual(receipt.params.effects,{destination:'dock-A12'});
  assert.equal(receipt.params.resulting_revision,5);
  const activeReport = requests.find(item=>
    item.method==='auip.controller.status.publish' && item.params.status==='active'
  );
  assert(activeReport);
  assert.equal(activeReport.params.lease_id,'lease-9');
  assert(requests.some(item=>
    item.method==='auip.state.publish'
    && item.params.state.controller.status==='active'
  ));
  wallNow = 1100;
  const command = app.controllerStep({nowMs:999999});
  assert.equal(command.accepted,true);
  assert.equal(observedNow.at(-1),1100);
  assert.deepEqual(applied,[{
    navigateTo:'dock-A12',arrival:'soft_capture',avoid:'debris-9',
  }]);

  eventListener('auip.action.requested',{
    app_session_id:'controller-app',
    action:{
      action_id:'stale-policy',type:'vehicle.set_navigation_policy',
      actor:'kurisu',expected_revision:app.revision(),
      payload:{destination:'must-not-replace',arrival:'unsafe'},
      controller_lease:lease,
    },
  });
  await new Promise(resolve=>setTimeout(resolve,0));
  await app.settled();
  const staleReceipt = requests.find(item=>
    item.method==='auip.action.result'
    && item.params.action_id==='stale-policy'
  );
  assert(staleReceipt);
  assert.equal(staleReceipt.params.accepted,false);
  assert.match(staleReceipt.params.reason,/stale_controller_lease/);
  assert.equal(destination,'dock-A12');
  assert.equal(app.controllerStatus().status,'active');

  eventListener('auip.controller.revoke.requested',{
    app_session_id:'controller-app',
    revoke:{...lease,requested_at_ms:1200,reason:'user takeover'},
  });
  for (let i=0;i<20 && app.controllerStatus().status!=='stopping';i+=1) {
    await new Promise(resolve=>setTimeout(resolve,0));
  }
  wallNow = 1250;
  assert.equal(app.controllerStep({nowMs:999999}).code,'controller_stopping');
  wallNow = 1300;
  const stopped = app.acknowledgeControllerSafePoint({nowMs:999999});
  await stopped.publication;
  assert.equal(app.controllerStatus().status,'idle');
  assert(requests.some(item=>
    item.method==='auip.controller.status.publish' && item.params.status==='idle'
  ));

  const expiringLease = {
    ...lease,lease_id:'lease-10',generation:10,policy_revision:4,
    issued_at_ms:1300,expires_at_ms:1400,takeover:'immediate',
  };
  eventListener('auip.action.requested',{
    app_session_id:'controller-app',
    action:{
      action_id:'policy-2',type:'vehicle.set_navigation_policy',
      actor:'kurisu',
      expected_revision:app.revision(),
      payload:{destination:'dock-B7',arrival:'fast_capture'},
      controller_lease:expiringLease,
    },
  });
  for (let i=0;i<20 && app.controllerStatus().status!=='active';i+=1) {
    await new Promise(resolve=>setTimeout(resolve,0));
  }
  await app.settled();
  assert.equal(app.controllerStatus().status,'active');
  wallNow = 1400;
  failNextControllerStatus = true;
  const expired = app.controllerStep({nowMs:0});
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(expired.code,'controller_lease_expired');
  assert.equal(app.controllerStatus().status,'idle');
  assert.equal(app.controllerStatus().reason,'expired');
  assert.equal(releases.at(-1),'expired');
  assert(diagnostics.some(item=>
    item.code==='controller_status_publication_failed'
    && item.reason==='stale_controller_lease'
  ));
  assert.deepEqual(unhandledRejections,[]);

  const renewedLease = {
    ...lease,lease_id:'lease-11',generation:11,policy_revision:5,
    issued_at_ms:1400,expires_at_ms:2000,takeover:'immediate',
  };
  eventListener('auip.action.requested',{
    app_session_id:'controller-app',
    action:{
      action_id:'policy-3',type:'vehicle.set_navigation_policy',
      actor:'kurisu',
      expected_revision:app.revision(),
      payload:{destination:'dock-C3',arrival:'precision_capture'},
      controller_lease:renewedLease,
    },
  });
  for (let i=0;i<20 && app.controllerStatus().status!=='active';i+=1) {
    await new Promise(resolve=>setTimeout(resolve,0));
  }
  await app.settled();
  assert.equal(app.controllerStatus().status,'active');
  wallNow = 1500;
  const renewed = app.controllerStep();
  assert.equal(renewed.accepted,true);
  assert.deepEqual(applied.at(-1),{
    navigateTo:'dock-C3',arrival:'precision_capture',avoid:'debris-9',
  });
  assert(requests.some(item=>
    item.method==='auip.controller.status.publish'
    && item.params.status==='active'
    && item.params.lease_id==='lease-11'
  ));

  // A phase may stop its ordinary app loop. The next semantic action must
  // reconcile an elapsed lease without executing one more controller command
  // or publishing an active governance snapshot in that action's receipt.
  wallNow = 2000;
  const appliedBeforeReset = applied.length;
  eventListener('auip.action.requested',{
    app_session_id:'controller-app',
    action:{
      action_id:'reset-after-paused-expiry',type:'vehicle.reset',
      actor:'kurisu',expected_revision:app.revision(),payload:{},
    },
  });
  await new Promise(resolve=>setTimeout(resolve,0));
  await app.settled();
  const resetReceipt = requests.find(item=>
    item.method==='auip.action.result'
    && item.params.action_id==='reset-after-paused-expiry'
  );
  assert(resetReceipt);
  assert.equal(
    resetReceipt.params.accepted,
    true,
    JSON.stringify(resetReceipt.params)
  );
  assert.equal(resetReceipt.params.state.controller.status,'idle');
  assert.equal(resetReceipt.params.state.controller.reason,'expired');
  assert.equal(app.controllerStatus().status,'idle');
  assert.equal(releases.at(-1),'expired');
  assert.equal(applied.length,appliedBeforeReset);
  for (let i=0;i<20 && !requests.some(item=>
    item.method==='auip.controller.status.publish'
    && item.params.status==='idle'
    && item.params.lease_id==='lease-11'
  );i+=1) {
    await new Promise(resolve=>setTimeout(resolve,0));
  }
  assert(requests.some(item=>
    item.method==='auip.controller.status.publish'
    && item.params.status==='idle'
    && item.params.lease_id==='lease-11'
  ));

  const checkpointLease = {
    ...lease,lease_id:'lease-12',generation:12,policy_revision:6,
    issued_at_ms:2000,expires_at_ms:2100,takeover:'immediate',
  };
  eventListener('auip.action.requested',{
    app_session_id:'controller-app',
    action:{
      action_id:'policy-4',type:'vehicle.set_navigation_policy',
      actor:'kurisu',expected_revision:app.revision(),
      payload:{destination:'dock-D4',arrival:'steady_capture'},
      controller_lease:checkpointLease,
    },
  });
  for (let i=0;i<20 && app.controllerStatus().status!=='active';i+=1) {
    await new Promise(resolve=>setTimeout(resolve,0));
  }
  await app.settled();
  assert.equal(app.controllerStatus().status,'active');
  wallNow = 2100;
  const appliedBeforeCheckpoint = applied.length;
  const expiryCheckpoint = app.checkpointIfDue({minimumIntervalMs:100000});
  assert.equal(expiryCheckpoint.committed,true);
  await expiryCheckpoint.publication;
  assert.equal(app.controllerStatus().status,'idle');
  assert.equal(applied.length,appliedBeforeCheckpoint);
})().catch(error=>{console.error(error);process.exit(1);});
"""
    result = subprocess.run(
        [node, "-e", script, str(SDK)],
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


def test_managed_web_controller_rejects_a_snapshot_that_discards_governance() -> None:
    node = shutil.which("node")
    if not node:
        return
    script = r"""
const assert = require('assert');
const sdk = require(process.argv[1]);
const manifest = {
  schema:'amadeus.auip/v0',
  app:{id:'bad-controller-projection',title:'Bad controller projection'},
  events:{'app.ready':{beat:true}},
  actions:{
    'app.set_policy':{
      description:'Set policy.',risk:'local_execution',
      inputSchema:{
        type:'object',properties:{mode:{type:'string'}},
        required:['mode'],additionalProperties:false,
      },
    },
  },
  stances:['spectator','participant'],
  situationKinds:['controller/v1'],
  controller:{
    policyActions:['app.set_policy'],leaseDurationMs:30000,
    maxActionRateHz:2,takeover:'immediate',
  },
};
const idle = {
  kind:'controller/v1',status:'idle',policyRevision:null,
  policyAction:null,policySummary:'',
};
assert.throws(
  ()=>sdk.createManagedApp({
    manifest,
    snapshot:context=>({
      // This is the real authoring bug: the supplied active status is lost.
      controller:{...idle},
    }),
    actions:{},
    controller:{
      observe:()=>({}),decide:()=>null,apply:()=>({accepted:false}),
      clearIntent:()=>{},
      policySummary:()=> 'Policy',
    },
  }),
  /snapshot must preserve the supplied governance status/
);
"""
    result = subprocess.run(
        [node, "-e", script, str(SDK)],
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


def _main() -> None:
    test_sdk_registers_publishes_and_resolves_actions()
    test_rejected_action_event_does_not_suppress_later_terminal_event()
    test_websocket_transport_uses_the_restricted_request_envelope()
    test_sdk_consumes_the_launcher_fragment_without_app_owned_session_identity()
    test_sdk_self_attach_asks_before_using_the_existing_registration_path()
    test_managed_web_app_serializes_host_receipt_before_later_local_commit()
    test_managed_web_app_closes_registration_when_initial_state_is_rejected()
    test_managed_web_background_checkpoints_are_cadenced_without_delaying_events()
    test_managed_web_controller_activates_after_receipt_and_reports_takeover()
    test_managed_web_controller_rejects_a_snapshot_that_discards_governance()


if __name__ == "__main__":
    _main()
