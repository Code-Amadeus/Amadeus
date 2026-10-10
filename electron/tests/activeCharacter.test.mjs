import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'
import * as React from 'react'
import * as jsxRuntime from 'react/jsx-runtime'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSourceRequire } from './helpers/loadTypeScript.mjs'

const root = new URL('../src/renderer/', import.meta.url)
function load(relative, imports, globals = {}) {
  const source = readFileSync(new URL(relative, root), 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText
  const module = { exports: {} }
  vm.runInNewContext(compiled, {
    module, exports: module.exports,
    require: name => {
      if (name in imports) return imports[name]
      if (name === 'react') return React
      if (name === 'react/jsx-runtime') return jsxRuntime
      if (name.startsWith('.')) return createSourceRequire(new URL(relative, root))(name)
      throw new Error(`Unexpected import: ${name}`)
    }, ...globals,
  })
  return module.exports
}
const kurisu = {
  character_id: 'kurisu', name: 'Kurisu', display_name: 'Makise Kurisu', short_name: 'Kurisu',
  ui_name: '牧瀬 紅莉栖', accessible_name: '牧濑红莉栖',
}
const named = name => ({ character_id: 'mira', name, display_name: name, short_name: name, ui_name: name, accessible_name: name })
const active = load('activeCharacter.tsx', {})
const flush = () => new Promise(resolve => setImmediate(resolve))

test('active projection requires the complete host name contract and discards unrelated saved data', () => {
  assert.deepEqual({ ...active.readActiveCharacter({ ...kurisu, pending: named('Mira') }) }, kurisu)
  assert.deepEqual({ ...active.readActiveCharacter(named('<Mira & "friend">')) }, named('<Mira & "friend">'))
  assert.equal(active.readActiveCharacter({ name: 'Mira' }), null)
  assert.equal(active.readActiveCharacter({ ...kurisu, short_name: '' }), null)
})

test('one live identity read per connection ignores delayed previous-backend results and saved selections', async () => {
  let value = null, effect, dependencies, cleanup
  const requests = []
  const hooks = {
    ...React,
    useState: () => [value, next => { value = next }],
    useEffect: (fn, deps) => {
      if (!dependencies || deps.some((dep, index) => dep !== dependencies[index])) {
        dependencies = deps; effect = fn
      }
    },
  }
  const module = load('activeCharacter.tsx', { react: hooks })
  const send = method => new Promise(resolve => { requests.push({ method, resolve }) })
  const render = connected => {
    const result = module.useActiveCharacterSnapshot(connected, send)
    if (effect) { cleanup?.(); cleanup = effect(); effect = null }
    return result
  }
  assert.equal(render(false), null)
  assert.equal(requests.length, 0)
  render(true); render(true)
  assert.equal(requests.length, 1)
  assert.equal(requests[0].method, 'character.active')
  render(false); render(true)
  requests[0].resolve(kurisu); await flush()
  assert.equal(render(true), null, 'the disconnected backend cannot name the new one')
  requests[1].resolve(named('Mira')); await flush()
  assert.equal(render(true).short_name, 'Mira')
  const savedNextStart = kurisu
  assert.equal(savedNextStart.short_name, 'Kurisu')
  assert.equal(render(true).short_name, 'Mira', 'saved settings do not supply running identity')
  assert.equal(requests.length, 2)
  assert.equal(render(false), null)
})

function renderLabels(identity, locale = 'en-US') {
  const i18n = load('i18n.tsx', {}, { localStorage: { getItem: () => locale } })
  const avatar = load('components/ChatAvatarSettings.tsx', {
    '../activeCharacter': active, '../i18n': i18n,
    './FluentIcon': { __esModule: true, default: () => React.createElement('svg') },
  }).default
  const card = load('components/work/AuipExperienceCard.tsx', {
    '../../activeCharacter': active,
    react: { ...React, useState: () => [true, () => {}] },
  }).default
  return renderToStaticMarkup(React.createElement(i18n.I18nProvider, null,
    React.createElement(active.ActiveCharacterContext.Provider, { value: identity },
      React.createElement(avatar),
      React.createElement(card, { experience: { title: 'Game', status: 'active', appSessionId: 'session' } }),
    ),
  ))
}
test('default visible avatar and AUIP labels keep their exact English and Chinese text', () => {
  const english = renderLabels(kurisu)
  assert.match(english, />Kurisu avatar</)
  assert.match(english, /aria-label="Upload image · Kurisu avatar"/)
  assert.match(english, />Let Kurisu play</)
  assert.match(renderLabels(kurisu, 'zh-CN'), />Kurisu 头像</)
})
test('avatar and AUIP labels use Mira and React escapes names as text', () => {
  const mira = renderLabels(named('Mira'))
  assert.match(mira, />Mira avatar</)
  assert.match(mira, />Let Mira play</)
  assert.ok(!mira.includes('Kurisu'))
  const escaped = renderLabels(named('<Mira & "friend">'))
  assert.match(escaped, /&lt;Mira &amp; &quot;friend&quot;&gt; avatar/)
  assert.ok(!escaped.includes('<Mira'))
})
test('unknown running identity uses generic labels', () => {
  const markup = renderLabels(null)
  assert.match(markup, />Assistant avatar</)
  assert.match(markup, />Let the assistant play</)
  assert.ok(!markup.includes('Kurisu'))
})

async function companion(identity, atlas = false) {
  const elements = new Map()
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      tagName: id === 'portrait' ? 'IMG' : 'DIV', textContent: '', dataset: {}, attributes: {},
      setAttribute(name, value) { this.attributes[name] = value },
      addEventListener() {}, replaceWith(next) { elements.set(id, next) },
    })
    return elements.get(id)
  }
  let source, resolveIdentity
  const window = {
    companion: { portraits: async () => atlas ? {} : { normal: { idle: ['normal.png'] } },
      initialIdentity: identity,
      active: () => new Promise(resolve => { resolveIdentity = resolve }), connected() {} },
    addEventListener() {},
    CompanionAtlas: { validate: data => data, Player: class { select() {} setPaused() {} } },
  }
  const scope = vm.createContext({
    window, console, URL, URLSearchParams, location: { search: '?bridgePort=17897', href: 'http://127.0.0.1:17898/companion_panel.html' },
    localStorage: { getItem: () => null },
    document: { hidden: false, fonts: { ready: Promise.resolve() }, getElementById: element,
      createElement: () => ({ tagName: 'CANVAS', dataset: {}, attributes: {}, setAttribute(name, value) { this.attributes[name] = value } }),
      addEventListener() {}, body: { classList: { toggle() {} } } },
    fetch: async () => ({ ok: true, json: async () => ({}) }),
    ResizeObserver: class { observe() {} disconnect() {} },
    EventSource: class { constructor() { source = this } },
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
  })
  vm.runInContext(readFileSync(new URL('../../render/web/companion_panel.js', import.meta.url), 'utf8'), scope)
  const firstPaint = { name: element('speaker').textContent, accessibleName: element('portrait').alt }
  await flush(); source.onopen(); resolveIdentity(identity); await flush()
  return { element, source, window, firstPaint, resolve: value => { resolveIdentity(value) } }
}
test('Companion default and custom names preserve image/canvas accessibility text and treat names as text', async () => {
  for (const atlas of [false, true]) for (const identity of [kurisu, named('Mira'), named('<Mira & "friend">')]) {
    const panel = await companion(identity, atlas)
    assert.equal(panel.firstPaint.name, identity.ui_name, 'the startup role is visible before asynchronous reads')
    assert.equal(panel.firstPaint.accessibleName, identity.accessible_name)
    assert.equal(panel.element('speaker').textContent, identity.ui_name)
    const portrait = panel.element('portrait')
    assert.equal(atlas ? portrait.attributes['aria-label'] : portrait.alt, identity.accessible_name)
    assert.equal(panel.element('speaker').innerHTML, undefined)
  }
})
test('Companion retains the pinned name offline and ignores stale results while reading the active role again', async () => {
  const panel = await companion(kurisu)
  panel.source.onerror()
  assert.equal(panel.element('speaker').textContent, '牧瀬 紅莉栖')
  panel.source.onopen()
  panel.resolve(named('Mira')); await flush()
  assert.equal(panel.element('speaker').textContent, 'Mira')
  panel.source.onerror(); panel.source.onopen()
  panel.source.onerror(); panel.resolve(kurisu); await flush()
  assert.equal(panel.element('speaker').textContent, 'Mira')
})

