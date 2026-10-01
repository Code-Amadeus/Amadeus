import assert from 'node:assert/strict'
import test from 'node:test'

import { isCompanionOnlyStartup, isWallpaperStartup, resolveStartupMode } from '../src/main/startupMode.ts'
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

test('companion-only is its own startup mode on every platform', () => {
  for (const platform of ['darwin', 'linux', 'win32']) {
    assert.equal(resolveStartupMode(['electron', '.', '--companion'], {}, platform), 'companion')
    assert.equal(resolveStartupMode(['electron', '.'], { AMADEUS_COMPANION: '1' }, platform), 'companion')
    assert.equal(isCompanionOnlyStartup(['electron', '.', '--companion'], {}, platform), true)
    assert.equal(isCompanionOnlyStartup(['electron', '.'], {}, platform), false)
  }
})

test('companion-only never starts the wallpaper host, even with wallpaper opt-ins', () => {
  const optIns = [
    { AMADEUS_WALLPAPER: '1' },
    { AMADEUS_WINDOWS_STARTUP_MODE: 'wallpaper' },
  ]
  for (const platform of ['darwin', 'linux', 'win32']) {
    for (const override of optIns) {
      const environment = { ...override, AMADEUS_COMPANION: '1' }
      assert.equal(isWallpaperStartup(['electron', '.'], environment, platform, 'wallpaper'), false)
    }
  }
  assert.equal(isWallpaperStartup(['electron', '.', '--companion'], { AMADEUS_WALLPAPER: '1' }, 'win32'), false)
})

test('an explicit wallpaper request still wins over a saved window preference', () => {
  assert.equal(resolveStartupMode(['electron', '.', '--wallpaper'], {}, 'win32', 'window'), 'wallpaper')
  assert.equal(resolveStartupMode(['electron', '.', '--no-wallpaper'], {}, 'win32', 'wallpaper'), 'window')
  assert.equal(resolveStartupMode(['electron', '.'], {}, 'win32', 'wallpaper'), 'wallpaper')
  assert.equal(resolveStartupMode(['electron', '.'], {}, 'win32', 'window'), 'window')
  assert.equal(isCompanionOnlyStartup(['electron', '.', '--companion'], {}, 'win32', 'wallpaper'), true)
})
