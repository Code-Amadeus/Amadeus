import assert from 'node:assert/strict'
import test from 'node:test'

import {
  MACOS_WINDOW_LEVELS,
  wallpaperWindowPolicy,
} from '../src/main/wallpaperWindowPolicy.ts'

test('macOS uses a desktop-level full-scene host', () => {
  assert.deepEqual(wallpaperWindowPolicy('darwin'), {
    constructorOptions: {
      type: 'desktop',
      focusable: false,
      hiddenInMissionControl: true,
    },
    hostMode: 'scene',
    joinAllWorkspaces: true,
    interactiveLevel: { level: 'normal', relativeLevel: MACOS_WINDOW_LEVELS.canvas },
    visibleLevel: { level: 'normal', relativeLevel: MACOS_WINDOW_LEVELS.scene },
    supportsWindowShape: false,
  })
})

test('macOS levels preserve the desktop interaction invariant', () => {
  assert.ok(MACOS_WINDOW_LEVELS.dockWallpaper < MACOS_WINDOW_LEVELS.desktop)
  assert.ok(MACOS_WINDOW_LEVELS.desktop < MACOS_WINDOW_LEVELS.scene)
  assert.ok(MACOS_WINDOW_LEVELS.scene < MACOS_WINDOW_LEVELS.finderDesktopIcons)
  assert.ok(MACOS_WINDOW_LEVELS.finderDesktopIcons < MACOS_WINDOW_LEVELS.canvas)
  assert.ok(MACOS_WINDOW_LEVELS.canvas < MACOS_WINDOW_LEVELS.normalApplication)
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
