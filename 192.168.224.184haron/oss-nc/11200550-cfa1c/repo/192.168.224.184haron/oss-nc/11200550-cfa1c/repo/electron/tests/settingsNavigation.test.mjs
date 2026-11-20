import assert from 'node:assert/strict'
import test from 'node:test'
import * as React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const mocks = {
  '../i18n': { useI18n: () => ({ locale: 'en-US', t: (value, variables = {}) => value.replace(/\{(\w+)\}/g, (_, key) => variables[key] ?? `{${key}}`), setLocale() {} }) },
  '../theme': { useTheme: () => ({ selection: 'classic', reason: '', appliedVersion: '', setTheme() {} }) },
  '../activeCharacter': { useActiveCharacter: () => null },
  './FluentIcon': { __esModule: true, default: () => null },
  '../styles/characterWorkspace.css': {}, '../styles/characterVisuals.css': {}, '../styles/workPreview.css': {},
}

function withStoredSection(t, section) {
  const previous = globalThis.window
  globalThis.window = { localStorage: { getItem: key => key === 'amadeus.settings.section' ? section : null, setItem() {} } }
  t.after(() => { globalThis.window = previous })
}

function settings(props = {}) {
  const Settings = loadTypeScript(new URL('../src/renderer/components/SettingsPage.tsx', import.meta.url), mocks).default
  return renderToStaticMarkup(React.createElement(Settings, {
    send: async () => ({}), subscribe: () => () => {}, connected: false, reconnectBackend: async () => {}, ...props,
  }))
}

const currentSection = markup => markup.match(/<button aria-current="page">([^<]*)<\/button>/)?.[1]

test('Settings opens on General, remembers a section, and sends a remembered Extensions back to General', t => {
  for (const [stored, expected] of [[null, 'General'], ['voice', 'Voice'], ['extensions', 'General'], ['nonsense', 'General']]) {
    withStoredSection(t, stored)
    assert.equal(currentSection(settings()), expected, String(stored))
  }
})

test('Settings lists Diagnostics last and no Extensions section; Diagnostics shows the App-owned panel', t => {
  withStoredSection(t, 'diagnostics')
  const markup = settings({ diagnostics: React.createElement('div', { id: 'diagnostics-panel' }) })
  const sections = [...markup.matchAll(/<nav[^>]*class="settings-section-nav"[^>]*>(.*?)<\/nav>/g)][0][1]
    .match(/<button[^>]*>([^<]*)<\/button>/g).map(button => button.replace(/<[^>]+>/g, ''))
  assert.deepEqual(sections, ['Capabilities', 'General', 'Characters', 'Graphics', 'Models', 'Voice', 'Providers', 'Diagnostics'])
  assert.ok(markup.includes('id="diagnostics-panel"'))
})

test('links to other pages sit beside their group title instead of spanning the group', t => {
  withStoredSection(t, 'general')
  const general = settings()
  for (const label of ['Manage plugins', 'Open Characters']) {
    assert.match(general, new RegExp(`<div class="settings-group-header">(?:(?!</section>).)*<button type="button" class="settings-button[^"]*" data-tone="secondary">${label}</button></div>`), label)
  }
  withStoredSection(t, 'voice')
  assert.match(settings(), /<div class="settings-group-header">(?:(?!<\/section>).)*>Manage community voices<\/button><\/div>/)
})

test('a settings row names its source only when it matters and mentions restart only while one is pending', () => {
  const { startupFieldNotes } = loadTypeScript(new URL('../src/renderer/components/SettingsPage.tsx', import.meta.url), mocks)
  const field = { key: 'TTS_BACKEND', label: 'Backend', type: 'select', value: 'gpt_sovits', editable: true, restart_required: true }
  const desktop = (overrides = {}) => ({ sources: {}, locked: {}, pendingKeys: [], pendingRevisions: {}, ...overrides })
  assert.deepEqual(startupFieldNotes(field, desktop({ sources: { TTS_BACKEND: 'user' } })), [], 'built-in and desktop sources are not shown')
  assert.deepEqual(startupFieldNotes(field, desktop({ sources: { TTS_BACKEND: 'dotenv' } })), ['.env'])
  assert.deepEqual(startupFieldNotes(field, desktop({ locked: { TTS_BACKEND: true } })), ['locked'])
  assert.deepEqual(startupFieldNotes(field, desktop({ pendingKeys: ['TTS_BACKEND'] })), ['Applies after restart'])
  assert.deepEqual(startupFieldNotes({ ...field, restart_required: false }, desktop({ pendingKeys: ['TTS_BACKEND'] })), [],
    'live settings apply without a restart')
  assert.deepEqual(startupFieldNotes({ ...field, value: undefined }, null), ['editable in Electron app', 'Value unavailable until backend connects'])
})

test('the sidebar lists Chat, Wallpaper, VN Player, Plugins and Settings', t => {
  const previous = globalThis.localStorage
  globalThis.localStorage = { getItem: () => null, setItem() {} }
  t.after(() => { globalThis.localStorage = previous })
  const Sidebar = loadTypeScript(new URL('../src/renderer/components/Sidebar.tsx', import.meta.url), mocks).default
  const markup = renderToStaticMarkup(React.createElement(Sidebar, { page: 'plugins', onNavigate() {}, wallpaperActive: true, onToggleWallpaper() {} }))
  const items = [...markup.matchAll(/<button[^>]*class="sidebar-item"[^>]*>.*?<span>([^<]*)<\/span><\/button>/g)].map(match => match[1])
  assert.deepEqual(items, ['Chat', 'Wallpaper', 'VN Player', 'Plugins', 'Settings'])
  assert.match(markup, /aria-pressed="true"[^>]*>(?:(?!<\/button>).)*<span>Wallpaper<\/span>/, 'Wallpaper shows that it is running')
  assert.match(markup, /aria-current="page"[^>]*>(?:(?!<\/button>).)*<span>Plugins<\/span>/)
})
