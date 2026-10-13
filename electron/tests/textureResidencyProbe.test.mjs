import assert from 'node:assert/strict';
import vm from 'node:vm';
import test from 'node:test';
import { createRequire } from 'node:module';
import { installResidencyProbe } from './textureResidencyProbe.mjs';
const { createFrameStore } = createRequire(import.meta.url)('../../render/web/frame_store.js');
const flush = () => new Promise(resolve => setImmediate(resolve));

async function run(trace, limit = 100000, { loopMode = 'once_then_hold', labels = [] } = {}) {
  let time = 0;
  const loaded = [], destroyed = [];
  const backend = { now: () => ++time, load: async function(url) {
    assert.equal(this, backend); loaded.push(url);
    return { texture: { url }, cpuBytes: 5, gpuBytes: 5 };
  }, upload: async function() { assert.equal(this, backend); },
  destroy: function(texture) { assert.equal(this, backend); destroyed.push(texture.url); } };
  const sprite = { _currentEmotion: 'transition', _frameBackend: backend,
    _frameUrls: { transition: ['/0', '/1', '/2'] }, _frames: { transition: [] },
    _clipConfigs: { transition: { loopMode } }, _prefetchHints: new Map(), _displayMisses: 0,
    _sampleFrameIndex: (_label, index) => index,
    _onTextureViewChange(view, index, texture) { this._frames.transition[index] = texture; },
    _showFrame(index) { if (!this._frames.transition[index]) this._displayMisses++; return 'display-return'; } };
  const store = sprite._frameStore = createFrameStore({ backend, budgetBytes: 10,
    onViewChange: (...args) => sprite._onTextureViewChange(...args) });
  store.replaceViews('frames:transition', sprite._frameUrls.transition);
  const originals = [backend.load, backend.upload, backend.destroy, store.replaceDemand, sprite._showFrame, sprite._onTextureViewChange];
  const host = { renderApp: { _sprite: sprite } };
  if (trace) vm.runInNewContext(`(${installResidencyProbe.toString()})(${limit},${JSON.stringify(labels)})`,
    { window: host, performance: { now: () => ++time } });
  assert.equal(sprite._showFrame(1), 'display-return');
  store.replaceDemand('current', [{ url: '/1', priority: 100 }]); await flush();
  const first = store.get('/1'); assert.ok(first);
  store.replaceDemand('current', [{ url: '/2', priority: 100 }]); await flush();
  assert.equal(store.get('/1'), null); assert.ok(store.get('/2'));
  assert.equal(sprite._showFrame(2), 'display-return');
  const stats = store.stats();
  const evidence = trace ? host.__residencyProbe.stop() : null;
  assert.deepEqual([backend.load, backend.upload, backend.destroy, store.replaceDemand, sprite._showFrame, sprite._onTextureViewChange], originals);
  store.destroy();
  return { observed: { loaded, destroyed, stats, misses: sprite._displayMisses }, evidence };
}

test('residency observation preserves loading, eviction, display results, receivers and cleanup', async () => {
  const baseline = await run(false), traced = await run(true);
  assert.deepEqual(traced.observed, baseline.observed);
  assert.equal(traced.evidence.dropped, 0);
  const events = traced.evidence.rows;
  for (const kind of ['demand', 'load-start', 'load-end', 'upload-start', 'upload-end', 'publish', 'unpublish', 'destroy', 'miss'])
    assert.ok(events.some(event => event.kind === kind), kind);
  const target = events.filter(event => event.label === 'transition' && event.index === 1);
  assert.ok(target.findIndex(event => event.kind === 'load-start') < target.findIndex(event => event.kind === 'publish'));
  assert.ok(target.findIndex(event => event.kind === 'publish') < target.findIndex(event => event.kind === 'unpublish'));
});

test('bounded trace reports overflow without changing playback', async () => {
  const bounded = await run(true, 3), baseline = await run(false);
  assert.deepEqual(bounded.observed, baseline.observed);
  assert.equal(bounded.evidence.rows.length, 3);
  assert.ok(bounded.evidence.dropped > 0);
});


test('explicit loop observation preserves playback and leaves unselected loops out of the trace', async () => {
  const baseline = await run(false, 100000, { loopMode: 'loop' });
  const unselected = await run(true, 100000, { loopMode: 'loop' });
  const selected = await run(true, 100000, { loopMode: 'loop', labels: ['transition'] });
  assert.deepEqual(selected.observed, baseline.observed);
  assert.deepEqual(unselected.observed, baseline.observed);
  assert.equal(unselected.evidence.rows.length, 0);
  for (const kind of ['load-start', 'load-end', 'upload-start', 'upload-end', 'publish', 'unpublish', 'miss']) {
    assert.ok(selected.evidence.rows.some(row => row.kind === kind), kind);
  }
  assert.equal(selected.evidence.dropped, 0);
});