test('an unavailable Companion startup identity keeps the application name', async () => {
  const panel = await companion(null)
  assert.equal(panel.firstPaint.name, 'Amadeus')
  assert.equal(panel.firstPaint.accessibleName, 'Amadeus')
})

test('Companion identity reads are limited to the owned top-level card', async () => {
  const handlers = new Map()
  let reads = 0
  const { CompanionPanel } = load('../main/companionPanel.ts', {
    electron: { ipcMain: { handle: (method, handler) => handlers.set(method, handler) } },
    'node:fs': {}, 'node:path': {}, './companionPanelLayout.js': {}, './companionPortraits.js': {},
  })
  const panel = new CompanionPanel({ active: async () => { reads++; return kurisu } })
  const owner = { mainFrame: {} }
  panel.window = { webContents: owner }
  const read = handlers.get('companion.active')
  assert.equal(read({ sender: {}, senderFrame: owner.mainFrame }), null)
  assert.equal(read({ sender: owner, senderFrame: {} }), null)
  assert.equal(reads, 0)
  assert.equal(await read({ sender: owner, senderFrame: owner.mainFrame }), kurisu)
  assert.equal(reads, 1)
})

test('Companion launches with the pinned name before its window is created and shown', async () => {
  for (const identity of [kurisu, named('Mira'), named('<Mira & "friend">')]) {
    const order = []
    let options
    const { CompanionPanel } = load('../main/companionPanel.ts', {
      electron: {
        ipcMain: { handle() {} },
        screen: { getCursorScreenPoint: () => ({}), getDisplayNearestPoint: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }) },
        BrowserWindow: class {
          constructor(value) {
            options = value; order.push('create')
            this.webContents = { setWindowOpenHandler() {}, on() {} }
          }
          on() {} isDestroyed() { return false }
          async loadURL() { order.push('load') }
          showInactive() { order.push('show') }
        },
      },
      'node:fs': { readFileSync() { throw new Error('no saved placement') } },
      'node:path': { join: (...values) => values.join('/') },
      './companionPanelLayout.js': { clampPanel: rect => rect }, './companionPortraits.js': {},
    })
    const panel = new CompanionPanel({
      active: async () => { order.push('identity'); return identity },
      bridge: () => ({ assetPort: 17898, bridgePort: 17897, assetVersion: 'test' }),
      target: () => null, slice: () => [], userDataDir: 'test', preload: 'companion.cjs',
    })
    assert.equal(await panel.toggle('work'), true)
    assert.deepEqual(order, ['identity', 'create', 'load', 'show'])
    const argument = options.webPreferences.additionalArguments[0]
    let exposed
    load('../preload/companion.cts', { electron: {
      contextBridge: { exposeInMainWorld: (name, value) => { assert.equal(name, 'companion'); exposed = value } },
      ipcRenderer: { invoke: method => method },
    } }, { process: { argv: ['electron', argument] } })
    assert.deepEqual({ ...exposed.initialIdentity }, { ui_name: identity.ui_name, accessible_name: identity.accessible_name })
    assert.equal(exposed.active(), 'companion.active')
  }
})


