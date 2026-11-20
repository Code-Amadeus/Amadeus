import assert from 'node:assert/strict'
import test from 'node:test'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'
const exports = loadTypeScript(new URL('../src/renderer/components/providerConnectionCatalog.ts', import.meta.url))

test('built-in Work Provider connections remain discoverable without the backend', () => {
  const catalog = exports.buildWorkProviderCatalog({ provider: 'codex', enabled: true }, null)
  assert.deepEqual(catalog.connections.map(group => group.id), ['pi', 'browser', 'openclaw', 'codex'])
  assert.equal(catalog.connections.find(group => group.id === 'codex').active, true)
  assert.equal(catalog.connections.find(group => group.id === 'openclaw').status, 'Optional')
  assert.ok(catalog.routing.fields.some(field => field.key === 'WORK_CODING_PROVIDER'))
  assert.ok(catalog.routing.fields.some(field => field.key === 'WORK_EXECUTION_PROVIDER'))
  assert.ok(!catalog.routing.fields.some(field => field.key === 'COOPERATIVE_CHAT_ENABLED'))
})

test('a stored retired Off value does not disable role assignments or Provider setup', () => {
  const catalog = exports.buildWorkProviderCatalog({ provider: 'pi' }, {
    values: { COOPERATIVE_CHAT_ENABLED: 'false' },
    secrets: { DEEPSEEK_API_KEY: { configured: true } },
  })
  assert.equal(catalog.routing.active, true)
  assert.equal(catalog.connections.find(group => group.id === 'pi').active, true)
  assert.equal(catalog.connections.find(group => group.id === 'pi').configured, true)
  assert.equal(catalog.connections.find(group => group.id === 'pi').status, 'Backend status unavailable')
  assert.ok(!catalog.routing.fields.some(field => field.key === 'COOPERATIVE_CHAT_ENABLED'))
})

test('new daily-agent defaults select Pi and leave OpenClaw optional', () => {
  const catalog = exports.buildWorkProviderCatalog({ provider: '', enabled: true }, null)
  assert.equal(catalog.connections.find(group => group.id === 'pi').active, true)
  assert.equal(catalog.connections.find(group => group.id === 'openclaw').active, false)
  assert.equal(catalog.connections.find(group => group.id === 'openclaw').status, 'Optional')
})

test('default Pi setup reuses the Models credential instead of requiring another agent login', () => {
  const missing = exports.buildWorkProviderCatalog({ provider: 'pi', enabled: true }, {
    values: { PI_MODEL_PROVIDER: 'deepseek' }, secrets: {},
  }).connections.find(group => group.id === 'pi')
  assert.equal(missing.configured, false)
  assert.equal(missing.status, 'Needs setup')
  assert.match(missing.description, /reuse credentials configured in Models/i)

  const ready = exports.buildWorkProviderCatalog({ provider: 'pi', enabled: true }, {
    values: { PI_MODEL_PROVIDER: 'deepseek' },
    secrets: { DEEPSEEK_API_KEY: { configured: true } },
  }).connections.find(group => group.id === 'pi')
  assert.equal(ready.configured, true)
  assert.notEqual(ready.status, 'Needs setup')
})

test('an execution assignment does not remove the separate coding assignment', () => {
  const catalog = exports.buildWorkProviderCatalog({ provider: 'openclaw', enabled: true }, { secrets: {} })
  assert.equal(catalog.connections.find(group => group.id === 'openclaw').status, 'Needs setup')
  assert.equal(catalog.connections.find(group => group.id === 'codex').active, true)
  assert.equal(catalog.connections.find(group => group.id === 'browser').status, 'Optional')
})

