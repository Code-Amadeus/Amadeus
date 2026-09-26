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
    'speech_synthesis', 'tts_embedded_v3', 'voice_reference_profile', 'tts_fish_audio', 'tts_remote', 'tts_mimo',
  ])
  assert.equal(groups.find(group => group.id === 'asr_remote').status, 'Optional')
  assert.equal(groups.find(group => group.id === 'tts_remote').status, 'Optional')
  assert.equal(groups.find(group => group.id === 'tts_fish_audio').status, 'Optional')
  assert.ok(groups.find(group => group.id === 'conversation_asr').fields.some(field => field.key === 'ASR_BACKEND'))
  assert.ok(groups.find(group => group.id === 'speech_synthesis').fields.some(field => field.key === 'TTS_BACKEND'))
  const profile = groups.find(group => group.id === 'tts_embedded_v3').fields.find(field => field.key === 'TTS_VOICE_PROFILE')
  assert.deepEqual(profile.options.map(option => option.value), ['kurisu_v3', 'kurisu_v2pro', 'custom'])
})

test('Fish Audio is selectable offline and exposes its native WebSocket settings', () => {
  const groups = exports.buildVoiceConfigurationCatalog({
    asrBackend: 'qwen3_asr', ttsBackend: 'fish_audio', wakeEnabled: false, aecEnabled: true,
  }, null)
  const selector = groups.find(group => group.id === 'speech_synthesis').fields.find(field => field.key === 'TTS_BACKEND')
  assert.equal(selector.value, 'fish_audio')
  assert.ok(selector.options.some(option => option.value === 'fish_audio' && option.label === 'Fish Audio'))
  const fish = groups.find(group => group.id === 'tts_fish_audio')
  assert.equal(fish.active, true)
  assert.equal(fish.configured, false)
  assert.equal(fish.status, 'Needs setup')
  assert.deepEqual(fish.fields.map(field => field.key), [
    'FISH_TTS_WS_URL', 'FISH_TTS_API_KEY', 'FISH_TTS_MODEL', 'FISH_TTS_REFERENCE_ID', 'FISH_TTS_LATENCY',
  ])
  assert.equal(fish.fields.find(field => field.key === 'FISH_TTS_WS_URL').value, 'wss://api.fish.audio/v1/tts/live')
  assert.equal(fish.fields.find(field => field.key === 'FISH_TTS_API_KEY').type, 'secret')
  assert.deepEqual(fish.fields.find(field => field.key === 'FISH_TTS_LATENCY').options, ['normal', 'balanced', 'low'])
})

test('saved Fish selection uses its own credentials and retains custom model and voice', () => {
  const groups = exports.buildVoiceConfigurationCatalog({
    asrBackend: 'qwen3_asr', ttsBackend: 'gpt_sovits', wakeEnabled: false, aecEnabled: true,
  }, {
    values: { TTS_BACKEND: 'fish_audio', FISH_TTS_MODEL: 'custom-engine', FISH_TTS_REFERENCE_ID: 'custom-voice', FISH_TTS_LATENCY: 'low' },
    secrets: { FISH_TTS_API_KEY: { configured: true } },
  })
  const fish = groups.find(group => group.id === 'tts_fish_audio')
  assert.equal(fish.active, true)
  assert.equal(fish.configured, true)
  assert.equal(fish.status, 'Backend status unavailable')
  assert.equal(fish.fields.find(field => field.key === 'FISH_TTS_MODEL').value, 'custom-engine')
  assert.equal(fish.fields.find(field => field.key === 'FISH_TTS_REFERENCE_ID').value, 'custom-voice')
  assert.equal(fish.fields.find(field => field.key === 'FISH_TTS_LATENCY').value, 'low')
  assert.equal(fish.fields.find(field => field.key === 'FISH_TTS_API_KEY').value, '')
  assert.equal(groups.find(group => group.id === 'tts_mimo').status, 'Optional')
  assert.equal(groups.find(group => group.id === 'tts_remote').status, 'Optional')
})

test('selected remote voice services request only their own credentials', () => {
  const groups = exports.buildVoiceConfigurationCatalog({
    asrBackend: 'openai_compatible', ttsBackend: 'mimo', wakeEnabled: false, aecEnabled: true,
  }, { secrets: {} })
  assert.equal(groups.find(group => group.id === 'asr_remote').status, 'Needs setup')
  assert.equal(groups.find(group => group.id === 'tts_mimo').status, 'Needs setup')
  assert.equal(groups.find(group => group.id === 'tts_remote').status, 'Optional')
})
