import assert from 'node:assert/strict'
import test from 'node:test'
import vm from 'node:vm'
import { parseArgs, createJourney, seededRandom, summarizeGaps, createEvidenceSummary, installTextureProbe, installTranscodeCounter } from './textureProbe.mjs'

test('shared counter installs on vendor load before rendering and preserves results, receivers and failures', async () => {
  const listeners = new Set(), window = {}, receiver = { marker: 17 }
  const context = { window, performance, Promise, document: {
    addEventListener(name, fn, capture) { assert.equal(name, 'load'); assert.equal(capture, true); listeners.add(fn) },
    removeEventListener(name, fn) { listeners.delete(fn) },
  } }
  vm.runInNewContext(`(${installTranscodeCounter.toString()})()`, context)
  assert.equal(window.__textureTranscodes.installed, false)
  window.PixiBasisKtx2Shim = { KTX2Parser: { async transcode(value) { assert.equal(this, receiver); if (value < 0) throw Error('failed'); return value } } }
  for (const listener of listeners) listener({ target: { tagName: 'SCRIPT' } })
  assert.equal(listeners.size, 0)
  assert.equal(await window.PixiBasisKtx2Shim.KTX2Parser.transcode.call(receiver, 42), 42)
  await assert.rejects(window.PixiBasisKtx2Shim.KTX2Parser.transcode.call(receiver, -1), /failed/)
  assert.equal(window.__textureTranscodes.attempts, 2)
  assert.equal(window.__textureTranscodes.completed, 1)
  assert.equal(window.__textureTranscodes.failures, 1)
})

test('probe defaults to the current-source baseline with sampling disabled', () => {
  const options = parseArgs([])
  assert.equal(options.mode, 'baseline')
  assert.equal(options.sampling, false)
  assert.equal(options.durationSeconds, 60)
  assert.equal(options.fps, 30)
  assert.equal(options.scenario, false)
  assert.equal(options.contextLoss, false)
})

test('explicit paired settings and the optional ten-minute journey parse', () => {
  const options = parseArgs(['sampled', '60', '--sampling', 'on', '--duration-seconds', '600',
    '--seed', '0', '--sample-seconds', '1', '--scenario', '--companion', '--context-loss', '--output', 'probe run'])
  assert.equal(options.sampling, true)
  assert.equal(options.durationSeconds, 600)
  assert.equal(options.seed, 0)
  assert.equal(options.output, 'probe run')
  assert.equal(options.scenario && options.companion && options.contextLoss, true)
  assert.equal(parseArgs(['--sampling', 'on']).mode, 'sampled')
})

test('profile-specific residency measurements cannot silently change the requested FPS', () => {
  assert.equal(parseArgs(['baseline', '30', '--profile', 'power_saving']).profile, 'power_saving')
  assert.equal(parseArgs(['baseline', '60', '--profile', 'standard']).profile, 'standard')
  assert.throws(() => parseArgs(['baseline', '30', '--profile', 'standard']))
  assert.throws(() => parseArgs(['baseline', '60', '--profile', 'power_saving']))
  assert.throws(() => parseArgs(['--profile', 'unknown']))
})

test('contradictory, incomplete, historical, and unbounded settings fail explicitly', () => {
  for (const args of [['baseline', '30', '--sampling', 'on'], ['sampled', '60', '--sampling', 'off'],
    ['baseline', '20'], ['store'], ['--historical-renderer-ref', 'c86177c'], ['--duration-seconds'],
    ['--duration-seconds', 'NaN'], ['--duration-seconds', '44'], ['--duration-seconds', '3601'],
    ['--sample-seconds', '0'], ['--seed', '-1'], ['--seed', '4294967296'], ['--seed', '1.5']]) {
    assert.throws(() => parseArgs(args), undefined, args.join(' '))
  }
})

