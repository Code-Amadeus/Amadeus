const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const budget = require('../render/web/render_budget.js');
const storeApi = require('../render/web/frame_store.js');

const source = fs.readFileSync(path.join(__dirname, '../render/web/renderer.js'), 'utf8');
const start = source.indexOf('class SpriteRenderer {');
const end = source.indexOf('class SpriteForgeRuntime {', start);
assert.ok(start >= 0 && end > start, 'the renderer class is available to the VM harness');

async function flush() {
  for (let i = 0; i < 16; i++) await Promise.resolve();
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve, done: false };
}

class Display {
  constructor() {
    this.anchor = { set() {} };
    this.scale = { set() {} };
    this.texture = { width: 1, height: 1 };
    this.visible = true;
  }
  addChild() {}
  clear() { this.ellipse = null; }
  beginFill() {}
  drawEllipse(...values) { this.ellipse = values; }
  endFill() {}
}

function fixture(t, { budgetBytes = 100, sampling = false, fps = 30 } = {}) {
  let time = 0, nextTexture = 0, nextTimer = 0, unload, randomCalls = 0;
  const loads = [], uploads = [], destroyed = [], timers = new Map();
  const demands = new Map();
  const guardedMath = Object.create(Math);
  guardedMath.random = () => { randomCalls++; assert.fail('texture prefetch cannot consume a graph random draw'); };
  const backend = {
    now: () => ++time,
    load(url, signal) {
      const pending = deferred();
      loads.push({ url, signal, pending });
      return pending.promise;
    },
    upload(texture) {
      const pending = deferred();
      uploads.push({ texture, pending });
      return pending.promise;
    },
    destroy(texture) {
      assert.equal(texture.destroyed, false, 'an owned texture is destroyed once');
      texture.destroyed = texture.baseTexture.destroyed = true;
      texture.payload = null;
      destroyed.push(texture);
    },
    dispose() {},
  };
  const ticker = {
    deltaMS: 1000 / fps, running: true,
    add(callback) { this.callback = callback; },
    stop() { this.running = false; },
    tick(milliseconds = 100) {
      assert.ok(this.running, 'the test explicitly advances a running ticker');
      this.deltaMS = milliseconds;
      this.callback(1);
    },
  };
  const context = {
    app: { ticker, screen: { width: 1200, height: 800 }, renderer: {} },
    frameRateController: { effectiveMaxFps: fps }, graphicsProfile: 'standard',
    renderBudget: budget.resolveRenderBudget({ maxFps: fps, textureSampling: sampling }),
    PIXI: { Container: Display, Sprite: Display, Graphics: Display }, Math: guardedMath,
    window: {
      RenderBudget: budget,
      FrameTextureBackend: { createFrameTextureBackend: () => backend },
      FrameStore: { createFrameStore: options => {
        const store = storeApi.createFrameStore({ ...options, budgetBytes });
        const replaceDemand = store.replaceDemand;
        store.replaceDemand = (owner, requests, options) => {
          const snapshot = Array.from(requests, request => ({ ...request }));
          demands.set(owner, snapshot);
          replaceDemand(owner, snapshot, options);
        };
        return store;
      } },
      addEventListener(name, callback) { if (name === 'unload') unload = callback; },
    },
    console: { log() {}, warn() {}, error() {} },
    setTimeout(callback) { const id = ++nextTimer; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end) + '\nglobalThis.SpriteRenderer=SpriteRenderer;', context);
  const sprite = new context.SpriteRenderer(new Display());
  t.after(() => unload());

  function finishLoad(url) {
    const call = loads.find(item => item.url === url && !item.pending.done);
    assert.ok(call, `a load for ${url} is pending; observed ${loads.map(item => item.url)}`);
    call.pending.done = true;
    const texture = { url, id: ++nextTexture, width: 100, height: 100, destroyed: false,
      baseTexture: { valid: true, destroyed: false }, payload: new Uint8Array(4), signal: call.signal };
    call.pending.resolve({ texture, cpuBytes: 4, gpuBytes: 6 });
    return texture;
  }
  function finishUpload(url) {
    const call = uploads.find(item => item.texture.url === url && !item.pending.done);
    assert.ok(call, `an upload for ${url} is pending`);
    call.pending.done = true;
    call.pending.resolve();
  }
  async function complete(url) {
    const texture = finishLoad(url);
    await flush();
    finishUpload(url);
    await flush();
    return texture;
  }
  async function finishCancelled() {
    for (let round = 0; round < 8; round++) {
      const cancelledLoads = loads.filter(item => item.signal.aborted && !item.pending.done);
      const cancelledUploads = uploads.filter(item => item.texture.signal.aborted && !item.pending.done);
      if (!cancelledLoads.length && !cancelledUploads.length) return;
      for (const call of cancelledLoads) finishLoad(call.url);
      for (const call of cancelledUploads) finishUpload(call.texture.url);
      await flush();
    }
    assert.fail('canceled backend work settles within the bounded completion steps');
  }
  return { sprite, ticker, loads, uploads, destroyed, demands, complete, finishLoad, finishUpload, finishCancelled,
    get randomCalls() { return randomCalls; },
    withRandom(value, select) {
      const original = guardedMath.random;
      guardedMath.random = () => { randomCalls++; return value; };
      try { return select(); } finally { guardedMath.random = original; }
    },
    runtime() {
      const runtimeEnd = source.indexOf('class Live2DRenderer {', end);
      assert.ok(runtimeEnd > end);
      vm.runInContext(source.slice(end, runtimeEnd) + '\nglobalThis.SpriteForgeRuntime=SpriteForgeRuntime;', context);
      return new context.SpriteForgeRuntime(sprite);
    },
    runTimer() {
      const first = timers.entries().next().value;
      assert.ok(first, 'a transition timer is pending');
      timers.delete(first[0]); first[1]();
    } };
}

