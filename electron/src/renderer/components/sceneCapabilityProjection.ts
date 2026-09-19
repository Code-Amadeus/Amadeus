import { chatProviderSupportsImages } from './chatModelCapabilities'

export type SceneCapabilityState = 'ready' | 'attention' | 'inactive' | 'unknown'
export type SceneCapabilityImportance = 'core' | 'expected' | 'optional'
export type SceneCompleteness = 'available' | 'degraded' | 'unavailable'
export type SceneConfigureSection = 'general' | 'models' | 'voice' | 'providers'

export interface SceneCapabilityItem {
  id: string
  label: string
  implementation: string
  detail: string
  importance: SceneCapabilityImportance
  state: SceneCapabilityState
  stateLabel?: string
  policyLabel?: string
  currentLabel?: string
  actionLabel?: string
  configureSection?: SceneConfigureSection
}

export interface SceneCapabilityProfile {
  id: 'wallpaper' | 'chat' | 'vn'
  label: string
  description: string
  completeness: SceneCompleteness
  capabilities: SceneCapabilityItem[]
  readyCount: number
  expectedCount: number
}

export type SceneId = SceneCapabilityProfile['id']

export interface CapabilitySceneUsage extends SceneCapabilityItem {
  sceneId: SceneId
  sceneLabel: string
  used: boolean
}

export interface CapabilityProfile {
  id: string
  label: string
  category: 'Conversation roles' | 'Voice' | 'Perception' | 'Presentation' | 'Work' | 'Application'
  description: string
  implementation: string
  state: SceneCapabilityState
  stateLabel?: string
  configureSection?: SceneConfigureSection
  scenes: CapabilitySceneUsage[]
}

export interface RuntimeProviderAvailability {
  provider_id: string
  configured?: boolean
  ready?: boolean
  registered?: boolean
  reason?: string
}

interface ConfigurationField {
  key?: string
  value?: unknown
  options?: Array<string | { value: string; label: string }>
}

interface ConfigurationGroup {
  id: string
  label?: string
  description?: string
  active?: boolean
  configured?: boolean
  status?: string
  status_ok?: boolean
  status_detail?: string
  fields?: ConfigurationField[]
}

type SettingsSnapshot = Record<string, unknown>

const MODEL_LABELS: Record<string, string> = {
  deepseek: 'DeepSeek',
  openai: 'OpenAI-compatible',
  gemini: 'Gemini',
  bedrock: 'AWS Bedrock',
  local: 'Pure-local model',
  hybrid: 'Hybrid local + Bedrock',
  hybrid2: 'Hybrid local + DeepSeek',
  hybrid3: 'Hybrid local + OpenAI',
}

const ACTIVE_MODEL_CONNECTIONS: Record<string, string[]> = {
  deepseek: ['deepseek'],
  openai: ['openai'],
  gemini: ['gemini'],
  bedrock: ['bedrock'],
  local: ['local'],
  hybrid: ['hybrid_local', 'bedrock'],
  hybrid2: ['hybrid_local', 'deepseek'],
  hybrid3: ['hybrid_local', 'openai'],
}

function groups(value: unknown): ConfigurationGroup[] {
  return Array.isArray(value) ? value as ConfigurationGroup[] : []
}

function findGroup(source: ConfigurationGroup[], id: string): ConfigurationGroup | undefined {
  return source.find(group => group.id === id)
}

function groupAvailable(group: ConfigurationGroup | undefined): boolean {
  if (!group) return false
  if (typeof group.status_ok === 'boolean') return group.status_ok
  return group.configured === true
}

function field(group: ConfigurationGroup | undefined, key: string): ConfigurationField | undefined {
  return (group?.fields || []).find(item => item.key === key)
}

function fieldValue(group: ConfigurationGroup | undefined, key: string, fallback = ''): string {
  const value = field(group, key)?.value
  return value === undefined || value === null ? fallback : String(value)
}

function optionLabel(group: ConfigurationGroup | undefined, key: string, fallback: string): string {
  const item = field(group, key)
  const value = item?.value === undefined || item?.value === null ? '' : String(item.value)
  const match = (item?.options || []).find(option =>
    (typeof option === 'string' ? option : option.value) === value,
  )
  if (!match) return value || fallback
  return typeof match === 'string' ? match : match.label
}

