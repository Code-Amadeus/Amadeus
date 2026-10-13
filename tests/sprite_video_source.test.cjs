const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const budget = require('../render/web/render_budget.js');
const storeApi = require('../render/web/frame_store.js');

const renderer = fs.readFileSync(path.join(__dirname, '../render/web/renderer.js'), 'utf8');
const start = renderer.indexOf('class SpriteRenderer {');
const end = renderer.indexOf('class SpriteForgeRuntime {', start);
const label = 'idle_closed_eye';
const urls = count => Array.from({ length: count }, (_, index) => `${label}:${index}`);
const texture = (index, kind = 'static') => ({ sourceIndex: index, kind, width: 100, height: 100,
  destroyed: false, baseTexture: { valid: true, destroyed: false } });

class Display {
  constructor() { this.anchor = { set() {} }; this.scale = { set() {} }; this.texture = { height: 1 }; }
  addChild() {}
  clear() {}
}

async function flush() { for (let i = 0; i < 24; i++) await Promise.resolve(); }
async function settle(f) {
  for (let i = 0; i < 100; i++) {
    await new Promise(resolve => setImmediate(resolve));
    const stats = f.sprite._frameStore.stats();
    if (!stats.queued && !stats.inFlight) return;
  }
  assert.fail('the deterministic static backend settles');
}

function fixture(t, { sampling = true, fps = 30, auto = true } = {}) {
  const loads = [], demands = new Map(), pins = new Map(), sessions = [], warnings = [];
  let unload, disposals = 0, randomCalls = 0;
  const seededMath = Object.create(Math);
  seededMath.random = () => [0.1, 0.8, 0.4][randomCalls++ % 3];
  const backend = {
    now: () => 0,
    load(url, signal) {
      let resolve;
      const pending = new Promise(done => { resolve = done; });
      const call = { url, signal, resolve, done: false };
      loads.push(call);
      if (auto) complete(call);
      return pending;
    },
    async upload() {},
    destroy(tex) { tex.destroyed = tex.baseTexture.destroyed = true; },
    dispose() { disposals++; },
  };
  function complete(call) {
    assert.ok(call && !call.done, 'the requested static load is pending');
    call.done = true;
    const tex = texture(Number(call.url.split(':').at(-1)));
    tex.url = call.url;
    call.resolve({ texture: tex, cpuBytes: 4, gpuBytes: 6 });
    return tex;
  }
  const ticker = { deltaMS: 1000 / fps, add(fn) { this.callback = fn; },
    tick(ms = 1000 / fps) { this.deltaMS = ms; this.callback(1); } };
  const controller = budget.createFrameRateController({ update() {} }, fps);
  const context = {
    app: { ticker, screen: { width: 1000, height: 800 }, renderer: {} },
    renderBudget: budget.resolveRenderBudget({ maxFps: fps, textureSampling: sampling }),
    frameRateController: controller,
    Math: seededMath,
    PIXI: { Container: Display, Sprite: Display, Graphics: Display, Texture: { EMPTY: { height: 1 } } },
    window: { RenderBudget: budget, FrameTextureBackend: { createFrameTextureBackend: () => backend },
      FrameStore: { createFrameStore(options) {
        const store = storeApi.createFrameStore(options);
        const replaceDemand = store.replaceDemand, replacePins = store.replacePins;
        store.replaceDemand = (owner, requests, settings) => {
          const snapshot = Array.from(requests, row => ({ ...row }));
          demands.set(owner, snapshot); replaceDemand(owner, snapshot, settings);
        };
        store.replacePins = (owner, requested) => {
          const snapshot = Array.from(requested); pins.set(owner, snapshot); replacePins(owner, snapshot);
        };
        return store;
      } },
      addEventListener(event, callback) { if (event === 'unload') unload = callback; },
    },
    console: { log() {}, warn(...values) { warnings.push(values); } }, setTimeout, clearTimeout,
  };
  vm.createContext(context);
  vm.runInContext(renderer.slice(start, end) + '\nglobalThis.SpriteRenderer=SpriteRenderer;', context);
  const sprite = new context.SpriteRenderer(new Display());
  sprite._applyCurrentTransform = sprite._hideMouthLayer = sprite._updateMouthLayer = () => {};
  t.after(() => unload());

  function videoDescriptor({ head = 18, count = 738, allReady = false, createError = null } = {}) {
    return { headFrameCount: head, createSource(callbacks) {
      if (createError) throw createError;
      const ready = new Set(allReady ? Array.from({ length: count }, (_, index) => index) : []);
      const textures = new Map(), retained = new Map();
      const source = { ready, textures, retained, windows: [], released: 0, destroyed: 0,
        hasAny() { return ready.size > 0; },
        has(index) { return ready.has(index); },
        texture(index) {
          if (!ready.has(index)) return null;
          if (!textures.has(index)) textures.set(index, texture(index, 'video'));
          return textures.get(index);
        },
        requestWindow(targets) { this.windows.push(Array.from(targets, row => ({ ...row }))); },
        releaseClip() { this.released++; },
        destroy() {
          this.destroyed++;
          for (const tex of textures.values()) if (!retained.has(tex)) tex.destroyed = true;
        },
        retainDisplay(tex) { assert.equal(tex.destroyed, false); retained.set(tex, (retained.get(tex) || 0) + 1); },
        releaseDisplay(tex) {
          assert.ok(retained.has(tex), 'only an owned display is released');
          const remaining = retained.get(tex) - 1;
          if (remaining) retained.set(tex, remaining); else retained.delete(tex);
          if (!remaining && this.destroyed) tex.destroyed = true;
        },
        arrive(index) { ready.add(index); callbacks.onFrame(index); },
        fail(error = Error('synthetic decoder failure')) { callbacks.onError(error); },
      };
      sessions.push(source);
      return source;
    } };
  }
  return { sprite, ticker, loads, demands, pins, sessions, warnings, videoDescriptor,
    hostFps: value => controller.setHostMaxFps(value), unload: () => unload(),
    complete(url) { return complete(loads.find(call => call.url === url && !call.done)); },
    async finishCancelled() {
      for (let round = 0; round < 10; round++) {
        await flush();
        const cancelled = loads.filter(call => call.signal.aborted && !call.done);
        if (!cancelled.length) return;
        for (const call of cancelled) complete(call);
      }
      assert.fail('cancelled synthetic static work settles');
    },
    get disposals() { return disposals; },
    get randomCalls() { return randomCalls; },
    runtime() {
      const runtimeEnd = renderer.indexOf('class SubtitleOverlay {', end);
      vm.runInContext(renderer.slice(end, runtimeEnd) + '\nglobalThis.SpriteForgeRuntime=SpriteForgeRuntime;', context);
      return new context.SpriteForgeRuntime(sprite);
    },
  };
}

