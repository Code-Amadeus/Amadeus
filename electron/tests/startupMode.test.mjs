import assert from 'node:assert/strict'
import test from 'node:test'

import { isWallpaperStartup } from '../src/main/startupMode.ts'
import { managesWindowsWallpaper } from '../src/main/windowsWallpaper.ts'

test('wallpaper startup is explicit in argv or environment', () => {
  for (const platform of ['darwin', 'linux', 'win32']) {
    assert.equal(isWallpaperStartup(['electron', '.', '--wallpaper'], {}, platform), true)
    assert.equal(isWallpaperStartup(['electron', '.'], { AMADEUS_WALLPAPER: '1' }, platform), true)
  }
})

test('macOS and Linux retain explicit wallpaper startup and the existing flag precedence', () => {
  for (const platform of ['darwin', 'linux']) {
    assert.equal(isWallpaperStartup(['electron', '.'], {}, platform), false)
    assert.equal(isWallpaperStartup(['electron', '.'], { AMADEUS_WALLPAPER: '0' }, platform), false)
    assert.equal(isWallpaperStartup(['electron', '.', '--wallpaper'], { AMADEUS_WALLPAPER: '0' }, platform), true)
  }
})

test('Windows opens the control panel by default without opting into wallpaper setup', () => {
  assert.equal(isWallpaperStartup(['electron', '.'], {}, 'win32'), false)
  assert.equal(isWallpaperStartup(['electron', '.'], {}, 'win32', 'invalid'), false)
  assert.equal(isWallpaperStartup(['electron', '.', '--no-wallpaper'], {}, 'win32'), false)
  assert.equal(isWallpaperStartup(['electron', '.'], { AMADEUS_WALLPAPER: '0' }, 'win32'), false)
  assert.equal(isWallpaperStartup(['electron', '.'], { AMADEUS_WALLPAPER_HOST: 'external' }, 'win32'), false)
  assert.equal(isWallpaperStartup(['electron', '.', '--wallpaper'], { AMADEUS_WALLPAPER_HOST: 'external' }, 'win32'), true)
})


test('Windows GUI preference chooses the next launch without changing explicit overrides', () => {
  assert.equal(isWallpaperStartup(['electron', '.'], {}, 'win32', 'window'), false)
  assert.equal(isWallpaperStartup(['electron', '.'], {}, 'win32', 'wallpaper'), true)
  assert.equal(isWallpaperStartup(['electron', '.'], { AMADEUS_WINDOWS_STARTUP_MODE: 'window' }, 'win32', 'wallpaper'), false)
  assert.equal(isWallpaperStartup(['electron', '.'], { AMADEUS_WINDOWS_STARTUP_MODE: 'wallpaper' }, 'win32', 'window'), true)
  assert.equal(isWallpaperStartup(['electron', '.', '--wallpaper'], {}, 'win32', 'window'), true)
  assert.equal(isWallpaperStartup(['electron', '.'], { AMADEUS_WALLPAPER: '1' }, 'win32', 'window'), true)
  assert.equal(isWallpaperStartup(['electron', '.', '--no-wallpaper'], {}, 'win32', 'wallpaper'), false)
  assert.equal(isWallpaperStartup(['electron', '.'], { AMADEUS_WALLPAPER: '0' }, 'win32', 'wallpaper'), false)
  for (const platform of ['darwin', 'linux']) {
    assert.equal(isWallpaperStartup(['electron', '.'], {}, platform, 'wallpaper'), false)
  }
})

test('wallpaper entry choices never override external host ownership', () => {
  for (const mode of ['window', 'wallpaper']) {
    for (const override of [{}, { AMADEUS_WINDOWS_STARTUP_MODE: 'wallpaper' }, { AMADEUS_WALLPAPER: '1' }]) {
      const environment = { AMADEUS_WALLPAPER_HOST: 'external', ...override }
      assert.equal(isWallpaperStartup(['Amadeus.exe'], environment, 'win32', mode),
        mode === 'wallpaper' || Object.keys(override).length > 0)
      assert.equal(isWallpaperStartup(['Amadeus.exe', '--wallpaper'], environment, 'win32', mode), true)
      assert.equal(managesWindowsWallpaper('win32', environment), false)
    }
  }
})
