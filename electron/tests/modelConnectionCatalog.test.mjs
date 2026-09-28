import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import ts from 'typescript'

const source = fs.readFileSync(new URL('../src/renderer/components/modelConnectionCatalog.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const exports = {}
new Function('exports', compiled)(exports)

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
