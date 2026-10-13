import assert from 'node:assert/strict'
import test from 'node:test'
import vm from 'node:vm'
import { installTextureGcProbe } from './textureGcProbe.mjs'

function fixture(options = {}, limit = 10000) {
  let now = 0
  const value = {}, error = Error('GC failed'), calls = []
  const texture = { managedTextures: [{}, {}, {}] }
  const gc = { mode: 0, maxIdle: 3600, checkCountMax: 600, count: 100, checkCount: 100,
    run(...args) { calls.push({ receiver: this, args }); now += 3; texture.managedTextures.pop()
      if (args[0] === 'fail') throw error
      return value
    } }
  const original = gc.run
  const context = { window: { PIXI: { GC_MODES: { AUTO: 0 } },
    wallpaperApp: { scene: { app: { renderer: { textureGC: gc, texture } } } } },
    performance: { now: () => now } }
  const installed = vm.runInNewContext(`(${installTextureGcProbe.toString()})(${JSON.stringify(options)},${limit})`, context)
  return { gc, original, value, error, calls, installed, probe: context.window.__textureGcProbe }
}

test('GC observation preserves settings, receiver, arguments and return value', () => {
  const f = fixture({ trace: true })
  assert.equal(f.gc.maxIdle, 3600); assert.equal(f.gc.checkCountMax, 600)
  assert.equal(f.gc.run('token', 12), f.value)
  assert.equal(f.calls[0].receiver, f.gc); assert.deepEqual(f.calls[0].args, ['token', 12])
  const result = f.probe.stop()
  assert.equal(result.final.calls, 1); assert.equal(result.final.totalMs, 3)
  assert.equal(result.rows[0].before, 3); assert.equal(result.rows[0].after, 2)
  assert.equal(result.rows[0].failed, false); assert.equal(result.dropped, 0)
  assert.equal(f.gc.run, f.original)
})

test('scoped tuning changes only idle/check thresholds and restores them once', () => {
  const f = fixture({ maxIdle: 120, checkCountMax: 30 })
  assert.equal(f.gc.maxIdle, 120); assert.equal(f.gc.checkCountMax, 30)
  assert.equal(f.gc.count, 100); assert.equal(f.gc.checkCount, 100)
  assert.equal(f.gc.run, f.original)
  const first = f.probe.stop()
  assert.equal(first.applied.maxIdle, 120)
  assert.equal(f.gc.maxIdle, 3600); assert.equal(f.gc.checkCountMax, 600)
  assert.equal(f.probe.stop(), first)
})

test('observer propagates errors and makes bounded trace loss visible', () => {
  const f = fixture({ maxIdle: 120, checkCountMax: 30, trace: true }, 1)
  assert.throws(() => f.gc.run('fail'), error => error === f.error)
  assert.equal(f.gc.run(), f.value)
  const result = f.probe.stop()
  assert.equal(result.rows.length, 1); assert.equal(result.rows[0].failed, true)
  assert.equal(result.final.calls, 2); assert.equal(result.dropped, 1)
  assert.equal(f.gc.run, f.original); assert.equal(f.gc.maxIdle, 3600)
})
