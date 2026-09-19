import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import ts from 'typescript'

const source = fs.readFileSync(new URL('../src/renderer/components/voiceConfigurationCatalog.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const exports = {}
new Function('exports', compiled)(exports)

test('voice setup remains discoverable without a running backend', () => {
  const groups = exports.buildVoiceConfigurationCatalog({
    asrBackend: 'qwen3_asr', ttsBackend: 'gpt_sovits', wakeEnabled: false, aecEnabled: true,
  }, null)
  assert.deepEqual(groups.map(group => group.id), [
    'conversation_asr', 'asr_remote', 'wake_asr', 'acoustic_pipeline',
    'speech_synthesis', 'tts_embedded_v3', 'voice_reference_profile', 'tts_remote', 'tts_mimo',
  ])
  assert.equal(groups.find(group => group.id === 'asr_remote').status, 'Optional')
  assert.equal(groups.find(group => group.id === 'tts_remote').status, 'Optional')
  assert.ok(groups.find(group => group.id === 'conversation_asr').fields.some(field => field.key === 'ASR_BACKEND'))
  assert.ok(groups.find(group => group.id === 'speech_synthesis').fields.some(field => field.key === 'TTS_BACKEND'))
})

test('selected remote voice services request only their own credentials', () => {
  const groups = exports.buildVoiceConfigurationCatalog({
    asrBackend: 'openai_compatible', ttsBackend: 'mimo', wakeEnabled: false, aecEnabled: true,
  }, { secrets: {} })
  assert.equal(groups.find(group => group.id === 'asr_remote').status, 'Needs setup')
  assert.equal(groups.find(group => group.id === 'tts_mimo').status, 'Needs setup')
  assert.equal(groups.find(group => group.id === 'tts_remote').status, 'Optional')
})
