import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import test from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
function compile(relativePath, dependencies = require) {
  const source = fs.readFileSync(new URL(relativePath, import.meta.url), 'utf8')
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText
  const exports = {}
  new Function('require', 'exports', code)(dependencies, exports)
  return exports
}
const { DesktopSettingsStore } = compile('../src/main/desktopSettings.ts', name => name === 'electron'
  ? { safeStorage: { isEncryptionAvailable: () => false } } : require(name))
const runtime = compile('../src/renderer/components/desktopRuntimeSettings.ts')
const key = 'AMADEUS_MAIN_CHAT_CHARACTER_PROMPT_JA'

function makeStore(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-character-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const file = path.join(directory, 'settings.json')
  const dotenv = path.join(directory, '.env')
  return { store: new DesktopSettingsStore(file, dotenv), file, dotenv }
}

test('Japanese multiline persona survives saving and a backend restart', t => {
  const { store, file, dotenv } = makeStore(t)
  const prompt = 'あなたは牧瀬紅莉栖。\n親しみのある口調で話す。' + '字'.repeat(5000)
  const values = runtime.desktopValuesForRuntimeSettings({ main_chat_character_prompt_ja: prompt })
  assert.equal(values[key], prompt)
  const saved = store.update({}, { values })
  assert.equal(runtime.runtimeSettingFromDesktopValues('main_chat_character_prompt_ja', saved.values), prompt)
  const restarted = new DesktopSettingsStore(file, dotenv)
  assert.equal(restarted.backendEnvironment({})[key], prompt)
  assert.equal(restarted.snapshot({}).values[key], prompt)
  assert.ok(restarted.snapshot({}).pendingRevisions[key] > 0)
})

test('blank saving restores the built-in character even with a dotenv override', t => {
  const { store, file, dotenv } = makeStore(t)
  fs.writeFileSync(dotenv, `${key}=dotenv-persona\n`)
  store.update({}, { values: { [key]: 'custom-persona' } })
  store.update({}, { values: { [key]: ' \n\t' } })
  assert.equal(store.snapshot({}).values[key], '')
  store.update({}, { values: { [key]: 'another-persona' } })
  store.update({}, { values: { [key]: '' } })
  const restarted = new DesktopSettingsStore(file, dotenv)
  assert.equal(restarted.backendEnvironment({})[key], '')
  assert.equal(restarted.snapshot({}).values[key], '')
  assert.equal(restarted.snapshot({}).sources[key], 'user')
})

test('launch environment keeps authority and invalid persona values do not persist', t => {
  const { store } = makeStore(t)
  const environment = { [key]: 'launch-persona' }
  assert.equal(store.snapshot(environment).locked[key], true)
  assert.throws(() => store.update(environment, { values: { [key]: 'edit' } }), /locked/)
  assert.equal(store.backendEnvironment(environment)[key], undefined)
  for (const value of [false, 'bad\0prompt', '字'.repeat(8193)]) {
    assert.throws(() => store.update({}, { values: { [key]: value } }))
  }
  assert.equal(store.snapshot({}).values[key], undefined)
})