test('one-shot heads cover 750ms while loop heads stay finite at 250ms without changing source clocks or drawing RNG', async t => {
  for (const sampling of [false, true]) {
    const f = fixture(t, { budgetBytes: 500, sampling }), s = f.sprite;
    const once = Array.from({ length: 21 }, (_, i) => 'once-' + i);
    const loop = Array.from({ length: 21 }, (_, i) => 'loop-' + i);
    s.loadFrames('one-shot', once);
    s.loadFrames('loop', loop);
    s.setIdleFrameIntervalMs('one-shot', 50);
    s.setIdleFrameIntervalMs('loop', 50);
    s.setClipConfig('one-shot', { loopMode: 'once_then_hold' });
    s.setClipConfig('loop', { loopMode: 'loop' });
    s._frameIdx = 17; s._activeFrameIdx = 9; s._sampleTimeMs = 340;
    s.prefetchLabels(['one-shot', 'loop'], 'interactive', { reason: 'head-contract' });
    const requests = f.demands.get('prefetch:head-contract');
    assert.deepEqual(requests.filter(row => once.includes(row.url)).map(row => once.indexOf(row.url)),
      Array.from({ length: 16 }, (_, i) => i));
    assert.deepEqual(requests.filter(row => loop.includes(row.url)).map(row => loop.indexOf(row.url)),
      Array.from({ length: 6 }, (_, i) => i));
    for (let round = 0; round < 32; round++) {
      await flush();
      const pending = f.loads.filter(call => !call.pending.done && !call.signal.aborted);
      if (!pending.length) break;
      for (const call of pending) await f.complete(call.url);
    }
    assert.equal(s._frameStore.stats().queued, 0);
    assert.equal(s._frames['one-shot'][12].url, once[12], 'the authored 600ms transition source is prepared');
    assert.equal(s._frames['one-shot'].length, once.length);
    assert.equal(s._frames.loop.length, loop.length);
    assert.equal(Boolean(s._frames['one-shot'][16]), false, 'a finite head does not preload the entire clip');
    assert.equal(Boolean(s._frames.loop[6]), false, 'repeatable clips retain their shorter head');
    assert.equal(s._frameIdx, 17);
    assert.equal(s._activeFrameIdx, 9);
    assert.equal(s._sampleTimeMs, 340);
    assert.equal(s._currentEmotion, 'normal');
    assert.equal(f.randomCalls, 0);
  }
});

