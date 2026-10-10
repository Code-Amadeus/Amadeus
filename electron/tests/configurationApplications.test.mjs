import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const catalog = loadTypeScript(new URL('../src/shared/configCatalog.ts', import.meta.url))
const runtimeUrl = new URL('../src/renderer/components/desktopRuntimeSettings.ts', import.meta.url)
const credentials = { isEncryptionAvailable: () => true, encryptString: value => Buffer.from(value), decryptString: value => value.toString() }
function makeStore(t, overrides = {}) {
  const { DesktopSettingsStore } = loadTypeScript(new URL('../src/main/desktopSettings.ts', import.meta.url), {
    electron: { safeStorage: credentials }, ...overrides,
  })
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-live-config-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  return new DesktopSettingsStore(path.join(directory, 'settings.json'), path.join(directory, '.env'))
}
function withWindow(t, value) {
  const previous = globalThis.window
  globalThis.window = value
  t.after(() => { globalThis.window = previous })
}

test('theme choices retain their explanatory text in English and Chinese', t => {
  withWindow(t, { localStorage: { getItem: key => key === 'amadeus.settings.section' ? 'general' : null } })
  for (const locale of ['en-US', 'zh-CN']) {
    const Settings = loadTypeScript(new URL('../src/renderer/components/SettingsPage.tsx', import.meta.url), {
      '../i18n': { useI18n: () => ({ locale, t: value => locale === 'zh-CN' ? catalog.catalogTranslations[value] || value : value, setLocale() {} }) },
      '../theme': { useTheme: () => ({ theme: 'classic', setTheme() {} }) },
      '../activeCharacter': { useActiveCharacter: () => null },
      './FluentIcon': { __esModule: true, default: () => null },
      '../styles/characterWorkspace.css': {}, '../styles/characterVisuals.css': {}, '../styles/workPreview.css': {},
    }).default
    const markup = renderToStaticMarkup(React.createElement(Settings, {
      send: async () => ({}), subscribe: () => () => {}, connected: false, reconnectBackend: async () => {},
    }))
    for (const description of locale === 'en-US' ? [
      'Clean neutral desktop palette.', 'Dark translucent surfaces inspired by the Wallpaper Slice.',
    ] : ['简洁、中性的桌面配色。', '以壁纸切片为灵感的深色半透明界面。']) {
      assert.ok(markup.includes(`<span>${description}</span>`), `${locale}: missing theme explanation ${description}`)
    }
    assert.ok(!markup.includes('<option value="wallpaper_surface"'))
    assert.ok(!markup.includes('<option value="region"'))
  }
})

test('legacy accepted values stay valid without adding new choices to the existing forms', t => {
  const store = makeStore(t)
  store.update({}, { values: { AUIP_ACTION_REASONING_EFFORT: 'ultra', AMADEUS_VISION_SCOPE: 'region' } })
  assert.equal(store.backendEnvironment({}).AMADEUS_VISION_SCOPE, 'region')
  assert.equal(store.backendEnvironment({}).AUIP_ACTION_REASONING_EFFORT, 'ultra')
  assert.deepEqual(catalog.catalogConfiguration('auip_action').fields.find(field => field.key === 'AUIP_ACTION_REASONING_EFFORT').options,
    ['none', 'minimal', 'low', 'medium', 'high', 'max'])
  assert.deepEqual(catalog.catalogConfiguration('vision').fields.find(field => field.key === 'AMADEUS_VISION_SCOPE').options,
    ['full_screen', 'current_window', 'selected_window'])
})

test('frontend and desktop startup settings have explicit policies and do not enter the backend environment', t => {
  const store = makeStore(t)
  const saved = store.update({}, { values: {
    AMADEUS_UI_LOCALE: 'zh-CN', AMADEUS_UI_THEME: 'classic', AMADEUS_WINDOWS_STARTUP_MODE: 'wallpaper',
  } })
  assert.equal(saved.restartRequired, false)
  assert.deepEqual(saved.pendingRevisions, {})
  assert.deepEqual(store.backendEnvironment({}), {})
  assert.equal(catalog.catalogApplication('AMADEUS_WINDOWS_STARTUP_MODE'), 'desktop_restart')
  assert.equal(catalog.catalogApplication('AMADEUS_UI_LOCALE'), 'frontend')
  assert.equal(catalog.catalogApplication('AMADEUS_VISION_ENABLED'), 'host')
  assert.equal(catalog.catalogApplication('VTS_ENABLED'), 'backend_restart')
  assert.equal(catalog.catalogConfiguration('desktop_startup', saved).fields[0].restart_required, false)
})