function groupDetail(group: ConfigurationGroup | undefined, fallback: string): string {
  return String(group?.status_detail || group?.description || fallback)
}

function backendUnknown(item: SceneCapabilityItem, factsAvailable: boolean): SceneCapabilityItem {
  return factsAvailable ? item : {
    ...item,
    detail: 'Connect the backend to read the active implementation and runtime state.',
    state: 'unknown',
    stateLabel: 'Backend not connected',
  }
}

function mainModelCapability(
  settings: SettingsSnapshot,
  modelConnections: ConfigurationGroup[],
  label: string,
): SceneCapabilityItem {
  const provider = String(settings.llm_provider || 'deepseek').trim().toLowerCase()
  const requiredConnections = ACTIVE_MODEL_CONNECTIONS[provider] || [provider]
  const runtimeFactsAvailable = Array.isArray(settings.model_connections)
  const available = requiredConnections.length > 0
    && requiredConnections.every(id => groupAvailable(findGroup(modelConnections, id)))
  return {
    id: 'main_chat',
    label,
    implementation: `${MODEL_LABELS[provider] || provider || 'Main chat model'} · ${runtimeFactsAvailable ? 'current selection' : 'recommended default'}`,
    detail: available
      ? 'The current conversational model connection is configured.'
      : 'The current conversational model is missing a required connection or local runtime.',
    importance: 'core',
    state: available ? 'ready' : 'attention',
    configureSection: 'models',
  }
}

function speechSynthesisCapability(
  voiceGroups: ConfigurationGroup[],
): SceneCapabilityItem {
  const synthesis = findGroup(voiceGroups, 'speech_synthesis')
  const backend = optionLabel(synthesis, 'TTS_BACKEND', 'Speech backend')
  const active = synthesis?.active !== false && fieldValue(synthesis, 'TTS_BACKEND', 'disabled') !== 'disabled'
  const available = active && groupAvailable(synthesis)
  return {
    id: 'speech_synthesis',
    label: 'Speech output',
    implementation: backend,
    detail: groupDetail(synthesis, 'Shared speech synthesis used when this scene produces spoken output.'),
    importance: 'expected',
    state: available ? 'ready' : 'attention',
    configureSection: 'voice',
  }
}

function conversationRecognitionCapability(
  voiceGroups: ConfigurationGroup[],
  importance: SceneCapabilityImportance,
  detail: string,
): SceneCapabilityItem {
  const recognition = findGroup(voiceGroups, 'conversation_asr')
  const available = groupAvailable(recognition)
  return {
    id: 'conversation_asr',
    label: 'Conversation recognition',
    implementation: optionLabel(recognition, 'ASR_BACKEND', 'Conversation ASR'),
    detail: available ? detail : groupDetail(recognition, 'The selected recognizer is not available.'),
    importance,
    state: available ? 'ready' : importance === 'optional' ? 'inactive' : 'attention',
    configureSection: 'voice',
  }
}

function wakeRecognitionCapability(
  settings: SettingsSnapshot,
  voiceGroups: ConfigurationGroup[],
): SceneCapabilityItem {
  const wake = findGroup(voiceGroups, 'wake_asr')
  const enabled = settings.wake_enabled === true || wake?.active === true
  const available = enabled && groupAvailable(wake)
  return {
    id: 'wake_asr',
    label: 'Wake recognition',
    implementation: optionLabel(wake, 'WAKE_ASR_BACKEND', 'Wake ASR'),
    detail: enabled
      ? groupDetail(wake, 'Starts and stops with the Wallpaper lifecycle.')
      : 'The Wallpaper preset can start Wake with the surface, but Wake is disabled in the current startup profile.',
    importance: 'expected',
    state: available ? 'ready' : 'attention',
    configureSection: 'voice',
  }
}

