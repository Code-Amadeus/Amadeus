// Run with Electron after `npm run build`. Exercises the real main-process
// startup and before-quit hooks, without model calls or microphone capture.
const { app, BrowserWindow } = require('electron')
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
process.env.AMADEUS_ELECTRON_USER_DATA_DIR = path.join(output, 'electron-probe-data')
process.env.AMADEUS_ELECTRON_CACHE_DIR = path.join(output, 'electron-probe-cache')
process.env.WAKE_ENABLED = '0'
process.env.WAKE_AUTO_START_WITH_WALLPAPER = '0'
process.env.VTS_ENABLED = '0'
process.env.VTS_HEARTBEAT_ENABLED = '0'
process.env.VTS_RECONNECT_ENABLED = '0'

let baseline
let failed = false
app.on('will-quit', event => {
  if (!baseline) return
  event.preventDefault()
  const before = baseline
  baseline = null
  void inspect().then(after => {
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
      app.quit()
      return
    }
    await new Promise(resolve => setTimeout(resolve, 500))
  }
  throw new Error('Electron did not mount a wallpaper and load its Slice within 120 seconds')
})().catch(error => { failed = true; console.error(error); app.quit() })
