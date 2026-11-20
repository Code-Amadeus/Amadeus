import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import { zstdCompressSync } from 'node:zlib';
import { installBc7Cache } from '../../tools/probes/bc7-cache/install.mjs';
import { decodeFrame } from '../../tools/probes/bc7-cache/decode.mjs';
import { installTranscodeCounter } from './textureProbe.mjs';
const nativeFetch = globalThis.fetch;
globalThis.fetch = async (url, options) => String(url).startsWith('file:')
  ? new Response(fs.readFileSync(new URL(url)), { headers: { 'Content-Type': 'application/wasm' } })
  : nativeFetch(url, options);
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const raw = Uint8Array.from({ length: 16 }, (_, i) => i * 13);
const compressed = zstdCompressSync(raw);
const entry = { file: 'test.bc7.zst', width: 4, height: 4, rawBytes: 16,
  compressedBytes: compressed.length, compressedSha256: hash(compressed), rawSha256: hash(raw) };

test('actual WASM decoder preserves BC7 bytes; corrupt content and invalid dimensions are rejected', async () => {
  assert.deepEqual(new Uint8Array((await decodeFrame(compressed, entry)).buffer), raw);
  const bad = Uint8Array.from(compressed); bad[bad.length - 1] ^= 1;
  await assert.rejects(decodeFrame(bad, entry), /checksum/);
  await assert.rejects(decodeFrame(compressed, { ...entry, rawSha256: 'bad' }), /checksum/);
  await assert.rejects(decodeFrame(compressed, { ...entry, width: 0 }), /dimensions/);
});

function fixture({ corrupt = false, supported = true, height = 4 } = {}) {
  const requests = [], listeners = new Set();
  const context = { console, performance, setTimeout, clearTimeout, URL, DOMException,
    location: { href: 'http://127.0.0.1:1234/render/web/wallpaper_engine.html' },
    document: { addEventListener(_name, fn) { listeners.add(fn); }, removeEventListener(_name, fn) { listeners.delete(fn); } },
    Worker: class {
      postMessage({ id, bytes, entry }) {
        decodeFrame(bytes, entry).then(result => this.onmessage({ data: { id, ...result } }),
          error => this.onmessage({ data: { id, error: error.message } }));
      }
    },
    wallpaperApp: { scene: { app: { renderer: { gl: { getExtension: () => supported } } } } },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(new URL('../../render/web/vendor/pixi.min.js', import.meta.url), 'utf8'), context);
  vm.runInContext(fs.readFileSync(new URL('../../render/web/vendor/pixi-basis-ktx2.global.js', import.meta.url), 'utf8'), context);
  context.PixiBasisKtx2Shim.KTX2Parser.transcode = async bytes => ({ original: bytes });
  context.PIXI.settings.ADAPTER.fetch = async url => {
    requests.push(url);
    const bytes = String(url).startsWith('/__bc7_cache/') ? Uint8Array.from(compressed) : new Uint8Array([42]);
    if (corrupt && String(url).startsWith('/__bc7_cache/')) bytes[0] ^= 1;
    return { ok: true, arrayBuffer: async () => bytes.buffer };
  };
  vm.runInContext(`(${installTranscodeCounter.toString()})()`, context);
  vm.runInContext(`(${installBc7Cache.toString()})(${JSON.stringify({ basisFormat: 6, internalFormat: 36492,
    entries: { 'idle/0.ktx2': { ...entry, height } } })})`, context);
  return { context, requests, async load(url = '/spriteforge/idle/0.ktx2', init) {
    const response = await context.PIXI.settings.ADAPTER.fetch(url, init);
    return context.PixiBasisKtx2Shim.KTX2Parser.transcode(await response.arrayBuffer());
  } };
}

test('cache hit creates real Pixi BC7 resource with identical bytes and no UASTC transcode', async () => {
  const f = fixture(), result = await f.load();
  assert.equal(result.basisFormat, 6);
  assert.equal(result[0].format, 36492);
  assert.deepEqual(Array.from(result[0]._levelBuffers[0].levelBuffer), Array.from(raw));
  assert.equal(f.context.__bc7Cache.hits, 1);
  assert.equal(f.context.__textureTranscodes.completed, 0);
  assert.deepEqual(f.requests, ['/__bc7_cache/test.bc7.zst']);
  await f.load('/assets/spriteforge/runtime/kurisu/idle/0.ktx2');
  assert.equal(f.context.__bc7Cache.hits, 2);
});

test('corrupt derived data falls back once to source; unsupported GPU and absent entries use source directly', async () => {
  const corrupt = fixture({ corrupt: true });
  assert.equal((await corrupt.load()).original.byteLength, 1);
  assert.equal(corrupt.context.__bc7Cache.failures, 1);
  assert.equal(corrupt.context.__textureTranscodes.completed, 1);
  assert.equal(corrupt.requests.length, 2);
  const unsupported = fixture({ supported: false });
  await unsupported.load();
  assert.equal(unsupported.context.__bc7Cache.unsupported, 1);
  assert.deepEqual(unsupported.requests, ['/spriteforge/idle/0.ktx2']);
  const absent = fixture();
  await absent.load('/spriteforge/not-in-cache.ktx2');
  assert.equal(absent.context.__bc7Cache.misses, 1);
  assert.equal(absent.context.__textureTranscodes.completed, 1);
});

test('aborted cached loads do not silently refetch the source', async () => {
  const f = fixture(), controller = new AbortController();
  controller.abort();
  await assert.rejects(f.load(undefined, { signal: controller.signal }), { name: 'AbortError' });
  assert.equal(f.requests.length, 1);
  assert.equal(f.context.__textureTranscodes.completed, 0);
});

test('non-block-aligned source dimensions use the same padded GPU levels as the Basis worker', async () => {
  const f = fixture({ height: 3 }), result = await f.load();
  assert.equal(result[0].height, 4);
  assert.equal(result[0]._levelBuffers[0].levelHeight, 4);
  assert.equal(result[0]._levelBuffers[0].levelWidth, 4);
  assert.equal(result[0]._levelBuffers[0].levelBuffer.byteLength, 16);
});

test('fixed-route control returns a serializable value to Electron', () => {
  const runtime = {};
  assert.equal(vm.runInNewContext('void (renderApp._spriteforgeRuntime._advanceNow=()=>{})',
    { renderApp: { _spriteforgeRuntime: runtime } }), undefined);
  assert.equal(typeof runtime._advanceNow, 'function');
});
