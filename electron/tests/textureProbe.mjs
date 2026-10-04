// Probe-only argument, journey, and evidence helpers. No renderer imports.

// Install before navigation's vendor scripts, identically for main and store.
// Observe the shared transcoder boundary without changing either renderer.
export function installTranscodeCounter() {
  const counters = window.__textureTranscodes = { installed: false, attempts: 0, completed: 0, failures: 0, elapsedMs: 0 }
  const install = () => {
    const parser = window.PixiBasisKtx2Shim?.KTX2Parser
    if (counters.installed || typeof parser?.transcode !== 'function') return
    const transcode = parser.transcode
    parser.transcode = function (...args) {
      counters.attempts++
      const start = performance.now()
      let result
      try { result = transcode.apply(this, args) }
      catch (error) { counters.failures++; throw error }
      return Promise.resolve(result).then(value => {
        counters.completed++; counters.elapsedMs += performance.now() - start
        return value
      }, error => { counters.failures++; throw error })
    }
    counters.installed = true
    document.removeEventListener('load', loaded, true)
  }
  const loaded = event => { if (event.target?.tagName === 'SCRIPT') install() }
  document.addEventListener('load', loaded, true)
  install()
}
export const usage = `Usage: electron tests/fpsTextures.probe.mjs [baseline|sampled] [30|60] [options]
  --sampling off|on        Explicit sampling setting (default: off; sampled means on)
  --profile PROFILE       custom (default), standard at 60 FPS, power_saving at 30 FPS
  --duration-seconds N     Measurement duration, 45..3600 (default: 60; use 600 for soak)
  --sample-seconds N       Snapshot/drain interval, 0.5..10 (default: 2)
  --seed N                 Unsigned 32-bit journey and graph seed (default: 103)
  --scenario              Exercise the public work-activity scenario if available
  --companion             Exercise companion presentation suppression/restoration
  --context-loss          Lose and restore WebGL context if supported
  --texture-disposal      Dispose the held GPU texture and verify re-upload pixels
  --fixed-route           Use the same scheduled clip route without graph draws
  --high-performance-gpu  Request Chromium's high-performance GPU preference
  --expect-gpu TEXT       Refuse measurement on a different WebGL renderer
  --bc7-cache DIRECTORY   Experimental prebuilt, source-verified BC7 cache
  --disk-cache            Exercise automatic product cache generation and reuse
  --cache-profile PATH    Shared isolated user-cache profile across product runs
  --cold-clip LABEL       Isolate a real root successor after head prefetch
  --cold-fill             Preload and release low-priority demand before entry
  --cold-start-delay-ms N  Inject one bounded cold-entry loading pause (0..1000)
  --cold-enter-seconds N   Delay fixed entry to exercise an idle loading pipeline
  --output DIRECTORY      New run directory (must not already exist)
  --help                  Print this help without starting a host
Both modes use the current checked-out source. Run off and on separately with
the same seed, FPS, duration, and optional phases. No historical source override.`