test('repeated trigger hints replace prior URL needs and cancel obsolete work through the actual store', async t => {
  const f = fixture(t, { budgetBytes: 200 }), s = f.sprite, runtime = f.runtime();
  s.loadFrames('first', ['first-0', 'first-1', 'first-2']);
  s.loadFrames('second', ['second-0', 'second-1', 'second-2']);
  s.setSpeaking(true); runtime.speechActive = true;
  runtime.loadGraph({ rootNodeId: 'root', graph: { nodes: [
    { id: 'root', label: 'normal', isRoot: true }, { id: 'a', label: 'first' }, { id: 'b', label: 'second' },
  ], edges: [{ from: 'root', to: 'a', prob: 0 }, { from: 'root', to: 'b', prob: 0 }] } });
  // Isolate trigger replacement from the independent graph-entry retention.
  s.prefetchLabels([], 'warm', { reason: 'graph-entries' });
  runtime._prefetchForTrigger('first', 'interactive');
  await flush();
  const old = f.loads.find(call => call.url === 'first-1');
  assert.ok(old && !old.signal.aborted, 'the first non-poster head is actually loading');
  runtime._prefetchForTrigger('second', 'interactive');
  runtime._prefetchForTrigger('second', 'interactive');
  await flush();
  assert.equal(old.signal.aborted, true);
  const requested = [...f.demands.values()].flat().map(row => row.url);
  assert.equal(requested.some(url => url.startsWith('first-')), false, 'superseded trigger needs do not accumulate');
  assert.ok(requested.includes('second-1'));
  await f.finishCancelled();
  assert.ok(f.loads.some(call => call.url === 'second-1'));
  assert.equal(s._frames.first.length, 3);
  assert.equal(s._frames.second.length, 3);
  assert.equal(runtime.currentNodeId, 'root', 'hint replacement does not select a new graph node');
  assert.equal(f.randomCalls, 0);
});

test('speech pauses warm starts but preserves resident heads and resumes after the quiet interval', async t => {
  const f = fixture(t, { budgetBytes: 80 }), s = f.sprite;
  s.loadFrames('normal', ['body']);
  s.loadFrames('entry', ['head-0', 'head-1', 'head-2']);
  s.prefetchLabels(['entry'], 'warm', { reason: 'graph-entries' });
  await flush();
  await f.complete('body'); await f.complete('head-0');
  const head = await f.complete('head-1');
  s.setSpeaking(true);
  await flush();
  assert.equal(f.demands.get('prefetch:graph-entries').length, 3);
  assert.equal(head.destroyed, false);
  assert.ok(f.loads.filter(call => call.url === 'head-2').every(call => !call.signal.aborted), 'pausing does not cancel active work');
  s.loadFrames('late', ['late-0', 'late-1']);
  s.prefetchLabels(['entry', 'late'], 'warm', { reason: 'graph-entries' });
  await flush();
  assert.equal(f.loads.some(call => call.url === 'late-1'), false);
  s.setSpeaking(false);
  await flush();
  assert.equal(f.loads.some(call => call.url === 'late-1'), false);
  s._lastSpeechStateChangedAt = Date.now() - 901;
  s._refreshTextureDemand();
  for (const call of f.loads.filter(call => !call.pending.done)) await f.complete(call.url);
  await flush();
  assert.ok(f.loads.some(call => call.url === 'late-1'));
  assert.equal(head.destroyed, false);
});

test('current loop retains visited frames without eagerly decoding the entire cycle', async t => {
  const f = fixture(t, { budgetBytes: 160 }), s = f.sprite;
  const urls = Array.from({ length: 12 }, (_, i) => `lap-${i}`);
  s.loadFrames('normal', urls);
  s.setIdleFrameIntervalMs('normal', 150);
  const settle = async () => {
    await flush();
    for (let round = 0; round < 20; round++) {
      const pending = f.loads.filter(call => !call.pending.done);
      if (!pending.length) break;
      for (const call of pending) await f.complete(call.url);
    }
  };
  await settle();
  assert.ok(f.loads.length < urls.length, 'retention alone does not start far-future decodes');
  for (let lap = 0; lap < 3; lap++) {
    const before = f.loads.length;
    for (let i = 0; i < urls.length; i++) { s._frameIdx = i; s._showFrame(i); await settle(); }
    if (lap > 0) assert.equal(f.loads.length - before, 0);
    assert.ok(s._frameStore.stats().residentBytes <= 160);
  }
  s.holdFrame(3);
  assert.equal(f.demands.get('current-cycle').length, 0);
  assert.equal(s.sprite.texture.url, 'lap-3');
});

