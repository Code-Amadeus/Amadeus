import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawn } from 'node:child_process'
import { createSourceRequire } from './helpers/loadTypeScript.mjs'
import test from 'node:test'
import ts from 'typescript'

const require = createSourceRequire(new URL('../src/main/desktopSettings.ts', import.meta.url))
function compile(relativePath, dependencies = require) {
  const source = fs.readFileSync(new URL(relativePath, import.meta.url), 'utf8')
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true, jsx: ts.JsxEmit.ReactJSX },
  }).outputText
  const exports = {}
  new Function('require', 'exports', code)(dependencies, exports)
  return exports
}
const shared = compile('../src/shared/characterStartup.ts')
const startup = compile('../src/main/backendStartup.ts', name => name === '../shared/characterStartup.js' ? shared : require(name))
const management = compile('../src/renderer/components/characterManagement.ts', name => name === '../../shared/characterStartup' ? shared : require(name))
const { DesktopSettingsStore } = compile('../src/main/desktopSettings.ts', name => name === 'electron'
  ? { safeStorage: { isEncryptionAvailable: () => false } } : require(name))
const selection = { characterId: 'missing-role', source: 'user', locked: false }
const active = { character_id: 'one', name: 'Original', display_name: 'Original', short_name: 'Original', ui_name: 'Original', accessible_name: 'Original' }

test('readiness observes an exited child even after its owner clears the slot', async () => {
  const launched = spawn(process.execPath, ['-e', 'process.exit(78)'], { stdio: 'ignore' })
  let owned = launched
  launched.once('exit', () => { owned = null })
  await assert.rejects(startup.waitForBackendReadiness(launched, async () => 'unavailable', 0, 3000), error => {
    assert.equal(owned, null)
    assert.equal(error.exitCode, 78)
    assert.equal(startup.backendStartupFailure(error.exitCode, error.message, selection).kind, 'character')
    return true
  })
})

test('only the reserved exit code identifies a character failure', () => {
  for (const code of [null, undefined, 0, 1, 77, 79]) {
    assert.equal(startup.backendStartupFailure(code, 'character missing or invalid', selection).kind, 'backend')
  }
  assert.equal(startup.backendStartupFailure(78, 'unrelated words', selection).kind, 'character')
})

test('recovery persists literal Kurisu over an invalid dotenv selection, then stops and starts', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'am-m1-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const dotenv = path.join(directory, '.env')
  fs.writeFileSync(dotenv, 'AMADEUS_CHARACTER_ID=missing-role\n')
  const file = path.join(directory, 'settings.json')
  const store = new DesktopSettingsStore(file, dotenv)
  const actions = []
  await startup.recoverCharacterStartup(startup.backendStartupFailure(78, 'exit', { ...selection, source: 'dotenv' }), {
    save: update => { actions.push('save'); return store.update({}, update) },
    stop: async () => { actions.push('stop') },
    start: async () => { actions.push('start'); assert.equal(store.backendEnvironment({}).AMADEUS_CHARACTER_ID, 'kurisu') },
  })
  assert.deepEqual(actions, ['save', 'stop', 'start'])
  assert.equal(new DesktopSettingsStore(file, dotenv).snapshot({}).values.AMADEUS_CHARACTER_ID, 'kurisu')
  assert.equal(fs.readFileSync(dotenv, 'utf8'), 'AMADEUS_CHARACTER_ID=missing-role\n')
})

test('recovery does not stop on save failure or when environment authority locks the choice', async () => {
  const calls = []
  const actions = { save: () => { calls.push('save'); throw new Error('disk failed') },
    stop: async () => { calls.push('stop') }, start: async () => { calls.push('start') } }
  await assert.rejects(startup.recoverCharacterStartup(startup.backendStartupFailure(78, 'exit', selection), actions), /disk failed/)
  assert.deepEqual(calls, ['save'])
  calls.length = 0
  await assert.rejects(startup.recoverCharacterStartup(startup.backendStartupFailure(78, 'exit', { ...selection, source: 'environment', locked: true }), actions), /locked/)
  await assert.rejects(startup.recoverCharacterStartup(startup.backendStartupFailure(1, 'other failure', selection), actions), /No character/)
  assert.deepEqual(calls, [])
})

test('a failed restart leaves the saved selection and propagates the failure', async () => {
  let saved
  await assert.rejects(startup.recoverCharacterStartup(startup.backendStartupFailure(78, 'exit', selection), {
    save: update => { saved = update; return {} }, stop: async () => {}, start: async () => { throw new Error('restart failed') },
  }), /restart failed/)
  assert.deepEqual(saved, { values: { AMADEUS_CHARACTER_ID: 'kurisu' } })
})

