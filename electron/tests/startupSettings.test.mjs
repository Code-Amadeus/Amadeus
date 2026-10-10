import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'
const { startupValue, startupValues, projectStartupFields } = loadTypeScript(new URL('../src/shared/startupSettings.ts', import.meta.url))
const { catalogConfiguration } = loadTypeScript(new URL('../src/shared/configCatalog.ts', import.meta.url))
const { DesktopSettingsStore } = loadTypeScript(new URL('../src/main/desktopSettings.ts', import.meta.url), {
  electron: { safeStorage: { isEncryptionAvailable: () => true, encryptString: value => Buffer.from(value) } },
})

test('environment source, lock and displayed model agree even with an older saved model', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'startup-projection-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  const store = new DesktopSettingsStore(path.join(dir, 'settings.json'), path.join(dir, '.env'))
  store.update({}, { values: { MIMO_TTS_MODEL: 'saved-model' }, secrets: { MIMO_TTS_API_KEY: 'private-stored' } })
  const snapshot = store.snapshot({ MIMO_TTS_MODEL: 'environment-model', MIMO_TTS_API_KEY: '' })
  assert.equal(snapshot.sources.MIMO_TTS_MODEL, 'environment')
  assert.equal(snapshot.locked.MIMO_TTS_MODEL, true)
  assert.equal(catalogConfiguration('tts_mimo', snapshot).fields.find(field => field.key === 'MIMO_TTS_MODEL').value, 'environment-model')
  assert.equal(snapshot.secrets.MIMO_TTS_API_KEY.configured, false)
  assert.ok(!('MIMO_TTS_API_KEY' in snapshot.startupValues))
  assert.ok(!JSON.stringify(snapshot).includes('private-stored'))
})

test('dotenv is explicitly unknown offline and resolved after connection; clearing does not resurrect stale runtime values', () => {
  const base = [{ key: 'MIMO_TTS_MODEL', type: 'text', value: 'default-model' }]
  const running = [{ ...base[0], value: 'dotenv-model' }]
  const snapshot = { values: {}, sources: { MIMO_TTS_MODEL: 'dotenv' } }
  assert.equal(projectStartupFields(base, undefined, snapshot)[0].value, undefined)
  assert.equal(projectStartupFields(base, running, snapshot)[0].value, 'dotenv-model')
  const pending = { ...snapshot, pendingRevisions: { MIMO_TTS_MODEL: 4 } }
  assert.equal(projectStartupFields(base, running, pending)[0].value, undefined)
  assert.equal(projectStartupFields(base, running, { ...pending, sources: { MIMO_TTS_MODEL: 'default' } })[0].value, 'default-model')
})

test('pending user overrides, explicit empty and false survive projection', () => {
  const snapshot = { startupValues: { MODEL: '', ENABLED: 'false' }, sources: { MODEL: 'environment', ENABLED: 'user' }, pendingRevisions: { ENABLED: 8 } }
  const result = projectStartupFields([{ key: 'MODEL', type: 'text', value: 'default' }, { key: 'ENABLED', type: 'boolean', value: true }], [{ key: 'ENABLED', type: 'boolean', value: true }], snapshot)
  assert.equal(result[0].value, '')
  assert.equal(result[1].value, false)
  assert.equal(startupValue('MODEL', snapshot, 'default'), '')
  assert.deepEqual(startupValues({ ...snapshot, values: { MODEL: 'old' } }), { MODEL: '', ENABLED: 'false' })
})
