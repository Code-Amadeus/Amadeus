import assert from 'node:assert/strict'
import test from 'node:test'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'
const exports = loadTypeScript(new URL('../src/renderer/components/modelConnectionCatalog.ts', import.meta.url))

test('remote model services remain discoverable before credentials exist', () => {
  const groups = exports.buildRemoteModelConnectionCatalog('deepseek', null)
  assert.deepEqual(groups.map(group => group.id), ['deepseek', 'openai', 'gemini', 'bedrock'])
  assert.equal(groups.find(group => group.id === 'deepseek').active, true)
  assert.equal(groups.find(group => group.id === 'deepseek').configured, false)
  assert.equal(groups.find(group => group.id === 'deepseek').status, 'Needs setup')
  assert.equal(groups.find(group => group.id === 'openai').status, 'Optional')
  assert.ok(groups.every(group => group.fields.length > 0))
})

test('desktop credential state changes status without changing the provider catalog', () => {
  const groups = exports.buildRemoteModelConnectionCatalog('hybrid3', {
    values: { OPENAI_BASE_URL: 'https://example.invalid/v1' },
    secrets: { OPENAI_API_KEY: { configured: true } },
  })
  const openai = groups.find(group => group.id === 'openai')
  assert.equal(openai.active, true)
  assert.equal(openai.configured, true)
  assert.equal(openai.fields.find(item => item.key === 'OPENAI_BASE_URL').value, 'https://example.invalid/v1')
})

test('local, hybrid and RAG entries remain discoverable while the backend is offline', () => {
  const local = exports.buildLocalModelConnectionCatalog('hybrid2', null)
  const optional = exports.buildOptionalModelServiceCatalog(null)
  assert.deepEqual(local.map(group => group.id), ['local', 'hybrid_local'])
  assert.equal(local.find(group => group.id === 'hybrid_local').active, true)
  assert.ok(local.find(group => group.id === 'local').fields.some(item => item.key === 'LOCAL_LLM_TYPE'))
  assert.deepEqual(optional.map(group => group.id), ['character_rag'])
  assert.ok(optional[0].fields.some(item => item.key === 'RAG_INDEX_DIR'))
})

test('model inheritance and local engine selection retain independent saved inputs', () => {
  const snapshot = { values: { LOCAL_LLM_URL: 'http://localhost:9999/v1', LOCAL_LLM_MODEL: 'local-model', LOCAL_LLM_TYPE: 'ollama', LOCAL_LLM_CLI_PATH: 'kept-cli' } }
  const groups = exports.buildLocalModelConnectionCatalog('hybrid3', snapshot)
  const hybrid = groups.find(group => group.id === 'hybrid_local')
  assert.equal(hybrid.fields.find(field => field.key === 'HYBRID_LOCAL_LLM_URL').value, snapshot.values.LOCAL_LLM_URL)
  assert.equal(hybrid.fields.find(field => field.key === 'HYBRID_LOCAL_LLM_MODEL').value, 'local-model')
  assert.ok(groups[0].fields.some(field => field.key === 'LOCAL_LLM_OLLAMA_URL'))
  assert.ok(!groups[0].fields.some(field => field.key === 'LOCAL_LLM_CLI_PATH'))
  const changed = exports.buildLocalModelConnectionCatalog('local', { values: { ...snapshot.values, LOCAL_LLM_TYPE: 'cli', HYBRID_LOCAL_LLM_MODEL: '' } })
  assert.equal(changed[0].fields.find(field => field.key === 'LOCAL_LLM_CLI_PATH').value, 'kept-cli')
  assert.equal(changed[1].fields.find(field => field.key === 'HYBRID_LOCAL_LLM_MODEL').value, '')
})
