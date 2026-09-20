import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = fs.readFileSync(new URL('../src/main/desktopSettings.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText
const exports = {}
new Function('require', 'exports', compiled)(name => name === 'electron'
  ? { safeStorage: {
      isEncryptionAvailable: () => true,
      encryptString: value => Buffer.from(`encrypted:${value}`),
      decryptString: buffer => buffer.toString().slice(10),
    } }
  : require(name), exports)

test('Pi startup selection persists into the existing backend environment', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-pi-settings-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const store = new exports.DesktopSettingsStore(path.join(root, 'settings.json'), path.join(root, '.env'))
  store.update({}, { values: { COOPERATIVE_CHAT_PROVIDER: 'pi', PI_PROVIDER_ENABLED: true,
    PI_MODEL_PROVIDER: 'deepseek', PI_MODEL: 'daily-model', PI_NODE_PATH: 'node' } })
  const env = store.backendEnvironment({})
  assert.equal(env.COOPERATIVE_CHAT_PROVIDER, 'pi')
  assert.equal(env.PI_PROVIDER_ENABLED, 'true')
  assert.equal(env.PI_MODEL, 'daily-model')
})

test('a blank desktop install can persist a complete model selection for the next backend', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-model-connections-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const file = path.join(root, 'settings.json')
  const dotenv = path.join(root, '.env')
  const store = new exports.DesktopSettingsStore(file, dotenv)
  store.update({}, {
    values: {
      LLM_PROVIDER: 'hybrid3',
      OPENAI_BASE_URL: 'https://api.openai.com/v1',
      OPENAI_MODEL_NAME: 'gpt-5.4-mini',
      HYBRID_LOCAL_LLM_URL: 'http://127.0.0.1:8080/v1',
      HYBRID_LOCAL_LLM_MODEL: 'local-head',
      RAG_ENABLED: true,
      RAG_INDEX_DIR: '.amadeus/character-rag',
      COOPERATIVE_CHAT_ENABLED: true,
      COOPERATIVE_CHAT_PROVIDER: 'codex',
      CODEX_PROVIDER_TRANSPORT: 'app_server',
      CODEX_APP_SERVER_AUTH_MODE: 'chatgpt',
      AMADEUS_VISION_ENABLED: true,
      AMADEUS_VISION_MODE: 'watching',
      AMADEUS_PRESENTATION_LOCALE: 'zh-CN',
      ENABLE_CUDA_GRAPH: '1',
      EXP_TTS_MAX_CONCURRENCY: '1',
      TTS_OUTPUT_LANGUAGE: '英文',
    },
    secrets: { OPENAI_API_KEY: 'test-api-key' },
  })

  const reloaded = new exports.DesktopSettingsStore(file, dotenv)
  const environment = reloaded.backendEnvironment({})
  assert.equal(environment.LLM_PROVIDER, 'hybrid3')
  assert.equal(environment.OPENAI_API_KEY, 'test-api-key')
  assert.equal(environment.HYBRID_LOCAL_LLM_URL, 'http://127.0.0.1:8080/v1')
  assert.equal(environment.RAG_ENABLED, 'true')
  assert.equal(environment.COOPERATIVE_CHAT_ENABLED, 'true')
  assert.equal(environment.COOPERATIVE_CHAT_PROVIDER, 'codex')
  assert.equal(environment.CODEX_APP_SERVER_PROVIDER_ENABLED, 'true')
  assert.equal(environment.DIRECT_CODEX_PROVIDER_ENABLED, 'false')
  assert.equal(environment.CODEX_APP_SERVER_AUTH_MODE, 'chatgpt')
  assert.equal(environment.AMADEUS_VISION_ENABLED, 'true')
  assert.equal(environment.AMADEUS_VISION_MODE, 'watching')
  assert.equal(environment.AMADEUS_PRESENTATION_LOCALE, 'zh-CN')
  assert.equal(environment.ENABLE_CUDA_GRAPH, '1')
  assert.equal(environment.TTS_OUTPUT_LANGUAGE, '英文')
  assert.ok(!JSON.stringify(reloaded.snapshot({})).includes('test-api-key'))
})

test('restart state tracks unapplied keys rather than all saved settings', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-settings-pending-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const store = new exports.DesktopSettingsStore(path.join(root, 'settings.json'), path.join(root, '.env'))

  let snapshot = store.update({}, { values: { AMADEUS_VISION_MODE: 'watching', LLM_PROVIDER: 'openai' } })
  assert.equal(snapshot.restartRequired, true)
  assert.deepEqual(snapshot.pendingKeys.sort(), ['AMADEUS_VISION_MODE', 'LLM_PROVIDER'])

  snapshot = store.markApplied({}, { AMADEUS_VISION_MODE: snapshot.pendingRevisions.AMADEUS_VISION_MODE })
  assert.equal(snapshot.restartRequired, true)
  assert.deepEqual(snapshot.pendingKeys, ['LLM_PROVIDER'])

  snapshot = store.markApplied({})
  assert.equal(snapshot.restartRequired, false)
  assert.deepEqual(snapshot.pendingKeys, [])
  assert.equal(snapshot.values.AMADEUS_VISION_MODE, 'watching')
})

test('a backend launch confirms only the exact setting revisions it started with', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-settings-launch-race-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const store = new exports.DesktopSettingsStore(path.join(root, 'settings.json'), path.join(root, '.env'))

  store.update({}, { values: { LLM_PROVIDER: 'openai' } })
  const launched = store.pendingRevisionSnapshot()
  store.update({}, { values: { LLM_PROVIDER: 'gemini' } })

  const snapshot = store.markApplied({}, launched)
  assert.equal(snapshot.restartRequired, true)
  assert.deepEqual(snapshot.pendingKeys, ['LLM_PROVIDER'])
  assert.equal(snapshot.values.LLM_PROVIDER, 'gemini')
  assert.notEqual(snapshot.pendingRevisions.LLM_PROVIDER, launched.LLM_PROVIDER)
})
