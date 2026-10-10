import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createSourceRequire } from './helpers/loadTypeScript.mjs'
import test from 'node:test'
import ts from 'typescript'

const require = createSourceRequire(new URL('../src/main/desktopSettings.ts', import.meta.url))
function compile(relativePath, dependencies = createSourceRequire(new URL(relativePath, import.meta.url))) {
  const source = fs.readFileSync(new URL(relativePath, import.meta.url), 'utf8')
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true, jsx: ts.JsxEmit.ReactJSX },
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


const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const { default: CharacterEditor } = compile('../src/renderer/components/MainChatCharacterSettings.tsx', name => {
  if (name === '../i18n') return { useI18n: () => ({ t: value => value }) }
  if (name === './SettingsPrimitives') return { CardShell: ({ children }) => React.createElement('div', null, children) }
  return createSourceRequire(new URL('../src/renderer/components/MainChatCharacterSettings.tsx', import.meta.url))(name)
})

for (const savedOverride of ['', '保存済みの人物設定']) {
  test(`one editor keeps the default only in its placeholder (${savedOverride ? 'custom' : 'default'})`, () => {
    const defaultPrompt = '内置の人物設定'
    const html = renderToStaticMarkup(React.createElement(CharacterEditor, {
      savedOverride, preview: { default: defaultPrompt, effective: savedOverride || defaultPrompt, active: true },
      canSave: true, locked: false, saving: false, onSave: async () => true,
    }))
    const textareas = [...html.matchAll(/<textarea\b([^>]*)>([\s\S]*?)<\/textarea>/g)]
    assert.equal(textareas.length, 1)
    assert.ok(textareas[0][1].includes(`placeholder="${defaultPrompt}"`))
    assert.equal(textareas[0][2], savedOverride)
    assert.ok(!textareas[0][1].includes('readonly'))
  })
}

test('inactive editor still edits Kurisu and explains both role and language applicability', () => {
  const html = renderToStaticMarkup(React.createElement(CharacterEditor, {
    savedOverride: '保存済みの紅莉栖設定',
    preview: { default: '紅莉栖の既定設定', effective: '保存済みの紅莉栖設定', active: false },
    canSave: true, locked: false, saving: false, onSave: async () => true,
  }))
  assert.match(html, /Kurisu Japanese persona override/)
  assert.match(html, /inactive for the current replies/)
  assert.match(html, /only when Kurisu is active and replies are Japanese/)
  assert.match(html, /VN, work commentary, English replies and Hybrid opening lines/)
  assert.match(html, /does not change the active character, permissions, art or voice/)
  assert.match(html, /placeholder="紅莉栖の既定設定"/)
  assert.match(html, />保存済みの紅莉栖設定<\/textarea>/)
})

test('Chinese editor preserves Kurisu ownership and inactive applicability', () => {
  const i18n = compile('../src/renderer/i18n.tsx')
  const { default: LocalizedEditor } = compile('../src/renderer/components/MainChatCharacterSettings.tsx', name => {
    if (name === '../i18n') return i18n
    if (name === './SettingsPrimitives') return { CardShell: ({ children }) => React.createElement('div', null, children) }
    return createSourceRequire(new URL('../src/renderer/components/MainChatCharacterSettings.tsx', import.meta.url))(name)
  })
  const previous = globalThis.localStorage
  globalThis.localStorage = { getItem: () => 'zh-CN' }
  try {
    const html = renderToStaticMarkup(React.createElement(i18n.I18nProvider, null,
      React.createElement(LocalizedEditor, {
        savedOverride: '', preview: { default: '紅莉栖の既定設定', active: false },
        canSave: true, locked: false, saving: false, onSave: async () => true,
      })))
    assert.match(html, /红莉栖日语人格覆盖/)
    assert.match(html, /仅在启用红莉栖且使用日语回复时生效/)
    assert.match(html, /不更换当前角色、权限、立绘或声音/)
  } finally {
    if (previous === undefined) delete globalThis.localStorage
    else globalThis.localStorage = previous
  }
})
