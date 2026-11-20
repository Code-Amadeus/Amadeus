import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import { createTagJourney, createAllClipTagJourney, installTagPreviewProbe } from './tagPreviewProbe.mjs';
import { parseArgs, createJourney } from './textureProbe.mjs';

test('preview modes retain identical public inputs, burst timing and after-speech handoffs', () => {
  const runs = ['off', 'correct', 'revoked'].map(mode => createJourney(parseArgs([
    'sampled', '60', '--duration-seconds', '190', '--tag-preview', mode])));
  const actual = runs.map(rows => rows.filter(row => row.command !== 'tag-preview'));
  assert.deepEqual(actual[0], actual[1]); assert.deepEqual(actual[1], actual[2]);
  assert.ok(actual[0].some(row => row.options?.presentation_handoff === 'after_speech'));
  assert.ok(actual[0].some(row => row.label === 'idle_closed_eye'));
  for (const [mode, rows] of runs.entries()) {
    let previousTrigger = -Infinity;
    for (const row of rows.filter(row => row.command === 'trigger')) {
      const preview = rows.find(other => other.phase === row.phase + '-preview');
      assert.ok(preview.at < row.at && preview.at > previousTrigger);
      assert.ok(row.at - preview.at <= 1.001);
      if (mode === 0) assert.equal(preview.previewLabel, null);
      if (mode === 1) assert.equal(preview.previewLabel, row.label);
      if (mode === 2) assert.notEqual(preview.previewLabel, row.label);
      previousTrigger = row.at;
    }
    assert.ok(rows.every((row, index) => row.at < 190 && (!index || row.at >= rows[index - 1].at)));
  }
  for (const args of [['--tag-preview'], ['--tag-preview', 'unknown'],
    ['--tag-preview', 'off', '--fixed-route'], ['--tag-preview', 'correct', '--only-clip', 'idle'],
    ['--tag-preview', 'revoked', '--cold-clip', 'idle']]) assert.throws(() => parseArgs(args));
});

test('preview uses real graph entry resolution and all direct successors without a graph decision', () => {
  const source = fs.readFileSync(new URL('../../render/web/renderer.js', import.meta.url), 'utf8');
  const start = source.indexOf('class SpriteForgeRuntime {'), end = source.indexOf('class SubtitleOverlay {', start);
  const hints = [], math = Object.create(Math);
  math.random = () => { throw Error('Preview consumed a random draw'); };
  const sprite = { _currentEmotion: 'root', _frameIdx: 23, _sampleTimeMs: 391,
    _held: true, _displayMisses: 0, prefetchLabels(labels, priority, options) {
      hints.push({ labels: Array.from(labels), priority, options });
    }, _showFrame() { return 42; }, clearHold() {}, setEmotion() {} };
  const context = { Math: math, performance, console: { log() {} }, window: {}, setTimeout() { throw Error('Unexpected timer'); }, clearTimeout() {} };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end) + '\nglobalThis.Runtime=SpriteForgeRuntime;', context);
  const runtime = new context.Runtime(sprite);
  runtime.graph = { nodes: [
    { id: 'r', label: 'root' }, { id: 'e', label: 'entry' },
    { id: 'a', label: 'target' }, { id: 'b', label: 'rare' },
  ], edges: [{ from: 'r', to: 'e', prob: 0 }, { from: 'e', to: 'a', prob: 0.95 },
    { from: 'e', to: 'b', prob: 0.05 }] };
  runtime.nodesById = Object.fromEntries(runtime.graph.nodes.map(n => [n.id, n]));
  runtime.labelToIds = Object.fromEntries(runtime.graph.nodes.map(n => [n.label, [n.id]]));
  runtime.cfg = {}; runtime.rootNodeId = runtime.currentNodeId = 'r';
  context.window.renderApp = { _sprite: sprite, _spriteforgeRuntime: runtime };
  const originalTrigger = runtime.trigger, originalShow = sprite._showFrame;
  vm.runInContext(`(${installTagPreviewProbe.toString()})()`, context);
  const probe = context.window.__tagPreviewProbe;
  const result = probe.preview('target');
  assert.deepEqual(new Set(result.labels), new Set(['entry', 'target', 'rare']));
  assert.deepEqual(JSON.parse(JSON.stringify(result.before)), JSON.parse(JSON.stringify(result.after)));
  assert.equal(runtime.currentNodeId, 'r'); assert.equal(sprite._frameIdx, 23);
  assert.equal(sprite._showFrame(), 42, 'observer preserves the return value');
  probe.preview(null);
  assert.deepEqual(hints.at(-1).labels, []);
  assert.equal(hints.at(-1).options.reason, 'probe-pending-tag');
  const trace = probe.stop();
  assert.equal(trace.dropped, 0); assert.equal(runtime.trigger, originalTrigger); assert.equal(sprite._showFrame, originalShow);
  assert.ok(trace.rows.some(row => row.kind === 'preview'));
  assert.ok(trace.rows.some(row => row.kind === 'withdraw'));
});


test('all-clip stress covers every canonical label in both speech states without forcing nodes', () => {
  const labels = ['loop', 'entry', 'rare', 'loop'];
  const routes = ['off', 'correct', 'revoked'].map(tagPreview => createAllClipTagJourney(labels, { durationSeconds: 45, tagPreview }));
  assert.deepEqual(routes[0].filter(row => row.command !== 'tag-preview'), routes[1].filter(row => row.command !== 'tag-preview'));
  assert.deepEqual(routes[1].filter(row => row.command !== 'tag-preview'), routes[2].filter(row => row.command !== 'tag-preview'));
  for (const route of routes) {
    const covered = new Set(); let speaking = false;
    for (const row of route) {
      if (row.command === 'speaking') speaking = row.active;
      if (row.command === 'trigger') covered.add(`${row.label}:${speaking}`);
      assert.notEqual(row.command, 'route-clip');
    }
    assert.deepEqual(covered, new Set(['loop:false', 'entry:false', 'rare:false', 'loop:true', 'entry:true', 'rare:true']));
    assert.ok(route.every((row, index) => row.at < 45 && (!index || row.at >= route[index - 1].at)));
  }
  assert.throws(() => createAllClipTagJourney(labels, { durationSeconds: 22 }), /two complete/);
  const options = parseArgs(['--all-clip-tags']);
  assert.equal(options.tagPreview, 'off');
  assert.throws(() => createJourney(options), /loaded runtime labels/);
  assert.throws(() => parseArgs(['--all-clip-tags', '--fixed-route']));
});
