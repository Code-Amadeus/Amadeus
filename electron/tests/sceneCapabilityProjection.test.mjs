import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import test from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const capabilitySource = fs.readFileSync(new URL('../src/renderer/components/chatModelCapabilities.ts', import.meta.url), 'utf8')
const capabilityCompiled = ts.transpileModule(capabilitySource, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const capabilityExports = {}
new Function('require', 'exports', capabilityCompiled)(require, capabilityExports)
const source = fs.readFileSync(new URL('../src/renderer/components/sceneCapabilityProjection.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const exports = {}
new Function('require', 'exports', compiled)(
  name => name === './chatModelCapabilities' ? capabilityExports : require(name),
  exports,
)

function group(id, configured, fields = {}, extra = {}) {
  return {
    id,
    configured,
    status_ok: configured,
    fields: Object.entries(fields).map(([key, value]) => ({ key, value })),
    ...extra,
  }
}

function configuredSnapshot() {
  return {
    llm_provider: 'deepseek',
    wake_enabled: true,
    aec_realtime_enabled: true,
    aec_realtime_barge_in: true,
    wallpaper_caption_mode: 'translated',
    chat_translation_subtitles_enabled: false,
    vision_enabled: false,
    vision_mode: 'off',
    model_connections: [
      group('deepseek', true, {}, { label: 'DeepSeek' }),
      group('openai', false, {}, { label: 'OpenAI-compatible' }),
      group('gemini', false, {}, { label: 'Gemini' }),
    ],
    voice_configuration: [
      group('speech_synthesis', true, { TTS_BACKEND: 'gpt_sovits' }, { active: true }),
      group('conversation_asr', true, { ASR_BACKEND: 'qwen3_asr' }),
      group('wake_asr', true, { WAKE_ASR_BACKEND: 'sense_voice' }, { active: true }),
    ],
  }
}

test('fully configured preset capabilities make the three scene summaries available', () => {
  const profiles = exports.buildSceneCapabilityProfiles(configuredSnapshot())
  assert.deepEqual(profiles.map(profile => profile.id), ['wallpaper', 'chat', 'vn'])
  assert.equal(profiles.find(profile => profile.id === 'wallpaper').completeness, 'available')
  assert.equal(profiles.find(profile => profile.id === 'chat').completeness, 'available')
  const vn = profiles.find(profile => profile.id === 'vn')
  assert.equal(vn.completeness, 'available')
  assert.equal(vn.capabilities.find(item => item.id === 'vn_echo_cancellation').state, 'ready')
  assert.match(vn.capabilities.find(item => item.id === 'vn_echo_cancellation').detail, /barge-in interruption stays suppressed/)
})

test('missing voice implementations degrade preset scenes without making optional Chat voice a blocker', () => {
  const snapshot = configuredSnapshot()
  snapshot.voice_configuration = [
    group('speech_synthesis', false, { TTS_BACKEND: 'gpt_sovits' }, { active: true }),
    group('conversation_asr', false, { ASR_BACKEND: 'qwen3_asr' }),
    group('wake_asr', false, { WAKE_ASR_BACKEND: 'sense_voice' }, { active: true }),
  ]
  const profiles = exports.buildSceneCapabilityProfiles(snapshot)
  assert.equal(profiles.find(profile => profile.id === 'wallpaper').completeness, 'degraded')
  assert.equal(profiles.find(profile => profile.id === 'chat').completeness, 'degraded')
  assert.equal(profiles.find(profile => profile.id === 'vn').completeness, 'degraded')
  const chatAsr = profiles.find(profile => profile.id === 'chat').capabilities.find(item => item.id === 'conversation_asr')
  assert.equal(chatAsr.importance, 'optional')
})

test('missing the scene model is unavailable while disabled optional capabilities stay neutral', () => {
  const snapshot = configuredSnapshot()
  snapshot.model_connections[0].configured = false
  snapshot.model_connections[0].status_ok = false
  const profiles = exports.buildSceneCapabilityProfiles(snapshot)
  const chat = profiles.find(profile => profile.id === 'chat')
  assert.equal(chat.completeness, 'unavailable')
  assert.equal(chat.capabilities.find(item => item.id === 'chat_translation').state, 'inactive')
  assert.equal(chat.capabilities.find(item => item.id === 'chat_visual_context').stateLabel, 'Missing credentials')
})

test('shared multimodal eligibility is projected across scenes without claiming the VN runtime is wired', () => {
  const snapshot = configuredSnapshot()
  snapshot.llm_provider = 'openai'
  snapshot.model_connections[1].configured = true
  snapshot.model_connections[1].status_ok = true
  snapshot.vision_enabled = true
  snapshot.vision_mode = 'on_demand'
  snapshot.vision_scope = 'selected_window'
  const profiles = exports.buildSceneCapabilityProfiles(snapshot)
  const chatVisual = profiles.find(profile => profile.id === 'chat').capabilities.find(item => item.id === 'chat_visual_context')
  const wallpaperVisual = profiles.find(profile => profile.id === 'wallpaper').capabilities.find(item => item.id === 'wallpaper_visual_context')
  const vnVisual = profiles.find(profile => profile.id === 'vn').capabilities.find(item => item.id === 'vn_visual_context')
  assert.equal(chatVisual.state, 'ready')
  assert.equal(wallpaperVisual.state, 'ready')
  assert.equal(chatVisual.policyLabel, 'Default off')
  assert.equal(chatVisual.currentLabel, 'Currently on')
  assert.equal(wallpaperVisual.policyLabel, 'Managed by Chat')
  assert.equal(wallpaperVisual.currentLabel, 'Inherited on')
  assert.equal(vnVisual.state, 'unknown')
  assert.equal(vnVisual.stateLabel, 'Not wired')
  assert.equal(vnVisual.currentLabel, 'Runtime not connected')
  assert.match(vnVisual.implementation, /Shared main chat/)
})

test('an image-capable model remains available when visual context is default-off and currently off', () => {
  const snapshot = configuredSnapshot()
  snapshot.llm_provider = 'openai'
  snapshot.model_connections[1].configured = true
  snapshot.model_connections[1].status_ok = true
  const profiles = exports.buildSceneCapabilityProfiles(snapshot)
  const visual = profiles.find(profile => profile.id === 'chat').capabilities.find(item => item.id === 'chat_visual_context')
  assert.equal(visual.state, 'inactive')
  assert.equal(visual.stateLabel, 'Available · Off')
  assert.equal(visual.policyLabel, 'Default off')
  assert.equal(visual.currentLabel, 'Currently off')
})

test('DeepSeek Flash is image-capable while DeepSeek Pro remains text-only', () => {
  const flash = configuredSnapshot()
  flash.model_connections[0].fields = [{ key: 'DEEPSEEK_MODEL_NAME', value: 'deepseek-v4-flash' }]
  const flashVisual = exports.buildSceneCapabilityProfiles(flash)
    .find(profile => profile.id === 'chat').capabilities.find(item => item.id === 'chat_visual_context')
  assert.equal(flashVisual.state, 'inactive')
  assert.equal(flashVisual.stateLabel, 'Available · Off')

  const pro = configuredSnapshot()
  pro.model_connections[0].fields = [{ key: 'DEEPSEEK_MODEL_NAME', value: 'deepseek-v4-pro' }]
  const proVisual = exports.buildSceneCapabilityProfiles(pro)
    .find(profile => profile.id === 'chat').capabilities.find(item => item.id === 'chat_visual_context')
  assert.equal(proVisual.state, 'attention')
  assert.equal(proVisual.stateLabel, 'Text-only model')
  assert.match(proVisual.detail, /does not accept direct image input/)
})

test('Hybrid2 follows the selected DeepSeek model capability', () => {
  const snapshot = configuredSnapshot()
  snapshot.llm_provider = 'hybrid2'
  snapshot.model_connections.push(group('hybrid_local', true))
  snapshot.model_connections[0].fields = [{ key: 'DEEPSEEK_MODEL_NAME', value: 'deepseek-v4-flash' }]
  const visual = exports.buildSceneCapabilityProfiles(snapshot)
    .find(profile => profile.id === 'chat').capabilities.find(item => item.id === 'chat_visual_context')
  assert.equal(visual.state, 'inactive')
  assert.equal(visual.stateLabel, 'Available · Off')
})

test('disconnected preview does not guess that Wallpaper visual context cannot be enabled', () => {
  const profiles = exports.buildSceneCapabilityProfiles({})
  const visual = profiles.find(profile => profile.id === 'wallpaper').capabilities.find(item => item.id === 'wallpaper_visual_context')
  assert.equal(visual.state, 'unknown')
  assert.equal(visual.stateLabel, 'Backend not connected')
  assert.equal(visual.policyLabel, 'Managed by Chat')
  assert.equal(visual.currentLabel, 'Waiting for shared state')
})

test('capability overview reports one implementation and scene registrations without duplicating providers', () => {
  const capabilities = exports.buildCapabilityProfiles(configuredSnapshot())
  assert.deepEqual(capabilities.map(item => item.id), [
    'main_conversation', 'vn_companion', 'work_execution', 'application_interaction', 'speech_output', 'conversation_recognition',
    'wake_recognition', 'echo_cancellation', 'voice_interruption', 'visual_understanding', 'translation',
  ])
  const speech = capabilities.find(item => item.id === 'speech_output')
  assert.equal(speech.implementation, 'gpt_sovits')
  assert.deepEqual(speech.scenes.map(scene => [scene.sceneId, scene.used]), [
    ['chat', true], ['wallpaper', true], ['vn', true],
  ])
  const wake = capabilities.find(item => item.id === 'wake_recognition')
  assert.deepEqual(wake.scenes.map(scene => [scene.sceneId, scene.used]), [
    ['chat', false], ['wallpaper', true], ['vn', false],
  ])
  const interruption = capabilities.find(item => item.id === 'voice_interruption')
  assert.deepEqual(interruption.scenes.map(scene => [scene.sceneId, scene.used]), [
    ['chat', true], ['wallpaper', true], ['vn', false],
  ])
})

test('Work Provider and AUIP are projected as implementations of user-facing capabilities', () => {
  const snapshot = configuredSnapshot()
  snapshot.cooperative_chat_enabled = true
  snapshot.cooperative_chat_provider = 'codex'
  snapshot.model_roles = [group('auip_action', true, {}, { active: true, status_ok: true, status_detail: 'B2 available' })]
  const capabilities = exports.buildCapabilityProfiles(snapshot, [
    { provider_id: 'codex', configured: true, ready: true, registered: true, reason: 'ready' },
  ])
  const work = capabilities.find(item => item.id === 'work_execution')
  assert.equal(work.state, 'ready')
  assert.equal(work.implementation, 'Codex agent · current selection')
  assert.deepEqual(work.scenes.map(scene => [scene.sceneId, scene.used]), [
    ['chat', true], ['wallpaper', true], ['vn', false],
  ])
  const appInteraction = capabilities.find(item => item.id === 'application_interaction')
  assert.equal(appInteraction.state, 'ready')
  assert.deepEqual(appInteraction.scenes.map(scene => [scene.sceneId, scene.used]), [
    ['chat', true], ['wallpaper', true], ['vn', false],
  ])
})

test('VN companion reports the selected role connection instead of a fixed DeepSeek implementation', () => {
  const snapshot = configuredSnapshot()
  snapshot.model_connections[1].configured = true
  snapshot.model_connections[1].status_ok = true
  snapshot.model_roles = [group('vn_companion', true, {
    VN_LLM_PROVIDER: 'openai',
    VN_LLM_MODEL: 'gpt-vn',
  }, { status: 'override' })]
  const profiles = exports.buildSceneCapabilityProfiles(snapshot)
  const role = profiles.find(profile => profile.id === 'vn').capabilities.find(item => item.id === 'vn_companion_model')
  assert.equal(role.implementation, 'OpenAI-compatible · gpt-vn')
  assert.equal(role.state, 'ready')
  assert.equal(role.policyLabel, 'Current selection')
})

test('scene display keeps shared Work and AUIP visible when Work is currently off', () => {
  const snapshot = configuredSnapshot()
  snapshot.cooperative_chat_enabled = false
  snapshot.model_roles = [group('auip_action', true, {}, { active: true, status_ok: true })]
  const capabilities = exports.buildCapabilityProfiles(snapshot)
  for (const capabilityId of ['work_execution', 'application_interaction']) {
    const capability = capabilities.find(item => item.id === capabilityId)
    assert.deepEqual(capability.scenes.map(scene => [scene.sceneId, scene.used]), [
      ['chat', true], ['wallpaper', true], ['vn', false],
    ])
  }
  assert.equal(capabilities.find(item => item.id === 'work_execution').state, 'inactive')
})