test('all positive graph neighbors are warm outside speech, eligible during speech, and withdrawn at an empty neighborhood', async t => {
  const f = fixture(t, { budgetBytes: 200 }), s = f.sprite, runtime = f.runtime();
  s.loadFrames('normal', ['normal']);
  s.loadFrames('next', ['next-0', 'next-1', 'next-2']);
  s.loadFrames('rare', ['rare-0', 'rare-1', 'rare-2']);
  runtime.loadGraph({ rootNodeId: 'root', graph: { nodes: [
    { id: 'root', label: 'normal', isRoot: true }, { id: 'leaf', label: 'next' }, { id: 'rare', label: 'rare' },
  ], edges: [{ from: 'root', to: 'leaf', prob: 0.95 }, { from: 'root', to: 'rare', prob: 0.05 }] } });
  assert.ok(f.demands.get('prefetch:graph-next').some(row => row.url === 'next-1' && row.priority === 65));
  assert.ok(f.demands.get('prefetch:graph-next').some(row => row.url === 'rare-1' && row.priority === 65));
  s.setSpeaking(true); runtime.speechActive = true;
  runtime._prefetchNodeNeighborhood('root');
  assert.ok(f.demands.get('prefetch:graph-next').some(row => row.url === 'next-1' && row.priority === 90));
  assert.ok(f.demands.get('prefetch:graph-next').some(row => row.url === 'rare-1' && row.priority === 90));
  await flush();
  await f.complete('normal'); await f.complete('next-0'); await f.complete('rare-0');
  const next = f.loads.find(call => call.url === 'next-1');
  assert.ok(next && !next.signal.aborted, 'speech does not defer the immediate successor head');
  const rare = f.loads.find(call => call.url === 'rare-1');
  assert.ok(rare && !rare.signal.aborted, 'the five-percent successor also prepares before a decision');
  const loadCount = f.loads.length;
  runtime._prefetchNodeNeighborhood('leaf');
  await flush();
  assert.equal(f.demands.get('prefetch:graph-next').length, 0);
  assert.equal(next.signal.aborted, true);
  assert.equal(rare.signal.aborted, true);
  s.setSpeaking(false); runtime.speechActive = false;
  s._lastSpeechStateChangedAt = 0;
  s._refreshTextureDemand();
  await f.finishCancelled();
  assert.equal(f.loads.length, loadCount, 'quiet playback cannot revive any obsolete neighbor');
  assert.equal(Boolean(s._frames.next[1]), false);
  assert.equal(Boolean(s._frames.rare[1]), false);
  assert.equal(runtime.currentNodeId, 'root');
  assert.equal(f.randomCalls, 0);
});

test('the five-percent successor head is resident before the ordinary selection draw and displays its original source', async t => {
  const f = fixture(t, { budgetBytes: 200 }), s = f.sprite, runtime = f.runtime();
  const rareUrls = Array.from({ length: 8 }, (_, i) => 'rare-source-' + i);
  s.loadFrames('normal', ['normal']);
  s.loadFrames('common', Array.from({ length: 8 }, (_, i) => 'common-source-' + i));
  s.loadFrames('rare', rareUrls);
  s.setIdleFrameIntervalMs('common', 100);
  s.setIdleFrameIntervalMs('rare', 100);
  runtime.loadGraph({ rootNodeId: 'root', graph: { nodes: [
    { id: 'root', label: 'normal', isRoot: true }, { id: 'common', label: 'common' }, { id: 'rare', label: 'rare' },
  ], edges: [{ from: 'root', to: 'common', prob: 0.95 }, { from: 'root', to: 'rare', prob: 0.05 }] } });
  for (let round = 0; round < 16; round++) {
    await flush();
    const pending = f.loads.filter(call => !call.pending.done && !call.signal.aborted);
    if (!pending.length) break;
    for (const call of pending) await f.complete(call.url);
  }
  assert.equal(s._frameStore.stats().queued, 0);
  assert.ok(s._frames.rare[1] && s._frames.rare[3], 'rare successor sources are ready before selecting a winner');
  assert.equal(Boolean(s._frames.rare[4]), false, 'the prepared loop head remains finite');
  assert.equal(f.randomCalls, 0, 'preparing all neighbors consumes no draw');
  const selected = f.withRandom(0.99, () => runtime._nextAutoNode('root'));
  assert.equal(selected, 'rare');
  assert.equal(f.randomCalls, 1, 'the ordinary graph decision retains its single draw');
  const misses = s._displayMisses;
  runtime._playNode(selected);
  f.ticker.tick(100);
  assert.equal(runtime.currentNodeId, 'rare');
  assert.equal(s.sprite.texture.url, rareUrls[1]);
  assert.equal(s._activeFrameIdx, 1);
  assert.equal(s._frames.rare.length, rareUrls.length);
  assert.equal(s._displayMisses, misses, 'the rare node can advance from its poster without a missing source');
  assert.equal(f.randomCalls, 1);
});

