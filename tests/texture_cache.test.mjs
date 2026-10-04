import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs/promises';
import { createRequire } from 'node:module';
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

function fixture({ supported = true, responseStatus = 204 } = {}) {
  const instances = [], posts = [];
  class Worker {
    constructor() { instances.push(this); this.jobs = []; }
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
  return { cache, instances, posts, resource };
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

test('write permission/disk errors stop further derivation for this page without affecting reads', async () => {
  const f = fixture({ responseStatus: 503 });
  f.cache.save({ url: '/frame', key, token: 'capability' }, f.resource);
  await f.instances[0].complete(); await settle();
  assert.equal(f.cache.stats().cacheWriteFailures, 1);
  f.cache.save({ url: '/other', key: 'b'.repeat(64), token: 'capability' }, f.resource);
  assert.equal(f.instances[0].jobs.length, 0);
  assert.equal(f.cache.canUse('/frame.ktx2'), true);
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