test('cold first-use and repeat inputs are identical in both sampling settings', () => {
  const off = createJourney(parseArgs(['baseline', '30', '--duration-seconds', '600']))
  const on = createJourney(parseArgs(['sampled', '30', '--duration-seconds', '600']))
  assert.deepEqual(off, on)
  assert.deepEqual(off.find(row => row.phase === 'first-speech'), { at: 3, phase: 'first-speech', command: 'speaking', active: true })
  const cold = off.filter(row => row.phase.startsWith('first-')).map(({ at, phase, ...input }) => input)
  const repeat = off.filter(row => row.phase.startsWith('repeat-')).map(({ at, phase, ...input }) => input)
  assert.deepEqual(cold, repeat)
  assert.ok(off.every((row, index) => row.at < 600 && (!index || row.at > off[index - 1].at)))
})

test('seeded soak inputs reproduce and a different seed changes later expressions', () => {
  const a = createJourney(parseArgs(['--duration-seconds', '600', '--seed', '103']))
  const b = createJourney(parseArgs(['--duration-seconds', '600', '--seed', '103']))
  const c = createJourney(parseArgs(['--duration-seconds', '600', '--seed', '104']))
  assert.deepEqual(a, b)
  assert.notDeepEqual(a.slice(9), c.slice(9))
  const random = seededRandom(0)
  assert.ok(Array.from({ length: 100 }, random).every(value => value >= 0 && value < 1))
})

test('gap summaries use nearest-rank percentiles, count strictly above 50ms, and preserve empty evidence', () => {
  assert.deepEqual(summarizeGaps([]), { count: 0, p50Ms: null, p95Ms: null, p99Ms: null, maxMs: null, over50Ms: 0 })
  assert.deepEqual(summarizeGaps([NaN, null, -1, Infinity, 50, 10, 51, 20]),
    { count: 4, p50Ms: 20, p95Ms: 51, p99Ms: 51, maxMs: 51, over50Ms: 1 })
  assert.equal(summarizeGaps(Array.from({ length: 100 }, (_, index) => index + 1)).p99Ms, 99)
})

test('phase summaries aggregate drained batches once and use each phase duration for throughput', () => {
  const summary = createEvidenceSummary()
  summary.ingest([
    { kind: 'phase-start', phase: 'cold', t: 1000 }, { kind: 'tick', phase: 'cold', t: 1001, dt: null },
    { kind: 'tick', phase: 'cold', t: 1011, dt: 10 }, { kind: 'miss', phase: 'cold', t: 1012 },
    { kind: 'load-start', phase: 'cold', t: 1020, attempt: 2 },
    { kind: 'load-end', phase: 'cold', t: 1030, ok: true },
  ])
  summary.ingest([
    { kind: 'load-end', phase: 'cold', t: 2000, ok: true }, { kind: 'load-end', phase: 'cold', t: 2100, ok: false },
    { kind: 'change', phase: 'cold', t: 2101 }, { kind: 'cycle', phase: 'cold', t: 2102 },
    { kind: 'hold', phase: 'cold', t: 2103 }, { kind: 'decision', phase: 'cold', t: 2104 },
    { kind: 'phase-end', phase: 'cold', t: 3000 }, { kind: 'phase-start', phase: 'repeat', t: 3000 },
  ])
  const [cold, repeat] = summary.finish()
  assert.equal(cold.durationMs, 2000)
  assert.equal(cold.loadedPerSecond, 1)
  assert.equal(cold.tickerGaps.count, 1)
  assert.equal(cold.misses, 1)
  assert.equal(cold.refetches, 1)
  assert.equal(cold.loadFailures, 1)
  assert.equal(cold.cycles, 1)
  assert.equal(cold.holds, 1)
  assert.equal(cold.changes, 1)
  assert.equal(cold.decisions, 1)
  assert.equal(repeat.durationMs, null)
  assert.equal(repeat.loadedPerSecond, null)
  assert.ok(!('ticks' in cold) && !('raf' in cold))
})