function install(f, { count = 738, head = 18, interval = 17, allReady = false } = {}) {
  const s = f.sprite;
  s.setExperimentalFrameVideoSource(label, f.videoDescriptor({ count, head, allReady }));
  s.loadFrames(label, urls(count));
  s.setIdleFrameIntervalMs(label, interval);
  s.setEmotion(label);
  return f.sessions.at(-1);
}

function snapshot(s, cycles) {
  return { label: s._currentEmotion, logical: s._frameIdx, active: s._activeFrameIdx,
    sampleTime: s._sampleTimeMs, held: s._held, heldIndex: s._heldFrameIdx,
    speaking: s._speaking, cycleLabel: s._cycleCompletedForEmotion, cycles,
    count: s._getEmotionFrames()?.length, displayed: s.sprite.texture.sourceIndex,
    sampling: s._frameSamplingPlans.get(label)?.indices.slice() || [] };
}

test('V1: identical ready source identities, ticks and events preserve state with sampling on and off', async t => {
  for (const sampling of [true, false]) {
    const baseline = fixture(t, { sampling }), hybrid = fixture(t, { sampling });
    const count = 120, interval = 17;
    install(hybrid, { count, interval, allReady: true });
    const b = baseline.sprite, h = hybrid.sprite;
    b.loadFrames(label, urls(count)); b.setIdleFrameIntervalMs(label, interval); b.setEmotion(label);
    b._frameStore.replaceDemand('test-readiness', urls(count).map(url => ({ url, priority: 100 })));
    hybrid.sprite._frameStore.replaceDemand('test-head-readiness', urls(count).slice(0, 18)
      .map(url => ({ url, priority: 100 })));
    await settle(baseline); await settle(hybrid);
    let bc = 0, hc = 0;
    b.setCycleCompleteHandler(() => bc++); h.setCycleCompleteHandler(() => hc++);
    function compare() { assert.deepEqual(snapshot(h, hc), snapshot(b, bc)); }
    const events = new Map([[20, s => s.setSpeaking(true)], [40, s => s.setSpeaking(false)],
      [55, s => s.holdFrame(null)], [58, s => s.clearHold()], [90, s => s.setSpeaking(true)],
      [100, s => s.setSpeaking(false)], [150, s => s.setClipConfig(label, { loopMode: 'once_then_hold' })]]);
    compare();
    for (let tick = 0; tick < 240; tick++) {
      if (events.has(tick)) { events.get(tick)(b); events.get(tick)(h); compare(); }
      if (tick === 115) { baseline.hostFps(60); hybrid.hostFps(60); }
      const ms = [16, 33, 100, 17][tick % 4];
      baseline.ticker.tick(ms); hybrid.ticker.tick(ms); compare();
    }
  }
});

