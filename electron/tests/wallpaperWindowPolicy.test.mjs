import assert from 'node:assert/strict'
import test from 'node:test'

import { wallpaperWindowPolicy } from '../src/main/wallpaperWindowPolicy.ts'

test('macOS uses a desktop-level full-scene host', () => {
  assert.deepEqual(wallpaperWindowPolicy('darwin'), {
    constructorOptions: {
      type: 'desktop',
      focusable: false,
      hiddenInMissionControl: true,
    },
    hostMode: 'scene',
    joinAllWorkspaces: true,
    interactiveLevel: { level: 'normal', relativeLevel: -2147483598 },
    visibleLevel: { level: 'normal', relativeLevel: -2147483609 },
    supportsWindowShape: false,
  })
})

test('Windows keeps the existing shaped interactive slice policy', () => {
  assert.deepEqual(wallpaperWindowPolicy('win32'), {
    constructorOptions: { focusable: true },
    hostMode: 'slice',
    joinAllWorkspaces: false,
    interactiveLevel: null,
    visibleLevel: null,
    supportsWindowShape: true,
  })
})
