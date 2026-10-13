// Serialized diagnostic only. FrameStore ownership and playback stay unchanged.
export function installTextureGcProbe(options = {}, limit = 10000) {
  const renderer = window.wallpaperApp.scene.app.renderer
  const gc = renderer.textureGC, texture = renderer.texture
  if (!gc || typeof gc.run !== 'function' || !Array.isArray(texture.managedTextures)) {
    throw Error('Pixi texture GC is unavailable')
  }
  const settings = () => ({ mode: gc.mode, maxIdle: gc.maxIdle, checkCountMax: gc.checkCountMax })
  const original = settings(), run = gc.run, rows = []
  let calls = 0, totalMs = 0, maxMs = 0, removed = 0, dropped = 0, stopped
  if (options.maxIdle !== undefined) {
    if (gc.mode !== window.PIXI.GC_MODES.AUTO) throw Error('Texture GC tuning requires AUTO mode')
    gc.maxIdle = options.maxIdle
    gc.checkCountMax = options.checkCountMax
  }
  const applied = settings()
  const snapshot = () => ({ atMs: performance.now(), count: gc.count, checkCount: gc.checkCount,
    managedTextures: texture.managedTextures.length, settings: settings(),
    calls, totalMs, maxMs, removed, dropped })
  const observed = function (...args) {
    const t = performance.now(), before = texture.managedTextures.length
    let failed = true
    try { const value = run.apply(this, args); failed = false; return value }
    finally {
      const ms = performance.now() - t, after = texture.managedTextures.length
      calls++; totalMs += ms; maxMs = Math.max(maxMs, ms); removed += Math.max(0, before - after)
      if (rows.length < limit) rows.push({ t, ms, count: gc.count, before, after, failed })
      else dropped++
    }
  }
  if (options.trace) gc.run = observed
  window.__textureGcProbe = { snapshot, stop() {
    if (stopped) return stopped
    stopped = { original, applied, final: snapshot(), rows, dropped,
      units: 'Pixi screen-render postrender counts; not milliseconds or source frames.' }
    if (options.trace && gc.run === observed) gc.run = run
    gc.maxIdle = original.maxIdle; gc.checkCountMax = original.checkCountMax
    return stopped
  } }
  return { original, applied, trace: Boolean(options.trace), units: 'screen-render postrender counts' }
}
