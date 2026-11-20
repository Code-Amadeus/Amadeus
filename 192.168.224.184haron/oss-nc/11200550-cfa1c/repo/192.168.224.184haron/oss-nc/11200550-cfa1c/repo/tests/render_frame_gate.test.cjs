const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const { createPresentGate, createFrameRateController } = require('../render/web/render_budget.js');

function timestamps(refresh, seconds = 12, jitter = 0.5) {
  let seed = 103;
  return Array.from({ length: refresh * seconds }, (_, i) => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return 1000 + i * 1000 / refresh + (seed / 2 ** 32 * 2 - 1) * jitter;
  });
}

test('presentation reaches the requested mean rate on integer and noninteger refresh ratios', () => {
  for (const refresh of [60, 75, 120, 144, 165, 240]) {
    for (const fps of [10, 24, 30, 60, 90, 120, 240]) {
      const gate = createPresentGate();
      const source = timestamps(refresh);
      const shown = source.filter(time => gate.shouldPresent(time, fps));
      const actual = (shown.length - 1) * 1000 / (shown.at(-1) - shown[0]);
      assert.ok(Math.abs(actual - Math.min(refresh, fps)) < 0.2, `${refresh}/${fps}: ${actual}`);
    }
  }
});

test('integer vsync division has no extra-vsync stalls with timestamp jitter', () => {
  for (const [refresh, fps] of [[60, 30], [60, 60], [120, 60], [240, 60]]) {
    const gate = createPresentGate();
    const shown = timestamps(refresh).filter(time => gate.shouldPresent(time, fps));
    for (let i = 1; i < shown.length; i++) {
      assert.ok(Math.abs(shown[i] - shown[i - 1] - 1000 / fps) <= 1.01, `${refresh}/${fps}`);
    }
  }
});

test('a stall drops missed presentations without a catch-up burst', () => {
  const gate = createPresentGate();
  const shown = timestamps(60).map((time, i) => time + (i >= 360 ? 250 : 0))
    .filter(time => gate.shouldPresent(time, 30));
  const gaps = shown.slice(1).map((time, i) => time - shown[i]);
  assert.equal(gaps.filter(gap => gap > 50).length, 1);
  assert.ok(gaps.every(gap => gap >= 1000 / 60 - 1.01));
});

test('duplicate timestamps do not render, and a reset starts a fresh phase', () => {
  const gate = createPresentGate();
  assert.equal(gate.shouldPresent(100, 30), true);
  assert.equal(gate.shouldPresent(100, 30), false);
  assert.equal(gate.shouldPresent(110, 30), false);
  gate.reset();
  assert.equal(gate.shouldPresent(111, 60), true);
});

function pixiTicker() {
  const context = { console, performance: { now: () => 0 }, setTimeout, clearTimeout };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(require.resolve('../render/web/vendor/pixi.min.js'), 'utf8'), context);
  return new context.PIXI.Ticker();
}

test('the real bundled Pixi ticker receives elapsed time across gated vsyncs', () => {
  const ticker = pixiTicker();
  const controller = createFrameRateController(ticker, 30);
  const deltas = [];
  ticker.add(() => deltas.push(ticker.deltaMS));
  controller.apply();
  ticker.update(0);
  ticker.update(1000 / 60);
  ticker.update(2000 / 60);
  assert.equal(ticker.maxFPS, 0);
  assert.equal(controller.effectiveMaxFps, 30);
  assert.equal(deltas.length, 2);
  assert.ok(Math.abs(deltas[1] - 1000 / 30) < 0.001);
  ticker.update(500);
  assert.equal(deltas.at(-1), 100, 'Pixi retains its existing delta clamp after a pause');
  ticker.destroy();
});

test('host cap updates and repeated apply keep a single wrapper and the effective cap', () => {
  const ticker = pixiTicker();
  const controller = createFrameRateController(ticker, 60);
  const wrapped = ticker.update;
  controller.apply();
  assert.equal(controller.setHostMaxFps(30), 30);
  assert.equal(controller.setHostMaxFps(120), 60);
  assert.equal(controller.setHostMaxFps(20), 20);
  assert.equal(controller.setHostMaxFps(0), 60);
  assert.equal(controller.apply(), 60);
  assert.equal(ticker.update, wrapped);
  assert.equal(ticker.maxFPS, 0);
  ticker.destroy();
});