test('normal sequential video playback filters body from all static demands and preserves 738 source frames', async t => {
  const f = fixture(t), s = f.sprite;
  const source = install(f, { allReady: true });
  s.prefetchLabels([label], 'interactive', { reason: 'entry' });
  await settle(f);
  for (let i = 0; i < 800; i++) f.ticker.tick(17);
  await settle(f);
  const body = url => { const index = Number(url.split(':').at(-1)); return index >= 18 && index < 737; };
  assert.equal(f.loads.filter(call => body(call.url)).length, 0);
  for (const rows of f.demands.values()) assert.equal(rows.filter(row => body(row.url)).length, 0);
  assert.equal(s._frames[label].length, 738);
  assert.ok(f.loads.some(call => call.url === `${label}:737`), 'planned endpoint stays static');
  assert.ok(source.windows.some(rows => rows.some(row => row.index >= 18)));
  assert.ok(source.windows.every(rows => rows.every(row => row.index >= 18 && row.index < 737)));
  assert.equal(s.getExperimentalFrameVideoStats().staticBodyHoldDemands, 0);
  assert.equal(s.getExperimentalFrameVideoStats().staticBodyFallbackDemands, 0);
});

test('V1: graph node transitions, trigger events and seeded random draws match the static baseline', async t => {
  const baseline = fixture(t), hybrid = fixture(t);
  install(hybrid, { count: 50, head: 3, interval: 20, allReady: true });
  const b = baseline.sprite, h = hybrid.sprite;
  b.loadFrames(label, urls(50)); b.setIdleFrameIntervalMs(label, 20); b.setEmotion(label);
  for (const f of [baseline, hybrid]) {
    f.sprite.loadFrames('normal', Array.from({ length: 30 }, (_, index) => `normal:${index}`));
    f.sprite.setIdleFrameIntervalMs('normal', 20);
    f.sprite._frameStore.replaceDemand('test-normal-readiness', f.sprite._frameUrls.normal
      .map(url => ({ url, priority: 100 })));
  }
  b._frameStore.replaceDemand('test-idle-readiness', urls(50).map(url => ({ url, priority: 100 })));
  const br = baseline.runtime(), hr = hybrid.runtime();
  const graph = { rootNodeId: 'idle', graph: { nodes: [
    { id: 'idle', label, isRoot: true }, { id: 'normal', label: 'normal' },
  ], edges: [{ from: 'idle', to: 'idle', prob: 0.3 }, { from: 'idle', to: 'normal', prob: 0.7 },
    { from: 'normal', to: 'idle', prob: 1 }] }, config: {} };
  br.loadGraph(graph); hr.loadGraph(graph);
  b.setCycleCompleteHandler(value => br.onCycleComplete(value));
  h.setCycleCompleteHandler(value => hr.onCycleComplete(value));
  await settle(baseline); await settle(hybrid);
  for (let tick = 0; tick < 250; tick++) {
    if ([30, 110, 200].includes(tick)) { br.trigger(label); hr.trigger(label); }
    baseline.ticker.tick(20); hybrid.ticker.tick(20);
    assert.deepEqual(snapshot(h, 0), snapshot(b, 0));
    assert.equal(hr.currentNodeId, br.currentNodeId);
    assert.equal(hr.pendingExpression, br.pendingExpression);
    assert.equal(hr.forcedNodeId, br.forcedNodeId);
    assert.equal(hybrid.randomCalls, baseline.randomCalls);
  }
  assert.ok(hybrid.randomCalls > 0, 'actual graph decisions consumed the seeded sequence');
});

test('V1: identical cold readiness and async arrivals preserve startup, missed targets and hold state', t => {
  const baseline = fixture(t, { auto: false }), hybrid = fixture(t, { auto: false });
  const source = install(hybrid, { count: 100, head: 3, interval: 20 });
  const b = baseline.sprite, h = hybrid.sprite;
  b.loadFrames(label, urls(100)); b.setIdleFrameIntervalMs(label, 20); b.setEmotion(label);
  let bc = 0, hc = 0;
  b.setCycleCompleteHandler(() => bc++); h.setCycleCompleteHandler(() => hc++);
  const compare = () => assert.deepEqual(snapshot(h, hc), snapshot(b, bc));
  function tick(ms) { baseline.ticker.tick(ms); hybrid.ticker.tick(ms); compare(); }
  function arrive(index) {
    b._onTextureViewChange('frames:' + label, index, texture(index));
    source.arrive(index); compare();
  }
  tick(40);
  b._frames[label][50] = texture(50); source.ready.add(50); // Identical has-any readiness.
  tick(40); tick(40);
  arrive(10); // The target is already behind this future frame's arrival.
  const target = h._sampleFrameIndex(label, h._frameIdx, h._sampleTimeMs);
  arrive(target);
  b.holdFrame(null); h.holdFrame(null); compare();
  b.clearHold(); h.clearHold(); compare();
  const next = hybrid.sessions.at(-1);
  const readyIndex = h._sampleFrameIndex(label, h._frameIdx, h._sampleTimeMs);
  next.ready.add(readyIndex);
  tick(40);
});

