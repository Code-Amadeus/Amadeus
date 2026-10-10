import type { ModelConnectionCatalogField, ModelConnectionCatalogGroup } from './modelConnectionCatalog'
import { catalogConfiguration, desktopCatalogFields, voiceBackendGroups, voiceBackendOptions } from '../../shared/configCatalog.js'

interface VoiceDesktopSnapshot {
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

const field = (
  key: string,
  label: string,
  type: ModelConnectionCatalogField['type'],
  value: string | boolean = '',
  options?: ModelConnectionCatalogField['options'],
  description?: string,
  range?: { min?: number; max?: number; step?: number },
): ModelConnectionCatalogField => ({
  key,
  label,
  type,
  value,
  options,
  description,
  ...range,
  editable: true,
  restart_required: true,
})

export function buildVoiceConfigurationCatalog(
  selection: VoiceRuntimeSelection,
  snapshot?: VoiceDesktopSnapshot | null,
): ModelConnectionCatalogGroup[] {
  const values = snapshot?.values || {}
  const value = (key: string, fallback = '') => values[key] || fallback
  const secret = (key: string) => Boolean(snapshot?.secrets?.[key]?.configured)
  const bool = (key: string, fallback: boolean) => values[key] === undefined ? fallback : values[key] === 'true'
  const asrBackend = value('ASR_BACKEND', selection.asrBackend || 'qwen3_asr')
  const synthesis = catalogConfiguration('speech_synthesis', snapshot, {
    TTS_BACKEND: selection.ttsBackend || String(desktopCatalogFields.TTS_BACKEND.default),
  })
  const ttsBackend = String(synthesis.fields[0].value)
  const wakeEnabled = bool('WAKE_ENABLED', selection.wakeEnabled)
  const aecEnabled = bool('AEC_REALTIME_ENABLED', selection.aecEnabled)
  const emotionEnabled = values.ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING === undefined
    ? Boolean(selection.emotionReferencesEnabled)
    : ['true', '1', 'yes'].includes(value('ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING').toLowerCase())
  const unknown = 'Backend status unavailable'

  return [
    {
      id: 'conversation_asr',
      label: 'Conversation recognition',
      description: 'Full transcription after manual listening or Wake handoff. Qwen is the embedded default.',
      active: true,
      configured: false,
      status: unknown,
      status_ok: false,
      fields: [
        field('ASR_BACKEND', 'Backend', 'select', asrBackend, [
          { value: 'qwen3_asr', label: 'Qwen3-ASR' },
          { value: 'sense_voice', label: 'SenseVoice' },
          { value: 'openai_compatible', label: 'OpenAI-compatible API' },
        ]),
        field('ASR_LANGUAGE', 'Recognition language', 'text', value('ASR_LANGUAGE', 'auto'), undefined, 'auto or an ISO-639-1 language code such as en, ja, or zh.'),
        field('ASR_CONTEXT', 'Context and terminology', 'text', value('ASR_CONTEXT'), undefined, 'Prompt or domain vocabulary used by compatible full recognizers.'),
        field('QWEN3_ASR_MODEL_PATH', 'Qwen model directory', 'path', value('QWEN3_ASR_MODEL_PATH'), undefined, 'Leave blank to use the bundled asset path or a compatible model cache.'),
        field('QWEN3_ASR_DEVICE', 'Qwen device', 'select', value('QWEN3_ASR_DEVICE', 'auto'), ['auto', 'cpu', 'cuda']),
        field('QWEN3_ASR_REQUIRE_CUDA', 'Require Qwen CUDA', 'boolean', bool('QWEN3_ASR_REQUIRE_CUDA', false)),
        field('MICROPHONE_DEVICE_INDEX', 'Microphone', 'select', value('MICROPHONE_DEVICE_INDEX', '-1'), [{ value: '-1', label: 'Automatic' }], 'Connect the backend to enumerate installed microphones.'),
        field('MICROPHONE_PREFERRED_NAME', 'Preferred microphone name', 'text', value('MICROPHONE_PREFERRED_NAME'), undefined, 'Optional partial-name fallback when device indices change.'),
        field('ASR_LISTEN_TIMEOUT_SECONDS', 'Wait for speech', 'number', value('ASR_LISTEN_TIMEOUT_SECONDS', '15'), undefined, 'Seconds to wait for speech to begin after listening starts.', { min: 1, max: 120, step: 1 }),
        field('ASR_VAD_SILENCE_MS', 'End-of-speech pause', 'number', value('ASR_VAD_SILENCE_MS', '350'), undefined, 'Silence required before a spoken turn is considered complete.', { min: 100, max: 3000, step: 50 }),
      ],
    },
    {
      id: 'asr_remote',
      label: 'Remote transcription API',
      description: 'OpenAI-compatible POST /audio/transcriptions, used only when selected above.',
      active: asrBackend === 'openai_compatible',
      configured: Boolean(value('ASR_API_BASE_URL')) && Boolean(value('ASR_API_MODEL')) && secret('ASR_API_KEY'),
      status: asrBackend === 'openai_compatible' ? (secret('ASR_API_KEY') ? unknown : 'Needs setup') : 'Optional',
      status_ok: false,
      fields: [
        field('ASR_API_BASE_URL', 'API base URL', 'url', value('ASR_API_BASE_URL', 'https://api.openai.com/v1')),
        field('ASR_API_KEY', 'API key', 'secret'),
        field('ASR_API_MODEL', 'Model', 'text', value('ASR_API_MODEL', 'gpt-4o-mini-transcribe')),
      ],
    },
    {
      id: 'wake_asr',
      label: 'Wake recognition',
      description: 'Independent always-on recognizer; it may use a different local backend from Conversation recognition.',
      active: wakeEnabled,
      configured: !wakeEnabled,
      status: wakeEnabled ? unknown : 'Off',
      status_ok: !wakeEnabled,
      fields: [
        field('WAKE_ENABLED', 'Wake service', 'boolean', wakeEnabled),
        field('WAKE_PHRASES', 'Wake phrases', 'text', value('WAKE_PHRASES', 'hi amadeus,hey amadeus,hello amadeus')),
        field('WAKE_AUTO_SEND_TO_CHAT', 'Send command to Chat', 'boolean', bool('WAKE_AUTO_SEND_TO_CHAT', true)),
        field('WAKE_ASR_BACKEND', 'Wake backend', 'select', value('WAKE_ASR_BACKEND', 'sense_voice'), ['sense_voice', 'qwen3_asr']),
        field('WAKE_SENSEVOICE_LANGUAGES', 'Wake languages', 'text', value('WAKE_SENSEVOICE_LANGUAGES', 'en')),
        field('SENSEVOICE_LANGUAGE', 'SenseVoice conversation language', 'select', value('SENSEVOICE_LANGUAGE', 'en'), ['auto', 'en', 'zh', 'ja', 'yue', 'ko']),
        field('SENSEVOICE_MODEL_PATH', 'SenseVoice model path', 'path', value('SENSEVOICE_MODEL_PATH')),
      ],
    },
    {
      id: 'acoustic_pipeline',
      label: 'Echo cancellation & interruption',
      description: 'Realtime AEC and barge-in controls shared by scene microphone paths.',
      active: aecEnabled,
      configured: true,
      status: aecEnabled ? 'Enabled' : 'Off',
      status_ok: true,
      fields: [
        field('AEC_REALTIME_ENABLED', 'Realtime echo cancellation', 'boolean', aecEnabled),
        field('AEC_REALTIME_BARGE_IN', 'Allow microphone interruption', 'boolean', bool('AEC_REALTIME_BARGE_IN', true)),
        field('AEC_REALTIME_DELAY_MS', 'AEC reference delay', 'number', value('AEC_REALTIME_DELAY_MS', '280'), undefined, 'Playback-to-microphone reference delay in milliseconds.', { min: 0, max: 2000, step: 10 }),
      ],
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
      id: 'voice_reference_profile',
      label: 'Voice reference profile',
      description: 'Shared reference audio and transcripts used only by TTS backends that support reference conditioning.',
      active: ttsBackend === 'gpt_sovits',
      configured: Boolean(value('TTS_REF_AUDIO_JA')) || Boolean(value('TTS_REF_AUDIO_EN')),
      status: ttsBackend === 'gpt_sovits' ? unknown : 'Optional',
      status_ok: false,
      fields: [
        field('TTS_REF_AUDIO_JA', 'Japanese reference audio', 'path', value('TTS_REF_AUDIO_JA', './assets/audio/reference/kurisu_reference.wav')),
        field('TTS_REF_TEXT_JA', 'Japanese reference transcript', 'text', value('TTS_REF_TEXT_JA')),
        field('TTS_REF_AUDIO_EN', 'English reference audio', 'path', value('TTS_REF_AUDIO_EN', './assets/audio/reference/english_recording.wav')),
        field('TTS_REF_TEXT_EN', 'English reference transcript', 'text', value('TTS_REF_TEXT_EN')),
      ],
    },

    {
      id: 'tts_emotion_references',
      label: 'Emotion voice references',
      description: 'Use an optional emotion voice pack for Windows CUDA V3 Japanese speech. References are prepared at startup.',
      active: ttsBackend === 'gpt_sovits' && emotionEnabled,
      configured: !emotionEnabled,
      status: emotionEnabled ? unknown : 'Off',
      status_ok: !emotionEnabled,
      fields: [
        field('ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING', 'Enable emotion voice references', 'boolean',
          emotionEnabled,
          undefined, 'Default off. Install the optional voice-kurisu-emotions pack and restart. Turning this off restores default reference speech.'),
      ],
    },

  ]
}

export const voiceConfigurationSections = {
  output: new Set(['speech_synthesis', 'voice_reference_profile', 'tts_emotion_references',
    ...voiceBackendGroups.filter(group => group.section === 'output').map(group => group.id)]),
  input: new Set(['conversation_asr', 'wake_asr', 'acoustic_pipeline']),
  remote: new Set(['asr_remote', ...voiceBackendGroups.filter(group => group.section === 'remote').map(group => group.id)]),
}