test('frontend aliases also remain outside backend launch inputs', t => {
  const groups = structuredClone(catalog.catalogGroups)
  groups.find(group => group.id === 'desktop_interface').config.TEST_FRONTEND_CHOICE = {
    type: 'enum', title: { 'en-US': 'Fixture interface choice', 'zh-CN': '测试界面选项' },
    default: 'first', options: ['first', 'second'], scope: 'desktop', aliases: ['OLD_FRONTEND_CHOICE'],
  }
  const store = makeStore(t, { './configCatalog.generated.js': { catalogGroups: groups } })
  store.update({}, { values: { OLD_FRONTEND_CHOICE: 'second' } })
  assert.deepEqual(store.backendEnvironment({}), {})
  assert.deepEqual(store.backendEnvironment({ OLD_FRONTEND_CHOICE: 'first' }), {})
})

test('a new ordinary live declaration reaches the production form, runtime conversion, persistence and validation', t => {
  const groups = structuredClone(catalog.catalogGroups)
  groups.find(group => group.id === 'presentation').config.TEST_RUNTIME_THRESHOLD = {
    type: 'integer', title: { 'en-US': 'Fixture runtime threshold', 'zh-CN': '测试运行时阈值' },
    default: 3, scope: 'session', runtime_key: 'fixture_threshold', min: 1, max: 8, step: 1,
    control: 'select', options: ['3', '5'],
  }
  const overrides = { './configCatalog.generated.js': { catalogGroups: groups } }
  const store = makeStore(t, overrides)
  const runtime = loadTypeScript(runtimeUrl, overrides)
  assert.deepEqual(runtime.desktopValuesForRuntimeSettings({ fixture_threshold: 5 }), { TEST_RUNTIME_THRESHOLD: 5 })
  const saved = store.update({}, { values: { TEST_RUNTIME_THRESHOLD: '5' } })
  assert.equal(runtime.runtimeSettingValue('fixture_threshold', saved), 5)
  assert.equal(store.backendEnvironment({}).TEST_RUNTIME_THRESHOLD, '5')
  assert.throws(() => store.update({}, { values: { TEST_RUNTIME_THRESHOLD: '9' } }), /between 1 and 8/)
  const localStorage = { getItem: key => key === 'amadeus.settings.section' ? 'general' : null }
  withWindow(t, { localStorage })
  const Settings = loadTypeScript(new URL('../src/renderer/components/SettingsPage.tsx', import.meta.url), {
    ...overrides,
    '../i18n': { useI18n: () => ({ locale: 'en-US', t: value => value, setLocale() {} }) },
    '../theme': { useTheme: () => ({ theme: 'classic', setTheme() {} }) },
    '../activeCharacter': { useActiveCharacter: () => null },
    './FluentIcon': { __esModule: true, default: () => null },
    '../styles/characterWorkspace.css': {}, '../styles/characterVisuals.css': {}, '../styles/workPreview.css': {},
  }).default
  const markup = renderToStaticMarkup(React.createElement(Settings, {
    send: async () => ({}), subscribe: () => () => {}, connected: false, reconnectBackend: async () => {},
  }))
  assert.match(markup, /aria-label="Fixture runtime threshold"/)
  assert.match(markup, /<option value="3" selected="">3<\/option>/)
})

test('runtime form projection respects environment authority, pending edits, explicit false and unknown dotenv', () => {
  const runtime = loadTypeScript(runtimeUrl)
  const key = 'AMADEUS_VISION_ENABLED'
  assert.equal(runtime.runtimeSettingValue('vision_enabled', {
    values: { [key]: 'true' }, startupValues: { [key]: 'false' }, sources: { [key]: 'environment' },
  }, true), false)
  assert.equal(runtime.runtimeSettingValue('vision_enabled', {
    values: { [key]: 'false' }, sources: { [key]: 'user' }, pendingRevisions: { [key]: 3 },
  }, true), false)
  assert.equal(runtime.runtimeSettingValue('vision_enabled', { sources: { [key]: 'dotenv' } }), undefined)
  assert.equal(runtime.runtimeSettingValue('vision_enabled', { sources: { [key]: 'dotenv' } }, true), true)
  assert.equal(runtime.runtimeSettingValue('vision_enabled', {
    sources: { [key]: 'dotenv' }, pendingRevisions: { [key]: 4 },
  }, true), undefined)
  assert.equal(runtime.runtimeSettingValue('vision_enabled', {
    startupValues: { [key]: ' ON ' }, sources: { [key]: 'environment' },
  }), true)
})

