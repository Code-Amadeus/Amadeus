import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash } from 'node:crypto';
import { loadHybridFixture, installHybridProbe, installHybridSources } from '../render/web/frame_video/hybrid_source.mjs';

test('fixture loading requires the currently hosted pack identity, not just a historical verified flag', async t => {
  const bytes = new Uint8Array(32 + 3 * 13), view = new DataView(bytes.buffer);
  bytes.set(new TextEncoder().encode('DKIF')); bytes.set(new TextEncoder().encode('VP90'), 8);
  view.setUint16(6, 32, true); view.setUint16(12, 4, true); view.setUint16(14, 4, true);
  view.setUint32(16, 60, true); view.setUint32(20, 1, true); view.setUint32(24, 3, true);
  const index = [];
  for (let i = 0; i < 3; i++) {
    const offset = 32 + i * 13;
    view.setUint32(offset, 1, true); view.setBigUint64(offset + 4, BigInt(i), true);
    bytes[offset + 12] = i;
    index.push({ sourceIndex: i, offset: offset + 12, size: 1, key: true });
  }
  const stream = name => ({ video: name + '.ivf', codec: 'vp09.00.51.08', index,
    videoSha256: createHash('sha256').update(bytes).digest('hex'),
    colorSpace: { matrix: 'bt709', fullRange: name === 'alpha' } });
  const metadata = { schema: 'amadeus.frame-video-split-experiment.v1', headFrames: 1, frames: 3, frameIntervalMs: 17,
    layout: { width: 4, height: 4, codedWidth: 4, codedHeight: 4 },
    reference: { clip: 'idle_closed_eye', productionLineageVerified: true, currentPackDimensionsMatch: true,
      packVersion: 'fixture-v1', manifestSha256: 'a'.repeat(64) },
    streams: { color: stream('color'), alpha: stream('alpha') } };
  const pack = { available: true, version: 'fixture-v1', sourceSha256: { 'runtime_manifest.json': 'a'.repeat(64) } };
  const saved = new Map(['location', 'VideoDecoder', 'window'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  t.after(() => {
    for (const [key, descriptor] of saved) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  globalThis.location = { href: 'http://localhost/' };
  globalThis.VideoDecoder = { isConfigSupported: async config => ({ supported: true, config }) };
  t.mock.method(globalThis, 'fetch', async url => ({ ok: true,
    json: async () => structuredClone(metadata), arrayBuffer: async () => bytes.buffer.slice(0) }));
  const loaded = await loadHybridFixture('/fixture/', pack);
  assert.equal(loaded.streams.length, 2);
  assert.equal(loaded.streams[0].frames.length, 3);
  assert.equal(loaded.bytes, bytes.length * 2);
  metadata.reference.clip = 'trans_smile';
  assert.equal((await loadHybridFixture('/fixture/', pack)).metadata.reference.clip, 'trans_smile');
  for (const invalid of ['', null, undefined]) {
    metadata.reference.clip = invalid;
    await assert.rejects(loadHybridFixture('/fixture/', pack), /verified installed PNG clip/);
  }
  metadata.reference.clip = 'trans_smile';
  metadata.reference.productionLineageVerified = false;
  await assert.rejects(loadHybridFixture('/fixture/', pack), /verified installed PNG clip/);
  metadata.reference.productionLineageVerified = true;
  const installations = [], originalLoad = async () => {};
  const sprite = { _frameUrls: { trans_smile: ['a','b','c'] }, _frameIntervals: { trans_smile: 17 },
    _mouthConfigs: {}, _frameBackend: { load: originalLoad },
    getExperimentalFrameVideoStats: () => ({}),
    setExperimentalFrameVideoSource: (label, descriptor) => installations.push({ label, descriptor }) };
  globalThis.window = { renderApp: { _sprite: sprite }, wallpaperApp: { scene: { app: { renderer: {} } } }, PIXI: {} };
  const experiment = await installHybridProbe('/fixture/', pack);
  assert.equal(experiment.label, 'trans_smile'); assert.equal(experiment.snapshot().label, 'trans_smile');
  assert.equal(installations.at(-1).label, 'trans_smile');
  experiment.disable(); assert.equal(sprite._frameBackend.load, originalLoad);
  sprite._frameIntervals.trans_smile = 5;
  await assert.rejects(installHybridProbe('/fixture/', pack), /frame interval differs/);
  assert.equal(installations.length, 2, 'invalid clip contracts cannot install or replace the source');
  sprite._frameIntervals.trans_smile = 17;
  const mouthConfig = { frameUrls: ['mouth-open', 'mouth-closed'], closedFrameIdx: 0 };
  sprite._mouthConfigs.trans_smile = mouthConfig;
  const withMouth = await installHybridProbe('/fixture/', pack);
  assert.equal(installations.at(-1).label, 'trans_smile');
  assert.equal(sprite._mouthConfigs.trans_smile, mouthConfig, 'video registration preserves runtime-owned mouth configuration');
  withMouth.disable();
  assert.equal(sprite._frameBackend.load, originalLoad);
  for (const invalid of [undefined, { ...pack, available: false }, { ...pack, version: 'older' },
    { ...pack, sourceSha256: { 'runtime_manifest.json': 'b'.repeat(64) } }]) {
    await assert.rejects(loadHybridFixture('/fixture/', invalid), /hosted pack identity/);
  }
});

for (const change of ['during-install', 'before-reinstall']) {
  test(`video registration rejects stale static URLs ${change}`, async t => {
    const previous = Object.getOwnPropertyDescriptor(globalThis, 'location');
    globalThis.location = { href: 'http://localhost/' };
    t.after(() => {
      if (previous) Object.defineProperty(globalThis, 'location', previous);
      else delete globalThis.location;
    });
    const pack = { available: true, version: 'v1', sourceSha256: { 'runtime_manifest.json': 'hash' } };
    const bundle = { schema: 'amadeus.frame-video-bundle-experiment.v1', packVersion: 'v1',
      manifestSha256: 'hash', clips: [{ label: 'idle', path: 'idle/' }] };
    const metadata = { schema: 'amadeus.frame-video-split-experiment.v1', headFrames: 1, frames: 3,
      frameIntervalMs: 20, reference: { clip: 'idle', packVersion: 'v1', manifestSha256: 'hash',
        productionLineageVerified: true, currentPackDimensionsMatch: true } };
    t.mock.method(globalThis, 'fetch', async url => ({ ok: true,
      json: async () => structuredClone(url.pathname.endsWith('bundle.json') ? bundle : metadata) }));
    const installs = [], load = async () => {};
    const sprite = { _frameUrls: { idle: ['a', 'b', 'c'] }, _frameIntervals: { idle: 20 },
      _frameBackend: { load }, getExperimentalFrameVideoStats: () => ({}),
      setExperimentalFrameVideoSource(label, descriptor) { installs.push({ label, descriptor }); } };
    const pending = installHybridSources(sprite, {}, {}, '/video/', pack, { bundle: true });
    if (change === 'during-install') {
      sprite._frameUrls.idle = ['new-a', 'new-b', 'new-c'];
      await assert.rejects(pending, /Static clip changed/);
      assert.equal(installs.length, 0);
      assert.equal(sprite._frameBackend.load, load);
    } else {
      const installed = await pending;
      installed.reinstall();
      assert.equal(installs.length, 2, 'unchanged source can be explicitly retried');
      sprite._frameUrls.idle.reverse();
      assert.throws(() => installed.reinstall(), /Static clip changed/);
      assert.equal(installs.length, 2);
      installed.disable();
    }
  });
}