function probeFixture(seed = 103) {
  let now = 0, ticker, raf, cancelled = false
  const originalTexture = { id: 'existing' }, loadedTexture = { id: 'loaded' }
  const sprite = { sprite: { texture: originalTexture }, _currentEmotion: 'idle', _frames: { idle: [loadedTexture, null] },
    _activeFrameIdx: 0, _frameIdx: 0, _held: false, _heldFrameIdx: null,
    _showFrame(index) { if (this._frames.idle[index]) this.sprite.texture = this._frames.idle[index] },
    _cycleCompleteHandler() { this.cycleCalls = (this.cycleCalls || 0) + 1 },
    _loadTextureFromCompressedAsset() { return Promise.resolve(loadedTexture) } }
  const math = Object.create(Math), originalRandom = Math.random
  math.random = originalRandom
  const runtime = { postSpeechHoldActive: false, transitionHoldActive: false,
    _nextAutoNode(from) { this.draw = math.random(); return from },
    setSpeaking(active) { this._nextAutoNode('idle'); if (active === 'throw') throw Error('decision failure') } }
  const originalNext = runtime._nextAutoNode, originalShow = sprite._showFrame
  const listeners = new Map()
  const context = { renderApp: { _sprite: sprite, _spriteforgeRuntime: runtime }, window: {}, Math: math,
    wallpaperApp: { scene: { app: { ticker: { add(fn) { ticker = fn }, remove() { ticker = null } },
      view: { addEventListener(name, fn) { listeners.set(name, fn) }, removeEventListener(name) { listeners.delete(name) } } } } },
    PIXI: { UPDATE_PRIORITY: { LOW: -25 } }, performance: { now() { return ++now } },
    requestAnimationFrame(fn) { raf = fn; return 1 }, cancelAnimationFrame() { cancelled = true } }
  vm.runInNewContext(`(${installTextureProbe.toString()})(${seed})`, context)
  return { sprite, runtime, math, originalRandom, originalNext, originalShow, originalTexture, context,
    tick: () => ticker(), raf: () => raf(++now), isStopped: () => cancelled && !ticker && !listeners.size,
    drain: () => JSON.parse(JSON.stringify(context.window.__textureProbe.drain())) }
}

test('probe instrumentation preserves missing-frame behavior and observes real holds, cycles, loads, and drained evidence', async () => {
  const fixture = probeFixture()
  fixture.sprite._showFrame(1)
  assert.equal(fixture.sprite.sprite.texture, fixture.originalTexture)
  fixture.sprite._showFrame(0)
  fixture.sprite._held = true; fixture.sprite._heldFrameIdx = 1
  fixture.tick(); fixture.raf()
  fixture.sprite._cycleCompleteHandler('idle')
  await fixture.sprite._loadTextureFromCompressedAsset('http://127.0.0.1:12345/spriteforge/idle/0.ktx2')
  await fixture.sprite._loadTextureFromCompressedAsset('http://127.0.0.1:12345/spriteforge/idle/0.ktx2')
  const events = fixture.drain().events
  assert.equal(events.filter(event => event.kind === 'miss').length, 1)
  assert.equal(events.find(event => event.kind === 'hold').held, true)
  assert.equal(events.find(event => event.kind === 'hold').heldIndex, 1)
  assert.equal(events.filter(event => event.kind === 'change').length, 1)
  assert.equal(fixture.sprite.cycleCalls, 1)
  assert.equal(events.filter(event => event.kind === 'load-start')[1].attempt, 2)
  assert.equal(events.find(event => event.kind === 'load-start').asset, '/spriteforge/idle/0.ktx2')
  assert.deepEqual(fixture.drain(), { events: [], dropped: 0 })
  fixture.context.window.__textureProbe.stop()
  assert.equal(fixture.sprite._showFrame, fixture.originalShow)
  assert.equal(fixture.runtime._nextAutoNode, fixture.originalNext)
  assert.equal(fixture.isStopped(), true)
})

test('seeded decision wrappers restore Math.random after nested calls and exceptions', () => {
  const a = probeFixture(), b = probeFixture()
  a.runtime.setSpeaking(true); b.runtime.setSpeaking(true)
  assert.equal(a.runtime.draw, b.runtime.draw)
  assert.equal(a.math.random, a.originalRandom)
  assert.throws(() => a.runtime.setSpeaking('throw'), /decision failure/)
  assert.equal(a.math.random, a.originalRandom)
  a.context.window.__textureProbe.stop(); b.context.window.__textureProbe.stop()
})
