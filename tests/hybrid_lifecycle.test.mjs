import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import budget from '../render/web/render_budget.js';
import storeApi from '../render/web/frame_store.js';
import { FrameDecoder } from '../tools/probes/frame-video/decoder.mjs';
import { createHybridDescriptor } from '../tools/probes/frame-video/hybrid_source.mjs';
import { runHybridLifecycle } from '../tools/probes/frame-video/hybrid_lifecycle.mjs';

// Actual SpriteRenderer + FrameStore + provider/FrameDecoder, with manual
// presentation pulses and software texture/decoder fakes. No native GL calls.
function fixture(t, { contextAvailable = true, noVideo = false, blankRecovery = false } = {}) {
  const label = 'idle_closed_eye', count = 40, head = 18;
  const view = new EventTarget(), frames = [], outputs = [], owned = new Set(), pendingUploads = [];
  const bodyUrls = new Set(Array.from({ length: count }, (_, index) => `${label}:${index}`).slice(head, -1));
  const staticBodyLoads = { normalBody: 0, explicitHoldBody: 0, fallbackBody: 0 };
  let time = 0, lost = false, restorePending = false, restored = false, blankUsed = false;
  const makeTexture = (index, kind) => ({ sourceIndex: index, kind, width: 2, height: 2, destroyed: false,
    pixels: new Uint8Array(Array.from({ length: 4 }, () => [30 + index + (kind === 'video' ? 1 : 0), 50, 70, 255]).flat()),
    baseTexture: { valid: true, destroyed: false, dirtyId: 1, _glTextures: {} } });
  class Display {
    constructor(texture = { width: 1, height: 1 }) {
      this.texture = texture; this.anchor = { set() {} }; this.scale = { set() {} };
    }
    addChild() {}
    clear() {}
    destroy() { this.destroyed = true; }
  }
  const renderer = { view, CONTEXT_UID: 1,
    gl: { NO_ERROR: 0, getError: () => 0, isContextLost: () => lost,
      getExtension: () => contextAvailable ? { loseContext() {
        lost = true; restorePending = true;
        for (const output of outputs) for (const texture of output.pool) texture.pixels.fill(0);
        view.dispatchEvent(new Event('webglcontextlost'));
      } } : null },
    extract: {
      pixels(target) {
        if (blankRecovery && restored && target.texture.kind === 'static' && !blankUsed) {
          blankUsed = true; return new Uint8Array(16);
        }
        return new Uint8Array(target.texture.pixels);
      },
      canvas(target) { return { toDataURL: () => 'data:image/png;base64,'
        + Buffer.from(`${target.texture.kind}:${target.texture.sourceIndex}`).toString('base64') }; },
    },
  };
  const backend = {
    now: () => time,
    async load(url) {
      if (bodyUrls.has(url)) {
        const failed = sprite.getExperimentalFrameVideoStats().failed;
        staticBodyLoads[failed ? 'fallbackBody' : sprite._held ? 'explicitHoldBody' : 'normalBody']++;
      }
      const texture = makeTexture(Number(url.split(':').at(-1)), 'static');
      owned.add(texture); return { texture, cpuBytes: 16, gpuBytes: 16 };
    },
    upload(texture) {
      const upload = () => { texture.baseTexture._glTextures[renderer.CONTEXT_UID] = { dirtyId: 1 }; };
      if (!lost) { upload(); return Promise.resolve(); }
      return new Promise(resolve => pendingUploads.push(() => { upload(); resolve(); }));
    },
    destroy(texture) { owned.delete(texture); texture.destroyed = texture.baseTexture.destroyed = true; },
    dispose() { while (pendingUploads.length) pendingUploads.shift()(); },
  };
  let unload;
  const ticker = { deltaMS: 17, add(callback) { this.tick = callback; } };
  const host = { PIXI: { Container: Display, Sprite: Display, Graphics: Display, Texture: { EMPTY: { height: 1 } } },
    RenderBudget: budget, FrameStore: storeApi, FrameTextureBackend: { createFrameTextureBackend: () => backend },
    addEventListener(event, callback) { if (event === 'unload') unload = callback; },
    wallpaperApp: { scene: { app: { renderer } } } };
  const source = fs.readFileSync(new URL('../render/web/renderer.js', import.meta.url), 'utf8');
  const start = source.indexOf('class SpriteRenderer {'), end = source.indexOf('class SpriteForgeRuntime {', start);
  const context = { window: host, app: { ticker, renderer }, PIXI: host.PIXI,
    frameRateController: { effectiveMaxFps: 30 }, renderBudget: { textureSampling: true },
    console: { log() {}, warn() {} }, setTimeout, clearTimeout };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end) + '\nglobalThis.SpriteRenderer=SpriteRenderer;', context);
  const sprite = new context.SpriteRenderer(new Display());
  sprite._applyCurrentTransform = sprite._hideMouthLayer = sprite._updateMouthLayer = () => {};
  host.renderApp = {
    _sprite: sprite, setEmotion: value => sprite.setEmotion(value),
    setIdleAnimation: value => sprite.setIdleAnimation(value),
    clearSpriteHold: () => sprite.clearHold(), holdSpriteFrame: value => sprite.holdFrame(value),
    releaseCharacter() { sprite.setSpeaking(false); sprite.clearHold(); },
  };
  sprite.loadFrames('normal', ['normal:0', 'normal:1']);
  sprite.setIdleFrameIntervalMs('normal', 17);
  class NativeDecoder {
    constructor(callbacks) { this.callbacks = callbacks; this.state = 'unconfigured'; }
    configure() { this.state = 'configured'; }
    decode(chunk) {
      if (noVideo) return;
      queueMicrotask(() => {
        const frame = { timestamp: chunk.timestamp, closes: 0, close() { this.closes++; } };
        frames.push(frame); this.callbacks.output(frame);
      });
    }
    async flush() {}
    close() { this.state = 'closed'; }
  }
  class Queue extends FrameDecoder {
    constructor(packets, config, options) {
      super(packets, config, { ...options, Decoder: NativeDecoder,
        Chunk: class { constructor(value) { Object.assign(this, value); } } });
    }
  }
  function makeOutput() {
    const pool = [makeTexture(0, 'video'), makeTexture(0, 'video')], retained = new Map();
    let displayed = null, destroyed = false;
    const output = { pool, get texture() { return displayed; },
      present(color, alpha) {
        assert.equal(color.timestamp, alpha.timestamp); assert.equal(color.closes, 0); assert.equal(alpha.closes, 0);
        const next = pool.find(texture => texture !== displayed && !retained.has(texture));
        if (!next) return null;
        const pixels = makeTexture(color.timestamp, 'video').pixels;
        next.sourceIndex = color.timestamp; next.pixels = pixels; displayed = next;
        return { texture: next, uploadMs: 0, unpackSubmitMs: 0 };
      },
      retain(texture) {
        retained.set(texture, (retained.get(texture) || 0) + 1);
        return () => {
          const remaining = retained.get(texture) - 1;
          if (remaining) retained.set(texture, remaining); else retained.delete(texture);
          if (destroyed && !remaining) texture.destroyed = true;
        };
      },
      destroy() {
        destroyed = true; displayed = null;
        for (const texture of pool) if (!retained.has(texture)) texture.destroyed = true;
      },
      snapshot: () => ({ liveOutputTextures: pool.filter(texture => !texture.destroyed).length,
        liveUploadTextures: destroyed ? 0 : 2, borrowedVideoFrames: 0 }),
    };
    outputs.push(output); return output;
  }
  const packets = Array.from({ length: count }, (_, index) => ({ sourceIndex: index, key: index % 4 === 0,
    data: new Uint8Array([index]) }));
  const experiment = createHybridDescriptor(host.PIXI, renderer, { metadata: { headFrames: head, frames: count, layout: {} },
    streams: [{ frames: packets, config: {} }, { frames: packets, config: {} }], split: true, bytes: 240 },
  { Decoder: Queue, makeOutput, capacity: 4 });
  host.__frameVideoExperiment = { label, ...experiment,
    snapshot: () => ({ ...experiment.snapshot(), staticBodyLoads: { ...staticBodyLoads } }),
    reinstall: () => sprite.setExperimentalFrameVideoSource(label, experiment.descriptor),
    disable: () => sprite.setExperimentalFrameVideoSource(label, null),
  };
  host.__frameVideoExperiment.reinstall();
  sprite.loadFrames(label, Array.from({ length: count }, (_, index) => `${label}:${index}`));
  sprite.setIdleFrameIntervalMs(label, 17);
  async function pulse(ms) {
    time += ms;
    if (restorePending) {
      restorePending = false; lost = false; restored = true; renderer.CONTEXT_UID++;
      for (const texture of owned) texture.baseTexture._glTextures[renderer.CONTEXT_UID] = { dirtyId: 1 };
      view.dispatchEvent(new Event('webglcontextrestored'));
      while (pendingUploads.length) pendingUploads.shift()();
    }
    for (let i = 0; i < 30; i++) await Promise.resolve();
    ticker.tick(1);
    for (let i = 0; i < 30; i++) await Promise.resolve();
  }
  t.after(() => unload());
  return { host, frames, outputs, sprite, owned, run: options => runHybridLifecycle({ host,
    now: () => time, delay: pulse, timeoutMs: 5000, ...options }) };
}