test('same-label speech entry resets logical index and sampling time while preserving the displayed video until replacement', async t => {
  const f = fixture(t), s = f.sprite;
  const source = install(f, { count: 100, head: 3, interval: 20, allReady: true });
  await settle(f);
  s._frameIdx = 40; s._sampleTimeMs = 805; s._showFrame(40);
  const display = s.sprite.texture;
  s.setSpeaking(true);
  assert.equal(s._currentEmotion, label);
  assert.equal(s._frameIdx, 0); assert.equal(s._sampleTimeMs, null);
  assert.equal(s._held, false); assert.equal(s.sprite.texture, display);
  assert.ok(source.windows.at(-1).some(row => row.index < 40));
  f.ticker.tick(20);
  assert.equal(s._frameIdx, 1);
  assert.equal(s._activeFrameIdx, 1, 'the preserved static head supplies the reset');
});

test('inactive hints keep their stated horizon without opening a source or expanding every video head', async t => {
  const f = fixture(t, { sampling: false }), s = f.sprite;
  s.setExperimentalFrameVideoSource(label, f.videoDescriptor());
  s.loadFrames(label, urls(738)); s.setIdleFrameIntervalMs(label, 17);
  s.prefetchLabels([label], 'interactive', { reason: 'head' });
  await settle(f);
  assert.deepEqual(f.demands.get('prefetch:head').map(row => Number(row.url.split(':').at(-1))),
    Array.from({ length: 16 }, (_, index) => index));
  assert.equal(f.sessions.length, 0);
  assert.deepEqual(f.pins.get('required:'+label),[label+':0'], 'inactive video endpoints do not expand the global pin floor');
  assert.equal(s._frameIdx, 0);
});

test('startup waits for any ready frame, then advances even when the specific target is missing', async t => {
  const f = fixture(t, { auto: false }), s = f.sprite;
  const source = install(f, { count: 100, head: 3, interval: 20 });
  f.ticker.tick(40); assert.equal(s._frameIdx, 0);
  source.ready.add(50); // Any ready source is enough; no target arrival callback.
  f.ticker.tick(40);
  assert.equal(s._frameIdx, 2);
  assert.equal(s._activeFrameIdx, 50, 'initial fallback records the displayed video source identity');
  const previous = s.sprite.texture;
  f.ticker.tick(40);
  assert.equal(s._frameIdx, 4);
  assert.equal(s.sprite.texture, previous, 'a missing target keeps the previous display');
});

test('late target readiness replaces the current display, while a passed target arrival cannot rewind it', async t => {
  const f = fixture(t), s = f.sprite;
  const source = install(f, { count: 100, head: 3, interval: 20 });
  await settle(f);
  f.ticker.tick(100); assert.equal(s._frameIdx, 5);
  source.arrive(3); assert.equal(s._activeFrameIdx, 0);
  source.arrive(s._sampleFrameIndex(label, 5, s._sampleTimeMs));
  assert.equal(s._activeFrameIdx, s._sampleFrameIndex(label, 5, s._sampleTimeMs));
  const current = s.sprite.texture;
  source.arrive(4); assert.equal(s.sprite.texture, current);
});