test('fresh validation prevents a deleted, invalid or mismatched role from being saved', async () => {
  let saves = 0
  const save = async () => { saves += 1; return { ok: true } }
  for (const character of [{ character_id: 'one', valid: false, error: 'file was removed' }, { character_id: 'other', valid: true }]) {
    await assert.rejects(management.saveStartupCharacter('one', async () => ({ character }), save))
  }
  await assert.rejects(management.saveStartupCharacter('one', async () => { throw new Error('validation unavailable') }, save), /validation unavailable/)
  assert.equal(saves, 0)
})

test('new and edited roles save the visible persona without changing its names or requesting assets', async () => {
  const calls = []
  const send = async (method, params) => { calls.push({ method, params }); return { character: { character_id: 'host-generated-id' } } }
  const persona = 'あなたは牧瀬紅莉栖。\nKurisu の文章をそのまま見せる。'
  await management.saveCharacterDraft({ characterId: null, name: '  A role  ', persona }, send)
  await management.saveCharacterDraft({ characterId: 'host-generated-id', name: 'Changed name', persona }, send)
  assert.deepEqual(calls, [
    { method: 'character.create', params: { name: 'A role', persona } },
    { method: 'character.update', params: { character_id: 'host-generated-id', name: 'Changed name', persona } },
  ])
})

test('valid startup selection saves only the existing identity setting and preserves save errors', async () => {
  const calls = []
  const send = async (method, params) => { calls.push({ method, params }); return { character: { character_id: 'one', valid: true } } }
  const snapshot = { values: { AMADEUS_CHARACTER_ID: 'one' } }
  assert.equal(await management.saveStartupCharacter('one', send, async update => { calls.push(update); return { ok: true, settings: snapshot } }), snapshot)
  assert.deepEqual(calls, [{ method: 'character.validate', params: { character_id: 'one' } }, { values: { AMADEUS_CHARACTER_ID: 'one' } }])
  await assert.rejects(management.saveStartupCharacter('one', send, async () => ({ ok: false, error: 'locked by environment' })), /locked by environment/)
})

test('fresh catalog edits do not replace the pinned active name and parent authority ignores stored selection', () => {
  const response = { active, characters: [{ character_id: 'one', name: 'Edited', persona: 'changed', valid: true, builtin: false, editable: true }] }
  const catalog = management.characterCatalog(response)
  assert.equal(catalog.active.name, 'Original')
  assert.equal(catalog.characters[0].name, 'Edited')
  assert.equal(management.nextStartupCharacter({ values: { AMADEUS_CHARACTER_ID: 'saved-other' }, sources: { AMADEUS_CHARACTER_ID: 'environment' }, locked: { AMADEUS_CHARACTER_ID: true } }, active).characterId, 'one')
  assert.equal(management.nextStartupCharacter({ sources: { AMADEUS_CHARACTER_ID: 'dotenv' } }, active).characterId, null)
})

const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const { StartupFailureCard } = compile('../src/renderer/components/BackendStartupRecovery.tsx', name => {
  if (name === '../../shared/characterStartup') return shared
  if (name === '../i18n') return { useI18n: () => ({ t: (value, variables = {}) => value.replace(/\{(\w+)\}/g, (_, key) => variables[key] ?? `{${key}}`) }) }
  if (name === './SettingsPrimitives') return { CardShell: ({ children }) => React.createElement('div', null, children) }
  return require(name)
})

const { default: CharacterManagementSettings, CharacterRoleLabel } = compile('../src/renderer/components/CharacterManagementSettings.tsx', name => {
  if (name === './characterManagement') return management
  if (name === '../../shared/characterStartup') return shared
  if (name === '../i18n') return { useI18n: () => ({ t: (value, variables = {}) => value.replace(/\{(\w+)\}/g, (_, key) => variables[key] ?? `{${key}}`) }) }
  if (name === './SettingsPrimitives') return { CardShell: ({ children }) => React.createElement('div', null, children) }
  return require(name)
})

test('duplicate role names remain distinguishable by their full stable IDs in management labels', () => {
  const roles = ['role-0123456789', 'role-9876543210'].map(character_id => ({
    character_id, name: 'Same name', persona: '', builtin: false, valid: true, editable: true,
  }))
  const html = renderToStaticMarkup(React.createElement('div', null, roles.map(character => React.createElement(CharacterRoleLabel, {
    key: character.character_id, character, active: false, nextStart: false,
  }))))
  assert.equal([...html.matchAll(/>Same name<\/div>/g)].length, 2)
  for (const role of roles) assert.ok(html.includes(`>${role.character_id}</code>`))
})