test('product lifecycle preserves frozen pixels, restores held KTX2 identity, captures exact boundary and releases ownership', async t => {
  const f = fixture(t), result = await f.run();
  assert.equal(result.passed, true, result.error || JSON.stringify(result.checks));
  assert.equal(result.phase, 'post-measurement-diagnostics');
  assert.ok(!f.sprite._frameSamplingPlans.get('idle_closed_eye').timelineIndices.includes(18),
    'the exact boundary capture works even when playback sampling omits source18');
  assert.equal(result.evidence.freeze.beforeHold.hash, result.evidence.freeze.afterHold.hash);
  const recovery = result.evidence.contextRecovery;
  assert.equal(recovery.recovered.hash, recovery.explicitStatic.hash);
  assert.notEqual(result.evidence.freeze.beforeHold.hash, recovery.recovered.hash,
    'video/static color differs in this fixture, so recovery cannot compare against lossy video');
  assert.deepEqual(result.captures.map(row => [row.name, row.sourceIndex]), [
    ['video-body-18', 18], ['static-head-17', 17], ['static-body-18', 18],
  ]);
  assert.ok(result.captures.every(row => row.png.startsWith('data:image/png;base64,')));
  assert.notEqual(result.evidence.boundary.videoPixels.hash, result.evidence.boundary.staticPixels.hash);
  assert.equal(result.evidence.latchedFallback.state.video.failed, true);
  assert.equal(result.evidence.beforeFaultStaticLoads.normalBody, 0);
  assert.ok(result.evidence.contextFallbackStaticLoads.fallbackBody >= 1);
  assert.ok(result.evidence.explicitHoldStaticLoads.explicitHoldBody >= 1);
  assert.equal(result.evidence.explicitHoldStaticLoads.normalBody, 0);
  assert.equal(result.evidence.reinstalled.video.failed, false);
  assert.equal(result.evidence.cleanup.provider.active.length, 0);
  assert.ok(f.frames.length > 0 && f.frames.every(frame => frame.closes === 1));
  assert.equal(f.owned.size, 0); assert.ok(f.outputs.every(output => output.pool.every(texture => texture.destroyed)));
});

