// Run with Electron after `npm run build`. Exercises the real main-process
// startup and before-quit hooks, without model calls or microphone capture.
const { app, BrowserWindow, dialog } = require('electron')
const assert = require('node:assert/strict')
const { execFile } = require('node:child_process')
const { promisify } = require('node:util')
const fs = require('node:fs')
const path = require('node:path')
const { pathToFileURL } = require('node:url')
const execute = promisify(execFile)
const root = path.resolve(__dirname, '../..')
const output = path.join(root, 'build/windows-wallpaper')
const helper = path.join(output, 'host/Amadeus.Wallpaper.exe')
const inspect = async () => JSON.parse((await execute(helper, ['inspect'], { windowsHide: true })).stdout.trim())
process.env.NODE_ENV = 'production'
// This probe explicitly selects wallpaper regardless of the saved GUI mode.
process.env.AMADEUS_WALLPAPER = '1'
process.env.AMADEUS_ELECTRON_USER_DATA_DIR = path.join(output, 'electron-probe-data')
process.env.AMADEUS_ELECTRON_CACHE_DIR = path.join(output, 'electron-probe-cache')
process.env.WAKE_ENABLED = '0'
process.env.AMADEUS_E2E_NO_TTS = '1'
process.env.TTS_BACKEND = 'disabled'
process.env.WAKE_AUTO_START_WITH_WALLPAPER = '0'
process.env.VTS_ENABLED = '0'
process.env.VTS_HEARTBEAT_ENABLED = '0'
process.env.VTS_RECONNECT_ENABLED = '0'

const faultProbe = process.argv.includes('--host-exit')
let faultInjected = false
let reportedHostExit = false
if (faultProbe) dialog.showErrorBox = () => { reportedHostExit = true }
let baseline
let failed = false
app.on('will-quit', event => {
  if (!baseline) return
  event.preventDefault()
  const before = baseline
  baseline = null
  void (async () => {
    // Killing the helper deliberately removes its restoration worker. Use its
    // documented recovery command to restore the user's desktop in cleanup.
    if (faultInjected) await execute(helper, ['recover'], { windowsHide: true })
    return inspect()
  })().then(after => {
    const passed = !failed && JSON.stringify(before.wallpapers) === JSON.stringify(after.wallpapers)
      && JSON.stringify(before.options) === JSON.stringify(after.options) && !after.recoveryPending
    fs.writeFileSync(path.join(output, 'electron-experiment.json'), JSON.stringify({ passed, before, after }, null, 2))
    console.log(passed ? 'PASS Electron startup, scene and quit restoration' : 'FAIL Electron lifecycle')
    app.exit(passed ? 0 : 1)
  }).catch(error => { console.error(error); app.exit(1) })
})

