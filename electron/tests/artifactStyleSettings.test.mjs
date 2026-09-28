import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import ts from 'typescript'

// Run the real store with only Electron's OS-encryption boundary substituted.
const require = createRequire(import.meta.url)
const source = fs.readFileSync(new URL('../src/main/desktopSettings.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText
const exports = {}
new Function('require', 'exports', compiled)(name => name === 'electron'
  ? { safeStorage: { isEncryptionAvailable: () => false } }
  : require(name), exports)

test('artifact appearance setting persists both values and reaches the next backend', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-style-settings-'))
  const file = path.join(root, 'settings.json')
  const dotenv = path.join(root, '.env')
  const key = 'AUIP_ARTIFACT_STYLE_ENABLED'
  try {
    const store = new exports.DesktopSettingsStore(file, dotenv)
    for (const value of [false, true]) {
      store.update({}, { values: { [key]: value } })
      const reloaded = new exports.DesktopSettingsStore(file, dotenv)
      assert.equal(reloaded.backendEnvironment({})[key], String(value))
    }
    assert.throws(() => store.update({}, { values: { [key]: 'sometimes' } }))
    assert.equal(new exports.DesktopSettingsStore(file, dotenv).backendEnvironment({})[key], 'true')
  } finally {
    assert.equal(path.dirname(path.resolve(root)), path.resolve(os.tmpdir()))
    fs.rmSync(root, { recursive: true, force: true })
  }
})
