import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import ts from 'typescript'

const source = fs.readFileSync(new URL('../src/renderer/components/modelRoleCatalog.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const exports = {}
new Function('require', 'exports', compiled)(() => ({}), exports)

test('role catalog stays configurable while the backend is offline', () => {
  const roles = exports.buildModelRoleCatalog({ values: {} })
  assert.deepEqual(roles.map(role => role.id), [
    'vn_companion', 'work_planner', 'work_observer', 'browser_branch_planner',
    'auip_action', 'auip_narration', 'vn_subtitle_translation', 'vn_speech_translation',
  ])
  const vn = roles.find(role => role.id === 'vn_companion')
  const provider = vn.fields.find(field => field.key === 'VN_LLM_PROVIDER')
  assert.equal(provider.value, 'deepseek')
  assert.deepEqual(provider.options.map(option => option.value), ['deepseek', 'openai'])
  assert.equal(vn.status, 'Backend status unavailable')
})

test('saved role selections replace recommendations without changing the catalog', () => {
  const roles = exports.buildModelRoleCatalog({ values: {
    VN_LLM_PROVIDER: 'openai',
    VN_LLM_MODEL: 'gpt-vn',
    VN_SUBTITLE_TRANSLATE_PROVIDER: 'openai',
  } })
  const vn = roles.find(role => role.id === 'vn_companion')
  assert.equal(vn.fields.find(field => field.key === 'VN_LLM_PROVIDER').value, 'openai')
  assert.equal(vn.fields.find(field => field.key === 'VN_LLM_MODEL').value, 'gpt-vn')
  assert.equal(roles.find(role => role.id === 'vn_subtitle_translation').fields[0].value, 'openai')
})