void (async () => {
  baseline = await inspect()
  if (baseline.recoveryPending) throw new Error('Another wallpaper session is active')
  await import(pathToFileURL(path.join(root, 'electron/dist/main/index.js')).href)
  const deadline = Date.now() + 120000
  while (Date.now() < deadline) {
    const current = await inspect()
    const slices = BrowserWindow.getAllWindows().filter(window =>
      window.webContents.getURL().includes('/render/web/electron_slice.html') && !window.webContents.isLoadingMainFrame())
    if (current.wallpapers.some(item => path.basename(item.Path) === 'amadeus-managed') && slices.length > 0) {
      await new Promise(resolve => setTimeout(resolve, 10000))
      try {
        await execute(helper, ['screenshot', path.join(output, 'electron-mounted-scene.jpg')], { windowsHide: true })
      } catch (error) { console.warn('Wallpaper screenshot unavailable:', error.message) }
      const image = await slices[0].webContents.capturePage()
      fs.writeFileSync(path.join(output, 'electron-slice.png'), image.toPNG())
      console.log('MOUNTED Electron: real Slice loaded (an empty interaction region stays hidden)')
      const consoleWindow = BrowserWindow.getAllWindows().find(window => window.webContents.getURL().includes('mainWindow=1'))
      assert(consoleWindow, 'main window missing')
      // Consume Start-Process's first-show suppression before testing the gear.
      consoleWindow.showInactive()
      await new Promise(resolve => setTimeout(resolve, 100))
      consoleWindow.hide()
      await slices[0].webContents.executeJavaScript(`(() => {
        const composer = document.querySelector('#wallpaper-keyboard-composer');
        if (composer.hidden) document.querySelector('.wallpaper-keyboard-composer-toggle').click();
        document.querySelector('.composer-console').click();
        return true;
      })()`)
      const consoleDeadline = Date.now() + 3000
      while (!consoleWindow.isVisible() && Date.now() < consoleDeadline) await new Promise(resolve => setTimeout(resolve, 50))
      assert(consoleWindow.isVisible(), 'composer gear did not open the control panel')
      assert((await inspect()).wallpapers.some(item => path.basename(item.Path) === 'amadeus-managed'), 'opening the control panel stopped wallpaper')
      consoleWindow.close()
      assert(!consoleWindow.isDestroyed() && !consoleWindow.isVisible(), 'closing the control panel must return to wallpaper')
      console.log('PASS composer gear opens control panel; closing it preserves wallpaper')

      if (faultProbe) {
        const main = BrowserWindow.getAllWindows().find(window => window.webContents.getURL().includes('mainWindow=1'))
        assert(main, 'main renderer missing')
        // Start-Process -WindowStyle Hidden suppresses the first native show.
        // Consume that harness-only launch flag before testing failure reveal.
        main.showInactive()
        await new Promise(resolve => setTimeout(resolve, 100))
        main.hide()
        const connection = await main.webContents.executeJavaScript('window.amadeus.getBackendConnection()')
        const backend = new URL(connection.url.replace(/^ws/, 'http')).origin
        const token = connection.protocols.find(value => value.startsWith('amadeus.auth.')).slice('amadeus.auth.'.length)
        assert.equal((await fetch(backend + '/wallpaper/stop', { method: 'POST' })).status, 401)
        assert.equal((await fetch(backend + '/wallpaper/stop', { method: 'POST', headers: {
          'X-Amadeus-Token': token, Origin: 'https://untrusted.invalid',
        } })).status, 403)
        const children = JSON.parse((await execute('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
          `@(Get-CimInstance Win32_Process -Filter "Name = 'Amadeus.Wallpaper.exe'" | Where-Object ParentProcessId -eq ${process.pid} | Select-Object -ExpandProperty ProcessId) | ConvertTo-Json -Compress`,
        ], { windowsHide: true })).stdout)
        const ids = Array.isArray(children) ? children : [children]
        assert.equal(ids.length, 1, 'expected exactly one helper owned by this probe')
        faultInjected = true
        process.kill(ids[0])
        const cleanupDeadline = Date.now() + 12000
        let stopped = false
        while (Date.now() < cleanupDeadline) {
          const info = await (await fetch(backend + '/wallpaper/bridge-info')).json()
          if (info.running === false && reportedHostExit) { stopped = true; break }
          await new Promise(resolve => setTimeout(resolve, 100))
        }
        assert(stopped, 'helper death did not stop backend wallpaper ownership')
        assert(main.isVisible(), 'failure did not reveal main window')
        assert(slices[0].isDestroyed(), 'stale Slice survived helper death')
        assert(reportedHostExit, 'helper failure was not reported')
        console.log('PASS helper death: backend wallpaper stopped, Slice closed, window visible; stop endpoint authenticates and checks Origin')
      }

      app.quit()
      return
    }
    await new Promise(resolve => setTimeout(resolve, 500))
  }
  throw new Error('Electron did not mount a wallpaper and load its Slice within 120 seconds')
})().catch(error => { failed = true; console.error(error); app.quit() })
