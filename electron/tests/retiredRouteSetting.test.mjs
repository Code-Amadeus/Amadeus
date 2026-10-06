import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import test from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
function compile(relativePath, dependencies = require) {
  const source = fs.readFileSync(new URL(relativePath, import.meta.url), 'utf8')
  const code = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
    esModuleInterop: true, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText
  const exports = {}
  new Function('require', 'exports', code)(dependencies, exports)
  return exports
}
function settingsStore(file, dotenv, fileSystem = fs) {
  const { DesktopSettingsStore } = compile('../src/main/desktopSettings.ts', name => {
    if (name === 'electron') return { safeStorage: { isEncryptionAvailable: () => false } }
    if (name === 'fs') return fileSystem
    return require(name)
  })
  return new DesktopSettingsStore(file, dotenv)
}
const migration = compile('../src/renderer/components/RetiredRouteSetting.tsx', name => {
  if (name === '../i18n') return { useI18n: () => ({ t: value => value }) }
  return require(name)
})
const key = migration.RETIRED_ROUTE_KEY
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-retired-setting-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const file = path.join(root, 'settings.json')
  const dotenv = path.join(root, '.env')
  fs.writeFileSync(file, JSON.stringify({ version: 2, values: { [key]: 'false', LLM_PROVIDER: 'deepseek' },
    encryptedSecrets: {}, pendingRevisions: { [key]: 1 }, nextRevision: 2 }))
  return { file, dotenv, store: settingsStore(file, dotenv) }
}
function updateThrough(store, environment = {}) {
  return async request => {
    try { return { ok: true, settings: store.update(environment, request) } }
    catch (error) { return { ok: false, error: error.message } }
  }
}

test('stored false survives loads, unrelated saves, restart, and environment override', t => {
  const { file, dotenv, store } = fixture(t)
  const environment = { [key]: 'true' }
  const snapshot = store.update(environment, { values: { LLM_PROVIDER: 'openai' } })
  assert.equal(snapshot.sources[key], 'environment')
  assert.equal(snapshot.values[key], 'false')
  assert.equal(snapshot.retired_settings.find(row => row.source === 'user').value, false)
  assert.equal(store.backendEnvironment(environment)[key], undefined)
  const reloaded = settingsStore(file, dotenv).snapshot(environment)
  assert.equal(migration.retiredRouteMigration(reloaded, []).storedFalse, true)
  assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).values[key], 'false')
})

test('only explicit confirmation durably deletes the key, including under an environment lock', async t => {
  const { file, dotenv, store } = fixture(t)
  const environment = { [key]: 'true' }
  const saved = await migration.removeStoredRetiredRouteSetting(updateThrough(store, environment))
  assert.equal(migration.retiredRouteMigration(saved, []).storedFalse, false)
  assert.equal(saved.values[key], undefined)
  assert.equal(saved.pendingRevisions[key], undefined)
  assert.equal(settingsStore(file, dotenv).snapshot(environment).values[key], undefined)
  assert.equal(settingsStore(file, dotenv).backendEnvironment({})[key], undefined)
  assert.throws(() => store.update({}, { values: { [key]: true } }), /retired and read-only/)
  assert.throws(() => store.update({}, { values: { [key]: false } }), /retired and read-only/)
})

for (const target of ['primary', 'backup']) {
  test(`a ${target} persistence failure keeps the confirmation pending after restart`, async t => {
    const { file, dotenv, store } = fixture(t)
    const initial = store.snapshot({})
    const broken = settingsStore(file, dotenv, { ...fs, renameSync: (from, to) => {
      if (to === (target === 'primary' ? file : `${file}.bak`)) throw new Error('synthetic persistence failure')
      return fs.renameSync(from, to)
    } })
    await assert.rejects(() => migration.removeStoredRetiredRouteSetting(updateThrough(broken)), /synthetic persistence failure/)
    assert.equal(migration.retiredRouteMigration(initial, []).storedFalse, true)
    assert.equal(migration.retiredRouteMigration(settingsStore(file, dotenv).snapshot({}), []).storedFalse, true)
  })
}

test('environment and dotenv false are read-only explanations and preserve precedence', t => {
  const { dotenv, store } = fixture(t)
  fs.writeFileSync(dotenv, `${key}=false\n`)
  store.update({}, { values: { [key]: null } })
  let snapshot = store.snapshot({})
  assert.equal(snapshot.sources[key], 'dotenv')
  let state = migration.retiredRouteMigration(snapshot, [])
  assert.equal(state.storedFalse, false)
  assert.deepEqual(state.externalFalse.map(row => row.source), ['dotenv'])
  let html = renderToStaticMarkup(React.createElement(migration.default, { migration: state, saving: false, onConfirm: async () => {} }))
  assert.match(html, /read-only/)
  assert.ok(!html.includes('<button'))
  snapshot = store.snapshot({ [key]: 'true' })
  assert.equal(snapshot.sources[key], 'environment')
  assert.equal(migration.retiredRouteMigration(snapshot, []).externalFalse.length, 0)
  snapshot = store.snapshot({ [key]: 'false' })
  assert.deepEqual(migration.retiredRouteMigration(snapshot, []).externalFalse.map(row => row.source), ['environment'])
  assert.equal(fs.readFileSync(dotenv, 'utf8'), `${key}=false\n`)
})

test('offline stored false explains the original runtime semantics and requires confirmation', t => {
  const { store } = fixture(t)
  const state = migration.retiredRouteMigration(store.snapshot({}), undefined)
  const html = renderToStaticMarkup(React.createElement(migration.default, { migration: state, saving: false, onConfirm: async () => {} }))
  assert.match(html, /selected the original Chat runtime/)
  assert.match(html, /never prohibited Work execution/)
  assert.match(html, /I understand; remove the saved setting/)
  assert.match(html, /<button/)
})

test('a success response that retains the retired key cannot close the notice', async () => {
  await assert.rejects(() => migration.removeStoredRetiredRouteSetting(async () => ({ ok: true,
    settings: { values: { [key]: 'false' } } })), /not confirmed/)
})

test('offline dotenv interpolation does not invent a false migration fact', t => {
  const { dotenv, store } = fixture(t)
  store.update({}, { values: { [key]: null } })
  fs.writeFileSync(dotenv, key + '=${OTHER_STARTUP_SETTING}\n')
  assert.equal(store.snapshot({}).sources[key], 'dotenv')
  assert.deepEqual(store.snapshot({}).retired_settings, [])
})
