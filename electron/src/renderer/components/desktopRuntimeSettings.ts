const RUNTIME_VALUE_KEYS: Record<string, string> = {
  llm_provider: 'LLM_PROVIDER',
  local_llm_type: 'LOCAL_LLM_TYPE',
  asr_backend: 'ASR_BACKEND',
  vision_enabled: 'AMADEUS_VISION_ENABLED',
  vision_mode: 'AMADEUS_VISION_MODE',
  vision_scope: 'AMADEUS_VISION_SCOPE',
  vision_max_long_side: 'AMADEUS_VISION_MAX_LONG_SIDE',
  vision_jpeg_quality: 'AMADEUS_VISION_JPEG_QUALITY',
  vision_region: 'AMADEUS_VISION_REGION',
  vision_window_handle: 'AMADEUS_VISION_WINDOW_HANDLE',
  presentation_locale: 'AMADEUS_PRESENTATION_LOCALE',
  wallpaper_caption_mode: 'AMADEUS_WALLPAPER_CAPTION_MODE',
  chat_translation_subtitles_enabled: 'AMADEUS_CHAT_TRANSLATION_SUBTITLES_ENABLED',
}

const BOOLEAN_RUNTIME_KEYS = new Set([
  'vision_enabled',
  'chat_translation_subtitles_enabled',
])

const NUMBER_RUNTIME_KEYS = new Set(['vision_max_long_side', 'vision_jpeg_quality'])

export function runtimeSettingFromDesktopValues(
  runtimeKey: string,
  values: Record<string, string> | undefined,
): unknown {
  if (!values) return undefined
  if (runtimeKey === 'tts_mode') {
    if (values.ENABLE_CUDA_GRAPH === undefined && values.EXP_TTS_MAX_CONCURRENCY === undefined) return undefined
    return values.ENABLE_CUDA_GRAPH === '1' ? 'cuda_graph' : 'parallel'
  }
  if (runtimeKey === 'tts_output_language') {
    const value = values.TTS_OUTPUT_LANGUAGE
    if (value === undefined) return undefined
    return value === '英文' ? 'en' : 'ja'
  }
  const desktopKey = RUNTIME_VALUE_KEYS[runtimeKey]
  const value = desktopKey ? values[desktopKey] : undefined
  if (value === undefined) return undefined
  if (BOOLEAN_RUNTIME_KEYS.has(runtimeKey)) return value === 'true'
  if (NUMBER_RUNTIME_KEYS.has(runtimeKey)) return Number(value)
  return value
}

export function desktopValuesForRuntimeSettings(
  values: Record<string, unknown>,
): Record<string, string | boolean | null> {
  const desktopValues: Record<string, string | boolean | null> = {}
  for (const [runtimeKey, rawValue] of Object.entries(values)) {
    if (runtimeKey === 'tts_mode') {
      const graph = String(rawValue) === 'cuda_graph'
      desktopValues.ENABLE_CUDA_GRAPH = graph ? '1' : '0'
      desktopValues.EXP_TTS_MAX_CONCURRENCY = graph ? '1' : '2'
      continue
    }
    if (runtimeKey === 'tts_output_language') {
      desktopValues.TTS_OUTPUT_LANGUAGE = String(rawValue).toLowerCase().startsWith('en') ? '英文' : '日文'
      continue
    }
    const desktopKey = RUNTIME_VALUE_KEYS[runtimeKey]
    if (desktopKey) desktopValues[desktopKey] = rawValue as string | boolean | null
  }
  return desktopValues
}

export async function persistDesktopRuntimeSettings(
  values: Record<string, unknown>,
): Promise<{ persisted: boolean; desktopKeys: string[]; pendingRevisions: Record<string, number>; settings?: Record<string, unknown> }> {
  if (!window.amadeus) return { persisted: false, desktopKeys: [], pendingRevisions: {} }
  const desktopValues = desktopValuesForRuntimeSettings(values)
  const desktopKeys = Object.keys(desktopValues)
  if (!desktopKeys.length) return { persisted: false, desktopKeys: [], pendingRevisions: {} }
  const result = await window.amadeus.updateDesktopSettings({ values: desktopValues })
  if (!result.ok) throw new Error(result.error || 'Could not save desktop setting')
  const pending = result.settings?.pendingRevisions
  const pendingRecord = pending && typeof pending === 'object' && !Array.isArray(pending)
    ? pending as Record<string, unknown>
    : {}
  const pendingRevisions = Object.fromEntries(desktopKeys.flatMap(key => {
    const revision = Number(pendingRecord[key])
    return Number.isSafeInteger(revision) && revision > 0 ? [[key, revision]] : []
  }))
  return { persisted: true, desktopKeys, pendingRevisions, settings: result.settings }
}

export async function markRuntimeSettingsApplied(
  values: Record<string, unknown>,
  expectedRevisions: Record<string, number>,
): Promise<Record<string, unknown> | undefined> {
  if (!window.amadeus) return undefined
  const keys = Object.keys(desktopValuesForRuntimeSettings(values))
  if (!keys.length) return undefined
  const revisions = Object.fromEntries(keys.flatMap(key => expectedRevisions[key] ? [[key, expectedRevisions[key]]] : []))
  if (!Object.keys(revisions).length) return undefined
  const result = await window.amadeus.markDesktopSettingsApplied(revisions)
  if (!result.ok) throw new Error(result.error || 'Could not confirm applied desktop setting')
  return result.settings
}