test('full release withdraws trigger needs while an active after-speech handoff retains them', async t => {
  const f = fixture(t, { budgetBytes: 200 }), s = f.sprite, runtime = f.runtime();
  s.loadFrames('normal', ['normal']);
  s.loadFrames('target', ['target-0', 'target-1', 'target-2']);
  runtime.loadGraph({ rootNodeId: 'root', graph: { nodes: [
    { id: 'root', label: 'normal', isRoot: true }, { id: 'target', label: 'target' },
  ], edges: [] } });
  runtime._prefetchForTrigger('target', 'interactive');
  runtime.pendingExpression = 'target'; runtime.postSpeechHoldActive = true;
  await flush();
  const pending = f.loads.find(call => call.url === 'target-1');
  assert.ok(pending && !pending.signal.aborted);
  runtime.release({ presentation_handoff: 'after_speech' });
  await flush();
  assert.ok(f.demands.get('prefetch:trigger').some(row => row.url === 'target-1'));
  assert.equal(pending.signal.aborted, false);
  assert.equal(runtime.pendingExpression, 'target');
  assert.equal(runtime.postSpeechHoldActive, true);
  runtime.release();
  await flush();
  assert.equal(f.demands.get('prefetch:trigger').length, 0);
  assert.equal(pending.signal.aborted, true);
  assert.equal(runtime.pendingExpression, null);
  assert.equal(runtime.postSpeechHoldActive, false);
  await f.finishCancelled();
  assert.equal(f.randomCalls, 0);
});

test('aliases share one allocation and eviction clears every registered source slot', async t => {
  const f = fixture(t, { budgetBytes: 30 }), s = f.sprite;
  s.loadFrames('normal', ['poster-normal', 'shared']);
  s.loadFrames('happy', ['poster-happy', 'shared']);
  s.prefetchLabels(['happy'], 'interactive');
  await flush();
  await f.complete('poster-normal');
  await f.complete('poster-happy');
  const shared = await f.complete('shared');
  assert.equal(s._frames.normal[1], shared);
  assert.equal(s._frames.happy[1], shared);
  assert.equal(f.loads.filter(call => call.url === 'shared').length, 1);
  assert.equal(s._frameStore.stats().residentBytes, 30);

  s.holdFrame(0);
  s.prefetchLabels([], 'interactive');
  s.loadFrames('pressure', ['new-poster']);
  await flush();
  await f.complete('new-poster');
  assert.equal(shared.destroyed, true);
  assert.equal(s._frames.normal[1], null);
  assert.equal(s._frames.happy[1], null);
  assert.equal(s._frames.normal.length, 2);
  assert.equal(s._frames.happy.length, 2);
});

