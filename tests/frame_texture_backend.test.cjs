const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const { createFrameTextureBackend } = require('../render/web/frame_texture_backend.js');

// Use the actual bundled resources/BaseTexture/Texture. Only the network, image
// decoder, GPU bind, and system clock are fakes; no GPU or character pack needed.
const context = { console, performance, setTimeout, clearTimeout };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../render/web/vendor/pixi.min.js'), 'utf8'), context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../render/web/vendor/pixi-basis-ktx2.global.js'), 'utf8'), context);
const P = context.PIXI, basis = context.PixiBasisKtx2Shim;
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

function compressed(format = basis.BASIS_FORMAT_TO_INTERNAL_FORMAT[6], levels = [new Uint8Array(16)]) {
  return new P.CompressedTextureResource(null, { format, width: 4, height: 4, levels: levels.length,
    levelBuffers: levels.map((levelBuffer, levelID) => ({ levelBuffer, levelID, levelWidth: 4 >> levelID, levelHeight: 4 >> levelID })) });
}
function resources(resource = compressed(), basisFormat = 6) {
  const result = [resource]; result.basisFormat = basisFormat; return result;
}

function fixture(overrides = {}) {
  let time = 0, lost = false, initCount = 0, extensionRefreshes = 0;
  const callbacks = new Set(), listeners = new Map(), images = [], fetches = [], binds = [], decoded = [];
  const ticker = { add(fn) { callbacks.add(fn); }, remove(fn) { callbacks.delete(fn); } };
  const parser = overrides.parser || { loadTranscoder() { initCount++; return Promise.resolve(); },
    transcode() { const result = resources(); decoded.push(result[0]); return Promise.resolve(result); } };
  const pixi = Object.assign(Object.create(P), { Ticker: { system: ticker } });
  const renderer = { CONTEXT_UID: 1,
    context: { webGLVersion: overrides.webGLVersion ?? 2, getExtensions() { extensionRefreshes++; } },
    view: { addEventListener(name, fn) { listeners.set(name, fn); }, removeEventListener(name) { listeners.delete(name); } },
    gl: { isContextLost: () => lost }, texture: { bind(base) {
      binds.push(base); time += overrides.bindCost || 0;
      base._glTextures[renderer.CONTEXT_UID] = { dirtyId: base.dirtyId };
      overrides.afterBind?.(state);
    } } };
  function createImage() {
    const image = { width: overrides.width ?? 4, height: overrides.height ?? 8,
      naturalWidth: overrides.width ?? 4, naturalHeight: overrides.height ?? 8, complete: false,
      decode: overrides.decode || (() => Promise.resolve()), assignments: [],
      get src() { return this.url || ''; },
      set src(value) {
        this.url = value; this.assignments.push(value);
        if (overrides.srcThrows && value) throw Error('image source rejected');
        if (!value || overrides.manualImage) return;
        queueMicrotask(() => {
          if (this.url !== value) return;
          this.complete = !overrides.imageError;
          if (overrides.imageError) this.onerror?.(); else this.onload?.();
        });
      } };
    images.push(image); return image;
  }
  const backend = createFrameTextureBackend({ renderer, pixi,
    shim: { ...basis, KTX2Parser: parser }, createImage, now: () => time,
    fetch: async (url, init) => {
      fetches.push({ url, signal: init.signal });
      return overrides.fetch ? overrides.fetch(url, init) : { ok: true, arrayBuffer: async () => new ArrayBuffer(4) };
    } });
  const state = { backend, renderer, parser, images, fetches, binds, decoded, callbacks, listeners,
    initCount: () => initCount, extensionRefreshes: () => extensionRefreshes,
    tick: () => { for (const fn of [...callbacks]) fn(); },
    lose() { lost = true; listeners.get('webglcontextlost')?.(); },
    restore() { lost = false; renderer.CONTEXT_UID++; listeners.get('webglcontextrestored')?.(); } };
  return state;
}