test('role editor leaves backend restart to its Settings owner', async () => {
  await management.saveCharacterDraft({ characterId: 'one', name: 'Edited', persona: 'updated' }, async () => ({ character: {} }))
  const html = renderToStaticMarkup(React.createElement(CharacterManagementSettings, {
    send: async () => ({}), connected: true, restarting: false,
    desktop: { values: { AMADEUS_CHARACTER_ID: 'one' }, sources: { AMADEUS_CHARACTER_ID: 'user' }, pendingRevisions: {} },
    kurisuPreview: {}, onSettingsChanged: () => {},
  }))
  assert.doesNotMatch(html, />Restart backend to apply<\/button>/)
})

test('offline character recovery is explicit and environment locks disable it', () => {
  const render = selection => renderToStaticMarkup(React.createElement(StartupFailureCard, {
    failure: startup.backendStartupFailure(78, 'exit', selection), recovering: false, error: '', notice: '', onRecover: () => {},
  }))
  const html = render(selection)
  assert.match(html, /missing or invalid/)
  assert.match(html, /Use built-in Kurisu and restart/)
  assert.doesNotMatch(html, /<button[^>]*disabled/)
  const locked = render({ ...selection, source: 'environment', locked: true })
  assert.match(locked, /<button[^>]*disabled/)
  assert.match(locked, /parent process environment/)
})

test('ordinary startup errors never offer character recovery', () => {
  const html = renderToStaticMarkup(React.createElement(StartupFailureCard, {
    failure: startup.backendStartupFailure(1, 'Provider initialization failed', selection), recovering: false, error: '', notice: '', onRecover: () => {},
  }))
  assert.match(html, /Provider initialization failed/)
  assert.doesNotMatch(html, /Kurisu|<button/)
})


test('persona boundaries trim user whitespace without rewriting interior text', async () => {
  const calls = []
  const send = async (method, params) => { calls.push(params); return {} }
  await management.saveCharacterDraft({ characterId: null, name: ' Mira ', persona: ' \nCalm.\n  Keep this indent. \n' }, send)
  await management.saveCharacterDraft({ characterId: 'one', name: 'Mira', persona: ' \n\t' }, send)
  assert.equal(calls[0].persona, 'Calm.\n  Keep this indent.')
  assert.equal(calls[1].persona, '')
})

test('editor counts match Host Unicode code points and consume advertised limits', () => {
  const limits = { name_max_chars: 128, persona_max_chars: 7900 }
  assert.deepEqual(management.characterCatalog({ characters: [], limits }).limits, limits)
  assert.equal(management.characterCatalog({ characters: [], limits: { name_max_chars: 128 } }).limits, null)
  assert.deepEqual(management.characterDraftLengths({ characterId: null, name: ` ${'😀'.repeat(128)} `, persona: `\n${'😀'.repeat(7900)}\n` }), { name: 128, persona: 7900 })
  assert.equal(management.characterDraftLengths({ name: 'Mira', persona: 'x'.repeat(7901) }).persona, 7901)
})

test('restart marker survives a fresh label render and is absent for unchanged, invalid or inactive roles', () => {
  const record = { character_id: 'one', name: 'Mira', persona: 'Edited', valid: true, builtin: false, editable: true, pending_restart: true }
  const render = (character, active = true) => renderToStaticMarkup(React.createElement(CharacterRoleLabel, { character, active, nextStart: true }))
  const catalog = management.characterCatalog({ characters: [record], active })
  for (let mount = 0; mount < 2; mount += 1) assert.match(render(catalog.characters[0]), /Modified\. Restart the backend to apply\./)
  assert.doesNotMatch(render({ ...record, pending_restart: false }), /Modified\./)
  assert.doesNotMatch(render({ ...record, valid: false }), /Modified\./)
  assert.doesNotMatch(render(record, false), /Modified\./)
})


const flush = () => new Promise(resolve => setImmediate(resolve))
function catalogHarness(send) {
  const source = fs.readFileSync(new URL('../src/renderer/components/CharacterManagementSettings.tsx', import.meta.url), 'utf8')
  const ast = ts.createSourceFile('settings.tsx', source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX)
  const component = ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'CharacterManagementSettings')
  const refresh = component.body.statements.filter(ts.isVariableStatement)
    .flatMap(node => [...node.declarationList.declarations]).find(node => node.name.getText(ast) === 'refresh')
  const effect = component.body.statements.find(node => ts.isExpressionStatement(node)
    && ts.isCallExpression(node.expression) && node.expression.expression.getText(ast) === 'useEffect').expression.arguments[0]
  const perform = component.body.statements.filter(ts.isVariableStatement)
    .flatMap(node => [...node.declarationList.declarations]).find(node => node.name.getText(ast) === 'perform')
  const state = { characters: [], active: null, limits: null, error: '', notice: '', busy: false }
  const bindings = { send, characterCatalog: management.characterCatalog, generation: { current: 0 },
    ...Object.fromEntries(Object.keys(state).map(key => [`set${key[0].toUpperCase()}${key.slice(1)}`, value => { state[key] = value }])) }
  const code = ts.transpileModule(`const refresh = ${refresh.initializer.arguments[0].getText(ast)};
    const perform = ${perform.initializer.getText(ast)};
    return { refresh, perform, effect: connected => (${effect.getText(ast)})() };`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText
  const handlers = new Function(...Object.keys(bindings), code)(...Object.values(bindings))
  let cleanup
  return { state, ...handlers, connect(value) { cleanup?.(); cleanup = handlers.effect(value) }, unmount() { cleanup?.() } }
}

