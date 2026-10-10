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
app.disableHardwareAcceleration()
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
  const environment = { AMADEUS_UI_THEME: 'classic' }
  store.update({}, { values: {
    AMADEUS_UI_LOCALE: 'en-US', AMADEUS_UI_THEME: 'wallpaper-slice', TTS_BACKEND: null, FISH_TTS_MODEL: null, FISH_TTS_LATENCY: null,
    MIMO_TTS_VOICE: null, TTS_API_VOICE: null,
    GRAPHICS_PROFILE: null, RENDER_MAX_FPS: null, RENDER_TEXTURE_SAMPLING: null,
    WORK_EXECUTION_PROVIDER: null, COOPERATIVE_CHAT_PROVIDER: 'openclaw',
    AMADEUS_VISION_ENABLED: 'false', AMADEUS_VISION_MODE: 'watching',
  } })
  store.markApplied({})
  ipcMain.handle('get-backend-connection', () => null)
  ipcMain.handle('backend-startup.failure', () => null)
  ipcMain.handle('desktop-settings.get', () => store.snapshot(environment))
  ipcMain.handle('desktop-settings.update', (_event, update) => ({ ok: true, settings: store.update(environment, update) }))
  ipcMain.handle('window-theme.set', () => true)
  ipcMain.handle('chat-avatars.get', () => ({ user: '', assistant: '' }))
  ipcMain.handle('companion-portraits.status', () => ({ installed: false }))
  win = new BrowserWindow({ width: 1100, height: 800, show: false, webPreferences: {
    offscreen: true,
    preload: path.join(root, 'electron/dist/preload/index.mjs'), contextIsolation: true, sandbox: false,
  } })
  await win.loadFile(path.join(root, 'electron/dist/renderer/index.html'), { query: { mainWindow: '1' } })
  await until(`Array.from(document.querySelectorAll("button")).some(b => b.textContent.trim().endsWith("Settings"))`)
  await win.webContents.executeJavaScript(`Array.from(document.querySelectorAll("button")).find(b => b.textContent.trim().endsWith("Settings")).click(); true`)
  await until(`Array.from(document.querySelectorAll('button')).some(b => b.textContent.includes('General'))`)
  await win.webContents.executeJavaScript(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('General')).click(); true`)
  await until(`Boolean(document.querySelector('select[aria-label="Startup mode"]'))`)
  await until(`(() => {
    const choices = Array.from(document.querySelectorAll('[aria-label="Interface theme"] button'));
    return choices.length === 2 && choices.every(button => button.disabled)
      && document.querySelector('[data-preview-theme="classic"]')?.getAttribute('aria-checked') === 'true';
  })()`)
  assert.equal(store.snapshot(environment).values.AMADEUS_UI_THEME, 'wallpaper-slice')
  assert.equal(store.backendEnvironment(environment).AMADEUS_UI_THEME, undefined)
  assert.equal(await win.webContents.executeJavaScript(`document.querySelector('[data-preview-theme="classic"] .settings-theme-option-copy span')?.textContent`), 'Clean neutral desktop palette.')
  assert.equal(await win.webContents.executeJavaScript(`document.querySelector('[data-preview-theme="wallpaper-slice"] .settings-theme-option-copy span')?.textContent`), 'Dark translucent surfaces inspired by the Wallpaper Slice.')
  console.log('PASS frontend theme follows the locked source and stays outside backend inputs')
  for (const mode of ['window', 'wallpaper']) {
    await win.webContents.executeJavaScript(`(() => { const s = document.querySelector('select[aria-label="Startup mode"]');
      s.value = ${JSON.stringify(mode)}; s.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`)
    await until(`(async () => (await window.amadeus.getDesktopSettings()).values.AMADEUS_WINDOWS_STARTUP_MODE === ${JSON.stringify(mode)})()`)
    const saved = new DesktopSettingsStore(file, '').snapshot({})
    assert.equal(saved.values.AMADEUS_WINDOWS_STARTUP_MODE, mode)
    assert.equal(saved.restartRequired, false)
    assert.equal(isWallpaperStartup(['Amadeus'], {}, 'win32', saved.values.AMADEUS_WINDOWS_STARTUP_MODE), mode === 'wallpaper')
  }
  await pause(400)
  fs.mkdirSync(output, { recursive: true })
  fs.writeFileSync(path.join(output, 'startup-settings.png'), (await win.webContents.capturePage(undefined, { stayHidden: true })).toPNG())
  console.log('PASS offline GUI startup choices persist and select the next launch without backend restart')
  await win.webContents.executeJavaScript(`(() => {
    const section = document.getElementById('settings-vision');
    section.querySelector('button[aria-pressed]').click(); return true;
  })()`)
  await until(`(async () => (await window.amadeus.getDesktopSettings()).values.AMADEUS_VISION_ENABLED === 'true')()`)
  assert.equal(store.snapshot(environment).values.AMADEUS_VISION_MODE, 'watching')
  assert.ok(store.snapshot(environment).pendingRevisions.AMADEUS_VISION_ENABLED)
  console.log('PASS offline live save preserves the selected vision mode and waits for application acknowledgment')
  // The built renderer, preload and main store consume the packaged catalog,
  // while every backend/network request remains blocked above.
  await win.webContents.executeJavaScript(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === 'Voice').click(); true`)
  await until(`Boolean(document.querySelector('select[aria-label="Backend"] option[value="fish_audio"]'))`)
  await win.webContents.executeJavaScript(`(() => {
    const select = document.querySelector('select[aria-label="Backend"] option[value="fish_audio"]').parentElement;
    select.value = 'fish_audio'; select.dispatchEvent(new Event('change', { bubbles: true })); return true;
  })()`)
  await until(`(async () => (await window.amadeus.getDesktopSettings()).values.TTS_BACKEND === 'fish_audio')()`)
  await until(`(() => { const input = document.querySelector('input[aria-label="Inference model"]');
    return input && !input.disabled && input.closest('details').open; })()`)
  await win.webContents.executeJavaScript(`(() => {
    const input = document.querySelector('input[aria-label="Inference model"]'); input.focus();
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, 'smoke-fish-model');
    input.dispatchEvent(new Event('input', { bubbles: true })); return true;
  })()`)
  await win.webContents.executeJavaScript(`new Promise(resolve => requestAnimationFrame(() => {
    document.querySelector('input[aria-label="Inference model"]').dispatchEvent(new FocusEvent('focusout', { bubbles: true })); resolve(true);
  }))`)
  await until(`(async () => (await window.amadeus.getDesktopSettings()).values.FISH_TTS_MODEL === 'smoke-fish-model')()`)
  await win.webContents.executeJavaScript(`(() => {
    const select = document.querySelector('select[aria-label="Latency mode"]'); select.value = 'low';
    select.dispatchEvent(new Event('change', { bubbles: true })); return true;
  })()`)
  await until(`(async () => (await window.amadeus.getDesktopSettings()).values.FISH_TTS_LATENCY === 'low')()`)
  const voice = new DesktopSettingsStore(file, '').snapshot({})
  assert.equal(voice.restartRequired, true)
  assert.equal(voice.values.FISH_TTS_MODEL, 'smoke-fish-model')
  assert.equal(voice.values.FISH_TTS_LATENCY, 'low')
  console.log('PASS packaged Fish catalog remains editable offline and persists startup overrides')
  for (const [backend, title, key, value] of [
    ['mimo', 'MiMo speech API (Xiaomi)', 'MIMO_TTS_VOICE', 'Mia'],
    ['openai_compatible', 'Remote speech API', 'TTS_API_VOICE', 'echo'],
  ]) {
    await win.webContents.executeJavaScript(`(() => {
      const select = document.querySelector('select[aria-label="Backend"] option[value="${backend}"]').parentElement;
      select.value = '${backend}'; select.dispatchEvent(new Event('change', { bubbles: true })); return true;
    })()`)
    const card = `Array.from(document.querySelectorAll('details.configuration-card-details')).find(card => card.querySelector('.settings-card-title')?.textContent === ${JSON.stringify(title)})`
    await until(`(() => { const card = ${card}; return card?.open && !card.querySelector('input[aria-label="Voice"]').disabled; })()`)
    await win.webContents.executeJavaScript(`(() => {
      const input = (${card}).querySelector('input[aria-label="Voice"]');
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, ${JSON.stringify(value)});
      input.dispatchEvent(new Event('input', { bubbles: true })); return true;
    })()`)
    await win.webContents.executeJavaScript(`new Promise(resolve => requestAnimationFrame(() => {
      (${card}).querySelector('input[aria-label="Voice"]').dispatchEvent(new FocusEvent('focusout', { bubbles: true })); resolve(true);
    }))`)
    await until(`(async () => (await window.amadeus.getDesktopSettings()).values.${key} === ${JSON.stringify(value)})()`)
    assert.equal(new DesktopSettingsStore(file, '').backendEnvironment({})[key], value)
  }
  await win.webContents.executeJavaScript(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === 'Graphics').click(); true`)
  await until(`document.querySelector('select[aria-label="Sample animation textures"]')?.value === 'true'`)
  await win.webContents.executeJavaScript(`(() => {
    const select = document.querySelector('select[aria-label="Graphics profile"]'); select.value = 'power_saving';
    select.dispatchEvent(new Event('change', { bubbles: true })); return true;
  })()`)
  await until(`document.querySelector('select[aria-label="Sample animation textures"]')?.value === 'false'`)
  await win.webContents.executeJavaScript(`(() => {
    const select = document.querySelector('select[aria-label="Sample animation textures"]'); select.value = 'true';
    select.dispatchEvent(new Event('change', { bubbles: true })); return true;
  })()`)
  await until(`(async () => (await window.amadeus.getDesktopSettings()).values.RENDER_TEXTURE_SAMPLING === 'true')()`)
  await win.webContents.executeJavaScript(`(() => {
    const select = document.querySelector('select[aria-label="Graphics profile"]'); select.value = 'custom';
    select.dispatchEvent(new Event('change', { bubbles: true })); return true;
  })()`)
  await until(`Boolean(document.querySelector('input[aria-label="Frame-rate limit"]'))`)
  assert.equal(await win.webContents.executeJavaScript(`document.querySelector('select[aria-label="Sample animation textures"]').value`), 'true')
  console.log('PASS packaged MiMo/OpenAI controls and graphics automatic/explicit settings work offline')
  environment.FISH_TTS_MODEL = 'locked-environment-model'
  await win.webContents.executeJavaScript(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim().endsWith('Chat')).click(); true`)
  await win.webContents.executeJavaScript(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim().endsWith('Settings')).click(); true`)
  await until(`Array.from(document.querySelectorAll('button')).some(b => b.textContent.trim() === 'Voice')`)
  await win.webContents.executeJavaScript(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === 'Voice').click(); true`)
  await until(`(() => { const input = document.querySelector('input[aria-label="Inference model"]');
    return input?.disabled && input.value === 'locked-environment-model'; })()`)
  assert.equal(store.snapshot(environment).values.FISH_TTS_MODEL, 'smoke-fish-model')
  console.log('PASS real form shows and locks environment input while preserving the saved override')
  await win.webContents.executeJavaScript(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === 'Providers').click(); true`)
  await until(`document.body.textContent.includes('Everyday execution: OpenClaw agent')`)
  assert.equal(store.backendEnvironment(environment).WORK_EXECUTION_PROVIDER, 'openclaw')
  console.log('PASS legacy saved routing agrees with the real summary and canonical backend input')
  app.quit()
}).catch(error => { console.error(error); app.exit(1) })
setTimeout(() => { console.error('Startup settings smoke timed out'); app.exit(1) }, 45000).unref()