function wallpaperEchoCancellationCapability(settings: SettingsSnapshot): SceneCapabilityItem {
  const enabled = settings.aec_realtime_enabled === true
  return {
    id: 'wallpaper_echo_cancellation',
    label: 'Echo cancellation',
    implementation: 'Built-in realtime audio pipeline',
    detail: enabled
      ? 'Realtime echo cancellation is configured and activates only while microphone capture overlaps shared playback.'
      : 'The built-in pipeline is present, but realtime echo cancellation is disabled in the current startup profile.',
    importance: 'expected',
    state: enabled ? 'ready' : 'attention',
    configureSection: 'voice',
  }
}

function chatAcousticCapability(settings: SettingsSnapshot): SceneCapabilityItem {
  const enabled = settings.aec_realtime_enabled === true
  return {
    id: 'chat_echo_cancellation',
    label: 'Echo cancellation',
    implementation: 'Shared microphone & playback pipeline',
    detail: enabled
      ? 'Available when manual Chat voice input overlaps shared speech playback.'
      : 'Optional for manual Chat voice input; realtime echo cancellation is currently off.',
    importance: 'optional',
    state: enabled ? 'ready' : 'inactive',
    configureSection: 'voice',
  }
}

function vnAcousticCapability(settings: SettingsSnapshot): SceneCapabilityItem {
  const enabled = settings.aec_realtime_enabled === true
  return {
    id: 'vn_echo_cancellation',
    label: 'Realtime echo cancellation',
    implementation: 'Shared microphone & playback pipeline',
    detail: enabled
      ? 'VN continuous recognition uses the shared microphone service, so captured frames pass through AEC against the shared TTS reference. Main Chat barge-in interruption stays suppressed while VN is active.'
      : 'VN can keep listening while shared TTS plays, but realtime echo cancellation is disabled in the current startup profile.',
    importance: 'expected',
    state: enabled ? 'ready' : 'attention',
    configureSection: 'voice',
  }
}

function voiceInterruptionCapability(
  settings: SettingsSnapshot,
  scene: 'chat' | 'wallpaper',
): SceneCapabilityItem {
  const enabled = settings.aec_realtime_enabled === true && settings.aec_realtime_barge_in === true
  const optional = scene === 'chat'
  return {
    id: scene === 'chat' ? 'chat_voice_interruption' : 'wallpaper_voice_interruption',
    label: 'Voice interruption',
    implementation: 'Barge-in detector & turn coordinator',
    detail: enabled
      ? 'Confirmed near-end speech may interrupt shared Chat playback; the turn coordinator owns cancellation and ASR handoff.'
      : 'Voice interruption requires both realtime AEC and microphone interruption in the startup profile.',
    importance: optional ? 'optional' : 'expected',
    state: enabled ? 'ready' : optional ? 'inactive' : 'attention',
    configureSection: 'voice',
  }
}

function configuredTranslationConnections(modelConnections: ConfigurationGroup[]): string[] {
  return ['deepseek', 'gemini', 'openai']
    .map(id => findGroup(modelConnections, id))
    .filter(group => groupAvailable(group))
    .map(group => String(group?.label || group?.id || ''))
    .filter(Boolean)
}

function translationCapability(
  id: string,
  label: string,
  enabled: boolean,
  importance: SceneCapabilityImportance,
  modelConnections: ConfigurationGroup[],
  enabledDetail: string,
  disabledDetail: string,
): SceneCapabilityItem {
  const connections = configuredTranslationConnections(modelConnections)
  const available = connections.length > 0
  return {
    id,
    label,
    implementation: available ? `Automatic · ${connections.join(', ')}` : 'No configured translation model',
    detail: enabled ? enabledDetail : disabledDetail,
    importance,
    state: enabled ? available ? 'ready' : 'attention' : 'inactive',
    stateLabel: enabled ? available ? 'Enabled' : 'Missing credentials' : 'Available · Off',
    configureSection: enabled ? 'models' : 'general',
  }
}