test('real Pixi frame resources are owned, independent, and absent from every global texture cache', async () => {
  const f = fixture();
  const before = [Object.keys(P.utils.TextureCache).length, Object.keys(P.utils.BaseTextureCache).length];
  const [a, b] = await Promise.all([f.backend.load('frame.ktx2'), f.backend.load('frame.ktx2')]);
  assert.notEqual(a.texture, b.texture);
  assert.notEqual(a.texture.baseTexture.resource, b.texture.baseTexture.resource);
  assert.equal(a.texture.baseTexture.resource.internal, true);
  assert.deepEqual([Object.keys(P.utils.TextureCache).length, Object.keys(P.utils.BaseTextureCache).length], before);
  assert.equal(a.texture.baseTexture.mipmap, P.MIPMAP_MODES.OFF);
  assert.equal(a.texture.baseTexture.alphaMode, P.ALPHA_MODES.NO_PREMULTIPLIED_ALPHA);
  assert.equal(a.cpuBytes, 16); assert.equal(a.gpuBytes, 16);
  f.backend.destroy(a.texture);
  assert.equal(f.decoded[0].destroyed, true);
  assert.equal(b.texture.destroyed, false);
  const borrowedResource = new P.BufferResource(new Uint8Array(4), { width: 1, height: 1 });
  const borrowed = new P.Texture(new P.BaseTexture(borrowedResource));
  f.backend.destroy(borrowed);
  f.backend.dispose();
  assert.equal(f.decoded[1].destroyed, true);
  assert.equal(borrowed.destroyed, false);
  assert.equal(borrowedResource.destroyed, false);
  borrowed.destroy(true); borrowedResource.destroy();
});

test('compressed GPU cost uses actual levels for every shim-supported format; CPU backing buffers are counted once', async () => {
  for (const [format, internal] of Object.entries(basis.BASIS_FORMAT_TO_INTERNAL_FORMAT)) {
    const buffer = new ArrayBuffer(64);
    const level0 = new Uint8Array(buffer, 0, Number(format) === 0 || Number(format) === 2 || Number(format) === 8 ? 8 : 16);
    const level1 = new Uint8Array(buffer, 24, 8);
    const resource = compressed(internal, [level0, level1]);
    const f = fixture({ parser: { loadTranscoder: async () => {}, transcode: async () => resources(resource, Number(format)) } });
    const loaded = await f.backend.load('mipped.ktx2');
    assert.equal(loaded.cpuBytes, 64, `CPU basis format ${format}`);
    assert.equal(loaded.gpuBytes, level0.byteLength + 8, `GPU basis format ${format}`);
    assert.equal(loaded.texture.baseTexture.mipmap, P.MIPMAP_MODES.ON_MANUAL);
    f.backend.dispose();
  }
});

test('uncompressed Basis types retain the shim options and account packed pixels', async () => {
  for (const [format, bytesPerPixel] of [[13, 4], [14, 2], [15, 2], [16, 2]]) {
    const data = new Uint8Array(4 * 4 * bytesPerPixel);
    const resource = new P.BufferResource(data, { width: 4, height: 4 });
    const f = fixture({ parser: { loadTranscoder: async () => {}, transcode: async () => resources(resource, format) } });
    const loaded = await f.backend.load('uncompressed.ktx2');
    assert.equal(loaded.cpuBytes, data.byteLength);
    assert.equal(loaded.gpuBytes, data.byteLength);
    assert.equal(loaded.texture.baseTexture.type, basis.BASIS_FORMAT_TO_TYPE[format]);
    assert.equal(loaded.texture.baseTexture.format, format === 13 ? P.FORMATS.RGBA : P.FORMATS.RGB);
    f.backend.dispose();
  }
});

test('concurrent backend instances share one shim initialization and the existing two-worker pool setter', async () => {
  const previous = globalThis.Worker;
  globalThis.Worker = function () {};
  try {
    let initializations = 0, poolSets = 0;
    const ready = deferred();
    const parser = { loadTranscoder(js, wasm) {
      assert.equal(js, './vendor/basis_transcoder.js'); assert.equal(wasm, './vendor/basis_transcoder.wasm');
      initializations++; return ready.promise;
    }, transcode: async () => resources(),
    set TRANSCODER_WORKER_POOL_LIMIT(value) { assert.equal(value, 2); poolSets++; } };
    const a = fixture({ parser }), b = fixture({ parser });
    const loads = [a.backend.load('a.ktx2'), b.backend.load('b.ktx2')];
    await flush(); assert.equal(initializations, 1); ready.resolve();
    await Promise.all(loads);
    assert.equal(poolSets, 1);
    a.backend.dispose(); b.backend.dispose();
  } finally { if (previous === undefined) delete globalThis.Worker; else globalThis.Worker = previous; }
});

