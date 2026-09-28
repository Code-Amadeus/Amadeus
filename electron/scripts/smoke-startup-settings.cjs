// Real renderer/preload/settings store, with no backend process or network.
const { app, BrowserWindow, ipcMain, session } = require('electron')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { pathToFileURL } = require('node:url')
const root = path.resolve(__dirname, '../..')
const output = path.join(root, 'electron/build')
const profile = path.join(output, 'startup-settings-smoke')
app.setPath('userData', profile)
let win
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(source) {
  const deadline = Date.now() + 10000
  while (Date.now() < deadline) {
    if (await win.webContents.executeJavaScript(source)) return
    await pause(50)
  }
  throw new Error('UI condition timed out: ' + source)
}
app.whenReady().then(async () => {
  session.defaultSession.webRequest.onBeforeRequest({ urls: ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*'] },
    (_request, callback) => callback({ cancel: true }))
  const { DesktopSettingsStore } = await import(pathToFileURL(path.join(root, 'electron/dist/main/desktopSettings.js')).href)
  const { isWallpaperStartup } = await import(pathToFileURL(path.join(root, 'electron/dist/main/startupMode.js')).href)
  const file = path.join(profile, 'settings.json')
  const store = new DesktopSettingsStore(file, path.join(profile, '.env'))
  store.update({}, { values: { AMADEUS_UI_LOCALE: 'en-US' } })
  ipcMain.handle('get-backend-connection', () => null)
  ipcMain.handle('desktop-settings.get', () => store.snapshot({}))
  ipcMain.handle('desktop-settings.update', (_event, update) => ({ ok: true, settings: store.update({}, update) }))
  ipcMain.handle('window-theme.set', () => true)
  ipcMain.handle('chat-avatars.get', () => ({ user: '', assistant: '' }))
  ipcMain.handle('companion-portraits.status', () => ({ installed: false }))
  win = new BrowserWindow({ width: 1100, height: 800, show: true, webPreferences: {
    preload: path.join(root, 'electron/dist/preload/index.mjs'), contextIsolation: true, sandbox: false,
  } })
  await win.loadFile(path.join(root, 'electron/dist/renderer/index.html'), { query: { mainWindow: '1' } })
  await until(`Array.from(document.querySelectorAll("button")).some(b => b.textContent.trim().endsWith("Settings"))`)
  await win.webContents.executeJavaScript(`Array.from(document.querySelectorAll("button")).find(b => b.textContent.trim().endsWith("Settings")).click(); true`)
  await until(`Array.from(document.querySelectorAll('button')).some(b => b.textContent.includes('General'))`)
  await win.webContents.executeJavaScript(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('General')).click(); true`)
  await until(`Boolean(document.querySelector('select[aria-label="Startup mode"]'))`)
  for (const mode of ['window', 'wallpaper']) {
    await win.webContents.executeJavaScript(`(() => { const s = document.querySelector('select[aria-label="Startup mode"]');
      s.value = ${JSON.stringify(mode)}; s.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`)
    await until(`(async () => (await window.amadeus.getDesktopSettings()).values.AMADEUS_WINDOWS_STARTUP_MODE === ${JSON.stringify(mode)})()`)
    const saved = new DesktopSettingsStore(file, '').snapshot({})
    assert.equal(saved.values.AMADEUS_WINDOWS_STARTUP_MODE, mode)
    assert.equal(saved.restartRequired, false)
    assert.equal(isWallpaperStartup(['Amadeus'], {}, 'win32', saved.values.AMADEUS_WINDOWS_STARTUP_MODE), mode === 'wallpaper')
  }
  win.showInactive()
  await pause(400)
  fs.mkdirSync(output, { recursive: true })
  fs.writeFileSync(path.join(output, 'startup-settings.png'), (await win.webContents.capturePage()).toPNG())
  console.log('PASS offline GUI startup choices persist and select the next launch without backend restart')
  app.quit()
}).catch(error => { console.error(error); app.exit(1) })
setTimeout(() => { console.error('Startup settings smoke timed out'); app.exit(1) }, 45000).unref()
