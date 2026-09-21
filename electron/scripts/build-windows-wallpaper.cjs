const { execFileSync } = require('node:child_process')
const path = require('node:path')

// electron-builder hook: other targets must not build or install Windows tools.
module.exports = async context => {
  if (context.electronPlatformName !== 'win32') return
  execFileSync('powershell.exe', [
    '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
    path.resolve(__dirname, '../../scripts/setup_windows_wallpaper.ps1'), '-BuildOnly', '-Rebuild',
  ], { stdio: 'inherit', windowsHide: true })
}