export function parseArgs(argv) {
  const options = { mode: 'baseline', fps: 30, sampling: false, durationSeconds: 60,
    sampleSeconds: 2, seed: 103, profile: 'custom', scenario: false, companion: false, contextLoss: false, help: false }
  const positional = []
  let explicitSampling
  const values = new Map([['--duration-seconds', 'durationSeconds'], ['--sample-seconds', 'sampleSeconds'],
    ['--seed', 'seed'], ['--output', 'output'], ['--cold-clip', 'coldClip'], ['--cold-start-delay-ms', 'coldStartDelayMs'],
    ['--cold-enter-seconds', 'coldEnterSeconds']])
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i]
    if (token === '--help') options.help = true
    else if (token === '--scenario') options.scenario = true
    else if (token === '--companion') options.companion = true
    else if (token === '--context-loss') options.contextLoss = true
    else if (token === '--texture-disposal') options.textureDisposal = true
    else if (token === '--fixed-route') options.fixedRoute = true
    else if (token === '--disk-cache') options.diskCache = true
    else if (token === '--high-performance-gpu') options.highPerformanceGpu = true
    else if (token === '--expect-gpu' || token === '--bc7-cache' || token === '--cache-profile') {
      const value = argv[++i]
      if (!value || value.startsWith('--')) throw Error('Missing expected GPU')
      options[token === '--bc7-cache' ? 'bc7Cache' : token === '--cache-profile' ? 'cacheProfile' : 'expectGpu'] = value
    }
    else if (token === '--cold-fill') options.coldFill = true
    else if (token === '--sampling' || token === '--profile' || values.has(token)) {
      const value = argv[++i]
      if (!value || value.startsWith('--')) throw Error(`Missing value for ${token}`)
      if (token === '--sampling') {
        if (!['off', 'on'].includes(value)) throw Error('--sampling expects off|on')
        explicitSampling = value === 'on'
      } else if (token === '--profile') options.profile = value
      else options[values.get(token)] = ['--output', '--cold-clip'].includes(token) ? value : Number(value)
    } else if (token.startsWith('--')) throw Error(`Unknown option: ${token}`)
    else positional.push(token)
  }
  if (positional.length > 2) throw Error('Expected at most mode and FPS')
  if (positional[0]) options.mode = positional[0]
  if (positional[1]) options.fps = Number(positional[1])
  if (!['baseline', 'sampled'].includes(options.mode)) throw Error('Expected baseline|sampled')
  if (![30, 60].includes(options.fps)) throw Error('Expected FPS 30|60')
  if (!['custom', 'standard', 'power_saving'].includes(options.profile)) throw Error('Unknown graphics profile')
  if ((options.profile === 'standard' && options.fps !== 60) || (options.profile === 'power_saving' && options.fps !== 30)) {
    throw Error('Graphics profile and requested FPS disagree')
  }
  options.sampling = explicitSampling ?? options.mode === 'sampled'
  if (positional[0] && explicitSampling !== undefined && options.sampling !== (options.mode === 'sampled')) {
    throw Error('Mode and --sampling disagree; use baseline/off or sampled/on')
  }
  options.mode = options.sampling ? 'sampled' : 'baseline'
  for (const [key, min, max] of [['durationSeconds', 45, 3600], ['sampleSeconds', 0.5, 10]]) {
    if (!Number.isFinite(options[key]) || options[key] < min || options[key] > max) {
      throw Error(`${key} must be between ${min} and ${max}`)
    }
  }
  if (!Number.isInteger(options.seed) || options.seed < 0 || options.seed > 0xffffffff) {
    throw Error('seed must be an unsigned 32-bit integer')
  }
  if (options.coldClip && !/^[a-zA-Z0-9_-]+$/.test(options.coldClip)) throw Error('Invalid cold clip label')
  if (options.coldFill && !options.coldClip) throw Error('--cold-fill requires --cold-clip')
  if (options.fixedRoute && options.coldClip) throw Error('Fixed route and cold-clip are separate experiments')
  if (options.coldStartDelayMs !== undefined && (!options.coldClip || !Number.isFinite(options.coldStartDelayMs)
    || options.coldStartDelayMs < 0 || options.coldStartDelayMs > 1000)) throw Error('Invalid cold-start delay')
  if (options.coldEnterSeconds !== undefined && (!options.coldClip || !Number.isFinite(options.coldEnterSeconds)
    || options.coldEnterSeconds < 20 || options.coldEnterSeconds + 15 >= options.durationSeconds)) throw Error('Invalid cold entry time')
  return options
}

export function seededRandom(seed) {
  let state = seed >>> 0
  return () => {
    state += 0x6D2B79F5
    let value = Math.imul(state ^ state >>> 15, state | 1)
    value ^= value + Math.imul(value ^ value >>> 7, value | 61)
    return ((value ^ value >>> 14) >>> 0) / 4294967296
  }
}