test('role dropdowns follow registered compatible candidates and persist independent choices', () => {
  const catalog = exports.buildWorkProviderCatalog({ provider: 'pi', enabled: true,
    roleCandidates: { coding: ['codex', 'pi', 'custom-agent'], execution: ['pi', 'codex', 'openclaw', 'custom-agent'] } },
    { values: { WORK_CODING_PROVIDER: 'custom-agent', WORK_EXECUTION_PROVIDER: 'openclaw' } })
  const coding = catalog.routing.fields.find(field => field.key === 'WORK_CODING_PROVIDER')
  const execution = catalog.routing.fields.find(field => field.key === 'WORK_EXECUTION_PROVIDER')
  assert.equal(coding.value, 'custom-agent')
  assert.equal(execution.value, 'openclaw')
  assert.ok(coding.options.some(option => option.value === 'custom-agent'))
  assert.ok(!coding.options.some(option => option.value === 'openclaw'))
  assert.equal(catalog.connections.find(group => group.id === 'pi').active, false)
  assert.equal(catalog.connections.find(group => group.id === 'codex').active, false)
  assert.equal(catalog.connections.find(group => group.id === 'openclaw').active, true)
})

test('Codex separates ChatGPT subscription auth from reusable model API connections', () => {
  const subscription = exports.buildWorkProviderCatalog({ provider: 'codex', enabled: true }, {
    values: { CODEX_APP_SERVER_AUTH_MODE: 'chatgpt' }, secrets: {},
  }).connections.find(group => group.id === 'codex')
  assert.equal(subscription.status, 'Needs Codex login')
  assert.ok(subscription.fields.some(field => field.key === 'CODEX_APP_SERVER_CHATGPT_MODEL'))
  assert.ok(!subscription.fields.some(field => field.key === 'CODEX_APP_SERVER_MODEL_PROVIDER'))

  const api = exports.buildWorkProviderCatalog({ provider: 'codex', enabled: true }, {
    values: { CODEX_APP_SERVER_AUTH_MODE: 'model_api', CODEX_APP_SERVER_MODEL_PROVIDER: 'openai' },
    secrets: { OPENAI_API_KEY: { configured: true } },
  }).connections.find(group => group.id === 'codex')
  assert.notEqual(api.status, 'Needs setup')
  assert.ok(api.fields.find(field => field.key === 'CODEX_APP_SERVER_MODEL_PROVIDER').options.some(option => option.value === 'openai'))
})

test('Codex uses resolved dotenv authentication to select subscription controls', () => {
  const snapshot = { sources: { CODEX_PROVIDER_TRANSPORT: 'dotenv', CODEX_APP_SERVER_AUTH_MODE: 'dotenv' } }
  const group = exports.buildWorkProviderCatalog({ provider: 'codex' }, snapshot, {
    CODEX_PROVIDER_TRANSPORT: 'app_server', CODEX_APP_SERVER_AUTH_MODE: 'chatgpt',
  }).connections.find(group => group.id === 'codex')
  assert.ok(group.fields.some(field => field.key === 'CODEX_APP_SERVER_CHATGPT_MODEL'))
  assert.ok(!group.fields.some(field => field.key === 'CODEX_APP_SERVER_MODEL_PROVIDER'))
  assert.equal(group.fields.find(field => field.key === 'CODEX_APP_SERVER_AUTH_MODE').value, 'chatgpt')
})

test('Codex inherited model follows the planned connection instead of the running model', () => {
  const snapshot = { values: { OPENAI_MODEL_NAME: 'next-model', CODEX_APP_SERVER_MODEL_PROVIDER: 'openai' }, sources: {
    OPENAI_MODEL_NAME: 'user', CODEX_APP_SERVER_MODEL_PROVIDER: 'user', CODEX_APP_SERVER_MODEL: 'default',
  }, pendingRevisions: { OPENAI_MODEL_NAME: 3 }, secrets: { OPENAI_API_KEY: { configured: true } } }
  const group = exports.buildWorkProviderCatalog({ provider: 'codex' }, snapshot, {
    OPENAI_MODEL_NAME: 'old-model', CODEX_APP_SERVER_MODEL: 'old-model',
  }).connections.find(group => group.id === 'codex')
  assert.equal(group.fields.find(field => field.key === 'CODEX_APP_SERVER_MODEL').value, 'next-model')
})