test('already initialized page transcoder sources are borrowed without loading or destroying them', async () => {
  let loaded = 0;
  const worker = { jsSource: 'shared', wasmSource: new ArrayBuffer(4), terminate() { throw Error('borrowed worker terminated'); } };
  const parser = { TranscoderWorker: worker, loadTranscoder() { loaded++; }, transcode: async () => resources() };
  const f = fixture({ parser }); await f.backend.load('frame.ktx2'); f.backend.dispose();
  assert.equal(loaded, 0); assert.equal(worker.jsSource, 'shared'); assert.equal(worker.wasmSource.byteLength, 4);
});

test('the actual shim initializes two borrowed workers once and shares them across concurrent backend loads', async () => {
  const previous = globalThis.Worker;
  const workerContext = { console, performance, setTimeout, clearTimeout, URL, Blob };
  let initMessages = 0, transcodeMessages = 0, terminations = 0;
  class Worker {
    postMessage(message) {
      queueMicrotask(() => {
        if (message.type === 'init') {
          initMessages++; this.onmessage({ data: { type: 'init', success: true } });
        } else {
          transcodeMessages++;
          this.onmessage({ data: { type: 'transcode', success: true, requestID: message.requestID, basisFormat: 6,
            imageArray: [{ width: 4, height: 4, levelArray: [{ levelID: 0, levelWidth: 4, levelHeight: 4, levelBuffer: new Uint8Array(16) }] }] } });
        }
      });
    }
    terminate() { terminations++; }
  }
  workerContext.Worker = Worker; globalThis.Worker = Worker;
  try {
    vm.createContext(workerContext);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../render/web/vendor/pixi.min.js'), 'utf8'), workerContext);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../render/web/vendor/pixi-basis-ktx2.global.js'), 'utf8'), workerContext);
    const shim = workerContext.PixiBasisKtx2Shim, pixi = workerContext.PIXI;
    shim.KTX2Parser.setTranscoder('synthetic', new ArrayBuffer(4));
    shim.KTX2Parser.defaultRGBAFormat = { basisFormat: 6 };
    shim.KTX2Parser.defaultRGBFormat = { basisFormat: 2 };
    const ticker = { add() {}, remove() {} };
    const options = { renderer: { gl: { isContextLost: () => false } },
      pixi: Object.assign(Object.create(pixi), { Ticker: { system: ticker } }), shim,
      fetch: async () => ({ ok: true, arrayBuffer: async () => new ArrayBuffer(4) }) };
    const a = createFrameTextureBackend(options), b = createFrameTextureBackend(options);
    await Promise.all([a.load('a.ktx2'), a.load('b.ktx2'), b.load('c.ktx2')]);
    assert.equal(initMessages, 2); assert.equal(transcodeMessages, 3);
    assert.equal(shim.KTX2Parser.workerPool.length, 2);
    a.dispose(); b.dispose();
    assert.equal(terminations, 0, 'display disposal cannot terminate page-owned workers');
  } finally { if (previous === undefined) delete globalThis.Worker; else globalThis.Worker = previous; }
});

test('initialization and fetch failures retain the exact PNG fallback transformation without cached textures', async () => {
  for (const failure of ['init', 'fetch']) {
    const f = failure === 'init' ? fixture({ parser: { loadTranscoder: async () => { throw Error('no WASM'); } } })
      : fixture({ fetch: async () => ({ ok: false, status: 404 }) });
    const url = 'file:///pack/frames_loop_ktx2_uastc_q4_z18/001.KTX2?version=1#frame';
    const a = await f.backend.load(url), b = await f.backend.load(url);
    assert.equal(f.images[0].src, 'file:///pack/frames_loop/001.png?version=1#frame');
    assert.notEqual(a.texture, b.texture);
    assert.equal(a.cpuBytes, 128); assert.equal(a.gpuBytes, 172);
    assert.equal(a.texture.baseTexture.resource.internal, true);
    assert.equal(a.texture.baseTexture.resource.createBitmap, false);
    f.backend.dispose(); assert.ok(f.images.every(image => image.src === ''));
  }
});

test('image onload tolerates a decode() rejection and preserves its source until owned destruction', async () => {
  const f = fixture({ decode: async () => { throw Error('file decode unsupported'); } });
  const loaded = await f.backend.load('file:///frame.png');
  assert.equal(f.images[0].src, 'file:///frame.png');
  assert.equal(loaded.texture.valid, true);
  f.backend.destroy(loaded.texture);
  assert.equal(f.images[0].src, ''); f.backend.dispose();
});