// The first triggers happen before preload settles. Later phases repeat the same
// public journey; the soak continues with deterministic, timestamped inputs.
export function createJourney(options) {
  if (options.fixedRoute) {
    const clips = [
      ['idle', 8], ['trans_smile', 1.2], ['smile_speaking', 8, true],
      ['speaking_trans', 1.2], ['speaking_long', 10, true], ['idle_side_butterfly', 14],
      ['closed_eye_trans', 1.2], ['speaking_closed_eye_2', 8, true], ['idle_closed_eye', 16],
      ['thinking_trans', 1.2], ['thinking_speaking2', 8, true], ['sad_trans', 1.2],
      ['sad_speaking', 6, true], ['idle2', 8],
    ]
    const actions = []
    let at = 0, lap = 0
    while (at < options.durationSeconds) {
      for (const [label, seconds, speaking = false] of clips) {
        if (at >= options.durationSeconds) break
        actions.push({ at: Math.round(at * 1000) / 1000, phase: `route-${lap}-${label}`, command: 'route-clip', label, speaking })
        at += seconds
      }
      lap++
    }
    return actions
  }
  const enter = options.coldEnterSeconds ?? 20;
  if (options.coldClip) return [
    { at: 0, phase: 'cold-prepare', command: 'cold-prepare', label: options.coldClip },
    { at: 18, phase: 'cold-settle', command: 'cold-settle', label: options.coldClip },
    { at: enter, phase: 'cold-enter', command: 'cold-enter', label: options.coldClip },
    { at: enter + 18, phase: 'cold-return', command: 'idle' },
    { at: enter + 23, phase: 'cold-repeat', command: 'cold-enter', label: options.coldClip },
  ].filter(action => action.at < options.durationSeconds)
  const actions = [
    { at: 0, phase: 'startup', command: 'idle' },
    { at: 3, phase: 'first-speech', command: 'speaking', active: true },
    { at: 8, phase: 'first-speech-stop', command: 'speaking', active: false },
    { at: 11, phase: 'first-transition', command: 'trigger', label: 'trans_smile' },
    { at: 16, phase: 'first-idle', command: 'idle' },
    { at: 23, phase: 'repeat-speech', command: 'speaking', active: true },
    { at: 28, phase: 'repeat-speech-stop', command: 'speaking', active: false },
    { at: 31, phase: 'repeat-transition', command: 'trigger', label: 'trans_smile' },
    { at: 36, phase: 'repeat-idle', command: 'idle' },
  ]
  const random = seededRandom(options.seed)
  const labels = ['trans_smile', 'sad_trans', 'shy_trans', 'surprise_trans', 'angry_trans', 'thinking_trans']
  for (let at = 45, cycle = 1; at + 10 < options.durationSeconds; at += 15, cycle++) {
    const label = labels[Math.floor(random() * labels.length)]
    actions.push({ at, phase: `soak-${cycle}-expression`, command: 'trigger', label },
      { at: at + 3, phase: `soak-${cycle}-speech`, command: 'speaking', active: true },
      { at: at + 8, phase: `soak-${cycle}-stop`, command: 'speaking', active: false },
      { at: at + 11, phase: `soak-${cycle}-idle`, command: 'idle' })
  }
  return actions.filter(action => action.at < options.durationSeconds)
}

// Serialized into the isolated renderer; validates pixels, not only restore events.
export function readTexturePixels() {
  const app = wallpaperApp.scene.app, texture = renderApp._sprite.sprite.texture
  const target = new PIXI.Sprite(texture)
  let pixels
  try { pixels = app.renderer.extract.pixels(target) }
  finally { target.destroy({ texture: false, baseTexture: false }) }
  let hash = 2166136261, rgbSum = 0, alphaSum = 0
  for (let i = 0; i < pixels.length; i++) {
    hash = Math.imul(hash ^ pixels[i], 16777619)
    if (i % 4 === 3) alphaSum += pixels[i]; else rgbSum += pixels[i]
  }
  const gl = app.renderer.gl, errors = []
  for (let i = 0; i < 16; i++) { const error = gl.getError(); if (error === gl.NO_ERROR) break; errors.push(error) }
  return { width: texture.width, height: texture.height, hash: (hash >>> 0).toString(16), rgbSum, alphaSum, glErrors: errors }
}

