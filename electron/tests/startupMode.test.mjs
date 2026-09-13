import assert from 'node:assert/strict'
import test from 'node:test'

import { isWallpaperStartup } from '../src/main/startupMode.ts'

test('wallpaper startup is explicit in argv or environment', () => {
  assert.equal(isWallpaperStartup(['electron', '.', '--wallpaper'], {}), true)
  assert.equal(isWallpaperStartup(['electron', '.'], { AMADEUS_WALLPAPER: '1' }), true)
})

test('ordinary Electron startup remains visible', () => {
  assert.equal(isWallpaperStartup(['electron', '.'], {}), false)
  assert.equal(isWallpaperStartup(['electron', '.'], { AMADEUS_WALLPAPER: '0' }), false)
})
