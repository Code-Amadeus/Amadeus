import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { createRequire } from 'node:module'
import ts from 'typescript'
import * as React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import * as workspace from '../src/renderer/components/characterWorkspace.ts'
import { createSourceRequire } from './helpers/loadTypeScript.mjs'
const require = createRequire(import.meta.url)
function compile(relative, imports = {}, globals = {}) {
  const source = fs.readFileSync(new URL(relative, import.meta.url), 'utf8')
  const code = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
  } }).outputText
  const exports = {}
  new Function('require', 'exports', ...Object.keys(globals), code)(
    name => name in imports ? imports[name] : createSourceRequire(new URL(relative, import.meta.url))(name), exports, ...Object.values(globals))
  return exports
}
test('fresh and obsolete nested routes return to the character overview', () => {
  assert.equal(workspace.characterSection(null), 'overview')
  assert.equal(workspace.characterSection('voice'), 'overview')
  assert.equal(workspace.characterSection('obsolete'), 'overview')
  assert.equal(workspace.characterSection('appearance'), 'appearance')
})

function page(section, locale = 'en-US', identity = { ui_name: '牧瀬 紅莉栖' }, connected = true) {
  const store = { getItem: key => key === workspace.CHARACTER_SECTION_KEY ? section : locale }
  const i18n = compile('../src/renderer/i18n.tsx', {}, { localStorage: store })
  const loaded = compile('../src/renderer/components/CharacterPage.tsx', {
    '../i18n': i18n, '../activeCharacter': { useActiveCharacter: () => identity },
    './FluentIcon': { __esModule: true, default: () => null }, './characterWorkspace': workspace, '../styles/characterWorkspace.css': {},
  }, { localStorage: store })
  const panels = Object.fromEntries(workspace.CHARACTER_SECTIONS.map(id => [id, React.createElement('div', { 'data-panel': id }, `${id} content`)]))
  return renderToStaticMarkup(React.createElement(i18n.I18nProvider, null, React.createElement(loaded.default, {
    panels, connected, section: workspace.characterSection(section), onSectionChange() {}, voiceSummary: 'GPT-SoVITS · Amadeus', onOpenVoice() {},
  })))
}

for (const section of workspace.CHARACTER_SECTIONS.filter(id => id !== 'overview')) {
  test(`Characters lazily mounts only the selected ${section} panel`, () => {
    const html = page(section)
    assert.equal([...html.matchAll(/data-panel=/g)].length, 1)
    assert.ok(html.includes(`data-panel="${section}"`))
    assert.ok(html.includes(`id="character-tab-${section}" aria-selected="true"`))
    assert.ok(html.includes(`aria-labelledby="character-tab-${section}"`))
    assert.ok(html.includes('牧瀬 紅莉栖'))
    if (section === 'appearance') assert.ok(html.includes('These settings apply to the application.'))
  })
}

test('overview is read-only, opens the single Voice editor and translates with the real dictionary', () => {
  const html = page('overview')
  assert.equal([...html.matchAll(/data-panel=/g)].length, 0)
  assert.equal([...html.matchAll(/character-overview-card/g)].length, 4)
  assert.ok(html.includes('Open voice settings'))
  assert.ok(html.includes('Saved speech engine'))
  assert.ok(!html.includes('<input'))
  assert.ok(!html.includes('<iframe'))
  const chinese = page('overview', 'zh-CN')
  for (const value of ['角色', '身份与人格', '外观', '语音', '资料与记忆', '所有角色共享', '打开语音设置']) assert.ok(chinese.includes(value), value)
  assert.ok(!chinese.includes('Shared by all roles'))
})

test('active-role header distinguishes a connected lookup from a disconnected backend', () => {
  const loading = page('identity', 'en-US', null, true)
  assert.ok(loading.includes('Loading active role…'))
  assert.ok(!loading.includes('Backend not connected'))
  assert.ok(page('identity', 'en-US', null, false).includes('Backend not connected'))
})