export function summarizeGaps(values) {
  const sorted = values.filter(value => Number.isFinite(value) && value >= 0).sort((a, b) => a - b)
  const percentile = fraction => sorted.length ? sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)] : null
  return { count: sorted.length, p50Ms: percentile(0.5), p95Ms: percentile(0.95),
    p99Ms: percentile(0.99), maxMs: sorted.at(-1) ?? null, over50Ms: sorted.filter(value => value > 50).length }
}

export function createEvidenceSummary() {
  const phases = new Map()
  const phase = name => {
    if (!phases.has(name)) phases.set(name, { name, startedAtMs: null, endedAtMs: null,
      raf: [], ticks: [], misses: 0, changes: 0, cycles: 0, holds: 0,
      loadsStarted: 0, loadsCompleted: 0, loadFailures: 0, refetches: 0, decisions: 0 })
    return phases.get(name)
  }
  return {
    ingest(events) {
      for (const event of events) {
        const row = phase(event.phase)
        if (event.kind === 'phase-start') row.startedAtMs = event.t
        else if (event.kind === 'phase-end') row.endedAtMs = event.t
        else if (event.kind === 'raf') row.raf.push(event.dt)
        else if (event.kind === 'tick') row.ticks.push(event.dt)
        else if (event.kind === 'miss') row.misses++
        else if (event.kind === 'change') row.changes++
        else if (event.kind === 'cycle') row.cycles++
        else if (event.kind === 'hold') row.holds++
        else if (event.kind === 'decision') row.decisions++
        else if (event.kind === 'load-start') { row.loadsStarted++; if (event.attempt > 1) row.refetches++ }
        else if (event.kind === 'load-end') { if (event.ok) row.loadsCompleted++; else row.loadFailures++ }
      }
    },
    finish() {
      return [...phases.values()].map(({ raf, ticks, ...row }) => {
        const durationMs = row.startedAtMs !== null && row.endedAtMs !== null
          ? Math.max(0, row.endedAtMs - row.startedAtMs) : null
        return { ...row, durationMs, rafGaps: summarizeGaps(raf), tickerGaps: summarizeGaps(ticks),
          loadedPerSecond: durationMs > 0 ? row.loadsCompleted * 1000 / durationMs : null }
      })
    },
  }
}

