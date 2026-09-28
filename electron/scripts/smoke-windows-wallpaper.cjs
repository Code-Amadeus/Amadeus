// Native Electron/Windows smoke for the actual unpacked package. This checks
// startup choices, the wallpaper tray and packaged resources, not a fake Lively desktop.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { pathToFileURL } = require('node:url')
const { app, Tray } = require('electron')
const { execFileSync } = require('node:child_process')

app.whenReady().then(async () => {
  const resources = path.resolve(process.argv[2] || 'build/win-unpacked/resources')
  const main = path.join(resources, 'app.asar', 'dist', 'main')
  const { isWallpaperStartup } = await import(pathToFileURL(path.join(main, 'startupMode.js')).href)
  const { WindowsWallpaperTray } = await import(pathToFileURL(path.join(main, 'windowsWallpaperTray.js')).href)
  const { managesWindowsWallpaper } = await import(pathToFileURL(path.join(main, 'windowsWallpaper.js')).href)
  assert.equal(isWallpaperStartup(['Amadeus.exe'], {}, 'win32'), false, 'Fresh installs and upgrades without a saved choice open the control panel')
  assert.equal(isWallpaperStartup(['Amadeus.exe'], {}, 'win32', 'wallpaper'), true)
  assert.equal(isWallpaperStartup(['Amadeus.exe', '--wallpaper'], {}, 'win32'), true)
  const external = { AMADEUS_WALLPAPER_HOST: 'external' }
  assert.equal(isWallpaperStartup(['Amadeus.exe'], external, 'win32', 'wallpaper'), true)
  assert.equal(managesWindowsWallpaper('win32', external), false, 'Wallpaper startup must not override external host ownership')
  const icon = path.join(resources, 'assets/icons/app/app_icon.ico')
  assert.ok(fs.existsSync(icon), 'Packaged Windows tray icon is missing')
  const tray = new WindowsWallpaperTray(icon, () => {}, () => {})
  try {
    assert.equal(tray.hasIcon, true)
    tray.setStatus('preparing')
    tray.setStatus('mounting')
    tray.setStatus('active')
    tray.setStatus('restoring')
    tray.setStatus('idle')
  } finally { tray.destroy() }

  const missing = path.join(resources, 'missing-icon.ico')
  assert.throws(() => new Tray(missing), /image|path/i, 'Regression premise: a raw missing icon path must fail')
  const fallback = new WindowsWallpaperTray(missing, () => {}, () => {})
  try { assert.equal(fallback.hasIcon, false) }
  finally { fallback.destroy() }

  const helper = path.join(resources, 'windows-wallpaper', 'Amadeus.Wallpaper.exe')
  assert.ok(fs.existsSync(helper), 'beforePack did not publish the helper')
  assert.ok(fs.existsSync(path.join(resources, 'windows-wallpaper', 'setup_windows_wallpaper.ps1')))
  // Read-only. Never installs Lively, mounts a scene or modifies the CI desktop.
  const inspected = JSON.parse(execFileSync(helper, ['inspect'], { encoding: 'utf8', windowsHide: true }).trim())
  assert.equal(typeof inspected.running, 'boolean')
  console.log('PASS packaged Windows startup choices and external host ownership: native tray, missing-icon recovery, setup resources and helper runtime')
  app.quit()
}).catch(error => { console.error(error); app.exit(1) })