test('missing real context-loss support is reported and all diagnostic resources still release', async t => {
  const f = fixture(t, { contextAvailable: false }), result = await f.run();
  assert.equal(result.passed, false); assert.match(result.error, /context loss is available/);
  assert.equal(result.validation.contextLoss, false);
  assert.equal(result.evidence.cleanup.provider.active.length, 0);
  assert.equal(f.owned.size, 0); assert.ok(f.frames.every(frame => frame.closes === 1));
});

test('a valid static texture with blank recovered pixels fails content verification and still cleans up', async t => {
  const f = fixture(t, { blankRecovery: true }), result = await f.run();
  assert.equal(result.passed, false);
  assert.match(result.error, /restored fallback matches explicit static pixels/);
  assert.equal(result.evidence.contextRecovery.recovered.rgbSum, 0);
  assert.ok(result.evidence.contextRecovery.explicitStatic.rgbSum > 0);
  assert.equal(f.owned.size, 0); assert.equal(result.evidence.cleanup.provider.active.length, 0);
});

test('missing video readiness ends at a bounded condition timeout with complete cleanup', async t => {
  const f = fixture(t, { noVideo: true }), result = await f.run({ timeoutMs: 200 });
  assert.equal(result.passed, false); assert.match(result.error, /Timed out: current body video display/);
  assert.equal(result.readbacks, 0);
  assert.equal(f.owned.size, 0); assert.equal(result.evidence.cleanup.provider.active.length, 0);
});