test('freeze-current retains video output; a cold explicit hold requests static and resumes from its source index', async t => {
  const f = fixture(t, { auto: false }), s = f.sprite;
  const source = install(f, { count: 100, head: 3, interval: 20 });
  await flush(); f.complete(`${label}:0`); await flush();
  source.arrive(5); s._showFrame(5);
  const current = s.sprite.texture;
  s.holdFrame(null);
  assert.equal(s.sprite.texture, current);
  assert.equal(s._heldFrameIdx, 5);
  assert.equal(source.destroyed, 1);
  assert.equal(current.destroyed, false);
  assert.equal(f.pins.get('hold').length, 0);
  assert.equal(s.getExperimentalFrameVideoStats().staticBodyHoldDemands, 0);
  s.holdFrame(81);
  assert.equal(s._heldFrameIdx, 81);
  assert.equal(s._activeFrameIdx, 5);
  assert.equal(s.sprite.texture, current);
  assert.deepEqual(f.pins.get('hold'), [`${label}:81`]);
  await f.finishCancelled(); f.complete(`${label}:81`); await flush();
  assert.equal(s._activeFrameIdx, 81);
  assert.equal(s.sprite.texture.kind, 'static');
  assert.equal(current.destroyed, true);
  assert.equal(s.getExperimentalFrameVideoStats().staticBodyHoldDemands, 1);
  assert.equal(f.loads.filter(call => call.url === `${label}:81`).length, 1,
    'the explicit static hold is an actual one-load body exception');
  assert.equal(f.loads.filter(call => {
    const index = Number(call.url.split(':').at(-1)); return index >= 3 && index < 99;
  }).length, 1, 'freeze-current itself loaded no static body');
  s.clearHold();
  assert.equal(s._frameIdx, 81);
  assert.equal(f.sessions.length, 2);
  const resumed = f.sessions.at(-1);
  assert.ok(resumed.windows.at(-1).some(row => row.index === 81));
});

test('sampling-off null hold keeps logical cold-hold semantics when video display is behind', async t => {
  const f = fixture(t, { auto: false, sampling: false }), s = f.sprite;
  const source = install(f, { count: 100, head: 3, interval: 20 });
  await flush(); f.complete(`${label}:0`); await flush();
  source.arrive(5); s._showFrame(5);
  f.ticker.tick(40);
  assert.equal(s._frameIdx, 7); assert.equal(s._activeFrameIdx, 5);
  s.holdFrame(null);
  assert.equal(s._heldFrameIdx, 7); assert.equal(s._activeFrameIdx, 7);
  assert.equal(s.sprite.texture.sourceIndex, 5);
  assert.equal(s._videoFrozenHold, false);
  assert.deepEqual(f.pins.get('hold'), [`${label}:7`]);
  await f.finishCancelled(); f.complete(`${label}:7`); await flush();
  assert.equal(s.sprite.texture.sourceIndex, 7);
});

test('old video output survives clip release until a successor upload and ignores stale callbacks after reentry', async t => {
  const f = fixture(t, { auto: false }), s = f.sprite;
  const source = install(f, { count: 100, head: 3, interval: 20 });
  await flush(); f.complete(`${label}:0`); await flush();
  source.arrive(5); s._showFrame(5);
  const old = s.sprite.texture;
  s.loadFrames('normal', ['normal:0']); s.setEmotion('normal');
  assert.equal(source.released, 1); assert.equal(source.destroyed, 1);
  assert.equal(old.destroyed, false); assert.equal(s.sprite.texture, old);
  source.arrive(10);
  assert.equal(s.sprite.texture, old); assert.equal(s.getExperimentalFrameVideoStats().failed, false);
  await flush(); f.complete('normal:0'); await flush();
  assert.equal(old.destroyed, true);
  source.fail(); assert.equal(s.getExperimentalFrameVideoStats().failed, false);
  s.setEmotion(label);
  const replacement = f.sessions.at(-1);
  assert.notEqual(replacement, source);
  s._frameIdx = 5;
  source.arrive(5); assert.notEqual(s.sprite.texture.kind, 'video');
  replacement.arrive(s._sampleFrameIndex(label, 5));
  assert.equal(s.sprite.texture.kind, 'video');
});

test('decoder failure restores static demands once, stays latched across clip entry, and explicit reinstall retries', async t => {
  const f = fixture(t), s = f.sprite;
  const source = install(f, { count: 100, head: 3, interval: 20, allReady: true });
  await settle(f); s._showFrame(5);
  const previous = s.sprite.texture;
  source.fail();
  assert.equal(s.getExperimentalFrameVideoStats().failed, true);
  assert.equal(previous.destroyed, false);
  assert.ok(f.demands.get('current').some(row => row.url === `${label}:5`));
  await settle(f);
  assert.equal(s.sprite.texture.kind, 'static'); assert.equal(previous.destroyed, true);
  assert.ok(s.getExperimentalFrameVideoStats().staticBodyFallbackDemands > 0);
  assert.ok(f.loads.filter(call => {
    const index = Number(call.url.split(':').at(-1)); return index >= 3 && index < 99;
  }).length > 0, 'fallback actually fetched static body textures');
  s.setEmotion('normal'); s.setEmotion(label); f.ticker.tick();
  assert.equal(f.sessions.length, 1); assert.equal(f.warnings.length, 1);
  source.fail(); assert.equal(s.getExperimentalFrameVideoStats().failures, 1);
  s.setExperimentalFrameVideoSource(label, f.videoDescriptor({ count: 100, head: 3, allReady: true }));
  assert.equal(f.sessions.length, 2); assert.equal(s.getExperimentalFrameVideoStats().failed, false);
});

