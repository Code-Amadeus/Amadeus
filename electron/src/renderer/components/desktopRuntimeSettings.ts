import { desktopCatalogFields, runtimeCatalogFields } from '../../shared/configCatalog.js'
import { startupValue, type StartupSnapshot } from '../../shared/startupSettings.js'

// TTS mode and language are composite projections owned by the existing TTS controls.
const COMPOSITE_RUNTIME_INPUTS: Record<string, string[]> = {
  tts_mode: ['ENABLE_CUDA_GRAPH', 'EXP_TTS_MAX_CONCURRENCY'],
  tts_output_language: ['TTS_OUTPUT_LANGUAGE'],
}

export function runtimeSettingValue(runtimeKey: string, snapshot?: StartupSnapshot | null, effective?: unknown): unknown {
  const field = runtimeCatalogFields[runtimeKey]
  if (field) {
    const defaultValue = field.computed_default && !snapshot?.pendingRevisions?.[field.key] ? effective : field.default
    const value = startupValue(field.key, snapshot, defaultValue as string | number | boolean | undefined, effective as string | number | boolean | undefined)
    return value === undefined ? undefined : runtimeSettingFromDesktopValues(runtimeKey, { [field.key]: String(value) })
  }
  const inputs = COMPOSITE_RUNTIME_INPUTS[runtimeKey]
  if (!inputs) return effective
  // An existing backend owns the composite value unless an input is pending or
  // explicitly saved/known. Without it, use declared defaults for each input.
  if (effective !== undefined && !inputs.some(key => snapshot?.pendingRevisions?.[key]
    || snapshot?.sources?.[key] === 'user' || snapshot?.startupValues?.[key] !== undefined)) return effective
  const values = Object.fromEntries(inputs.map(key => [key, startupValue(key, snapshot, desktopCatalogFields[key].default)]))
  if (Object.values(values).some(value => value === undefined)) return undefined
  return runtimeSettingFromDesktopValues(runtimeKey, Object.fromEntries(Object.entries(values).map(([key, value]) => [key, String(value)])))
}

export function runtimeSettingFromDesktopValues(
  runtimeKey: string,
  values: Record<string, string> | undefined,
): unknown {
  if (!values) return undefined
  if (runtimeKey === 'tts_mode') {
    if (values.ENABLE_CUDA_GRAPH === undefined && values.EXP_TTS_MAX_CONCURRENCY === undefined) return undefined
    if ((values.ENABLE_CUDA_GRAPH ?? desktopCatalogFields.ENABLE_CUDA_GRAPH.default) === 'auto') return 'auto'
    if (values.ENABLE_CUDA_GRAPH === '1') return 'cuda_graph'
    return Number(values.EXP_TTS_MAX_CONCURRENCY || desktopCatalogFields.EXP_TTS_MAX_CONCURRENCY.default) > 1 ? 'parallel2' : 'parallel'
  }
  if (runtimeKey === 'tts_output_language') {
    const value = values.TTS_OUTPUT_LANGUAGE
    if (value === undefined) return undefined
    return value === '英文' ? 'en' : 'ja'
  }
  const field = runtimeCatalogFields[runtimeKey]
  const value = field ? values[field.key] : undefined
  if (value === undefined) return undefined
  if (field.type === 'boolean') return (field.true_values || ['true', '1', 'yes']).includes(value.trim().toLowerCase())
  if (['integer', 'number'].includes(field.type)) return Number(value)
  return value
}

export function desktopValuesForRuntimeSettings(
  values: Record<string, unknown>,
): Record<string, string | boolean | null> {
  const desktopValues: Record<string, string | boolean | null> = {}
  for (const [runtimeKey, rawValue] of Object.entries(values)) {
    if (runtimeKey === 'tts_mode') {
      const graph = String(rawValue) === 'cuda_graph'
      desktopValues.ENABLE_CUDA_GRAPH = String(rawValue) === 'auto' ? 'auto' : graph ? '1' : '0'
      desktopValues.EXP_TTS_MAX_CONCURRENCY = String(rawValue) === 'parallel2' ? '2' : '1'
      continue
    }
    if (runtimeKey === 'tts_output_language') {
      desktopValues.TTS_OUTPUT_LANGUAGE = String(rawValue).toLowerCase().startsWith('en') ? '英文' : '日文'
      continue
    }
    const desktopKey = runtimeCatalogFields[runtimeKey]?.key
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
