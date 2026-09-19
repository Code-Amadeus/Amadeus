import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = fs.readFileSync(new URL('../src/main/desktopSettings.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText
const exports = {}
new Function('require', 'exports', compiled)(name => name === 'electron'
  ? { safeStorage: { isEncryptionAvailable: () => false } }
  : require(name), exports)

test('advanced role overrides persist and reach the next backend launch', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-model-roles-'))
  try {
    const store = new exports.DesktopSettingsStore(path.join(root, 'settings.json'), path.join(root, '.env'))
    const values = {
      COOPERATIVE_WORK_PLANNER_MODEL: 'planner-model',
      BROWSER_BRANCH_PROVIDER: 'openai',
      BROWSER_BRANCH_MODEL: 'browser-model',
      VN_LLM_PROVIDER: 'openai',
      VN_LLM_MODEL: 'vn-model',
      VN_SUBTITLE_TRANSLATE_PROVIDER: 'deepseek',
      VN_SUBTITLE_TRANSLATE_MODEL: 'subtitle-model',
      VN_TTS_TRANSLATE_PROVIDER: 'openai',
      VN_TTS_TRANSLATE_MODEL: 'speech-model',
    }
    store.update({}, { values })
    const environment = new exports.DesktopSettingsStore(path.join(root, 'settings.json'), path.join(root, '.env')).backendEnvironment({})
    for (const [key, value] of Object.entries(values)) assert.equal(environment[key], value)
    assert.throws(() => store.update({}, { values: { BROWSER_BRANCH_PROVIDER: 'gemini' } }), /Invalid value/)
    assert.throws(() => store.update({}, { values: { VN_LLM_PROVIDER: 'gemini' } }), /Invalid value/)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})
