import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import test from 'node:test'
import ts from 'typescript'

// Exercise the real persistence boundary without starting an Electron process.
const require = createRequire(import.meta.url)
const source = fs.readFileSync(new URL('../src/main/desktopSettings.ts', import.meta.url), 'utf8')
const code = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText
const exports = {}
vm.runInNewContext(code, {
  exports, Buffer, console, URL,
  require: name => name === 'electron'
    ? { safeStorage: { isEncryptionAvailable: () => false } }
    : require(name),
})

test('character RAG settings persist and reach the next backend launch', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-rag-settings-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const file = path.join(directory, 'settings.json')
  const dotenv = path.join(directory, '.env')
  const store = new exports.DesktopSettingsStore(file, dotenv)
  store.update({}, { values: {
    RAG_ENABLED: true, RAG_INDEX_DIR: '.amadeus/character-rag',
    RAG_TOP_K: '3', RAG_MAX_DISTANCE: '0.33',
  } })
  const restarted = new exports.DesktopSettingsStore(file, dotenv)
  const environment = restarted.backendEnvironment({})
  assert.equal(environment.RAG_ENABLED, 'true')
  assert.equal(environment.RAG_INDEX_DIR, '.amadeus/character-rag')
  assert.equal(environment.RAG_TOP_K, '3')
  assert.equal(environment.RAG_MAX_DISTANCE, '0.33')
  assert.equal(restarted.backendEnvironment({ RAG_ENABLED: 'false' }).RAG_ENABLED, undefined)
  assert.throws(() => store.update({}, { values: { RAG_MAX_DISTANCE: 'NaN' } }))
  assert.throws(() => store.update({}, { values: { RAG_TOP_K: '1.5' } }))
  store.update({}, { values: { RAG_ENABLED: false } })
  assert.equal(store.backendEnvironment({}).RAG_ENABLED, 'false')
})

test('the retired local-only flag cannot enable remote retrieval', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-rag-legacy-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const file = path.join(directory, 'settings.json')
  fs.writeFileSync(file, JSON.stringify({ version: 2, values: { RAG_ENABLED_FOR_LOCAL: 'true' } }))
  const store = new exports.DesktopSettingsStore(file, path.join(directory, '.env'))
  assert.equal(store.backendEnvironment({}).RAG_ENABLED, undefined)
  assert.throws(() => store.update({}, { values: { RAG_ENABLED_FOR_LOCAL: true } }))
})
