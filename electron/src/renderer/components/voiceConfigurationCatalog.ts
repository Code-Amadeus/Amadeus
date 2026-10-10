import { startupValues, type StartupSnapshot } from '../../shared/startupSettings.js'
import type { ModelConnectionCatalogGroup } from './modelConnectionCatalog'
import { catalogConfiguration, desktopCatalogFields, voiceBackendGroups, voiceBackendOptions } from '../../shared/configCatalog.js'

interface VoiceDesktopSnapshot extends StartupSnapshot {
  values?: Record<string, string>
  secrets?: Record<string, { configured?: boolean }>
}

interface VoiceRuntimeSelection {
  asrBackend: string
  ttsBackend: string
  wakeEnabled: boolean
  aecEnabled: boolean
  emotionReferencesEnabled?: boolean
}

export function buildVoiceConfigurationCatalog(
  selection: VoiceRuntimeSelection,
  snapshot?: VoiceDesktopSnapshot | null,
): ModelConnectionCatalogGroup[] {
  const values = startupValues(snapshot)
  const value = (key: string, fallback = '') => values[key] ?? String(desktopCatalogFields[key]?.default ?? fallback)
  const secret = (key: string) => Boolean(snapshot?.secrets?.[key]?.configured)
  const synthesis = catalogConfiguration('speech_synthesis', snapshot, {
    TTS_BACKEND: selection.ttsBackend || String(desktopCatalogFields.TTS_BACKEND.default),
  })
  const ttsBackend = String(synthesis.fields[0].value)
  const runtimeValues = {
    ASR_BACKEND: selection.asrBackend, WAKE_ENABLED: selection.wakeEnabled,
    AEC_REALTIME_ENABLED: selection.aecEnabled,
    ...(selection.emotionReferencesEnabled === undefined ? {} : { ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING: selection.emotionReferencesEnabled }),
  }
  const unknown = 'Backend status unavailable'
  const conversation = catalogConfiguration('conversation_asr', snapshot, runtimeValues)
  const wake = catalogConfiguration('wake_asr', snapshot, runtimeValues)
  const acoustic = catalogConfiguration('acoustic_pipeline', snapshot, runtimeValues)
  const emotion = catalogConfiguration('tts_emotion_references', snapshot, runtimeValues)
  const asrBackend = conversation.fields.find(field => field.key === 'ASR_BACKEND')?.value
  const wakeEnabled = wake.fields.find(field => field.key === 'WAKE_ENABLED')?.value
  const aecEnabled = acoustic.fields.find(field => field.key === 'AEC_REALTIME_ENABLED')?.value
  const emotionEnabled = emotion.fields.find(field => field.key === 'ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING')?.value

  return [
    {
      ...conversation,

      active: true,
      configured: false,
      status: unknown,
      status_ok: false,
      fields: conversation.fields.map(item => item.key === 'ASR_BACKEND'
        ? { ...item, type: 'select' as const, options: [
          { value: 'qwen3_asr', label: 'Qwen3-ASR' }, { value: 'sense_voice', label: 'SenseVoice' },
          { value: 'openai_compatible', label: 'OpenAI-compatible API' },
        ] } : item.key === 'MICROPHONE_DEVICE_INDEX' ? { ...item, type: 'select' as const, options: [{ value: '-1', label: 'Automatic' }] } : item),
    },
    {
      ...catalogConfiguration('asr_remote', snapshot, runtimeValues),

      active: asrBackend === 'openai_compatible',
      configured: Boolean(value('ASR_API_BASE_URL')) && Boolean(value('ASR_API_MODEL')) && secret('ASR_API_KEY'),
      status: asrBackend === 'openai_compatible' ? (secret('ASR_API_KEY') ? unknown : 'Needs setup') : 'Optional',
      status_ok: false,
    },
    {
      ...wake,

      active: wakeEnabled === true,
      configured: wakeEnabled === false,
      status: wakeEnabled === false ? 'Off' : unknown,
      status_ok: wakeEnabled === false,
    },
    {
      ...acoustic,

      active: aecEnabled === true,
      configured: true,
      status: aecEnabled === undefined ? unknown : aecEnabled ? 'Enabled' : 'Off',
      status_ok: aecEnabled !== undefined,
    },
    {
      ...synthesis,
      active: ttsBackend !== 'disabled', configured: false,
      status: ttsBackend === 'disabled' ? 'Off' : unknown,
      status_ok: ttsBackend === 'disabled',
      fields: synthesis.fields.map(item => ({ ...item, type: 'select' as const, options: voiceBackendOptions })),
    },
    ...voiceBackendGroups.map(group => {
      const backend = group.voice_backend!;
      const configured = backend.deployment === 'remote'
        && Object.entries(group.config).filter(([, field]) => field.secret).every(([key]) => secret(key));
      return {
        ...catalogConfiguration(group.id, snapshot, { TTS_DEVICE: 'auto' }),
        active: ttsBackend === backend.id, configured, status_ok: false,
        status: ttsBackend === backend.id ? (backend.deployment === 'remote' && !configured ? 'Needs setup' : unknown) : 'Optional',
      };
    }),
    {
      ...catalogConfiguration('voice_reference_profile', snapshot, runtimeValues),

      active: ttsBackend === 'gpt_sovits',
      configured: Boolean(value('TTS_REF_AUDIO_JA')) || Boolean(value('TTS_REF_AUDIO_EN')),
      status: ttsBackend === 'gpt_sovits' ? unknown : 'Optional',
      status_ok: false,
    },

    {
      ...emotion,

      active: ttsBackend === 'gpt_sovits' && emotionEnabled === true,
      configured: emotionEnabled === false,
      status: emotionEnabled === false ? 'Off' : unknown,
      status_ok: emotionEnabled === false,
    },

  ]
}

export const voiceConfigurationSections = {
  output: new Set(['speech_synthesis', 'voice_reference_profile', 'tts_emotion_references',
    ...voiceBackendGroups.filter(group => group.section === 'output').map(group => group.id)]),
  input: new Set(['conversation_asr', 'wake_asr', 'acoustic_pipeline']),
  remote: new Set(['asr_remote', ...voiceBackendGroups.filter(group => group.section === 'remote').map(group => group.id)]),
}