test('raster POT/NPOT textures inherit the real legacy Pixi defaults and account only mip levels generated for the context', async () => {
  const previousImage = context.HTMLImageElement, previousMipmap = P.BaseTexture.defaultOptions.mipmap;
  class Image {}
  context.HTMLImageElement = Image;
  try {
    assert.equal(previousMipmap, P.MIPMAP_MODES.POW2);
    for (const [mipmap, webGLVersion, potBytes, npotBytes] of [
      [P.MIPMAP_MODES.POW2, 2, 172, 60],
      [P.MIPMAP_MODES.OFF, 2, 128, 60],
      [P.MIPMAP_MODES.ON, 2, 172, 72],
      [P.MIPMAP_MODES.ON, 1, 172, 60],
      [P.MIPMAP_MODES.ON_MANUAL, 2, 128, 60],
    ]) {
      P.BaseTexture.defaultOptions.mipmap = mipmap;
      for (const [width, height, gpuBytes] of [[4, 8, potBytes], [3, 5, npotBytes]]) {
        const image = Object.assign(new Image(), { width, height, naturalWidth: width, naturalHeight: height,
          complete: true, src: 'legacy.png' });
        const legacy = P.Texture.from(image);
        const f = fixture({ width, height, webGLVersion });
        const result = await f.backend.load('frame.png');
        const base = result.texture.baseTexture;
        for (const setting of ['mipmap', 'scaleMode', 'alphaMode', 'wrapMode', 'format', 'type']) {
          assert.equal(base[setting], legacy.baseTexture[setting], `${setting}: ${width}x${height}, mode ${mipmap}`);
        }
        assert.equal(result.cpuBytes, width * height * 4);
        assert.equal(result.gpuBytes, gpuBytes, `${width}x${height}, mode ${mipmap}, WebGL${webGLVersion}`);
        // Independently run the real TextureSystem style logic, including the
        // real mip-generation decision rather than duplicating the backend.
        let generated = false;
        const gpu = {};
        base._glTextures[f.renderer.CONTEXT_UID] = gpu;
        const system = { CONTEXT_UID: f.renderer.CONTEXT_UID, webGLVersion,
          renderer: { context: { extensions: {} } },
          gl: { generateMipmap() { generated = true; }, texParameteri() {} },
          setStyle: P.TextureSystem.prototype.setStyle };
        P.TextureSystem.prototype.updateTextureStyle.call(system, base);
        assert.equal(generated, gpuBytes > width * height * 4);
        f.backend.dispose(); legacy.destroy(true);
      }
    }
  } finally {
    P.BaseTexture.defaultOptions.mipmap = previousMipmap;
    if (previousImage === undefined) delete context.HTMLImageElement; else context.HTMLImageElement = previousImage;
  }
});

test('file assets use the adapter fetch and the same native abort signal as HTTP assets', async () => {
  const f = fixture(); await f.backend.load('file:///pack/frame.ktx2');
  assert.equal(f.fetches[0].url, 'file:///pack/frame.ktx2');
  assert.ok(f.fetches[0].signal instanceof AbortSignal); f.backend.dispose();
});

test('native fetch abort never falls back to a second raster request', async () => {
  const controller = new AbortController();
  const f = fixture({ fetch: (url, { signal }) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => { const error = Error('fetch cancelled'); error.name = 'AbortError'; reject(error); });
  }) });
  const loading = f.backend.load('frame.ktx2', controller.signal);
  await flush(); controller.abort(); await assert.rejects(loading, { name: 'AbortError' });
  assert.equal(f.images.length, 0); f.backend.dispose();
});

test('aborting an active transcode retains the load slot until completion and destroys the late resource', async () => {
  const decoded = deferred(), controller = new AbortController();
  const f = fixture({ parser: { loadTranscoder: async () => {}, transcode: () => decoded.promise } });
  let finished = false;
  const loading = f.backend.load('frame.ktx2', controller.signal);
  loading.then(() => { finished = true; }, () => { finished = true; });
  await flush(); controller.abort(); await flush();
  assert.equal(finished, false, 'unabortable work must still occupy the owner load slot');
  const resource = compressed(); decoded.resolve(resources(resource));
  await assert.rejects(loading, { name: 'AbortError' });
  assert.equal(resource.destroyed, true); assert.equal(f.images.length, 0); f.backend.dispose();
});

