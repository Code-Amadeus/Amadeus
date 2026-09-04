import assert from 'node:assert/strict'
import test from 'node:test'

import {
  WallpaperCanvasLifecycle,
  wallpaperShapeSender,
} from '../src/main/wallpaperCanvasLifecycle.ts'
import { desktopPointHitsWindowRegions } from '../src/main/wallpaperHitTesting.ts'

class FakeScheduler {
  callback = null
  cleared = 0

  setInterval(callback) {
    this.callback = callback
    return callback
  }

  clearInterval(handle) {
    if (this.callback === handle) this.callback = null
    this.cleared += 1
  }

  tick() {
    this.callback?.()
  }
}

class FakeWindow {
  bounds = { x: 500, y: 200, width: 530, height: 460 }
  visible = false
  destroyed = false
  closeCount = 0
  hideCount = 0
  showCount = 0
  ignoreCalls = []

  close() {
    this.closeCount += 1
    this.destroyed = true
  }

  getBounds() { return this.bounds }
  hide() { this.hideCount += 1; this.visible = false }
  isDestroyed() { return this.destroyed }
  isVisible() { return this.visible }
  setIgnoreMouseEvents(ignore) { this.ignoreCalls.push(ignore) }
  showInactive() { this.showCount += 1; this.visible = true }
}

function createLifecycle(cursor = { x: 700, y: 500 }) {
  const scheduler = new FakeScheduler()
  const lifecycle = new WallpaperCanvasLifecycle({
    getCursorScreenPoint: () => cursor,
    pointHitsWindowRegions: desktopPointHitsWindowRegions,
    scheduler,
  })
  return { lifecycle, scheduler }
}

test('only the scene and Canvas senders can submit shape IPC', () => {
  const sceneSender = {}
  const canvasSender = {}

  assert.equal(wallpaperShapeSender(canvasSender, sceneSender, canvasSender), 'canvas')
  assert.equal(wallpaperShapeSender(sceneSender, sceneSender, canvasSender), 'scene')
  assert.equal(wallpaperShapeSender({}, sceneSender, canvasSender), null)
  assert.equal(wallpaperShapeSender(null, null, null), null)
})

test('empty regions restore pass-through, stop hit testing, and hide Canvas', () => {
  const { lifecycle, scheduler } = createLifecycle()
  const window = new FakeWindow()
  lifecycle.attach(window, 'bridge-a')

  lifecycle.commitRegions(window, [{ x: 20, y: 40, width: 360, height: 380 }])
  scheduler.tick()
  assert.equal(window.ignoreCalls.at(-1), false)
  assert.equal(window.visible, true)
  assert.equal(lifecycle.snapshot().hitTestActive, true)

  const result = lifecycle.commitRegions(window, [])

  assert.equal(result.accepted, true)
  assert.equal(window.ignoreCalls.at(-1), true)
  assert.equal(window.visible, false)
  assert.equal(lifecycle.snapshot().hitTestActive, false)
  assert.deepEqual(lifecycle.snapshot().hitRegions, [])
})

test('scene reload resets Canvas state and its timer', () => {
  const { lifecycle, scheduler } = createLifecycle()
  const window = new FakeWindow()
  lifecycle.attach(window, 'bridge-a')
  lifecycle.commitRegions(window, [{ x: 20, y: 40, width: 360, height: 380 }])
  scheduler.tick()

  assert.equal(lifecycle.prepareReload(window, 'bridge-b'), true)
  assert.deepEqual(lifecycle.snapshot(), {
    hasWindow: true,
    bridgeKey: 'bridge-b',
    hitRegions: [],
    hitTestActive: false,
    ignoringMouse: true,
  })
  assert.equal(window.visible, false)
  assert.equal(window.ignoreCalls.at(-1), true)
})

test('scene close closes Canvas and clears all lifecycle state', () => {
  const { lifecycle, scheduler } = createLifecycle()
  const window = new FakeWindow()
  lifecycle.attach(window, 'bridge-a')
  lifecycle.commitRegions(window, [{ x: 20, y: 40, width: 360, height: 380 }])
  scheduler.tick()

  lifecycle.close()

  assert.equal(window.closeCount, 1)
  assert.deepEqual(lifecycle.snapshot(), {
    hasWindow: false,
    bridgeKey: '',
    hitRegions: [],
    hitTestActive: false,
    ignoringMouse: true,
  })
})
