import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createSourceRequire } from './helpers/loadTypeScript.mjs'
import ts from 'typescript'

const require = createSourceRequire(new URL('../src/main/desktopSettings.ts', import.meta.url))
const source = fs.readFileSync(new URL('../src/main/desktopSettings.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText
const exports = {}
new Function('require', 'exports', compiled)(name => name === 'electron'
  ? { safeStorage: {
      isEncryptionAvailable: () => true,
      encryptString: value => Buffer.from(`encrypted:${value}`),
      decryptString: buffer => buffer.toString().slice(10),
    } }
  : require(name), exports)

function savedStore(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-settings-durability-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const file = path.join(root, 'settings.json')
  const store = new exports.DesktopSettingsStore(file, path.join(root, '.env'))
  store.update({}, { values: { WORK_EXECUTION_PROVIDER: 'pi' }, secrets: { DEEPSEEK_API_KEY: 'sk-test' } })
  return { root, file, store }
}

// A power loss during a write can leave a full-length file of NUL bytes.
function zeroFill(file) {
  fs.writeFileSync(file, Buffer.alloc(fs.statSync(file).size))
}

test('a damaged settings file is restored from its last save instead of reset by the next launch', t => {
  const { file, store } = savedStore(t)
  zeroFill(file)

  store.markApplied({}, {})

  const restored = JSON.parse(fs.readFileSync(file, 'utf8'))
  assert.equal(restored.values.WORK_EXECUTION_PROVIDER, 'pi')
  assert.ok(restored.encryptedSecrets.DEEPSEEK_API_KEY)
  assert.equal(store.backendEnvironment({}).DEEPSEEK_API_KEY, 'sk-test')
})

test('unreadable settings without a saved copy are set aside, not overwritten', t => {
  const { root, file, store } = savedStore(t)
  fs.rmSync(`${file}.bak`)
  zeroFill(file)
  const damaged = fs.readFileSync(file)

  store.markApplied({}, {})

  const setAside = fs.readdirSync(root).filter(name => name.startsWith('settings.json.corrupt-'))
  assert.equal(setAside.length, 1)
  assert.deepEqual(fs.readFileSync(path.join(root, setAside[0])), damaged)
  assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')).values, {})
})

test('settings that cannot be opened are never replaced with defaults', t => {
  const { file, store } = savedStore(t)
  fs.rmSync(`${file}.bak`)
  fs.rmSync(file)
  fs.mkdirSync(file)

  assert.throws(() => store.markApplied({}, {}))
  assert.ok(fs.statSync(file).isDirectory())
})

test('deleting the settings file still resets to defaults', t => {
  const { file, store } = savedStore(t)
  fs.rmSync(file)

  assert.equal(store.backendEnvironment({}).WORK_EXECUTION_PROVIDER, undefined)
  store.markApplied({}, {})

  assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')).values, {})
  assert.deepEqual(JSON.parse(fs.readFileSync(`${file}.bak`, 'utf8')).values, {})
})

test('a first launch without settings still starts from defaults', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-settings-fresh-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const store = new exports.DesktopSettingsStore(path.join(root, 'settings.json'), path.join(root, '.env'))

  assert.deepEqual(store.pendingRevisionSnapshot(), {})
  store.markApplied({}, {})

  assert.deepEqual(fs.readdirSync(root).sort(), ['settings.json', 'settings.json.bak'])
})
