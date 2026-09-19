import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import ts from 'typescript'

const source = fs.readFileSync(new URL('../src/renderer/components/providerConnectionCatalog.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const exports = {}
new Function('exports', compiled)(exports)

test('built-in Work Provider connections remain discoverable without the backend', () => {
  const catalog = exports.buildWorkProviderCatalog({ provider: 'codex', enabled: true }, null)
  assert.deepEqual(catalog.connections.map(group => group.id), ['browser', 'openclaw', 'codex'])
  assert.equal(catalog.connections.find(group => group.id === 'codex').active, true)
  assert.equal(catalog.connections.find(group => group.id === 'openclaw').status, 'Optional')
  assert.ok(catalog.routing.fields.some(field => field.key === 'COOPERATIVE_CHAT_PROVIDER'))
})

test('OpenClaw is the only connection requiring setup when selected without a token', () => {
  const catalog = exports.buildWorkProviderCatalog({ provider: 'openclaw', enabled: true }, { secrets: {} })
  assert.equal(catalog.connections.find(group => group.id === 'openclaw').status, 'Needs setup')
  assert.equal(catalog.connections.find(group => group.id === 'codex').status, 'Optional')
  assert.equal(catalog.connections.find(group => group.id === 'browser').status, 'Optional')
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
