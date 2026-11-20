const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../render/web/renderer.js'), 'utf8');
const start = source.indexOf('class RenderApp {');
const end = source.indexOf('window.renderApp = new RenderApp', start);
assert.ok(start > 0 && end > start);
const context = { app: { renderer: { textureGC: { maxIdle: 3600, checkCountMax: 600 } } }, AbortController, console };
vm.createContext(context);
// Only the class declaration; the trailing bootstrap comment/assignment is not executed.
vm.runInContext(source.slice(start, source.lastIndexOf('}', end) + 1) + ';globalThis.RenderApp = RenderApp;', context);
function fixture() {
  let budget, disabled = 0, loads = 0, refreshes = 0;
  const instance = Object.create(context.RenderApp.prototype);
  Object.assign(instance, { _mode: 'sprite', _characterConfig: {}, _frameSourceConfig: null,
    _frameSourceGeneration: 0, _frameSourcePending: null, _frameVideoPackage: null, _frameSourceFailed: false,
    _sprite: { _frameIdx: 23, _frameStore: { setBudgetBytes(value) { budget = value; } } },
    _spriteforgeRuntime: { currentNodeId: 'idle', refreshResourcePolicy() { refreshes++; } },
    _reportCharacterStatus() {},
    async _loadFrameVideoSources() { loads++; return { disable() { disabled++; } }; },
  });
  return { instance, get budget() { return budget; }, get disabled() { return disabled; }, get loads() { return loads; }, get refreshes() { return refreshes; } };
}
const candidate = () => ({ requested: 'auto', budgetMiB: 0, reason: 'video_pack_available', candidate: { baseUrl: '/video/', pack: {} } });
test('old packs and explicit static retain 2 GiB and default GC', () => {
  for (const [requested, reason] of [['auto', 'video_pack_missing'], ['static', 'static_selected']]) {
    const f = fixture();
    f.instance._configureFrameSource({ requested, budgetMiB: 0, candidate: null, reason });
    assert.equal(f.budget, 2048 * 1048576); assert.equal(f.loads, 0);
    assert.equal(f.instance.getCharacterStatus().diagnostic.frame_source.mode, 'static');
    assert.equal(context.app.renderer.textureGC.maxIdle, 3600);
    assert.equal(context.app.renderer.textureGC.checkCountMax, 600);
  }
});
test('verified video selects the complete balanced policy and failure restores static policy', async () => {
  const f = fixture(), a = f.instance;
  a._configureFrameSource(candidate());
  assert.equal(f.budget, 2048 * 1048576, 'static policy remains until capability/metadata checks finish');
  await a._frameSourcePending;
  assert.equal(f.budget, 384 * 1048576); assert.equal(a._sprite._frameResourcePolicy.mode, 'video');
  assert.equal(context.app.renderer.textureGC.maxIdle, 60);
  assert.equal(context.app.renderer.textureGC.checkCountMax, 30);
  assert.equal(a._sprite._frameIdx, 23, 'resource policy does not reset the animation clock');
  a._fallbackFrameVideo(Error('device lost'));
  assert.equal(f.disabled, 1); assert.equal(f.budget, 2048 * 1048576);
  assert.equal(a._sprite._frameResourcePolicy.mode, 'static');
  assert.equal(context.app.renderer.textureGC.maxIdle, 3600);
  a._startFrameVideo(); assert.equal(f.loads, 1, 'failure is latched, not silently retried');
});
test('unsupported codecs preserve legacy policy and expose the fallback', async () => {
  const f = fixture(), a = f.instance;
  a._loadFrameVideoSources = async () => { throw Error('Video configuration unsupported'); };
  a._configureFrameSource(candidate()); await a._frameSourcePending;
  assert.equal(f.budget, 2048 * 1048576);
  assert.equal(a.getCharacterStatus().diagnostic.frame_source.reason, 'video_unavailable');
});
test('explicit budget survives switching between video and static', async () => {
  const f = fixture(), a = f.instance;
  a._configureFrameSource({ ...candidate(), budgetMiB: 768 }); await a._frameSourcePending;
  assert.equal(f.budget, 768 * 1048576);
  a._configureFrameSource({ requested: 'static', budgetMiB: 768, candidate: null, reason: 'static_selected' });
  assert.equal(f.budget, 768 * 1048576); assert.equal(f.disabled, 1);
});
test('obsolete package preparation is aborted and cannot activate video after a static selection', async () => {
  const f = fixture(), a = f.instance; let resolve, signal;
  a._loadFrameVideoSources = (_config, s) => { signal = s; return new Promise(r => { resolve = r; }); };
  a._configureFrameSource(candidate()); const pending = a._frameSourcePending;
  a._configureFrameSource({ requested: 'static', budgetMiB: 0, candidate: null, reason: 'static_selected' });
  assert.equal(signal.aborted, true);
  let disposed = false; resolve({ disable() { disposed = true; } }); await pending;
  assert.equal(disposed, true); assert.equal(a._frameVideoPackage, null);
  assert.equal(f.budget, 2048 * 1048576); assert.equal(a._sprite._frameResourcePolicy.mode, 'static');
});


test('retiring sprite resources does not publish status for the model renderer', () => {
  const f = fixture(), a = f.instance;
  a._mode = 'live2d';
  a._reportCharacterStatus = () => assert.fail('model renderer owns its own status');
  a._applyFrameSourcePolicy('static', 'video_pack_missing');
  assert.equal(f.budget, 2048 * 1048576);
});
