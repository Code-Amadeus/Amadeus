import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs/promises';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { encodeFrame, decodeFrame } from '../render/web/texture_cache_codec.mjs';
const require = createRequire(import.meta.url);
const { createTextureCache } = require('../render/web/texture_cache.js');
const nativeFetch = globalThis.fetch;
globalThis.fetch = async (url, options) => String(url).startsWith('file:')
  ? new Response(await fs.readFile(new URL(url)), { headers: { 'Content-Type': 'application/wasm' } })
  : nativeFetch(url, options);
const key = 'a'.repeat(64), raw = Uint8Array.from({ length: 64 }, (_, i) => i * 3);

test('production WASM cache codec preserves bytes and rejects corrupt data and identities', async () => {
  const encoded = await encodeFrame({ key, width: 8, height: 8, buffer: raw.buffer });
  const decoded = await decodeFrame({ key, ...encoded });
  assert.deepEqual(new Uint8Array(decoded.buffer), raw);
  assert.equal(decoded.width, 8);
  await assert.rejects(decodeFrame({ key: 'b'.repeat(64), ...encoded }), /identity/);
  const corrupt = encoded.buffer.slice(0); new Uint8Array(corrupt)[corrupt.byteLength - 1] ^= 1;
  await assert.rejects(decodeFrame({ key, buffer: corrupt }), /checksum/);
  await assert.rejects(encodeFrame({ key, width: 7, height: 8, buffer: raw.buffer }), /Unsupported/);
});

test('WASM initialization failure is classified as transient by the real codec', () => {
  for (const failure of ['fetch', 'compile']) {
    const source = `
    import assert from 'node:assert/strict';
    globalThis.fetch = async () => {
      if (${JSON.stringify(failure)} === 'fetch') throw Error('synthetic WASM fetch failure');
      return new Response(new Uint8Array([0, 1, 2, 3]), { headers: { 'Content-Type': 'application/wasm' } });
    };
    const { encodeFrame } = await import(${JSON.stringify(new URL('../render/web/texture_cache_codec.mjs', import.meta.url).href)});
    await assert.rejects(encodeFrame({ key: 'a'.repeat(64), width: 8, height: 8,
      buffer: new ArrayBuffer(64) }), { cacheTransient: true });
  `;
    const result = spawnSync(process.execPath, ['--input-type=module', '-e', source], { encoding: 'utf8', windowsHide: true });
    assert.equal(result.status, 0, result.stderr);
  }
});