function sceneVisualCapability(
  settings: SettingsSnapshot,
  modelLabel: string,
  scene: 'chat' | 'wallpaper' | 'vn',
): SceneCapabilityItem {
  const backendFactsAvailable = groups(settings.model_connections).length > 0
  const provider = String(settings.llm_provider || 'deepseek').trim().toLowerCase()
  const supported = chatProviderSupportsImages(provider)
  const requiredConnections = ACTIVE_MODEL_CONNECTIONS[provider] || [provider]
  const modelConfigured = requiredConnections.length > 0
    && requiredConnections.every(id => groupAvailable(findGroup(groups(settings.model_connections), id)))
  const enabled = settings.vision_enabled === true && String(settings.vision_mode || 'off') !== 'off'
  const scope = String(settings.vision_scope || 'full_screen').replaceAll('_', ' ')
  const label = scene === 'chat' ? 'Screen & image context' : scene === 'wallpaper' ? 'Wallpaper visual context' : 'VN visual context'
  if (!backendFactsAvailable) {
    return {
      id: scene === 'chat' ? 'chat_visual_context' : scene === 'wallpaper' ? 'wallpaper_visual_context' : 'vn_visual_context',
      label,
      implementation: 'Shared main chat',
      detail: 'Connect the backend to read the selected main-chat model and shared visual-context state.',
      importance: 'optional',
      state: 'unknown',
      stateLabel: 'Backend not connected',
      policyLabel: scene === 'chat' ? 'Default off' : 'Managed by Chat',
      currentLabel: 'Waiting for shared state',
      configureSection: 'general',
    }
  }
  if (scene === 'vn') {
    return {
      id: 'vn_visual_context',
      label,
      implementation: `Shared main chat · ${modelLabel}`,
      detail: !modelConfigured
        ? 'The shared main-chat model is missing a required connection or local runtime.'
        : supported
        ? 'Target design: capture the game window and send it through the shared multimodal main-chat capability. The current VN runtime still owns a separate visual path, so this binding is not active yet.'
        : 'The target shared visual path requires a multimodal main-chat model; the current model is text-only in this runtime.',
      importance: 'optional',
      state: !modelConfigured || !supported ? 'attention' : 'unknown',
      stateLabel: !modelConfigured ? 'Missing credentials' : supported ? 'Not wired' : 'Text-only model',
      policyLabel: 'Managed by Chat',
      currentLabel: 'Runtime not connected',
      actionLabel: modelConfigured && supported ? undefined : 'Choose main model',
      configureSection: modelConfigured && supported ? undefined : 'models',
    }
  }
  return {
    id: scene === 'chat' ? 'chat_visual_context' : 'wallpaper_visual_context',
    label,
    implementation: `Shared main chat · ${modelLabel}`,
    detail: !modelConfigured
      ? 'The shared main-chat model is missing a required connection or local runtime.'
      : !supported
      ? 'The shared main-chat profile does not accept direct image input. Choose a multimodal main-chat model before using visual context.'
      : scene === 'wallpaper'
        ? enabled
          ? `Wallpaper chat turns inherit the shared visual context, currently using ${String(settings.vision_mode || 'on_demand').replaceAll('_', ' ')} mode with ${scope} capture.`
          : 'The current main-chat model supports images. Wallpaper inherits the shared visual setting, which is currently off.'
        : enabled
          ? `Shared visual context is enabled in ${String(settings.vision_mode || 'on_demand').replaceAll('_', ' ')} mode with ${scope} capture.`
          : 'The current main-chat model supports images; image and screen input are available but off in the current setup.',
    importance: 'optional',
    state: !modelConfigured || !supported ? 'attention' : enabled ? 'ready' : 'inactive',
    stateLabel: !modelConfigured ? 'Missing credentials' : !supported ? 'Text-only model' : enabled ? 'Enabled' : 'Available · Off',
    policyLabel: scene === 'chat' ? 'Default off' : 'Managed by Chat',
    currentLabel: supported
      ? scene === 'chat'
        ? enabled ? 'Currently on' : 'Currently off'
        : enabled ? 'Inherited on' : 'Inherited off'
      : 'Waiting for multimodal Chat',
    actionLabel: !modelConfigured || !supported ? 'Choose main model' : scene === 'wallpaper' ? 'Manage shared visual' : 'Configure',
    configureSection: !modelConfigured || !supported ? 'models' : 'general',
  }
}

