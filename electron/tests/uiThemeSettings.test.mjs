import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = fs.readFileSync(new URL('../src/main/desktopSettings.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText
const exports = {}
new Function('require', 'exports', compiled)(name => name === 'electron'
  ? { safeStorage: { isEncryptionAvailable: () => false } }
  : require(name), exports)

test('Electron theme persists as frontend-only state without requesting a backend restart', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-ui-theme-'))
  try {
    const file = path.join(root, 'settings.json')
    const dotenv = path.join(root, '.env')
    const store = new exports.DesktopSettingsStore(file, dotenv)
    const snapshot = store.update({}, { values: { AMADEUS_UI_THEME: 'wallpaper-slice' } })
    assert.equal(snapshot.values.AMADEUS_UI_THEME, 'wallpaper-slice')
    assert.equal(snapshot.restartRequired, false)
    assert.equal(store.backendEnvironment({}).AMADEUS_UI_THEME, undefined)
    assert.equal(new exports.DesktopSettingsStore(file, dotenv).snapshot({}).values.AMADEUS_UI_THEME, 'wallpaper-slice')
    assert.throws(() => store.update({}, { values: { AMADEUS_UI_THEME: 'neon' } }), /Invalid value/)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})


test('Windows startup mode persists without changing or restarting the backend', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-startup-mode-'))
  try {
    const file = path.join(root, 'settings.json')
    const store = new exports.DesktopSettingsStore(file, path.join(root, '.env'))
    for (const mode of ['window', 'wallpaper']) {
      const saved = store.update({}, { values: { AMADEUS_WINDOWS_STARTUP_MODE: mode } })
      assert.equal(saved.platform, process.platform)
      assert.equal(saved.restartRequired, false)
      assert.equal(store.backendEnvironment({}).AMADEUS_WINDOWS_STARTUP_MODE, undefined)
      assert.equal(new exports.DesktopSettingsStore(file, '').snapshot({}).values.AMADEUS_WINDOWS_STARTUP_MODE, mode)
    }
    const overridden = store.snapshot({ AMADEUS_WINDOWS_STARTUP_MODE: 'window' })
    assert.equal(overridden.values.AMADEUS_WINDOWS_STARTUP_MODE, 'window')
    assert.equal(overridden.locked.AMADEUS_WINDOWS_STARTUP_MODE, true)
    assert.throws(() => store.update({}, { values: { AMADEUS_WINDOWS_STARTUP_MODE: 'invalid' } }), /Invalid value/)
  } finally { fs.rmSync(root, { recursive: true, force: true }) }
})