test('same URLs remain resident and shorter replacement ignores late removed indices', async t => {
  const f = fixture(t), s = f.sprite;
  s.loadFrames('normal', ['a', 'b', 'c']);
  await flush();
  const a = await f.complete('a');
  s.loadFrames('normal', ['a', 'b', 'c']);
  assert.equal(s._frames.normal.length, 3);
  assert.equal(s._frames.normal[0], a);
  s.loadFrames('normal', ['a']);
  await f.finishCancelled();
  assert.equal(s._frames.normal.length, 1);
  assert.equal(s._frames.normal[0], a);
  assert.equal(s.sprite.texture, a);
  s.loadFrames('normal', ['a', 'a']);
  await flush();
  assert.equal(s._frames.normal.length, 2);
  assert.equal(s._frames.normal[0], a);
  assert.equal(s._frames.normal[1], a);
  assert.equal(f.loads.filter(call => call.url === 'a').length, 1);
});

test('a cold explicit hold publishes its exact source and mouth anchor without a ticker', async t => {
  const f = fixture(t, { sampling: true }), s = f.sprite;
  s.loadFrames('normal', Array.from({ length: 16 }, (_, i) => 'frame-' + i));
  s.setIdleFrameIntervalMs('normal', 17);
  s.loadMouthConfig('normal', { frameUrls: [], anchorTrack: Array.from({ length: 16 }, (_, i) => ({ cx: i })) });
  f.ticker.stop();
  await flush();
  await f.complete('frame-0');
  const requested = Array.from({ length: 16 }, (_, i) => i)
    .find(i => s._sampleFrameIndex('normal', i) !== i);
  assert.ok(Number.isInteger(requested), 'this hold requests an omitted source frame');
  s.holdFrame(requested);
  assert.equal(s.sprite.texture.url, 'frame-0');
  assert.equal(s._activeFrameIdx, 0);
  assert.equal(s._getMouthAnchor(s._mouthConfigs.normal).cx, 0);
  await f.finishCancelled();
  const held = f.finishLoad('frame-' + requested);
  await flush();
  assert.equal(s.sprite.texture.url, 'frame-0', 'upload completion gates visibility');
  f.finishUpload('frame-' + requested);
  await flush();
  assert.equal(s.sprite.texture, held);
  assert.equal(s._activeFrameIdx, requested);
  assert.equal(s._getMouthAnchor(s._mouthConfigs.normal).cx, requested);
});

test('emotion handoff pins the actual old display until the successor upload is ready', async t => {
  const f = fixture(t, { budgetBytes: 20 }), s = f.sprite;
  s.loadFrames('normal', ['normal-poster', 'old-display']);
  s.setIdleFrameIntervalMs('normal', 100);
  await flush();
  await f.complete('normal-poster');
  const previous = await f.complete('old-display');
  f.ticker.tick();
  assert.equal(s.sprite.texture, previous);
  s.loadFrames('happy', ['next-poster']);
  s.setEmotion('happy');
  await flush();
  assert.equal(s.sprite.texture, previous);
  assert.equal(previous.destroyed, false);
  assert.equal(s._activeFrameIdx, 1);
  const successor = f.finishLoad('next-poster');
  await flush();
  assert.equal(s.sprite.texture, previous);
  assert.equal(previous.destroyed, false);
  f.finishUpload('next-poster');
  await flush();
  assert.equal(s.sprite.texture, successor);
  assert.equal(s._activeFrameIdx, 0);
  assert.equal(previous.destroyed, true, 'the obsolete display releases its budget claim after handoff');
});

test('a canceled old view completion cannot replace the new label or extend the removed array', async t => {
  const f = fixture(t), s = f.sprite;
  s.loadFrames('normal', ['old']);
  await flush();
  s.loadFrames('happy', ['new']);
  s.setEmotion('happy');
  s.loadFrames('normal', []);
  await flush();
  assert.equal(f.loads.find(call => call.url === 'old').signal.aborted, true);
  const current = await f.complete('new');
  const stale = f.finishLoad('old');
  await flush();
  assert.equal(stale.destroyed, true);
  assert.equal(s.sprite.texture, current);
  assert.equal(s._frames.normal.length, 0);
  assert.equal(s._frames.happy.length, 1);
});