function finalizeProfile(
  profile: Omit<SceneCapabilityProfile, 'completeness' | 'readyCount' | 'expectedCount'>,
): SceneCapabilityProfile {
  const assessed = profile.capabilities.filter(item => item.importance !== 'optional')
  const readyCount = assessed.filter(item => item.state === 'ready').length
  const coreMissing = assessed.some(item => item.importance === 'core' && item.state !== 'ready')
  const expectedMissing = assessed.some(item => item.importance === 'expected' && item.state !== 'ready')
  return {
    ...profile,
    completeness: coreMissing ? 'unavailable' : expectedMissing ? 'degraded' : 'available',
    readyCount,
    expectedCount: assessed.length,
  }
}

export function buildSceneCapabilityProfiles(settings: SettingsSnapshot): SceneCapabilityProfile[] {
  const voiceGroups = groups(settings.voice_configuration)
  const modelConnections = groups(settings.model_connections)
  const modelRoles = groups(settings.model_roles)
  const voiceFactsAvailable = Array.isArray(settings.voice_configuration)
  const modelFactsAvailable = Array.isArray(settings.model_connections)
  const provider = String(settings.llm_provider || 'deepseek').trim().toLowerCase()
  const mainModelLabel = MODEL_LABELS[provider] || provider || 'Main chat model'
  const tts = backendUnknown(speechSynthesisCapability(voiceGroups), voiceFactsAvailable)
  const asrForWallpaper = backendUnknown(conversationRecognitionCapability(
    voiceGroups,
    'expected',
    'Loaded on demand after Wake handoff or another Wallpaper voice request.',
  ), voiceFactsAvailable)
  const asrForChat = backendUnknown(conversationRecognitionCapability(
    voiceGroups,
    'optional',
    'Available when the user presses the microphone button; it is not started with the Chat window.',
  ), voiceFactsAvailable)
  const asrForVn = backendUnknown(conversationRecognitionCapability(
    voiceGroups,
    'expected',
    'The current VN lifecycle requests continuous listening while its runtime is active.',
  ), voiceFactsAvailable)
  const wallpaperCaptionMode = String(settings.wallpaper_caption_mode || 'translated')
  const wallpaperTranslationEnabled = ['translated', 'bilingual'].includes(wallpaperCaptionMode)
  const chatTranslationEnabled = settings.chat_translation_subtitles_enabled === true
  const mainChat = backendUnknown(mainModelCapability(settings, modelConnections, 'Main conversation'), modelFactsAvailable)

  const wallpaper = finalizeProfile({
    id: 'wallpaper',
    label: 'Wallpaper',
    description: 'Ambient desktop scene. Its current lifecycle owns Wake startup and reuses the shared Chat and voice paths.',
    capabilities: [
      { ...mainChat, id: 'wallpaper_chat' },
      { ...tts },
      backendUnknown(wakeRecognitionCapability(settings, voiceGroups), voiceFactsAvailable),
      asrForWallpaper,
      backendUnknown(wallpaperEchoCancellationCapability(settings), voiceFactsAvailable),
      backendUnknown(voiceInterruptionCapability(settings, 'wallpaper'), voiceFactsAvailable),
      backendUnknown(translationCapability(
        'wallpaper_translation',
        'Wallpaper captions',
        wallpaperTranslationEnabled,
        wallpaperTranslationEnabled ? 'expected' : 'optional',
        modelConnections,
        `Caption mode is ${wallpaperCaptionMode}; translation is requested only for presentation.`,
        `Caption mode is ${wallpaperCaptionMode}; translated captions are not requested.`,
      ), modelFactsAvailable),
      sceneVisualCapability(settings, mainModelLabel, 'wallpaper'),
    ],
  })

  const chat = finalizeProfile({
    id: 'chat',
    label: 'Chat window',
    description: 'Foreground conversation. Speech output is shared; voice input and visual context are explicitly requested by the user.',
    capabilities: [
      { ...mainChat },
      { ...tts },
      asrForChat,
      backendUnknown(chatAcousticCapability(settings), voiceFactsAvailable),
      backendUnknown(voiceInterruptionCapability(settings, 'chat'), voiceFactsAvailable),
      backendUnknown(translationCapability(
        'chat_translation',
        'Translated subtitles',
        chatTranslationEnabled,
        'optional',
        modelConnections,
        'Completed Japanese assistant messages are translated for display only.',
        'Optional. Chat translation subtitles are not in the current setup.',
      ), modelFactsAvailable),
      sceneVisualCapability(settings, mainModelLabel, 'chat'),
    ],
  })

  const vnRole = findGroup(modelRoles, 'vn_companion')
  const vnProvider = fieldValue(vnRole, 'VN_LLM_PROVIDER', 'deepseek') || 'deepseek'
  const vnModelOverride = fieldValue(vnRole, 'VN_LLM_MODEL')
  const vnModel = findGroup(modelConnections, vnProvider)
  const vnProviderLabel = MODEL_LABELS[vnProvider] || vnProvider
  const vn = finalizeProfile({
    id: 'vn',
    label: 'Visual Novel',
    description: 'Dedicated companion session. It claims its voice-input route while active and shares the common speech playback pipeline.',
    capabilities: [
      backendUnknown({
        id: 'vn_companion_model',
        label: 'VN companion model',
        implementation: vnModelOverride ? `${vnProviderLabel} · ${vnModelOverride}` : `${vnProviderLabel} · connection default`,
        detail: groupAvailable(vnModel)
          ? `The VN companion role uses the configured ${vnProviderLabel} connection${vnModelOverride ? ' with an explicit model override' : ''}.`
          : `The VN companion role requires a configured ${vnProviderLabel} connection before its model lanes can run.`,
        importance: 'core',
        state: groupAvailable(vnModel) ? 'ready' : 'attention',
        policyLabel: !vnRole || vnRole.status === 'recommended' ? 'Recommended default' : 'Current selection',
        configureSection: 'models',
      }, modelFactsAvailable),
      { ...tts },
      asrForVn,
      backendUnknown(vnAcousticCapability(settings), voiceFactsAvailable),
      backendUnknown(translationCapability(
        'vn_translation',
        'VN subtitle & speech translation',
        true,
        'expected',
        modelConnections,
        'The VN bridge requests translation when display and speech languages require it.',
        '',
      ), modelFactsAvailable),
      sceneVisualCapability(settings, mainModelLabel, 'vn'),
    ],
  })

  return [wallpaper, chat, vn]
}

