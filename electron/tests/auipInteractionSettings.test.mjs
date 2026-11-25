import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const key = 'AUIP_INTERACTION_ENABLED'

test('AI application interaction defaults on and is a live, desktop-persisted setting', () => {
  const { catalogApplication, desktopCatalogFields, runtimeCatalogFields } = loadTypeScript(new URL('../src/shared/configCatalog.ts', import.meta.url))
  assert.equal(desktopCatalogFields[key]?.default, true)
  assert.equal(runtimeCatalogFields.auip_interaction_enabled?.key, key)
  assert.equal(catalogApplication(key), 'host')
})

test('the AI application interaction choice survives desktop settings reloads', t => {
  const { DesktopSettingsStore } = loadTypeScript(new URL('../src/main/desktopSettings.ts', import.meta.url), {
    electron: { safeStorage: { isEncryptionAvailable: () => false } },
  })
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-auip-interaction-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  const open = () => new DesktopSettingsStore(path.join(directory, 'settings.json'), path.join(directory, '.env'))
  assert.equal(open().backendEnvironment({})[key], undefined)
  for (const value of [false, true]) {
    open().update({}, { values: { [key]: value } })
    assert.equal(open().backendEnvironment({})[key], String(value))
  }
  assert.throws(() => open().update({}, { values: { [key]: 'sometimes' } }))
})
