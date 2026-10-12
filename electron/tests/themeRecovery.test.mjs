import assert from 'node:assert/strict'
import test from 'node:test'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

test('theme notifications never expose the Electron event and unsubscribe the same wrapper', () => {
  let api, registered, removed
  loadTypeScript(new URL('../src/preload/index.mts', import.meta.url), { electron: {
    contextBridge: { exposeInMainWorld: (name, value) => { if (name === 'amadeus') api = value } },
    ipcRenderer: { on: (name, listener) => { registered = { name, listener } }, removeListener: (name, listener) => { removed = { name, listener } } },
  } })
  let received
  const unsubscribe = api.onDesktopThemeChanged((...args) => { received = args })
  registered.listener({ sender: 'privileged Electron object' }, { payload: 'unexpected' })
  assert.deepEqual(received, [])
  unsubscribe()
  assert.deepEqual(removed, registered)
})

test('native wallpaper tray offers theme recovery without renderer participation', () => {
  let menu, restored = 0
  const { WindowsWallpaperTray } = loadTypeScript(new URL('../src/main/windowsWallpaperTray.ts', import.meta.url), { electron: {
    nativeImage: { createEmpty: () => ({ isEmpty: () => true }) },
    Tray: class { on() {} setToolTip() {} setContextMenu(value) { menu = value } destroy() {} },
    Menu: { buildFromTemplate: value => value },
  } })
  const tray = new WindowsWallpaperTray('', () => {}, () => {}, () => { restored++ })
  menu.find(item => item.label?.startsWith('Restore built-in theme')).click()
  assert.equal(restored, 1)
  tray.destroy()
})