const SCENE_LABELS: Record<SceneId, string> = {
  wallpaper: 'Wallpaper',
  chat: 'Chat window',
  vn: 'Visual Novel',
}

function unusedScene(sceneId: SceneId): CapabilitySceneUsage {
  return {
    sceneId,
    sceneLabel: SCENE_LABELS[sceneId],
    used: false,
    id: `unused_${sceneId}`,
    label: 'Not used',
    implementation: '—',
    detail: 'This capability is not part of the current built-in scene lifecycle.',
    importance: 'optional',
    state: 'inactive',
    currentLabel: 'Not used',
  }
}

function sceneUsage(
  profiles: SceneCapabilityProfile[],
  sceneId: SceneId,
  capabilityId: string,
): CapabilitySceneUsage {
  const profile = profiles.find(item => item.id === sceneId)
  const item = profile?.capabilities.find(capability => capability.id === capabilityId)
  return item
    ? { ...item, sceneId, sceneLabel: SCENE_LABELS[sceneId], used: true }
    : unusedScene(sceneId)
}

function capabilityProfile(
  profiles: SceneCapabilityProfile[],
  definition: {
    id: string
    label: string
    category: CapabilityProfile['category']
    description: string
    source: [SceneId, string]
    scenes: Partial<Record<SceneId, string>>
  },
): CapabilityProfile {
  const source = sceneUsage(profiles, definition.source[0], definition.source[1])
  const sceneIds: SceneId[] = ['chat', 'wallpaper', 'vn']
  return {
    id: definition.id,
    label: definition.label,
    category: definition.category,
    description: definition.description,
    implementation: source.implementation,
    state: source.state,
    stateLabel: source.stateLabel,
    configureSection: source.configureSection,
    scenes: sceneIds.map(sceneId => {
      const capabilityId = definition.scenes[sceneId]
      return capabilityId ? sceneUsage(profiles, sceneId, capabilityId) : unusedScene(sceneId)
    }),
  }
}

