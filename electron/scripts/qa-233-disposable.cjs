// QA branch only. The real application owns startup/quit. The external
// supervisor owns only this fresh hosted runner's synthetic desktop.
const { app, BrowserWindow, dialog } = require('electron')
const fs = require('node:fs')
const path = require('node:path')
const { pathToFileURL } = require('node:url')
if (process.env.RUNNER_ENVIRONMENT !== 'github-hosted') throw Error('Disposable VM required')
const dir = process.env.QA233_DIR
process.env.NODE_ENV = 'production'
process.env.AMADEUS_WALLPAPER = '1'
process.env.AMADEUS_ELECTRON_USER_DATA_DIR = path.join(dir, 'electron-data')
process.env.AMADEUS_ELECTRON_CACHE_DIR = path.join(dir, 'electron-cache')
// Record errors instead of leaving the isolated session waiting for a click.
dialog.showErrorBox = (title, body) => fs.appendFileSync(path.join(dir, 'errors'), title + ': ' + body + '\n')
;(async () => {
  await import(pathToFileURL(path.resolve(__dirname, '../dist/main/index.js')).href)
  const poll = setInterval(() => {
    const slice = BrowserWindow.getAllWindows().some(w => !w.isDestroyed() &&
      !w.webContents.isLoadingMainFrame() && w.webContents.getURL().includes('/render/web/electron_slice.html'))
    if (slice && !fs.existsSync(path.join(dir, 'slice-loaded'))) fs.writeFileSync(path.join(dir, 'slice-loaded'), '1')
    if (fs.existsSync(path.join(dir, 'quit'))) { clearInterval(poll); app.quit() }
  }, 200)
})().catch(() => app.exit(2))
