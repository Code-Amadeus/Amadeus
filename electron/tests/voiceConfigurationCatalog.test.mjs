import assert from 'node:assert/strict'
import test from 'node:test'
import { loadTypeScript } from './helpers/loadTypeScript.mjs'

const exports = loadTypeScript(new URL('../src/renderer/components/voiceConfigurationCatalog.ts', import.meta.url))
const { voiceBackendGroups } = loadTypeScript(new URL('../src/shared/configCatalog.ts', import.meta.url))

test('voice setup remains discoverable without a running backend', () => {
  const groups = exports.buildVoiceConfigurationCatalog({
    asrBackend: 'qwen3_asr', ttsBackend: 'gpt_sovits', wakeEnabled: false, aecEnabled: true,
  }, null)
  assert.deepEqual(new Set(groups.map(group => group.id)), new Set([
    'conversation_asr', 'asr_remote', 'wake_asr', 'acoustic_pipeline',
    'speech_synthesis', 'voice_reference_profile', 'tts_emotion_references', 'tts_japanese_dictionary', 'tts_japanese_reading',
    ...voiceBackendGroups.map(group => group.id),
  ]))
  assert.equal(groups.find(group => group.id === 'asr_remote').status, 'Optional')
  assert.equal(groups.find(group => group.id === 'speech_synthesis').description,
    'Select the shared TTS implementation used by Chat, Wallpaper, and VN speech policies.')
  assert.equal(groups.find(group => group.id === 'tts_embedded_v3').description,
    'Choose a compatible voice profile or provide a custom GPT/SoVITS checkpoint pair.')
  assert.equal(groups.find(group => group.id === 'tts_remote').status, 'Optional')
  assert.equal(groups.find(group => group.id === 'tts_remote').fields.find(field => field.key === 'TTS_API_STREAM_PROTOCOL').description, undefined)
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
  assert.deepEqual(selector.options.map(option => option.value), ['gpt_sovits', 'fish_audio', 'openai_compatible', 'mimo', 'disabled'])
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


test('emotion reference switch is default off, accepts the existing 1 value and requires restart', () => {
  const selection = { asrBackend: 'qwen3_asr', ttsBackend: 'gpt_sovits', wakeEnabled: false, aecEnabled: true }
  for (const value of [undefined, '1', 'true', 'false']) {
    const snapshot = { values: value === undefined ? {} : { ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING: value } }
    const group = exports.buildVoiceConfigurationCatalog(selection, snapshot).find(group => group.id === 'tts_emotion_references')
    assert.equal(group.fields[0].value, value === '1' || value === 'true')
    assert.equal(group.fields[0].restart_required, true)
    assert.equal(group.fields[0].type, 'boolean')
  }
})

test('Japanese dictionaries are optional and can be disabled with a restart', () => {
  const selection = { asrBackend: 'qwen3_asr', ttsBackend: 'gpt_sovits', wakeEnabled: false, aecEnabled: false }
  const field = value => exports.buildVoiceConfigurationCatalog(selection, { values: value }).find(group => group.id === 'tts_japanese_dictionary')
  assert.equal(field({}).fields[0].value, true)
  const disabled = field({ ENABLE_JAPANESE_USER_DICTIONARY: 'false' })
  assert.equal(disabled.active, false)
  assert.equal(disabled.status, 'Off')
  assert.equal(disabled.fields[0].restart_required, true)
  assert.equal(exports.voiceConfigurationSections.output.has('tts_japanese_dictionary'), true)
  const effective = exports.buildVoiceConfigurationCatalog({ ...selection, japaneseDictionaryEnabled: false }, null).find(group => group.id === 'tts_japanese_dictionary')
  assert.equal(effective.fields[0].value, false)
  assert.equal(effective.active, false)
  const english = exports.buildVoiceConfigurationCatalog(selection, { values: { TTS_OUTPUT_LANGUAGE: '英文' } }).find(group => group.id === 'tts_japanese_dictionary')
  assert.equal(english.active, false)
  const switched = exports.buildVoiceConfigurationCatalog({ ...selection, ttsOutputLanguage: 'en' }, { values: { TTS_OUTPUT_LANGUAGE: '日文' } }).find(group => group.id === 'tts_japanese_dictionary')
  assert.equal(switched.active, false)
})

test('Japanese context reading follows the saved switch and output language', () => {
  const selection = { asrBackend: 'qwen3_asr', ttsBackend: 'gpt_sovits', wakeEnabled: false, aecEnabled: false, ttsOutputLanguage: 'ja' }
  const group = (selected, values = {}) => exports.buildVoiceConfigurationCatalog(selected, { values }).find(item => item.id === 'tts_japanese_reading')
  assert.equal(group(selection).fields[0].value, true)
  assert.equal(group(selection, { ENABLE_JAPANESE_CONTEXT_READING: 'false' }).active, false)
  assert.equal(group({ ...selection, ttsOutputLanguage: 'en' }).active, false)
  assert.equal(group({ ...selection, ttsBackend: 'remote' }).active, false)
  assert.equal(group({ ...selection, japaneseReadingEnabled: false }).fields[0].value, false)
})

test('effective dotenv emotion setting is visible until a stored override changes it', () => {
  const selection = { asrBackend: 'qwen3_asr', ttsBackend: 'gpt_sovits', wakeEnabled: false, aecEnabled: true, emotionReferencesEnabled: true }
  const field = snapshot => exports.buildVoiceConfigurationCatalog(selection, snapshot).find(group => group.id === 'tts_emotion_references').fields[0]
  assert.equal(field(null).value, true)
  assert.equal(field({ values: { ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING: 'false' } }).value, false)
})

test('voice activation follows the same parsed startup booleans as its controls', () => {
  const selection = { asrBackend: 'qwen3_asr', ttsBackend: 'gpt_sovits', wakeEnabled: false, aecEnabled: false }
  const groups = exports.buildVoiceConfigurationCatalog(selection, { startupValues: {
    WAKE_ENABLED: ' YES ', AEC_REALTIME_ENABLED: '1',
  }, sources: { WAKE_ENABLED: 'environment', AEC_REALTIME_ENABLED: 'environment' } })
  for (const [id, key] of [['wake_asr', 'WAKE_ENABLED'], ['acoustic_pipeline', 'AEC_REALTIME_ENABLED']]) {
    const group = groups.find(group => group.id === id)
    assert.equal(group.fields.find(field => field.key === key).value, true)
    assert.equal(group.active, true)
    assert.notEqual(group.status, 'Off')
  }
  const cleared = exports.buildVoiceConfigurationCatalog({ ...selection, wakeEnabled: true }, {
    sources: { WAKE_ENABLED: 'default' }, pendingRevisions: { WAKE_ENABLED: 4 },
  }).find(group => group.id === 'wake_asr')
  assert.equal(cleared.fields[0].value, false)
  assert.equal(cleared.active, false)
  assert.equal(cleared.status, 'Off')
})