test('context loss of a retained frozen output unfreezes into a static cold hold without losing hold identity', async t => {
  const f = fixture(t, { auto: false }), s = f.sprite;
  const source = install(f, { count: 100, head: 3, interval: 20 });
  await flush(); f.complete(`${label}:0`); await flush();
  source.arrive(5); s._showFrame(5); s.holdFrame(null);
  const old = s.sprite.texture;
  assert.equal(source.destroyed, 1); assert.equal(old.destroyed, false);
  source.fail(Error('synthetic WebGL context loss'));
  assert.equal(s.getExperimentalFrameVideoStats().failed, true);
  assert.equal(s._videoFrozenHold, false); assert.equal(s._held, true);
  assert.equal(s._heldFrameIdx, 5); assert.equal(s._activeFrameIdx, 5);
  assert.equal(s.sprite.texture, old, 'the old output remains owned until its static replacement');
  assert.deepEqual(f.pins.get('hold'), [`${label}:5`]);
  await f.finishCancelled(); f.complete(`${label}:5`); await flush();
  assert.equal(s.sprite.texture.kind, 'static'); assert.equal(s.sprite.texture.sourceIndex, 5);
  assert.equal(old.destroyed, true); assert.equal(source.retained.size, 0);
  assert.equal(s.getExperimentalFrameVideoStats().staticBodyFallbackDemands, 1);
  assert.equal(f.loads.filter(call => call.url === `${label}:5`).length, 1);
});

test('request, upload and creation errors share the observable resource fallback', async t => {
  for (const operation of ['requestWindow', 'texture', 'createSource']) {
    const f = fixture(t), s = f.sprite;
    if (operation === 'createSource') {
      s.setExperimentalFrameVideoSource(label, f.videoDescriptor({ head: 3, createError: Error('unavailable') }));
      s.loadFrames(label, urls(100)); s.setEmotion(label);
      s._showFrame(5);
    } else {
      const source = install(f, { count: 100, head: 3, allReady: true });
      await settle(f);
      source[operation] = () => { throw Error(`synthetic ${operation} error`); };
      s._showFrame(5);
    }
    await settle(f);
    assert.equal(s.getExperimentalFrameVideoStats().failed, true, operation);
    assert.equal(s.getExperimentalFrameVideoStats().failures, 1, operation);
    assert.equal(s._frameVideoSource, null, operation);
    assert.ok(f.loads.some(row => row.url === `${label}:5`), operation);
  }
});

test('a synchronous factory error callback releases the newly created source and cannot install it after fallback', async t => {
  const f = fixture(t), s = f.sprite;
  const descriptor = f.videoDescriptor({ head: 3, count: 100 });
  s.setExperimentalFrameVideoSource(label, { headFrameCount: 3, createSource(callbacks) {
    const source = descriptor.createSource(callbacks);
    callbacks.onError(Error('synthetic synchronous initialization error'));
    return source;
  } });
  s.loadFrames(label, urls(100)); s.setEmotion(label);
  await settle(f);
  assert.equal(s._frameVideoSource, null);
  assert.equal(s.getExperimentalFrameVideoStats().failed, true);
  assert.equal(f.sessions[0].released, 1); assert.equal(f.sessions[0].destroyed, 1);
});

test('deactivate, reactivate and unload release source/output ownership without stale display writes', async t => {
  const f = fixture(t), s = f.sprite;
  const source = install(f, { count: 100, head: 3, allReady: true });
  await settle(f); s._showFrame(5);
  const old = s.sprite.texture;
  s.setActive(false);
  assert.equal(old.destroyed, true); assert.equal(source.retained.size, 0);
  assert.equal(source.released, 1); assert.equal(source.destroyed, 1);
  source.arrive(5); assert.equal(s.sprite.texture.height, 1);
  s.setActive(true); await settle(f);
  const next = f.sessions.at(-1); assert.notEqual(next, source);
  s._showFrame(5); const display = s.sprite.texture;
  f.unload();
  assert.equal(display.destroyed, true); assert.equal(next.retained.size, 0);
  assert.equal(next.released, 1); assert.equal(next.destroyed, 1);
  assert.equal(f.disposals, 2);
});

