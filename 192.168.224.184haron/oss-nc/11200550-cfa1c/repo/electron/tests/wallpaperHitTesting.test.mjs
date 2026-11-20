import assert from 'node:assert/strict'
import test from 'node:test'

import { desktopPointHitsWindowRegions } from '../src/main/wallpaperHitTesting.ts'

const windowBounds = { x: 500, y: 200, width: 530, height: 460 }
const regions = [
  { x: 440, y: 10, width: 70, height: 40 },
  { x: 20, y: 40, width: 360, height: 380 },
]

test('desktop points inside visible Canvas controls are interactive', () => {
  assert.equal(desktopPointHitsWindowRegions({ x: 960, y: 225 }, windowBounds, regions), true)
  assert.equal(desktopPointHitsWindowRegions({ x: 700, y: 500 }, windowBounds, regions), true)
})

test('transparent Canvas space remains click-through', () => {
  assert.equal(desktopPointHitsWindowRegions({ x: 900, y: 600 }, windowBounds, regions), false)
  assert.equal(desktopPointHitsWindowRegions({ x: 200, y: 200 }, windowBounds, regions), false)
})

test('empty and invalid regions never capture the desktop', () => {
  assert.equal(desktopPointHitsWindowRegions({ x: 500, y: 200 }, windowBounds, []), false)
  assert.equal(desktopPointHitsWindowRegions(
    { x: 500, y: 200 },
    windowBounds,
    [{ x: 0, y: 0, width: 0, height: 20 }],
  ), false)
})