test('caption default belongs to the legacy presentation owner and a pending clear stays unknown', () => {
  const runtime = loadTypeScript(runtimeUrl)
  const key = 'AMADEUS_WALLPAPER_CAPTION_MODE'
  assert.equal(runtime.runtimeSettingValue('wallpaper_caption_mode', { sources: { [key]: 'default' } }, 'source'), 'source')
  assert.equal(runtime.runtimeSettingValue('wallpaper_caption_mode', { sources: { [key]: 'default' } }), undefined)
  assert.equal(runtime.runtimeSettingValue('wallpaper_caption_mode', {
    sources: { [key]: 'default' }, pendingRevisions: { [key]: 3 },
  }, 'bilingual'), undefined)
})

test('known enum startup inputs remain visible verbatim, including owner-normalized values', () => {
  const snapshot = { startupValues: { GRAPHICS_PROFILE: 'STANDARD' }, sources: { GRAPHICS_PROFILE: 'environment' } }
  const field = catalog.catalogConfiguration('graphics_budget', snapshot).fields.find(field => field.key === 'GRAPHICS_PROFILE')
  assert.equal(field.value, 'STANDARD')
  assert.deepEqual(catalog.optionsWithCurrentValue(field.options, field.value)[0], 'STANDARD')
  assert.equal(catalog.optionsWithCurrentValue(field.options, 'standard').length, field.options.length)
})

test('numeric validation is independent of ranges, while CLI control values keep their string contract', t => {
  const store = makeStore(t)
  for (const value of ['junk', '1.5', 'Infinity']) {
    assert.throws(() => store.update({}, { values: { MICROPHONE_DEVICE_INDEX: value } }), /must be a number|must be an integer/)
  }
  store.update({}, { values: { MICROPHONE_DEVICE_INDEX: '-1', LOCAL_LLM_CLI_CONTEXT: 'custom-cli-value' } })
  assert.equal(store.backendEnvironment({}).MICROPHONE_DEVICE_INDEX, '-1')
  assert.equal(store.backendEnvironment({}).LOCAL_LLM_CLI_CONTEXT, 'custom-cli-value')
})

test('failed save does not supply an application receipt; saved live edits remain pending until matching acknowledgment', async t => {
  const store = makeStore(t)
  const runtime = loadTypeScript(runtimeUrl)
  withWindow(t, { amadeus: { updateDesktopSettings: async () => ({ ok: false, error: 'disk unavailable' }) } })
  await assert.rejects(runtime.persistDesktopRuntimeSettings({ vision_enabled: true }), /disk unavailable/)
  assert.deepEqual(store.pendingRevisionSnapshot(), {})
  window.amadeus = {
    updateDesktopSettings: async update => ({ ok: true, settings: store.update({}, update) }),
    markDesktopSettingsApplied: async revisions => ({ ok: true, settings: store.markApplied({}, revisions) }),
  }
  const first = await runtime.persistDesktopRuntimeSettings({ vision_enabled: true })
  assert.equal(first.persisted, true)
  assert.ok(store.pendingRevisionSnapshot().AMADEUS_VISION_ENABLED)
  // An application failure/offline connection provides no acknowledgment.
  assert.deepEqual(store.pendingRevisionSnapshot(), first.pendingRevisions)
  const second = await runtime.persistDesktopRuntimeSettings({ vision_enabled: false })
  await runtime.markRuntimeSettingsApplied({ vision_enabled: true }, first.pendingRevisions)
  assert.deepEqual(store.pendingRevisionSnapshot(), second.pendingRevisions)
  await runtime.markRuntimeSettingsApplied({ vision_enabled: false }, second.pendingRevisions)
  assert.deepEqual(store.pendingRevisionSnapshot(), {})
})

test('structured ACP records and retired-route deletion keep their dedicated validation boundaries', t => {
  const store = makeStore(t)
  assert.throws(() => store.update({}, { values: { AMADEUS_ACP_PROVIDERS: '{"agents":"invalid"}' } }), /ACP agents/i)
  assert.throws(() => store.update({}, { values: { COOPERATIVE_CHAT_ENABLED: 'false' } }), /retired and read-only/)
  store.update({ COOPERATIVE_CHAT_ENABLED: 'false' }, { values: { COOPERATIVE_CHAT_ENABLED: null } })
  assert.equal(store.snapshot({ COOPERATIVE_CHAT_ENABLED: 'false' }).retired_settings[0].source, 'environment')
})