function fixture({ supported = true, responseStatus = 204, constructorFailures = 0 } = {}) {
  const instances = [], posts = [];
  let creationAttempts = 0;
  class Worker {
    constructor() {
      creationAttempts++;
      if (constructorFailures-- > 0) throw Error('worker creation unavailable');
      instances.push(this); this.jobs = [];
    }
    postMessage(data) { this.jobs.push(data); }
    async complete() {
      const { id, kind, ...data } = this.jobs.shift();
      try {
        const result = await (kind === 'encode' ? encodeFrame(data) : decodeFrame(data));
        this.onmessage({ data: { id, ...result } });
      } catch (error) { this.onmessage({ data: { id, error: error.message } }); }
    }
    terminate() { this.terminated = true; }
  }
  const cache = createTextureCache({ Worker, renderer: { gl: { getExtension: () => supported } },
    location: new URL('http://127.0.0.1:1234/render/web/index.html'),
    pixi: { CompressedTextureResource: class { constructor(_, options) { Object.assign(this, options); } } },
    fetch: async (url, options) => { posts.push({ url, options }); return { ok: responseStatus === 204, status: responseStatus }; } });
  const resource = { format: 36492, levels: 1, width: 8, height: 8, _levelBuffers: [{ levelBuffer: raw }] };
  return { cache, instances, posts, resource, setStatus: value => { responseStatus = value; },
    get creationAttempts() { return creationAttempts; } };
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test('cache eligibility follows the actual context and same-origin HTTP boundary', () => {
  const f = fixture();
  assert.equal(f.cache.canUse('/frame.ktx2'), true);
  assert.equal(f.cache.canUse('http://localhost:9999/frame.ktx2'), false);
  assert.equal(f.cache.canUse('file:///frame.ktx2'), false);
  assert.equal(fixture({ supported: false }).cache.canUse('/frame.ktx2'), false);
  f.cache.dispose();
});

test('background writes are bounded, preserve owned CPU copies, and repair invalidated sources', async () => {
  const f = fixture(), source = '/frame.ktx2';
  f.cache.invalidate(source);
  f.cache.save({ url: source, key, token: 'capability' }, f.resource);
  f.cache.save({ url: '/other', key: 'b'.repeat(64), token: 'capability' }, f.resource);
  f.cache.save({ url: '/overflow', key: 'c'.repeat(64), token: 'capability' }, f.resource);
  assert.equal(f.cache.stats().cachePendingWrites, 2);
  assert.equal(f.cache.stats().cacheWriteSkipped, 1);
  assert.notEqual(f.instances[0].jobs[0].buffer, raw.buffer);
  assert.equal(raw.byteLength, 64);
  assert.match(f.cache.requestUrl(source), /source=1/);
  await f.instances[0].complete(); await settle();
  assert.equal(f.cache.requestUrl(source), source);
  assert.equal(f.posts[0].options.headers['X-Amadeus-Cache-Token'], 'capability');
  assert.equal(f.cache.stats().cacheWrites, 1);
  f.cache.dispose(); await settle();
  assert.ok(f.instances.every(worker => worker.terminated));
});

test('a temporary write rejection cools down without disabling reads and later playback can publish', async t => {
  t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: 1000 });
  const f = fixture({ responseStatus: 503 });
  f.cache.save({ url: '/frame', key, token: 'capability' }, f.resource);
  await f.instances[0].complete(); await settle();
  assert.equal(f.cache.stats().cacheWriteFailures, 1);
  f.cache.save({ url: '/other', key: 'b'.repeat(64), token: 'capability' }, f.resource);
  assert.equal(f.instances[0].jobs.length, 0);
  assert.equal(f.cache.canUse('/frame.ktx2'), true);
  assert.equal(f.cache.stats().cacheWritesDisabled, false);
  assert.equal(f.cache.stats().cacheWriteCoolingDown, true);
  f.setStatus(204);
  t.mock.timers.tick(30000);
  assert.equal(f.posts.length, 1, 'cooldown expiry does not start autonomous retries');
  f.cache.save({ url: '/frame', key, token: 'capability' }, f.resource);
  await f.instances[0].complete(); await settle();
  assert.equal(f.cache.stats().cacheWrites, 1);
  assert.equal(f.cache.stats().cacheWriteCoolingDown, false);
  f.cache.dispose();
});

test('403 stops writes for this capability lifetime while reads remain available', async t => {
  t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: 1000 });
  const f = fixture({ responseStatus: 403 });
  f.cache.save({ url: '/frame', key, token: 'capability' }, f.resource);
  await f.instances[0].complete(); await settle();
  f.setStatus(204); t.mock.timers.tick(60000);
  f.cache.save({ url: '/frame', key, token: 'capability' }, f.resource);
  assert.equal(f.instances[0].jobs.length, 0);
  assert.equal(f.cache.stats().cacheWritesDisabled, true);
  assert.equal(f.cache.canUse('/frame.ktx2'), true);
  f.cache.dispose();
});

