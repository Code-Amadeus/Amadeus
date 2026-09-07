import assert from 'node:assert/strict'
import test from 'node:test'

import {
  resolveFloatingCompanionBounds,
  resolveFloatingCompanionPlacement,
  resolveMainWindowPlacement,
  wantsFloatingCompanion,
} from '../src/main/windowPlacement.ts'

test('companion startup requires opt-in and honors a one-launch disable', () => {
  assert.equal(wantsFloatingCompanion([], {}), false)
  assert.equal(wantsFloatingCompanion([], { AMADEUS_FLOATING_COMPANION: '0' }), false)
  assert.equal(wantsFloatingCompanion([], { AMADEUS_FLOATING_COMPANION: '1' }), true)
  assert.equal(wantsFloatingCompanion(['--floating-companion'], {}), true)
  assert.equal(wantsFloatingCompanion(['--floating-companion', '--no-floating-companion'], { AMADEUS_FLOATING_COMPANION: '1' }), false)
})

const primary = {
  id: 1,
  bounds: { x: 0, y: 0, width: 2560, height: 1440 },
  workArea: { x: 0, y: 0, width: 2560, height: 1392 },
}

const portraitSecondary = {
  id: 2,
  bounds: { x: 2560, y: 0, width: 1080, height: 1920 },
  workArea: { x: 2560, y: 0, width: 1080, height: 1872 },
}

test('the main chat defaults to a centered primary window', () => {
  assert.deepEqual(
    resolveMainWindowPlacement([primary, portraitSecondary], primary),
    {
      bounds: { x: 730, y: 296, width: 1100, height: 800 },
      displayId: 1,
      fullscreen: false,
      secondary: false,
    },
  )
})

test('a small primary display keeps the main chat recoverable', () => {
  const smallPrimary = {
    id: 3,
    bounds: { x: -900, y: 40, width: 900, height: 600 },
    workArea: { x: -900, y: 40, width: 900, height: 560 },
  }
  assert.deepEqual(
    resolveMainWindowPlacement([smallPrimary], smallPrimary),
    {
      bounds: smallPrimary.workArea,
      displayId: 3,
      fullscreen: false,
      secondary: false,
    },
  )
})

test('primary and explicit display preferences remain deterministic', () => {
  assert.equal(
    resolveMainWindowPlacement([primary, portraitSecondary], primary, 'secondary').displayId,
    2,
  )
  assert.equal(
    resolveMainWindowPlacement([primary, portraitSecondary], primary, '2', true).fullscreen,
    true,
  )
})

test('the floating companion owns the first secondary display by default', () => {
  assert.deepEqual(
    resolveFloatingCompanionPlacement([primary, portraitSecondary], primary),
    {
      bounds: portraitSecondary.workArea,
      dedicatedDisplay: true,
      displayId: 2,
    },
  )
})

test('the floating companion falls back to a compact primary overlay', () => {
  assert.deepEqual(
    resolveFloatingCompanionPlacement([primary], primary),
    {
      bounds: { x: 2056, y: 548, width: 480, height: 820 },
      dedicatedDisplay: false,
      displayId: 1,
    },
  )
})

test('the floating companion stays inside the primary work area', () => {
  assert.deepEqual(
    resolveFloatingCompanionBounds({ x: 0, y: 0, width: 2560, height: 1392 }),
    { x: 2056, y: 548, width: 480, height: 820 },
  )
  assert.deepEqual(
    resolveFloatingCompanionBounds({ x: -900, y: 40, width: 320, height: 480 }),
    { x: -876, y: 64, width: 272, height: 432 },
  )
})

function assertInside(inner, outer) {
  assert.ok(inner.width > 0 && inner.height > 0)
  assert.ok(inner.x >= outer.x && inner.y >= outer.y)
  assert.ok(inner.x + inner.width <= outer.x + outer.width)
  assert.ok(inner.y + inner.height <= outer.y + outer.height)
}

test('single-screen placement fits small and scaled work areas including taskbar offsets', () => {
  for (const workArea of [
    { x: 0, y: 0, width: 1366, height: 720 },
    { x: 48, y: 0, width: 976, height: 576 },
    { x: -800, y: -400, width: 800, height: 392 },
    { x: 0, y: 32, width: 240, height: 320 },
  ]) {
    const display = { id: 7, bounds: workArea, workArea }
    const result = resolveFloatingCompanionPlacement([display], display)
    assert.equal(result.displayId, display.id)
    assert.equal(result.dedicatedDisplay, false)
    assertInside(result.bounds, workArea)
  }
})

test('disconnecting and reconnecting a requested secondary recomputes visible placement', () => {
  const connected = resolveFloatingCompanionPlacement([primary, portraitSecondary], primary, '2')
  const disconnected = resolveFloatingCompanionPlacement([primary], primary, '2')
  const reconnected = resolveFloatingCompanionPlacement([primary, portraitSecondary], primary, '2')
  assert.equal(connected.displayId, 2)
  assert.equal(disconnected.displayId, 1)
  assert.equal(disconnected.dedicatedDisplay, false)
  assertInside(disconnected.bounds, primary.workArea)
  assert.deepEqual(reconnected, connected)
})

test('a secondary on the left uses its current work area after metrics change', () => {
  const left = { id: 4, bounds: { x: -1920, y: -100, width: 1920, height: 1080 },
    workArea: { x: -1920, y: -60, width: 1920, height: 1040 } }
  const resized = { ...left, workArea: { x: -1280, y: -60, width: 1280, height: 680 } }
  assert.deepEqual(resolveFloatingCompanionPlacement([primary, left], primary).bounds, left.workArea)
  assert.deepEqual(resolveFloatingCompanionPlacement([primary, resized], primary).bounds, resized.workArea)
  assertInside(resolveFloatingCompanionPlacement([primary, left], primary, 'primary').bounds, primary.workArea)
})