test('explicit source removal during a frozen hold hands the held source identity back to FrameStore', async t => {
  const f = fixture(t, { auto: false }), s = f.sprite;
  const source = install(f, { count: 100, head: 3, interval: 20 });
  await flush(); f.complete(`${label}:0`); await flush();
  source.arrive(5); s._showFrame(5); s.holdFrame(null);
  const old = s.sprite.texture;
  s.setExperimentalFrameVideoSource(label, null);
  assert.equal(s._videoFrozenHold, false); assert.equal(s._held, true);
  assert.deepEqual(f.pins.get('hold'), [`${label}:5`]);
  assert.equal(s.sprite.texture, old); assert.equal(old.destroyed, false);
  await f.finishCancelled(); f.complete(`${label}:5`); await flush();
  assert.equal(s.sprite.texture.kind, 'static'); assert.equal(s.sprite.texture.sourceIndex, 5);
  assert.equal(old.destroyed, true); assert.equal(s.getExperimentalFrameVideoStats().installed, false);
});

test('mouth configuration and multiple video labels preserve exact holds and isolate failures', async t => {
  const f = fixture(t, { fps: 60 }), s = f.sprite;
  const names = ['speaking-a', 'speaking-b'];
  assert.throws(() => s.setExperimentalFrameVideoSource('', f.videoDescriptor()), /named clip/);
  for (const name of names) {
    s.loadFrames(name, Array.from({length: 30}, (_, i) => `${name}:${i}`));
    s.setIdleFrameIntervalMs(name, 17);
    s.loadMouthConfig(name, { frameUrls: [`${name}-mouth:0`], closedFrameIdx: 7,
      opennessByFrame: Array.from({length: 30}, (_, i) => i === 7 ? 0 : 1),
      anchorTrack: Array.from({length: 30}, (_, i) => ({cx:i, cy:i+1})) });
    s.setExperimentalFrameVideoSource(name, f.videoDescriptor({head:3, count:30, allReady:true}));
  }
  s.setEmotion(names[0]); s.setSpeaking(true); await settle(f);
  s._showFrame(9);
  const a = f.sessions.at(-1), tex = s.sprite.texture;
  assert.equal(s._getMouthAnchor(s._mouthConfigs[names[0]]).cx, 9);
  s.setEmotion(names[1]);
  const b = f.sessions.at(-1);
  assert.equal(a.destroyed, 1); assert.notEqual(a,b);
  assert.equal(s.getExperimentalFrameVideoStats().installedClips, 2);
  await settle(f); s._showFrame(10);
  assert.equal(tex.destroyed, true);
  a.fail();
  assert.equal(s.getExperimentalFrameVideoStats().failed, false, 'obsolete A cannot fail B');
  s.holdClosedFrame(); await settle(f);
  assert.equal(s._heldFrameIdx, 7); assert.equal(s._activeFrameIdx, 7);
  assert.equal(s.sprite.texture.kind, 'static');
  assert.equal(s._getMouthAnchor(s._mouthConfigs[names[1]]).cx, 7);
  s.clearHold(); f.sessions.at(-1).fail();
  assert.equal(s.getExperimentalFrameVideoStats(names[1]).failed, true);
  assert.equal(s.getExperimentalFrameVideoStats(names[0]).failed, false);
  s.setEmotion(names[0]);
  assert.ok(s._frameVideoSource);
  s.loadMouthConfig(names[0], {frameUrls: [], closedFrameIdx: 6});
  assert.equal(s._mouthConfigs[names[0]].closedFrameIdx, 6);
});


test('a 5ms one-shot video preserves exact static clock, terminal frame, holds and interrupted reentry', async t => {
  for (const sampling of [false, true]) {
    const baseline = fixture(t, { sampling, fps: 60 }), hybrid = fixture(t, { sampling, fps: 60 });
    const name = 'trans_smile', count = 120, frames = Array.from({ length: count }, (_, i) => `${name}:${i}`);
    const b = baseline.sprite, h = hybrid.sprite;
    let bc = 0, hc = 0;
    for (const s of [b, h]) {
      s.loadFrames('normal', ['normal:0']);
      s.loadFrames(name, frames); s.setIdleFrameIntervalMs(name, 5);
      s.setClipConfig(name, { loopMode: 'once_then_hold' });
    }
    h.setExperimentalFrameVideoSource(name, hybrid.videoDescriptor({ count, allReady: true }));
    b._frameStore.replaceDemand('test-readiness', frames.map(url => ({ url, priority: 100 })));
    h._frameStore.replaceDemand('test-head-readiness', frames.slice(0, 18).map(url => ({ url, priority: 100 })));
    b.setCycleCompleteHandler(() => bc++); h.setCycleCompleteHandler(() => hc++);
    b.setEmotion(name); h.setEmotion(name);
    await settle(baseline); await settle(hybrid);
    const compare = () => assert.deepEqual(snapshot(h, hc), snapshot(b, bc));
    const tick = async ms => { baseline.ticker.tick(ms); hybrid.ticker.tick(ms); await flush(); compare(); };
    compare();
    for (let i = 0; i < 12; i++) await tick(16.667);
    assert.equal(h.sprite.texture.kind, 'video');
    b.holdFrame(null); h.holdFrame(null); compare();
    await tick(80);
    b.holdFrame(42); h.holdFrame(42); await settle(baseline); await settle(hybrid); compare();
    assert.equal(h._activeFrameIdx, 42); assert.equal(h.sprite.texture.kind, 'static');
    b.clearHold(); h.clearHold();
    b.setEmotion('normal'); h.setEmotion('normal'); await flush(); compare();
    assert.ok(hybrid.sessions.every(source => source.destroyed === 1 && source.retained.size === 0),
      'interrupted output ownership releases after a ready alternate frame');
    b.setEmotion(name); h.setEmotion(name); compare();
    for (let i = 0; i < 45; i++) await tick(16.667);
    assert.equal(bc, 1); assert.equal(hc, 1);
    assert.equal(h._activeFrameIdx, count - 1); assert.equal(h.sprite.texture.kind, 'static');
    const end = snapshot(h, hc);
    for (let i = 0; i < 10; i++) await tick(16.667);
    assert.equal(hc, 1); assert.equal(h._activeFrameIdx, end.active);
    assert.equal(h._frames[name].length, count);
    assert.equal(h.getExperimentalFrameVideoStats().failures, 0);
  }
});