// Runs only when injected by the Electron probe. Every wrapper calls the real
// implementation. Math.random is replaced only during synchronous runtime
// decisions and restored in finally, including nested calls and exceptions.
export function installTextureProbe(seed) {
  const app = wallpaperApp.scene.app, sprite = renderApp._sprite, runtime = renderApp._spriteforgeRuntime
  let state = seed >>> 0
  const random = () => {
    state += 0x6D2B79F5
    let value = Math.imul(state ^ state >>> 15, state | 1)
    value ^= value + Math.imul(value ^ value >>> 7, value | 61)
    return ((value ^ value >>> 14) >>> 0) / 4294967296
  }
  const original = [], loadAttempts = new Map()
  let events = [], dropped = 0, phase = 'startup', previousRaf = null, previousTick = null
  let rafId, lastTexture = sprite.sprite.texture, lastHold = ''
  const emit = (kind, fields = {}) => {
    if (events.length >= 20000) { dropped++; return }
    events.push({ kind, t: performance.now(), phase, ...fields })
  }
  const wrap = (owner, name, replacement) => {
    if (typeof owner[name] !== 'function') return false
    const fn = owner[name]
    original.push(() => { owner[name] = fn })
    owner[name] = replacement(fn)
    return true
  }
  for (const name of ['_nextAutoNode', 'setSpeaking']) {
    wrap(runtime, name, fn => function (...args) {
      const previous = Math.random
      Math.random = random
      try {
        const result = fn.apply(this, args)
        if (name === '_nextAutoNode') emit('decision', { from: args[0], to: result })
        return result
      } finally { Math.random = previous }
    })
  }
  const legacyMisses = wrap(sprite, '_showFrame', fn => function (index, ...args) {
    const label = this._frames?.[this._currentEmotion] ? this._currentEmotion : 'normal'
    const frames = this._frames?.[label]
    if (frames?.length) {
      const logical = index % frames.length
      const target = this._sampleFrameIndex ? this._sampleFrameIndex(label, logical, this._sampleTimeMs) : logical
      if (!frames[target]) emit('miss', { label, logical, target })
    }
    return fn.call(this, index, ...args)
  })
  wrap(sprite, '_cycleCompleteHandler', fn => function (label, ...args) {
    emit('cycle', { label })
    return fn.call(this, label, ...args)
  })
  let legacyLoads = false
  for (const name of ['_loadTextureFromCompressedAsset', '_loadTextureFromRasterImage']) {
    legacyLoads = wrap(sprite, name, fn => function (url, ...args) {
      // Strip run-specific origin and query. Only repository-relative asset paths
      // are retained; do not copy a local file URL into evidence.
      const text = String(url), marker = text.indexOf('/spriteforge/')
      const asset = marker >= 0 ? text.slice(marker).split(/[?#]/)[0] : `asset-${loadAttempts.size + 1}`
      const attempt = (loadAttempts.get(text) || 0) + 1
      loadAttempts.set(text, attempt)
      const started = performance.now()
      emit('load-start', { asset, attempt })
      let promise
      try { promise = fn.call(this, url, ...args) }
      catch (error) { emit('load-end', { asset, ok: false, durationMs: performance.now() - started }); throw error }
      return Promise.resolve(promise).then(value => {
        emit('load-end', { asset, ok: Boolean(value), durationMs: performance.now() - started })
        return value
      }, error => {
        emit('load-end', { asset, ok: false, durationMs: performance.now() - started })
        throw error
      })
    }) || legacyLoads
  }
  const ticker = () => {
    const now = performance.now()
    emit('tick', { dt: previousTick === null ? null : now - previousTick })
    previousTick = now
    if (lastTexture !== sprite.sprite.texture) {
      emit('change', { label: sprite._currentEmotion, source: sprite._activeFrameIdx, logical: sprite._frameIdx })
      lastTexture = sprite.sprite.texture
    }
    const hold = JSON.stringify([runtime.postSpeechHoldActive, runtime.transitionHoldActive, sprite._held, sprite._heldFrameIdx])
    if (hold !== lastHold) {
      emit('hold', { postSpeech: runtime.postSpeechHoldActive, transition: runtime.transitionHoldActive,
        source: sprite._activeFrameIdx, label: sprite._currentEmotion, held: sprite._held,
        heldIndex: sprite._heldFrameIdx ?? null })
      lastHold = hold
    }
  }
  app.ticker.add(ticker, null, PIXI.UPDATE_PRIORITY.LOW)
  const raf = now => {
    emit('raf', { dt: previousRaf === null ? null : now - previousRaf, timestamp: now })
    previousRaf = now
    rafId = requestAnimationFrame(raf)
  }
  rafId = requestAnimationFrame(raf)
  const canvas = app.view
  const lost = () => emit('context-lost')
  const restored = () => emit('context-restored')
  canvas.addEventListener('webglcontextlost', lost)
  canvas.addEventListener('webglcontextrestored', restored)
  emit('phase-start')
  window.__textureProbe = {
    capabilities: { legacyMisses, legacyLoads, textureStats: typeof renderApp.getTextureStats === 'function' },
    setPhase(next) {
      emit('phase-end'); phase = next; previousRaf = null; previousTick = null; emit('phase-start')
    },
    drain() { const result = { events, dropped }; events = []; dropped = 0; return result },
    stop() {
      emit('phase-end'); cancelAnimationFrame(rafId); app.ticker.remove(ticker)
      canvas.removeEventListener('webglcontextlost', lost); canvas.removeEventListener('webglcontextrestored', restored)
      for (const restore of original.reverse()) restore()
    },
  }
  return window.__textureProbe.capabilities
}