function directSceneUsage(
  sceneId: SceneId,
  item: SceneCapabilityItem,
  used: boolean,
): CapabilitySceneUsage {
  return used
    ? { ...item, sceneId, sceneLabel: SCENE_LABELS[sceneId], used: true }
    : unusedScene(sceneId)
}

function providerDisplayName(providerId: string): string {
  const known: Record<string, string> = {
    codex: 'Codex agent',
    openclaw: 'OpenClaw agent',
    browser: 'Browser provider',
  }
  return known[providerId] || providerId.replaceAll('_', ' ').replace(/^./, value => value.toUpperCase()) || 'Work Provider'
}

export function buildCapabilityProfiles(
  settings: SettingsSnapshot,
  providerAvailability: RuntimeProviderAvailability[] = [],
): CapabilityProfile[] {
  const profiles = buildSceneCapabilityProfiles(settings)
  const backendFactsAvailable = Array.isArray(settings.model_connections)
  const workEnabled = settings.cooperative_chat_enabled === true
  const workProviderId = String(settings.cooperative_chat_provider || 'codex').trim().toLowerCase()
  const workProvider = providerAvailability.find(item => item.provider_id === workProviderId)
  const workReady = workEnabled && workProvider?.registered === true && workProvider?.ready === true
  const workImplementation = `${providerDisplayName(workProviderId)} · ${backendFactsAvailable ? 'current selection' : 'recommended default'}`
  const workItem: SceneCapabilityItem = {
    id: 'work_execution',
    label: 'Work execution',
    implementation: workImplementation,
    detail: workEnabled
      ? workReady
        ? 'The configured Work Provider passed its startup registration boundary.'
        : `The selected Work Provider is not ready: ${String(workProvider?.reason || 'provider unavailable')}.`
      : 'Cooperative Work execution is disabled in the current startup profile.',
    importance: 'expected',
    state: !backendFactsAvailable ? 'unknown' : workReady ? 'ready' : workEnabled ? 'attention' : 'inactive',
    stateLabel: !backendFactsAvailable ? 'Backend not connected' : workReady ? 'Provider registered' : workEnabled ? 'Provider unavailable' : 'Disabled',
    policyLabel: 'Delegated from Chat',
    currentLabel: workReady ? 'Ready' : 'Not ready',
    configureSection: 'providers',
  }
  const workCapability: CapabilityProfile = {
    id: 'work_execution',
    label: 'Work execution',
    category: 'Work',
    description: 'Delegated task execution. Work Providers are implementations of this capability, not separate capabilities themselves.',
    implementation: workItem.implementation,
    state: workItem.state,
    stateLabel: workItem.stateLabel,
    configureSection: workItem.configureSection,
    scenes: [
      directSceneUsage('chat', workItem, true),
      directSceneUsage('wallpaper', { ...workItem, detail: 'Wallpaper-originated conversation turns reuse the shared Chat delegation path.' }, true),
      unusedScene('vn'),
    ],
  }
  const modelRoles = groups(settings.model_roles)
  const auipAction = findGroup(modelRoles, 'auip_action')
  const auipReady = groupAvailable(auipAction)
  const auipItem: SceneCapabilityItem = {
    id: 'application_interaction',
    label: 'Application interaction',
    implementation: 'Host AUIP runtime · action role',
    detail: groupDetail(auipAction, 'The Host owns AUIP session identity, action legality, receipts, and application attachment.'),
    importance: 'optional',
    state: !backendFactsAvailable ? 'unknown' : auipReady ? 'ready' : 'attention',
    stateLabel: !backendFactsAvailable ? 'Backend not connected' : auipReady ? 'Available' : 'Action role needs setup',
    policyLabel: 'Shared conversation capability',
    currentLabel: auipAction?.active ? 'B2 action role active' : 'Observe or legacy mode',
    configureSection: 'models',
  }
  const applicationInteraction: CapabilityProfile = {
    id: 'application_interaction',
    label: 'Application interaction',
    category: 'Application',
    description: 'Host-governed interaction with verified AUIP applications. AUIP is the protocol; application interaction is the user-facing capability.',
    implementation: auipItem.implementation,
    state: auipItem.state,
    stateLabel: auipItem.stateLabel,
    configureSection: auipItem.configureSection,
    scenes: [
      directSceneUsage('chat', auipItem, true),
      directSceneUsage('wallpaper', {
        ...auipItem,
        detail: 'Wallpaper-originated conversation turns reuse the shared Host-governed AUIP interaction path.',
      }, true),
      unusedScene('vn'),
    ],
  }
  return [
    capabilityProfile(profiles, {
      id: 'main_conversation',
      label: 'Main conversation',
      category: 'Conversation roles',
      description: 'The shared conversational model used by Chat and Wallpaper-originated turns.',
      source: ['chat', 'main_chat'],
      scenes: { chat: 'main_chat', wallpaper: 'wallpaper_chat' },
    }),
    capabilityProfile(profiles, {
      id: 'vn_companion',
      label: 'VN companion',
      category: 'Conversation roles',
      description: 'The current dedicated model profile for Visual Novel reasoning and reactions.',
      source: ['vn', 'vn_companion_model'],
      scenes: { vn: 'vn_companion_model' },
    }),
    workCapability,
    applicationInteraction,
    capabilityProfile(profiles, {
      id: 'speech_output',
      label: 'Speech output',
      category: 'Voice',
      description: 'One shared TTS implementation consumed by scene-specific speech events.',
      source: ['chat', 'speech_synthesis'],
      scenes: { chat: 'speech_synthesis', wallpaper: 'speech_synthesis', vn: 'speech_synthesis' },
    }),
    capabilityProfile(profiles, {
      id: 'conversation_recognition',
      label: 'Conversation recognition',
      category: 'Voice',
      description: 'The shared full recognizer, activated manually, after Wake, or continuously according to scene policy.',
      source: ['wallpaper', 'conversation_asr'],
      scenes: { chat: 'conversation_asr', wallpaper: 'conversation_asr', vn: 'conversation_asr' },
    }),
    capabilityProfile(profiles, {
      id: 'wake_recognition',
      label: 'Wake recognition',
      category: 'Voice',
      description: 'The lightweight recognizer currently owned by the Wallpaper lifecycle.',
      source: ['wallpaper', 'wake_asr'],
      scenes: { wallpaper: 'wake_asr' },
    }),
    capabilityProfile(profiles, {
      id: 'echo_cancellation',
      label: 'Echo cancellation',
      category: 'Voice',
      description: 'Shared AEC applied when scene microphone capture overlaps speech playback.',
      source: ['wallpaper', 'wallpaper_echo_cancellation'],
      scenes: { chat: 'chat_echo_cancellation', wallpaper: 'wallpaper_echo_cancellation', vn: 'vn_echo_cancellation' },
    }),
    capabilityProfile(profiles, {
      id: 'voice_interruption',
      label: 'Voice interruption',
      category: 'Voice',
      description: 'Barge-in behavior that interrupts the shared Chat turn after confirmed near-end speech; separate from echo cancellation.',
      source: ['wallpaper', 'wallpaper_voice_interruption'],
      scenes: { chat: 'chat_voice_interruption', wallpaper: 'wallpaper_voice_interruption' },
    }),
    capabilityProfile(profiles, {
      id: 'visual_understanding',
      label: 'Visual understanding',
      category: 'Perception',
      description: 'Image understanding provided by the main Chat model; scenes differ only in capture source and runtime wiring.',
      source: ['chat', 'chat_visual_context'],
      scenes: { chat: 'chat_visual_context', wallpaper: 'wallpaper_visual_context', vn: 'vn_visual_context' },
    }),
    capabilityProfile(profiles, {
      id: 'translation',
      label: 'Presentation translation',
      category: 'Presentation',
      description: 'Shared model-backed translation with independent display and speech policies in each scene.',
      source: ['vn', 'vn_translation'],
      scenes: { chat: 'chat_translation', wallpaper: 'wallpaper_translation', vn: 'vn_translation' },
    }),
  ]
}