test('one stalled decode worker releases all its jobs, preserves the healthy worker, and recovers lazily', async t => {
  t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: 1000 });
  const f = fixture();
  const encoded = await encodeFrame({ key, width: 8, height: 8, buffer: raw.buffer });
  const first = f.cache.decode(encoded.buffer.slice(0), key);
  const healthy = f.cache.decode(encoded.buffer.slice(0), key);
  const queued = f.cache.decode(encoded.buffer.slice(0), key);
  const rejected = [assert.rejects(first, { cacheTransient: true }), assert.rejects(queued, { cacheTransient: true })];
  const late = f.instances[0].onmessage, oldId = f.instances[0].jobs[0].id;
  await f.instances[1].complete(); await healthy;
  t.mock.timers.tick(30000); await Promise.all(rejected);
  assert.equal(f.instances[0].terminated, true);
  assert.notEqual(f.instances[1].terminated, true);
  assert.equal(f.cache.stats().cacheWorkerRestarts, 1);
  assert.equal(f.cache.canUse('/frame.ktx2'), true);
  late({ data: { id: oldId, buffer: raw.buffer, width: 8, height: 8 } });
  const recovered = f.cache.decode(encoded.buffer.slice(0), key);
  await f.instances.find(worker => !worker.terminated && worker.jobs.length).complete();
  assert.equal((await recovered).width, 8);
  assert.equal(f.instances.length, 3, 'the retired worker is replaced on demand');
  f.cache.dispose();
});

test('repeated worker construction failures use a bounded cooldown and a later success restores caching', async t => {
  t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: 1000 });
  const f = fixture({ constructorFailures: 3 });
  const encoded = await encodeFrame({ key, width: 8, height: 8, buffer: raw.buffer });
  for (let i = 0; i < 3; i++) await assert.rejects(f.cache.decode(encoded.buffer.slice(0), key), { cacheTransient: true });
  assert.equal(f.cache.canUse('/frame.ktx2'), false);
  await assert.rejects(f.cache.decode(encoded.buffer.slice(0), key), /cooling down/);
  assert.equal(f.creationAttempts, 3);
  assert.equal(f.cache.stats().cacheWorkerFailures, 3);
  t.mock.timers.tick(30000);
  assert.equal(f.cache.canUse('/frame.ktx2'), true);
  const restored = f.cache.decode(encoded.buffer.slice(0), key);
  await f.instances[0].complete(); await restored;
  assert.equal(f.cache.stats().cacheDecodeCoolingDown, false);
  f.cache.dispose();
});

test('reported codec initialization failure retires its worker rather than misclassifying disk bytes', async () => {
  const f = fixture();
  const encoded = await encodeFrame({ key, width: 8, height: 8, buffer: raw.buffer });
  const interrupted = f.cache.decode(encoded.buffer.slice(0), key);
  const rejected = assert.rejects(interrupted, { cacheTransient: true });
  const worker = f.instances[0];
  worker.onmessage({ data: { id: worker.jobs[0].id, error: 'codec unavailable', cacheTransient: true } });
  await rejected;
  assert.equal(worker.terminated, true);
  assert.equal(f.cache.canUse('/frame.ktx2'), true);
  const next = f.cache.decode(encoded.buffer.slice(0), key);
  await f.instances[1].complete(); await next;
  assert.equal(f.cache.stats().cacheReadFailures, 0);
  f.cache.dispose();
});

test('an encoder crash cannot disable decoding and a later write starts a fresh encoder', async t => {
  t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: 1000 });
  const f = fixture();
  f.cache.save({ url: '/frame', key, token: 'capability' }, f.resource);
  f.instances[0].onerror({ message: 'worker interrupted' }); await settle();
  assert.equal(f.cache.canUse('/frame.ktx2'), true);
  const encoded = await encodeFrame({ key, width: 8, height: 8, buffer: raw.buffer });
  const decoded = f.cache.decode(encoded.buffer, key);
  await f.instances[1].complete(); await decoded;
  t.mock.timers.tick(30000);
  f.cache.save({ url: '/frame', key, token: 'capability' }, f.resource);
  await f.instances[2].complete(); await settle();
  assert.equal(f.cache.stats().cacheWrites, 1);
  f.cache.dispose();
});

test('disposing one cache owner cancels its decodes without terminating another owner', async () => {
  const f = fixture(), other = fixture();
  const encoded = await encodeFrame({ key, width: 8, height: 8, buffer: raw.buffer });
  const pending = f.cache.decode(encoded.buffer, key);
  const rejected = assert.rejects(pending, { name: 'AbortError' });
  f.cache.dispose(); await rejected;
  assert.ok(f.instances.every(worker => worker.terminated));
  assert.equal(other.cache.canUse('/frame.ktx2'), true);
  other.cache.dispose();
});
