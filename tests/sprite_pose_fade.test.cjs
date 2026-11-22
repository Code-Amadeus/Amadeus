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
    this.scale = {
      x: 1,
      y: 1,
      set(a, b) { this.x = a; this.y = b === undefined ? a : b; },
    };
    this.texture = { width: 1, height: 1 };
    this.visible = true;
    this.alpha = 1;
  }
  addChild() {}
  clear() { this.ellipse = null; }
  beginFill() {}
  drawEllipse(...values) { this.ellipse = values; }
  endFill() {}
}

function fixture(t, { poseFadeMs = 200 } = {}) {
  let time = 0, nextTexture = 0, nextTimer = 0, unload;
  const loads = [], uploads = [], destroyed = [], timers = new Map();
  const demands = new Map(), pins = new Map();
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
    destroy(texture) { destroyed.push(texture); },
    dispose() {},
  };
  const ticker = {
    deltaMS: 1000 / 60, running: true,
    add(callback) { this.callback = callback; },
    stop() { this.running = false; },
    tick(milliseconds = 100) {
      assert.ok(this.running, 'the test explicitly advances a running ticker');
      this.deltaMS = milliseconds;
      this.callback(1);
    },
  };
  const PIXI = { Container: Display, Sprite: Display, Graphics: Display, Texture: { EMPTY: { EMPTY: true } } };
  const context = {
    app: { ticker, screen: { width: 1200, height: 800 }, renderer: {} },
    frameRateController: budget.createFrameRateController({ update() {} }, 60),
    renderBudget: budget.resolveRenderBudget({ maxFps: 60, textureSampling: false }),
    poseFadeMs,
    PIXI,
    Math,
    window: {
      RenderBudget: budget,
      FrameTextureBackend: { createFrameTextureBackend: () => backend },
      FrameStore: { createFrameStore: options => {
        const store = storeApi.createFrameStore(options);
        const replaceDemand = store.replaceDemand;
        store.replaceDemand = (owner, requests, options) => {
          demands.set(owner, Array.from(requests, request => ({ ...request })));
          return replaceDemand(owner, requests, options);
        };
        const replacePins = store.replacePins;
        store.replacePins = (owner, urls) => {
          pins.set(owner, [...(urls || [])]);
          return replacePins(owner, urls);
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
  async function complete(url) {
    const texture = finishLoad(url);
    await flush();
    const upload = uploads.find(item => item.texture.url === url && !item.pending.done);
    assert.ok(upload, `an upload for ${url} is pending`);
    upload.pending.done = true;
    upload.pending.resolve();
    await flush();
    return texture;
  }
  return { sprite, ticker, loads, uploads, demands, pins, complete, PIXI,
    runTimer() {
      const first = timers.entries().next().value;
      assert.ok(first, 'a transition timer is pending');
      timers.delete(first[0]); first[1]();
    } };
}

test('a clip change without authored transition frames dissolves the outgoing frame and releases its pin', async t => {
  const f = fixture(t), s = f.sprite;
  s.loadFrames('thinking', ['thinking-0', 'thinking-1']);
  s.loadFrames('idle', ['idle-0', 'idle-1']);
  s.setEmotion('thinking');
  await flush();
  const held = await f.complete('thinking-0');
  assert.equal(s.sprite.texture, held);

  s.setEmotion('idle');
  const overlay = s._poseFadeSprite;
  assert.equal(overlay.visible, true, 'the outgoing pose stays on screen as a fading overlay');
  assert.equal(overlay.texture, held);
  assert.equal(overlay.alpha, 1);
  assert.deepEqual(f.pins.get('pose-fade'), ['thinking-0'],
    'the outgoing frame is pinned so the texture window cannot evict it mid-fade');
  assert.equal(overlay.x, s.sprite.x);
  assert.equal(overlay.scale.x, s.sprite.scale.x);

  f.ticker.tick(100);
  const midAlpha = overlay.alpha;
  assert.ok(midAlpha > 0 && midAlpha < 1, `mid-fade alpha eases out, observed ${midAlpha}`);
  f.ticker.tick(100);
  assert.equal(overlay.visible, false, 'the overlay retires once the fade completes');
  assert.equal(s._poseFade, null);
  assert.equal(overlay.texture, f.PIXI.Texture.EMPTY);
  assert.deepEqual(f.pins.get('pose-fade'), []);
});

test('renderPoseFadeMs=0 keeps the previous hard cut', async t => {
  const f = fixture(t, { poseFadeMs: 0 }), s = f.sprite;
  s.loadFrames('thinking', ['thinking-0']);
  s.loadFrames('idle', ['idle-0']);
  s.setEmotion('thinking');
  await flush();
  await f.complete('thinking-0');
  s.setEmotion('idle');
  assert.equal(s._poseFadeSprite.visible, false);
  assert.equal(s._poseFade, null);
  assert.deepEqual(f.pins.get('pose-fade') ?? [], [], 'no outgoing pin is held after a hard cut');
});

test('authored transition frames still win over the overlay fade', async t => {
  const f = fixture(t), s = f.sprite;
  s.loadFrames('thinking', ['thinking-0']);
  s.loadFrames('idle', ['idle-0']);
  s.loadTransitionFrames('thinking', 'idle', ['thinking-idle-0']);
  s.setEmotion('thinking');
  await flush();
  await f.complete('thinking-0');
  s.setEmotion('idle');
  assert.equal(s._poseFadeSprite.visible, false, 'an authored transition replaces the cut without a fade');
  assert.equal(s._poseFade, null);
  assert.ok(s._transitionQueue.length > 0, 'the authored transition sequence plays');
  f.runTimer();
  assert.equal(s._transitionQueue.length, 0);
});

test('a second clip change replaces the fade with the currently displayed frame', async t => {
  const f = fixture(t), s = f.sprite;
  s.loadFrames('thinking', ['thinking-0']);
  s.loadFrames('idle', ['idle-0']);
  s.loadFrames('sad', ['sad-0']);
  s.setEmotion('thinking');
  await flush();
  const thinking = await f.complete('thinking-0');
  s.setEmotion('idle');
  await flush();
  const idle = await f.complete('idle-0');
  assert.equal(s.sprite.texture, idle);
  f.ticker.tick(50);

  s.setEmotion('sad');
  const overlay = s._poseFadeSprite;
  assert.equal(overlay.visible, true);
  assert.equal(overlay.texture, idle, 'the second fade starts from the live frame, not the stale one');
  assert.deepEqual(f.pins.get('pose-fade'), ['idle-0'], 'the first fade pin is released with the fade');

  for (let i = 0; i < 5; i++) f.ticker.tick(100);
  assert.equal(overlay.visible, false);
  assert.deepEqual(f.pins.get('pose-fade'), []);
});

test('a deactivating character ends the fade and releases the outgoing frame pin', async t => {
  const f = fixture(t), s = f.sprite;
  s.loadFrames('thinking', ['thinking-0']);
  s.loadFrames('idle', ['idle-0']);
  s.setEmotion('thinking');
  await flush();
  await f.complete('thinking-0');
  s.setEmotion('idle');
  assert.equal(s._poseFadeSprite.visible, true);
  s.setActive(false);
  assert.equal(s._poseFadeSprite.visible, false);
  assert.equal(s._poseFade, null);
});
