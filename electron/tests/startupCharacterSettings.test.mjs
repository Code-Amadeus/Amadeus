import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createSourceRequire } from './helpers/loadTypeScript.mjs'
import test from 'node:test'
import ts from 'typescript'

const require = createSourceRequire(new URL('../src/main/desktopSettings.ts', import.meta.url))
const source = fs.readFileSync(new URL('../src/main/desktopSettings.ts', import.meta.url), 'utf8')
const code = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText
const exports = {}
new Function('require', 'exports', code)(name => name === 'electron'
  ? { safeStorage: { isEncryptionAvailable: () => false } } : require(name), exports)
const key = 'AMADEUS_CHARACTER_ID'

function makeStore(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-startup-character-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const file = path.join(root, 'settings.json')
  const dotenv = path.join(root, '.env')
  return { store: new exports.DesktopSettingsStore(file, dotenv), file, dotenv }
}

test('saved character identity remains pending until a new backend launch applies it', t => {
  const { store, file, dotenv } = makeStore(t)
  fs.writeFileSync(dotenv, `${key}=kurisu\n`)
  const firstLaunch = { ...store.backendEnvironment({}, { [key]: 'kurisu' }) }
  const saved = store.update({}, { values: { [key]: 'test-char' } })
  assert.equal(firstLaunch[key], undefined)
  assert.ok(saved.pendingRevisions[key] > 0)
  const restarted = new exports.DesktopSettingsStore(file, dotenv)
  const nextLaunch = restarted.backendEnvironment({})
  assert.equal(nextLaunch[key], 'test-char')
  restarted.markApplied({}, saved.pendingRevisions)
  assert.equal(restarted.snapshot({}).pendingRevisions[key], undefined)
  assert.equal(restarted.snapshot({}).values[key], 'test-char')
})

test('parent environment owns startup character and invalid identities cannot persist', t => {
  const { store } = makeStore(t)
  store.update({}, { values: { [key]: 'testchar' } })
  const environment = { [key]: 'kurisu' }
  assert.equal({ ...environment, ...store.backendEnvironment(environment) }[key], 'kurisu')
  assert.equal(store.snapshot(environment).locked[key], true)
  assert.equal(store.backendEnvironment(environment)[key], undefined)
  assert.throws(() => store.update(environment, { values: { [key]: 'other' } }), /locked/)
  for (const value of ['Kurisu', '../kurisu', 'bad role', 'x'.repeat(65)]) {
    assert.throws(() => store.update({}, { values: { [key]: value } }), /identifier/)
  }
  assert.equal(store.snapshot({}).values[key], 'testchar')
  store.update({}, { values: { [key]: null } })
  assert.equal(store.backendEnvironment({})[key], undefined)
})