test('abort while image decode is pending releases its source and never constructs or publishes a texture', async () => {
  const decoded = deferred(), controller = new AbortController();
  const f = fixture({ decode: () => decoded.promise });
  const loading = f.backend.load('frame.png', controller.signal);
  await flush(); controller.abort();
  await assert.rejects(loading, { name: 'AbortError' });
  decoded.resolve(); await flush();
  assert.equal(f.images[0].src, ''); assert.equal(f.images[0].onload, null); f.backend.dispose();
});

test('abort between raster completion and publication destroys the newly owned texture and decoded source', async () => {
  const decoded = deferred(), controller = new AbortController();
  const f = fixture({ decode: () => decoded.promise });
  const loading = f.backend.load('frame.png', controller.signal);
  await flush(); decoded.resolve(); queueMicrotask(() => controller.abort());
  await assert.rejects(loading, { name: 'AbortError' });
  assert.equal(f.images[0].src, ''); f.backend.dispose();
});

test('image network/source failures clean listeners and disposal aborts pending images', async () => {
  for (const options of [{ imageError: true }, { srcThrows: true }]) {
    const f = fixture(options); await assert.rejects(f.backend.load('broken.png'));
    assert.equal(f.images[0].src, ''); assert.equal(f.images[0].onerror, null); f.backend.dispose();
  }
  const f = fixture({ manualImage: true }), loading = f.backend.load('pending.png');
  f.backend.dispose(); await assert.rejects(loading, { name: 'AbortError' });
  assert.equal(f.images[0].src, ''); assert.equal(f.listeners.size, 0); assert.equal(f.callbacks.size, 0);
  await assert.rejects(f.backend.load('later.png'), { name: 'AbortError' });
});

test('uploads bind only admitted textures, obey a per-tick elapsed budget, and reuse only a clean current context', async () => {
  const f = fixture({ bindCost: 2 });
  const a = await f.backend.load('a.ktx2'), b = await f.backend.load('b.ktx2');
  assert.equal(f.binds.length, 0, 'decoding alone must not upload');
  let completed = 0;
  const jobs = [f.backend.upload(a.texture).then(() => completed++), f.backend.upload(b.texture).then(() => completed++)];
  f.tick(); await flush(); assert.equal(completed, 1); assert.equal(f.binds.length, 1);
  f.tick(); await Promise.all(jobs); assert.equal(completed, 2);
  await f.backend.upload(a.texture); assert.equal(f.binds.length, 2, 'clean current context skips bind');
  a.texture.baseTexture.update(); const dirty = f.backend.upload(a.texture); f.tick(); await dirty;
  assert.equal(f.binds.length, 3); assert.equal(f.callbacks.size, 0); f.backend.dispose();
});

test('context loss before or during bind keeps jobs pending until restore in the new context', async () => {
  for (const lossDuringBind of [false, true]) {
    let first = true;
    const f = fixture({ afterBind(state) { if (lossDuringBind && first) { first = false; state.lose(); } } });
    const loaded = await f.backend.load('frame.ktx2');
    if (!lossDuringBind) f.lose();
    let completed = false;
    const uploading = f.backend.upload(loaded.texture).then(() => { completed = true; });
    f.tick(); await flush(); assert.equal(completed, false); assert.equal(f.callbacks.size, 0);
    f.restore(); f.tick(); await uploading;
    assert.equal(completed, true);
    assert.ok(loaded.texture.baseTexture._glTextures[f.renderer.CONTEXT_UID]); f.backend.dispose();
  }
});

