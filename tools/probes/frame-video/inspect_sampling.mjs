// Read actual renderer selections with an immediately ready static backend.
// This isolates time/selection semantics, not GPU supply or visual acceptance.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import budget from '../../../render/web/render_budget.js';
import storeApi from '../../../render/web/frame_store.js';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const [output] = process.argv.slice(2);
if (!output) throw Error('Usage: node inspect_sampling.mjs NEW_OUTPUT.json');
const source = fs.readFileSync(path.join(root, 'render/web/renderer.js'), 'utf8');
const start = source.indexOf('class SpriteRenderer {'), end = source.indexOf('class SpriteForgeRuntime {', start);
if (start < 0 || end < start) throw Error('Renderer class boundary changed');
class Display {
  constructor() { this.anchor = { set() {} }; this.texture = {}; }
  addChild() {}
}
async function simulate(sampling, steps, legacyFloor = false) {
  let unload, cycles = 0, cycleAt = null, elapsed = 0;
  const ticker = { deltaMS: 0, add(callback) { this.tick = callback; } };
  const backend = { now: () => 0, upload: async () => {}, dispose() {},
    destroy(texture) { texture.destroyed = texture.baseTexture.destroyed = true; },
    load: async url => ({ texture: { url, sourceIndex: Number(url.split(':')[1]), width: 764, height: 1028,
      baseTexture: { valid: true } }, cpuBytes: 1, gpuBytes: 1 }) };
  const context = vm.createContext({ app: { ticker }, frameRateController: { effectiveMaxFps: 60 },
    renderBudget: budget.resolveRenderBudget({ maxFps: 60, textureSampling: sampling }),
    graphicsProfile: 'standard', PIXI: { Container: Display, Sprite: Display, Graphics: Display },
    window: { RenderBudget: budget, FrameStore: storeApi, FrameTextureBackend: { createFrameTextureBackend: () => backend },
      addEventListener(name, callback) { if (name === 'unload') unload = callback; } },
    console: { log() {}, warn() {}, error() {} }, setTimeout, clearTimeout });
  const testedSource = legacyFloor ? source.replace(
    'plan.timelineIndices.length < count ? Math.round(sample) : Math.floor(sample)', 'Math.floor(sample)') : source;
  vm.runInContext(testedSource.slice(start, testedSource.indexOf('class SpriteForgeRuntime {', start)) + '\nglobalThis.SpriteRenderer=SpriteRenderer', context);
  const sprite = new context.SpriteRenderer(new Display());
  sprite._applyCurrentTransform = sprite._hideMouthLayer = sprite._updateMouthLayer = () => {};
  const label = 'trans_smile', count = 120, interval = 5;
  const urls = Array.from({ length: count }, (_, index) => `${label}:${index}`);
  sprite.loadFrames(label, urls); sprite.setIdleFrameIntervalMs(label, interval);
  sprite.setClipConfig(label, { loopMode: 'once_then_hold' }); sprite.setEmotion(label);
  // Make every source immediately available in both conditions. Count planned
  // residency separately; this run measures selection rather than cache misses.
  sprite._frameStore.replaceDemand('diagnostic-ready', urls.map(url => ({ url, priority: 100 })));
  for (let i = 0; i < 100; i++) {
    await new Promise(resolve => setImmediate(resolve));
    if (!sprite._frameStore.stats().inFlight && !sprite._frameStore.stats().queued) break;
  }
  if (sprite._frames[label].filter(Boolean).length !== count) throw Error('Diagnostic source not ready');
  sprite.setCycleCompleteHandler(() => { cycles++; cycleAt ??= elapsed; });
  const rows = [{ timeMs: 0, logical: sprite._frameIdx, displayed: sprite._activeFrameIdx }];
  for (const step of steps) {
    elapsed += step; ticker.deltaMS = step; ticker.tick(1);
    rows.push({ timeMs: elapsed, logical: sprite._frameIdx, displayed: sprite._activeFrameIdx });
  }
  sprite.holdFrame(42);
  const exactHold = { requested: 42, logical: sprite._frameIdx, displayed: sprite._activeFrameIdx,
    url: sprite.sprite.texture.url };
  unload();
  return { rows, cycles, cycleAt, exactHold };
}
const scenarios = {
  regular60Hz: Array(45).fill(1000 / 60),
  jitterWithin20ms: Array.from({ length: 45 }, (_, i) => [16, 17, 18, 15, 17, 17][i % 6]),
  one80msStall: Array.from({ length: 45 }, (_, i) => i === 8 ? 80 : 1000 / 60),
};
const report = { schema: 'amadeus.sampling-selection.v1', clip: 'trans_smile', count: 120, intervalMs: 5,
  fps: 60, assumptions: ['All sources preloaded to isolate clock/selection from supply.',
    'Ticks are synthetic; comparisons are not visual acceptance or a GPU benchmark.',
    'Sampling currently also changes legacy max-four-steps and cycle-notification behavior.',
    'legacyFloor is an in-memory control; the current product rounds only reduced timelines.'],
  scenarios: {}, sourceSha256: {} };
for (const name of ['render/web/renderer.js', 'render/web/render_budget.js', 'tools/probes/frame-video/inspect_sampling.mjs'])
  report.sourceSha256[name] = createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex');
for (const [name, steps] of Object.entries(scenarios)) {
  const off = await simulate(false, steps), on = await simulate(true, steps);
  const legacyFloor = await simulate(true, steps, true);
  const legacyFloorDifferences = off.rows.map((row, i) => (legacyFloor.rows[i].displayed - row.displayed) * 5);
  const differences = off.rows.map((row, i) => ({ timeMs: row.timeMs,
    sourceDifference: on.rows[i].displayed - row.displayed,
    logicalDifference: on.rows[i].logical - row.logical }));
  report.scenarios[name] = { off, on, legacyFloor,
    legacyFloorDifferentTicks: legacyFloorDifferences.filter(Boolean).length,
    legacyFloorMaxDisplayTimeDifferenceMs: Math.max(...legacyFloorDifferences.map(Math.abs)),
    differentDisplayTicks: differences.filter(row => row.sourceDifference).length,
    maxDisplayTimeDifferenceMs: Math.max(...differences.map(row => Math.abs(row.sourceDifference) * 5)),
    maxLogicalSourceDifference: Math.max(...differences.map(row => Math.abs(row.logicalDifference))), differences };
}
const plan = budget.createFrameSamplingPlan(120, 5, 60);
report.planned = { retainedFrames: plan.indices.length, indices: plan.indices, timelineIndices: plan.timelineIndices,
  sampleIntervalMs: plan.sampleIntervalMs, sourceDurationMs: 600,
  bc7CpuPlusGpuBytesPerFrame: 2 * Math.ceil(764 / 4) * Math.ceil(1027 / 4) * 16 };
fs.writeFileSync(output, JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
console.log(JSON.stringify({ planned: report.planned, scenarios: Object.fromEntries(Object.entries(report.scenarios)
  .map(([name, row]) => [name, { differentDisplayTicks: row.differentDisplayTicks,
    maxDisplayTimeDifferenceMs: row.maxDisplayTimeDifferenceMs, maxLogicalSourceDifference: row.maxLogicalSourceDifference,
    offCycleAt: row.off.cycleAt, onCycleAt: row.on.cycleAt,
    legacyFloorDifferentTicks: row.legacyFloorDifferentTicks, legacyFloorMaxDisplayTimeDifferenceMs: row.legacyFloorMaxDisplayTimeDifferenceMs, exactHold: row.on.exactHold }])) }, null, 2));