test('role catalog reconnect clears an earlier failure and ignores old connection results', async () => {
  const pending = []
  const page = catalogHarness(() => new Promise((resolve, reject) => pending.push({ resolve, reject })))
  page.connect(true)
  pending[0].reject(new Error('old connection failed')); await flush()
  assert.equal(page.state.error, 'old connection failed')
  page.connect(false); page.connect(true)
  pending[1].resolve({ characters: [{ character_id: 'one', persona: '', name: 'Reconnected' }], active }); await flush()
  assert.equal(page.state.error, '')
  assert.equal(page.state.characters[0].name, 'Reconnected')
  const previous = page.refresh()
  page.connect(false); page.connect(true)
  pending[3].reject(new Error('new connection failed')); await flush()
  pending[2].resolve({ characters: [{ character_id: 'one', persona: '', name: 'Stale' }] }); await previous
  assert.equal(page.state.error, 'new connection failed')
  assert.equal(page.state.characters[0].name, 'Reconnected')
  const last = page.refresh(); page.unmount()
  pending[4].resolve({ characters: [] }); await last
  assert.equal(page.state.error, 'new connection failed')
  assert.equal(page.state.characters[0].name, 'Reconnected')
})

test('an older list response cannot clear a newer refresh or save failure', async () => {
  const pending = []
  const page = catalogHarness(() => new Promise((resolve, reject) => pending.push({ resolve, reject })))
  const first = page.refresh(), second = page.refresh()
  pending[1].reject(new Error('latest list failed')); await second
  pending[0].resolve({ characters: [{ character_id: 'one', persona: '', name: 'Stale' }] }); await first
  assert.equal(page.state.error, 'latest list failed')
  assert.deepEqual(page.state.characters, [])
  const third = page.refresh()
  await page.perform(async () => { throw new Error('save failed') })
  pending[2].resolve({ characters: [{ character_id: 'one', persona: '', name: 'Stale' }] }); await third
  assert.equal(page.state.error, 'save failed')
  assert.equal(page.state.busy, false)
})

test('shared selection authority matches actual desktop snapshots and all source labels', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'am-m1-source-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const dotenv = path.join(directory, '.env')
  const store = new DesktopSettingsStore(path.join(directory, 'settings.json'), dotenv)
  function check(env, source, label, locked = false) {
    const value = shared.startupCharacterSelection(store.snapshot(env))
    assert.equal(value.source, source)
    assert.equal(value.locked, locked)
    assert.equal(shared.settingSourceLabel(value.source), label)
    const html = renderToStaticMarkup(React.createElement(StartupFailureCard, {
      failure: startup.backendStartupFailure(78, 'exit', value), recovering: false, error: '', notice: '', onRecover() {},
    }))
    assert.ok(html.includes(`Startup selection source: ${label}`))
  }
  check({}, 'default', 'Built-in default')
  fs.writeFileSync(dotenv, 'AMADEUS_CHARACTER_ID=dotenv-role\n')
  check({}, 'dotenv', '.env')
  store.update({}, { values: { AMADEUS_CHARACTER_ID: 'user-role' } })
  check({}, 'user', 'Desktop settings')
  check({ AMADEUS_CHARACTER_ID: 'environment-role' }, 'environment', 'Process environment', true)
})


test('reconnecting clears obsolete save/restart notices and online loading does not expose a generated next-role ID', () => {
  const page = catalogHarness(() => new Promise(() => {}))
  page.state.notice = 'Startup role saved. Restart the backend to apply.'
  page.connect(false)
  assert.equal(page.state.notice, '')
  const html = renderToStaticMarkup(React.createElement(CharacterManagementSettings, {
    send: async () => ({}), connected: true, restarting: false,
    desktop: { values: { AMADEUS_CHARACTER_ID: 'character-synthetic-pending' }, sources: { AMADEUS_CHARACTER_ID: 'user' } },
    kurisuPreview: {}, onSettingsChanged() {},
  }))
  assert.match(html, /Loading active role…/)
  assert.match(html, /Loading role details…/)
  assert.doesNotMatch(html, /character-synthetic-pending/)
})