test('legacy transition registration replacement keeps the playing queue alive until completion', async t => {
  const f = fixture(t, { budgetBytes: 10 }), s = f.sprite;
  s.loadFrames('normal', ['normal']);
  s.loadFrames('happy', ['happy']);
  s.loadTransitionFrames('normal', 'happy', ['transition-0', 'transition-1']);
  await flush();
  await f.complete('normal');
  const happy = await f.complete('happy');
  const first = await f.complete('transition-0');
  const second = await f.complete('transition-1');
  s.setEmotion('happy');
  assert.equal(s.sprite.texture, first);
  s.loadTransitionFrames('normal', 'happy', ['replacement']);
  await flush();
  await f.complete('replacement');
  assert.equal(first.destroyed, false);
  assert.equal(second.destroyed, false);
  f.runTimer();
  assert.equal(s.sprite.texture, second);
  assert.equal(first.destroyed, false);
  f.runTimer();
  await flush();
  assert.equal(s.sprite.texture, happy);
  assert.equal(first.destroyed, true);
  assert.equal(second.destroyed, true);
  assert.equal(s._transitions['normal->happy'].length, 1);
});

test('mouth replacement releases old resources and late arrivals cannot displace the refreshed overlay', async t => {
  const f = fixture(t, { budgetBytes: 10 }), s = f.sprite;
  s.loadFrames('normal', ['body']);
  await flush();
  await f.complete('body');
  s.setMouth(0.7);
  s.loadMouthConfig('normal', { frameUrls: ['mouth-old', 'mouth-late'], openness: [0.7, 1], cx: 0, cy: 0, width: 8, height: 4 });
  await flush();
  const old = await f.complete('mouth-old');
  assert.equal(s._mouthOverlay.visible, true);
  assert.equal(s._mouthOverlay.texture, old);
  const config = { frameUrls: ['mouth-new'], openness: [0.7], cx: 0, cy: 0, width: 8, height: 4 };
  s.loadMouthConfig('normal', config);
  s.loadMouthConfig('normal', config);
  f.ticker.stop();
  await flush();
  assert.equal(s._mouthOverlay.visible, false);
  assert.equal(old.destroyed, true);
  const current = await f.complete('mouth-new');
  assert.equal(s._mouthOverlay.visible, true);
  assert.equal(s._mouthOverlay.texture, current);
  const stale = f.finishLoad('mouth-late');
  await flush();
  assert.equal(stale.destroyed, true);
  assert.equal(s._mouthTextures.normal.length, 1);
  assert.equal(s._mouthOverlay.texture, current);
  assert.equal(f.loads.filter(call => call.url === 'mouth-new').length, 1);
});

test('rolling lookahead under a tiny budget evicts behind playback and reloads an explicit return', async t => {
  const f = fixture(t, { budgetBytes: 30 }), s = f.sprite;
  s.loadFrames('normal', Array.from({ length: 10 }, (_, i) => 'rolling-' + i));
  s.setIdleFrameIntervalMs('normal', 100);
  await flush();
  await f.complete('rolling-0');
  const behind = await f.complete('rolling-1');
  const displayed = await f.complete('rolling-2');
  f.ticker.tick();
  f.ticker.tick();
  await flush();
  assert.equal(s.sprite.texture, displayed);
  await f.complete('rolling-3');
  assert.equal(behind.destroyed, true);
  assert.equal(s._frames.normal[1], null);
  assert.equal(s._frames.normal.length, 10);
  s.holdFrame(1);
  assert.equal(s.sprite.texture, displayed);
  await f.finishCancelled();
  const reloaded = await f.complete('rolling-1');
  assert.notEqual(reloaded, behind);
  assert.equal(s.sprite.texture, reloaded);
  assert.equal(s._activeFrameIdx, 1);
  assert.equal(f.loads.filter(call => call.url === 'rolling-1').length, 2);
  assert.ok(s._frameStore.stats().refetches >= 1);
  assert.equal(s._frames.normal.length, 10);
});

