import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import ts from 'typescript'

const require = createRequire(import.meta.url)
function load(relative, imports = require) {
  const source = fs.readFileSync(new URL(relative, import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText
  const exports = {}
  new Function('require', 'exports', compiled)(imports, exports)
  return exports
}
const { DesktopSettingsStore } = load('../src/main/desktopSettings.ts', name => name === 'electron'
  ? { safeStorage: { isEncryptionAvailable: () => true } } : require(name))
const { buildGraphicsConfiguration } = load('../src/renderer/components/graphicsConfigurationCatalog.ts')

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-graphics-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  return new DesktopSettingsStore(path.join(root, 'settings.json'), path.join(root, '.env'))
}

test('graphics settings persist, reach the next backend and respect process overrides', t => {
  const store = fixture(t)
  const values = { GRAPHICS_PROFILE: 'custom', RENDER_MAX_FPS: '45', RENDER_MAX_RESOLUTION: '1.25', RENDER_TEXTURE_SAMPLING: true }
  const saved = store.update({}, { values })
  assert.equal(saved.restartRequired, true)
  assert.deepEqual([...saved.pendingKeys].sort(), Object.keys(values).sort())
  const environment = store.backendEnvironment({})
  assert.equal(environment.GRAPHICS_PROFILE, 'custom')
  assert.equal(environment.RENDER_MAX_FPS, '45')
  assert.equal(environment.RENDER_MAX_RESOLUTION, '1.25')
  assert.equal(environment.RENDER_TEXTURE_SAMPLING, 'true')
  const explicit = { GRAPHICS_PROFILE: 'standard' }
  assert.equal({ ...explicit, ...store.backendEnvironment(explicit) }.GRAPHICS_PROFILE, 'standard')
  assert.equal(store.snapshot({ GRAPHICS_PROFILE: 'standard' }).locked.GRAPHICS_PROFILE, true)
  store.markApplied(environment, saved.pendingRevisions)
  assert.equal(store.snapshot({}).restartRequired, false)
})

test('desktop graphics validation matches existing render bounds', t => {
  const store = fixture(t)
  for (const [key, value] of [
    ['GRAPHICS_PROFILE', 'fast'], ['RENDER_MAX_FPS', '9'], ['RENDER_MAX_FPS', '241'],
    ['RENDER_MAX_FPS', '30.5'], ['RENDER_MAX_FPS', 'NaN'],
    ['RENDER_MAX_RESOLUTION', '0'], ['RENDER_MAX_RESOLUTION', '4.1'],
    ['RENDER_MAX_RESOLUTION', 'Infinity'], ['RENDER_TEXTURE_SAMPLING', 'yes'],
  ]) assert.throws(() => store.update({}, { values: { [key]: value } }))
  for (const [fps, density] of [['10', '0.25'], ['240', '4']]) {
    store.update({}, { values: { RENDER_MAX_FPS: fps, RENDER_MAX_RESOLUTION: density } })
  }
})

test('preset changes preserve custom values without enabling experimental sampling', t => {
  const store = fixture(t)
  store.update({}, { values: { GRAPHICS_PROFILE: 'custom', RENDER_MAX_FPS: '48', RENDER_MAX_RESOLUTION: '1.25' } })
  const preset = store.update({}, { values: { GRAPHICS_PROFILE: 'power_saving' } })
  let [budget, sampling] = buildGraphicsConfiguration(undefined, preset)
  assert.deepEqual(budget.fields.map(field => field.key), ['GRAPHICS_PROFILE'])
  assert.equal(sampling.fields[0].value, false)
  const custom = store.update({}, { values: { GRAPHICS_PROFILE: 'custom' } })
  ;[budget, sampling] = buildGraphicsConfiguration(undefined, custom)
  assert.equal(budget.fields.find(field => field.key === 'RENDER_MAX_FPS').value, '48')
  assert.equal(budget.fields.find(field => field.key === 'RENDER_MAX_RESOLUTION').value, '1.25')
  assert.ok(budget.fields.every(field => field.restart_required))
})

test('saved choices do not overwrite the separately reported running renderer budget', () => {
  const runtime = Object.freeze({ profile: 'standard', custom_max_fps: 30, custom_max_resolution: 1.5,
    texture_sampling: false, effective_max_fps: 60, effective_max_resolution: null })
  const [budget] = buildGraphicsConfiguration(runtime, { values: { GRAPHICS_PROFILE: 'custom', RENDER_MAX_FPS: '24' } })
  assert.equal(budget.fields.find(field => field.key === 'RENDER_MAX_FPS').value, '24')
  assert.equal(runtime.effective_max_fps, 60)
  assert.equal(runtime.effective_max_resolution, null)
  const [offline, sampling] = buildGraphicsConfiguration()
  assert.equal(offline.fields[0].value, 'standard')
  assert.equal(sampling.fields[0].value, false)
})