test('a cold video successor cannot adopt the previous clip display as its own frozen hold', async t => {
  const f=fixture(t,{auto:false,fps:60}),s=f.sprite;
  const old=install(f,{count:30,head:3,allReady:true});
  s._showFrame(8); const texture=s.sprite.texture;
  s.loadFrames('next',Array.from({length:30},(_,i)=>`next:${i}`));
  s.setExperimentalFrameVideoSource('next',f.videoDescriptor({count:30,head:3}));
  s.setEmotion('next');
  assert.equal(s.sprite.texture,texture);
  s.holdFrame(null);
  assert.equal(s._videoFrozenHold,false);
  assert.equal(s._heldFrameIdx,8);
  assert.deepEqual(f.pins.get('hold'),['next:8']);
  assert.equal(texture.destroyed,false);
  await f.finishCancelled(); await flush();
  f.complete('next:8'); await flush();
  assert.equal(s.sprite.texture.sourceIndex,8);assert.equal(s.sprite.texture.kind,'static');
  assert.equal(texture.destroyed,true);
});


test('an exact static hold never expands entry hints into video-body loads after resampling', async t => {
  const f=fixture(t),s=f.sprite;
  install(f,{count:100,head:3,interval:20,allReady:true});
  s.prefetchLabels([label],'interactive',{reason:'trigger',headMs:750});
  await settle(f);
  const held=Array.from({length:25},(_,i)=>i+3).find(i=>s._sampleFrameIndex(label,i)!==i);
  assert.ok(held>=3);
  s.holdFrame(held);await settle(f);
  assert.deepEqual(f.demands.get('prefetch:trigger').filter(row=>Number(row.url.split(':').at(-1))>=3),[]);
  s.clearHold();await settle(f);
  assert.deepEqual(f.loads.filter(row=>{const i=Number(row.url.split(':').at(-1));return i>=3&&i<99;}).map(row=>Number(row.url.split(':').at(-1))),[held]);
});


test('freezing a healthy video cannot suppress a failed successor static prefetch', async t => {
  const f = fixture(t, { sampling: false }), s = f.sprite;
  install(f, { count: 40, head: 3, interval: 20, allReady: true });
  const failed = 'failed-successor';
  s.loadFrames(failed, Array.from({ length: 40 }, (_, i) => `${failed}:${i}`));
  s.setIdleFrameIntervalMs(failed, 20);
  s.setExperimentalFrameVideoSource(failed, f.videoDescriptor({ head: 3, count: 40, allReady: true }));
  s.setEmotion(failed);
  f.sessions.at(-1).fail();
  s.setEmotion(label); await settle(f);
  s._showFrame(8); s.holdFrame(null);
  assert.equal(s._videoFrozenHold, true);
  s.prefetchLabels([failed], 'interactive', { reason: 'failed-next', headMs: 120 });
  await settle(f);
  assert.ok(f.demands.get('prefetch:failed-next').some(row => row.url === `${failed}:5`),
    'the failed successor needs its body in the static prefetch while another video is frozen');
  assert.ok(!f.demands.get('current').some(row => row.url === `${label}:8`),
    'the healthy frozen display still needs no redundant static hold');
  s.clearHold(); await settle(f);
  assert.ok(f.demands.get('prefetch:failed-next').some(row => row.url === `${failed}:5`));
});