test('graph entry-head hints use existing edges and config without extra decisions or clock resets', () => {
  const runtimeStart = source.indexOf('class SpriteForgeRuntime {');
  const runtimeEnd = source.indexOf('class Live2DRenderer {', runtimeStart);
  assert.ok(runtimeStart >= 0 && runtimeEnd > runtimeStart);
  const hints = [], selections = [];
  let randomCalls = 0, autoDecisions = 0, holdsCleared = 0;
  const sprite = {
    emotion: 'normal', frameIndex: 23, timeMs: 460,
    clearHold() { holdsCleared++; },
    setEmotion(label) {
      selections.push(label);
      if (label !== this.emotion) { this.frameIndex = 0; this.timeMs = 0; }
      this.emotion = label;
    },
    prefetchLabels(labels, priority, options) {
      hints.push({ labels: Array.from(labels), priority, ...options,
        emotion: this.emotion, frameIndex: this.frameIndex, timeMs: this.timeMs });
    },
  };
  const guardedMath = Object.create(Math);
  guardedMath.random = () => { randomCalls++; assert.fail('entry hints cannot consume a random draw'); };
  const context = { Math: guardedMath, console: { log() {} },
    setTimeout() { assert.fail('loading entry hints does not schedule a graph transition'); },
    clearTimeout() {} };
  vm.createContext(context);
  vm.runInContext(source.slice(runtimeStart, runtimeEnd)
    + '\nglobalThis.SpriteForgeRuntime=SpriteForgeRuntime;', context);
  const runtime = new context.SpriteForgeRuntime(sprite);
  runtime._nextAutoNode = () => { autoDecisions++; assert.fail('entry hints cannot choose an automatic successor'); };
  const payload = {
    rootNodeId: 'root',
    graph: {
      nodes: [
        { id: 'first', label: 'unused-first-node' },
        { id: 'root', label: 'normal', isRoot: true },
        { id: 'manual', label: 'manual-entry' },
        { id: 'manual-alias', label: 'manual-entry' },
        { id: 'auto', label: 'automatic-common' },
        { id: 'rare', label: 'automatic-rare' },
        { id: 'nonroot', label: 'nonroot-manual-entry' },
      ],
      edges: [
        { from: 'root', to: 'manual', prob: 0 },
        { from: 'root', to: 'manual-alias', prob: 0 },
        { from: 'root', to: 'auto', prob: 0.95 },
        { from: 'root', to: 'rare', prob: 0.05 },
        { from: 'auto', to: 'nonroot', prob: 0 },
      ],
    },
    config: {
      defaultSpeakingTriggerLabel: 'speaking-entry',
      closedEyeSpeakingTriggerLabel: 'closed-eye-entry',
      emotionEntryByIntent: { happy: 'manual-entry', sad: 'configured-emotion-entry' },
      thinkingEntryLabels: ['thinking-entry', 'manual-entry'],
      seriousEntryLabels: ['serious-entry'],
      postSpeechEmotionLabelByIntent: { happy: 'post-speech-entry' },
    },
  };
  runtime.loadGraph(payload);
  const entries = hints.filter(hint => hint.reason === 'graph-entries');
  assert.equal(entries.length, 1);
  assert.equal(entries[0].priority, 'warm');
  assert.deepEqual(new Set(entries[0].labels), new Set([
    'speaking-entry', 'closed-eye-entry', 'manual-entry', 'configured-emotion-entry',
    'thinking-entry', 'serious-entry', 'post-speech-entry',
  ]));
  assert.equal(entries[0].labels.length, 7, 'aliases and duplicate config entries request one head');
  const neighbors = hints.find(hint => hint.reason === 'graph-next');
  assert.deepEqual(new Set(neighbors.labels), new Set(['automatic-common', 'automatic-rare']));
  assert.equal(neighbors.priority, 'warm');
  assert.equal(runtime.rootNodeId, 'root');
  assert.equal(runtime.currentNodeId, 'root');
  assert.deepEqual(selections, ['normal']);
  assert.ok(hints.every(hint => hint.emotion === 'normal' && hint.frameIndex === 23 && hint.timeMs === 460));

  // An identical reload during playback cannot restart the selected root or
  // create another future decision merely to refill the hint set.
  sprite.frameIndex = 31; sprite.timeMs = 740;
  const hintCount = hints.length;
  runtime.loadGraph(JSON.parse(JSON.stringify(payload)));
  assert.equal(hints.length, hintCount);
  assert.deepEqual(selections, ['normal']);
  assert.equal(holdsCleared, 1);
  assert.equal(runtime.currentNodeId, 'root');
  assert.equal(sprite.frameIndex, 31);
  assert.equal(sprite.timeMs, 740);
  assert.equal(randomCalls, 0);
  assert.equal(autoDecisions, 0);
});