// Render the production Settings owner: the navigation must not create a second
// set of voice controls or mount the visual editor from the overview.
function settingsMarkup(section, characterTab = null, connected = false) {
  const store = { getItem: key => key === 'amadeus.settings.section' ? section
    : key === workspace.CHARACTER_SECTION_KEY ? characterTab : null }
  const cache = new Map()
  const load = url => {
    const path = url.pathname
    if (path.endsWith('/i18n.tsx')) return { useI18n: () => ({ locale: 'en-US', t: value => value, setLocale() {} }) }
    if (path.endsWith('/theme.tsx')) return { useTheme: () => ({ theme: 'classic', setTheme() {} }) }
    if (path.endsWith('/FluentIcon.tsx')) return { default: () => null }
    if (path.endsWith('/activeCharacter.tsx')) return { useActiveCharacter: () => null }
    if (path.endsWith('.css')) return {}
    if (cache.has(url.href)) return cache.get(url.href)
    const source = fs.readFileSync(url, 'utf8')
    const code = ts.transpileModule(source, { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    } }).outputText
    const exports = {}
    cache.set(url.href, exports)
    new Function('require', 'exports', 'window', 'localStorage', code)(name => {
      if (!name.startsWith('.')) return require(name)
      const base = new URL(name, url)
      const stem = base.href.replace(/\.js$/, '')
      const resolved = [base, new URL(stem + '.ts'), new URL(stem + '.tsx')].find(value => fs.existsSync(value))
      assert.ok(resolved, `${name} from ${url.href}`)
      return load(resolved)
    }, exports, { localStorage: store }, store)
    return exports
  }
  const Settings = load(new URL('../src/renderer/components/SettingsPage.tsx', import.meta.url)).default
  return renderToStaticMarkup(React.createElement(Settings, {
    send: async () => ({}), subscribe: () => () => {}, connected, reconnectBackend: async () => {},
  }))
}

test('Settings owns Characters and migrates the existing visual destination without mounting an overview preview', () => {
  const overview = settingsMarkup('characters')
  assert.ok(overview.includes('id="character-panel-overview"'))
  assert.ok(!overview.includes('id="visual-backend"'))
  assert.ok(!overview.includes('Japanese reference transcript'))
  assert.ok(!overview.includes('<iframe'))
  const migrated = settingsMarkup('visuals')
  assert.ok(migrated.includes('id="character-panel-appearance"'))
  assert.ok(migrated.includes('Character roles') === false)
  assert.ok(!migrated.includes('id="character-panel-overview"'))
})

test('all voice controls remain in one Settings section, including offline model files and reference audio', () => {
  const html = settingsMarkup('voice')
  for (const label of ['Voice checkpoint profile', 'Custom GPT semantic checkpoint', 'Custom SoVITS acoustic checkpoint',
    'Japanese reference audio', 'Japanese reference transcript', 'English reference audio', 'English reference transcript']) {
    assert.equal(html.split(`aria-label="${label}"`).length - 1, 1, label)
  }
  assert.ok(html.includes('Remote transcription API'))
  assert.ok(html.includes('Speech synthesis'))
  assert.ok(html.includes('Listening &amp; recognition'))
  assert.ok(html.includes('TTS output language'))
  assert.ok(!html.includes('character-panel-voice'))
})


test('SpriteForge frame packs belong to Appearance while audio reference packs belong to Voice', () => {
  const appearance = settingsMarkup('characters', 'appearance', true)
  const voice = settingsMarkup('voice', null, true)
  for (const label of ['Kurisu Character Pack', 'Visual Runtime Pack', 'VN Companion Portraits']) {
    assert.ok(appearance.includes(label), `${label} must appear with visual resources`)
    assert.ok(!voice.includes(label), `${label} must not be described as a voice resource`)
  }
  assert.ok(voice.includes('Kurisu V3 Emotion Reference Pack'))
  assert.ok(!appearance.includes('Kurisu V3 Emotion Reference Pack'))
})