test('real Pixi restore leaves compressed extension enablement stale; backend refreshes it before resident redraw', async () => {
  const f = fixture(), loaded = await f.backend.load('frame.ktx2');
  let enabled = false, extensionQueries = 0;
  const errors = [];
  const gl = { isContextLost: () => false, TEXTURE_2D: 3553, UNPACK_ALIGNMENT: 3317,
    pixelStorei() {}, getExtension(name) {
      if (name !== 'EXT_texture_compression_bptc') return null;
      enabled = true; extensionQueries++;
      return { COMPRESSED_RGBA_BPTC_UNORM_EXT: 36492 };
    }, compressedTexImage2D(target, level, format) {
      if (!enabled || format !== 36492) errors.push(1280); // WebGL INVALID_ENUM
    }, getError() { return errors.shift() || 0; } };
  f.renderer.gl = gl;
  const system = new P.ContextSystem(f.renderer);
  system.gl = gl; system.webGLVersion = 2;
  f.renderer.context = system;
  f.renderer.runners = { contextChange: { emit(context) { system.contextChange(context); } } };
  system.getExtensions();
  f.renderer.texture.bind = base => {
    const gpu = { dirtyId: -1 };
    // Use real compressed upload, then mirror TextureSystem.updateTexture's
    // dirtyId assignment. WebGL errors do not make resource.upload throw.
    assert.equal(base.resource.upload(f.renderer, base, gpu), true);
    gpu.dirtyId = base.dirtyId; base._glTextures[f.renderer.CONTEXT_UID] = gpu;
  };
  const first = f.backend.upload(loaded.texture); f.tick(); await first;
  assert.equal(f.backend.stats().textureUploads, 1);
  assert.equal(f.backend.stats().transcodesCompleted, 1);
  const queriesBeforeLoss = extensionQueries, cached = system.extensions.bptc;
  f.lose(); enabled = false; // WebGL resets extension enablement on restore.
  system.handleContextRestored();
  assert.equal(system.extensions.bptc, cached, 'real Pixi retains the old object');
  assert.equal(extensionQueries, queriesBeforeLoss, 'real Pixi did not request the extension again');
  assert.equal(loaded.texture.baseTexture.resource.upload(f.renderer, loaded.texture.baseTexture, {}), true);
  assert.equal(gl.getError(), 1280, 'stale cached extension passes Pixi guard but upload fails');

  f.listeners.get('webglcontextrestored')();
  assert.equal(extensionQueries, queriesBeforeLoss + 1);
  assert.equal(enabled, true);
  // The held resident frame can redraw without a new decode or store upload.
  f.renderer.texture.bind(loaded.texture.baseTexture);
  assert.equal(gl.getError(), 0);
  assert.equal(f.backend.stats().textureUploads, 3, 'includes redraw and attempted stale-context submission outside the queue');
  assert.equal(f.decoded.length, 1, 'restoration retains the original CPU payload');
  const again = f.backend.upload(loaded.texture); f.tick(); await again;
  assert.equal(extensionQueries, queriesBeforeLoss + 1, 'only one refresh per current context');
  f.backend.dispose();
});

test('completed transcodes count cancelled results, independently of store load attempts', async () => {
  const pending = deferred();
  const f = fixture({ parser: { loadTranscoder: async () => {}, transcode: () => pending.promise } });
  const controller = new AbortController();
  const load = f.backend.load('cancelled.ktx2', controller.signal);
  await flush();
  assert.equal(f.backend.stats().transcodeAttempts, 1);
  assert.equal(f.backend.stats().transcodesCompleted, 0);
  controller.abort();
  pending.resolve(resources());
  await assert.rejects(load, { name: 'AbortError' });
  assert.equal(f.backend.stats().transcodesCompleted, 1);
  assert.equal(f.backend.stats().fetchedPayloadBytes, 4);
  assert.equal(f.backend.stats().textureUploads, 0);
  f.backend.dispose();
});

test('empty and raster-only restores defer extension refresh until a compressed frame actually needs the context', async () => {
  for (const rasterOnly of [false, true]) {
    const f = fixture();
    if (rasterOnly) await f.backend.load('frame.png');
    f.restore();
    assert.equal(f.extensionRefreshes(), 0);
    const a = await f.backend.load('a.ktx2'), b = await f.backend.load('b.ktx2');
    const jobs = [f.backend.upload(a.texture), f.backend.upload(b.texture)];
    f.tick(); await Promise.all(jobs);
    assert.equal(f.extensionRefreshes(), 1);
    f.restore();
    assert.equal(f.extensionRefreshes(), 2, 'owned compressed frames need enablement before their next draw');
    f.backend.dispose();
  }
});

test('destroy/dispose cancel queued uploads without binding borrowed or destroyed resources', async () => {
  const f = fixture(), a = await f.backend.load('a.ktx2'), b = await f.backend.load('b.ktx2');
  const aJob = f.backend.upload(a.texture), bJob = f.backend.upload(b.texture);
  assert.equal(f.backend.upload(a.texture), aJob, 'duplicate admission shares one queue entry');
  f.backend.destroy(a.texture); await assert.rejects(aJob, { name: 'AbortError' });
  f.backend.dispose(); await assert.rejects(bJob, { name: 'AbortError' });
  assert.equal(f.binds.length, 0); assert.equal(f.callbacks.size, 0); assert.equal(f.listeners.size, 0);
  await assert.rejects(f.backend.upload({}), { name: 'AbortError' });
});