test('avatar interpolation preserves replacement-pattern characters in either locale', () => {
  for (const name of ['Money$$', 'A$&B', 'Back$`tick', "End$'tail"]) {
    for (const locale of ['en-US', 'zh-CN']) {
      const markup = renderLabels(named(name), locale)
      const text = renderToStaticMarkup(React.createElement('span', null, `${name}${locale === 'zh-CN' ? ' 头像' : ' avatar'}`)).slice(6, -7)
      assert.ok(markup.includes(`>${text}<`), `name must stay literal: ${name} (${locale})`)
    }
  }
})

test('avatar fallbacks preserve astral code points and retain Kurisu and unknown defaults', () => {
  for (const [identity, expected] of [[named('🌟Star'), '🌟'], [named('𠮷田'), '𠮷'], [kurisu, 'K'], [null, 'A']]) {
    const markup = renderLabels(identity)
    assert.ok(markup.includes(`class="auip-experience-avatar" aria-hidden="true">${expected}</span>`))
    assert.ok(markup.includes(`>${expected}</div>`), 'settings preview uses the same complete initial')
    assert.ok(markup.isWellFormed(), 'no unpaired surrogate is rendered')
  }
})


test('Chinese role status and built-in edit explanations use the real translator', () => {
  const i18n = load('i18n.tsx', {}, { localStorage: { getItem: () => 'zh-CN' } })
  const shared = load('../shared/characterStartup.ts', {})
  const management = load('components/characterManagement.ts', { '../../shared/characterStartup': shared })
  const { CharacterRoleLabel } = load('components/CharacterManagementSettings.tsx', {
    './characterManagement': management, '../i18n': i18n,
    './SettingsPrimitives': { CardShell: ({ children }) => children },
  })
  function Explanation() {
    return React.createElement('p', null, i18n.useI18n().t('Built-in characters are read-only.'))
  }
  const markup = renderToStaticMarkup(React.createElement(i18n.I18nProvider, null,
    React.createElement(CharacterRoleLabel, { character: { character_id: 'kurisu', name: 'Kurisu', builtin: true }, active: true, nextStart: true }),
    React.createElement(Explanation)))
  for (const expected of ['Kurisu', '内置', '当前使用', '下次启动', '内置角色为只读。']) assert.ok(markup.includes(expected))
  assert.doesNotMatch(markup, /Built-in|Active|Next start|read-only/)
})
