// Actual Electron/libuv parent; no BrowserWindow, backend or real Lively.
const { app } = require('electron')
const fs = require('node:fs')
const path = require('node:path')
const childProcess = require('node:child_process')
const { pathToFileURL } = require('node:url')
const [root, python, mode] = process.argv.slice(2)
app.setPath('userData', path.join(root, 'profile'))
app.disableHardwareAcceleration()
void app.whenReady().then(async () => {
  const { loadTypeScript } = await import(pathToFileURL(path.resolve(__dirname, '../helpers/loadTypeScript.mjs')).href)
  const mocks = mode === 'attached-control' ? {
    'node:child_process': { ...childProcess, spawn: (command, args, options) =>
      childProcess.spawn(command, args, { ...options, detached: false }) },
  } : {}
  const { windowsWallpaperDependencies } = loadTypeScript(
    pathToFileURL(path.resolve(__dirname, '../../src/main/windowsWallpaper.ts')), mocks)
  const helper = windowsWallpaperDependencies(root, '', false, python)
    .launch('http://127.0.0.1:1/wallpaper/lively/index.html')
  fs.writeFileSync(path.join(root, 'parent.json'), JSON.stringify({ parent: process.pid, launcher: helper.process.pid }))
  await helper.ready
  console.log(JSON.stringify({ ready: true }))
  if (mode === 'normal') {
    helper.process.stdin.end()
    await helper.done
    app.exit(0)
  }
}).catch(error => { console.error(error); app.exit(1) })
