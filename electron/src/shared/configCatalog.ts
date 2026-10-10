import { catalogGroups as generatedGroups } from './configCatalog.generated.js'
import { startupValue, type StartupSnapshot } from './startupSettings.js'

type LocalizedText = { 'en-US': string; 'zh-CN': string }
export type SettingApplication = 'frontend' | 'host' | 'backend_restart' | 'desktop_restart'
export interface CatalogField {
  type: 'string' | 'path' | 'url' | 'enum' | 'boolean' | 'integer' | 'number'
  title: LocalizedText
  description?: LocalizedText
  ui_description?: LocalizedText | null
  icon?: string
  default?: string | boolean | number
  desktop_default?: { value: string | boolean | number; platforms?: string[] }
  computed_default?: boolean
  example?: string | number | boolean
  example_active?: boolean
  secret?: boolean
  accepted_values?: string[]
  true_values?: string[]
  aliases?: string[]
  setting?: string
  control?: 'number' | 'select'
  scope?: 'backend' | 'session' | 'virtual' | 'desktop'
  visible_when?: Record<string, string[]>
  apply?: SettingApplication
  runtime_key?: string
  identifier?: boolean
  max_length?: number
  allow_empty?: boolean
  trim?: boolean
  options?: Array<string | { value: string; label: LocalizedText; description?: LocalizedText; hidden?: boolean }>
  schemes?: string[]
  min?: number
  max?: number
  step?: number
}
export interface CatalogGroup {
  id: string
  title: LocalizedText
  description: LocalizedText
  ui_description?: LocalizedText
  desktop: boolean
  apply: SettingApplication
  config: Record<string, CatalogField>
  section?: 'output' | 'remote' | 'input' | 'roles' | 'providers' | 'routing'
  order?: number
  voice_backend?: {
    id: string
    label: LocalizedText
    deployment: 'embedded' | 'remote'
    order: number
    factory: string
    probe: string
    summary: string
    streaming: boolean | string
    reference_conditioning: boolean
  }
}

export const catalogGroups = generatedGroups
export function catalogLaunchDefaults(platform: string): Record<string, string> {
  return Object.fromEntries(catalogGroups.flatMap(group => Object.entries(group.config).flatMap(([key, field]) => {
    const launch = field.desktop_default
    return launch && (!launch.platforms || launch.platforms.includes(platform)) ? [[key, String(launch.value)]] : []
  })))
}
export function catalogApplication(key: string): SettingApplication {
  const group = catalogGroups.find(group => key in group.config || Object.values(group.config).some(field => field.aliases?.includes(key)))
  return desktopCatalogFields[key]?.apply ?? group?.apply ?? 'backend_restart'
}
export const runtimeCatalogFields = Object.fromEntries(catalogGroups.flatMap(group => Object.entries(group.config)
  .filter(([, field]) => field.runtime_key).map(([key, field]) => [field.runtime_key!, { key, ...field }])))
export const desktopCatalogFields = Object.fromEntries(
  catalogGroups.filter(group => group.desktop).flatMap(group => Object.entries(group.config)
    .flatMap(([key, field]) => [[key, field] as const, ...(field.aliases || []).map(alias => [alias, field] as const)])),
)
export const catalogTranslations = Object.fromEntries(catalogGroups.flatMap(group =>
  [group.title, group.description, ...(group.ui_description ? [group.ui_description] : []), ...(group.voice_backend ? [group.voice_backend.label] : []),
    ...Object.values(group.config).flatMap(field => [field.title, ...(field.description ? [field.description] : []), ...(field.ui_description ? [field.ui_description] : []),
      ...(field.options || []).flatMap(option => typeof option === 'string' ? [] : [option.label, ...(option.description ? [option.description] : [])])])]
    .map(text => [text['en-US'], text['zh-CN']]),
))

export function catalogInputKeys(key: string): string[] {
  const entry = catalogGroups.flatMap(group => Object.entries(group.config))
    .find(([canonical, field]) => canonical === key || field.aliases?.includes(key))
  return entry ? [entry[0], ...(entry[1].aliases || [])] : [key]
}

export function visibleCatalogFields<T extends { key: string; value?: string | boolean }>(fields: T[]): T[] {
  const values = Object.fromEntries(fields.map(field => [field.key, field.value]))
  return fields.filter(field => Object.entries(desktopCatalogFields[field.key]?.visible_when || {})
    .every(([selector, choices]) => values[selector] === undefined || choices.includes(String(values[selector]))))
}

export const voiceBackendGroups = catalogGroups.filter(group => group.voice_backend)
  .sort((left, right) => (left.order ?? left.voice_backend!.order) - (right.order ?? right.voice_backend!.order))

export function catalogOptionValues(field: CatalogField): string[] | undefined {
  return field.options?.map(option => typeof option === 'string' ? option : option.value)
}

/** Show a known startup input verbatim even when its owner normalizes it later. */
export function optionsWithCurrentValue<T extends string | { value: string }>(options: T[], value: string): Array<T | string> {
  return options.some(option => (typeof option === 'string' ? option : option.value) === value) ? options : [value, ...options]
}

export const voiceBackendOptions = [
  ...voiceBackendGroups.map(group => ({ value: group.voice_backend!.id, label: group.voice_backend!.label['en-US'] })),
  { value: 'disabled', label: 'Disabled' },
]

export function catalogConfiguration(
  id: string,
  snapshot?: StartupSnapshot & { secrets?: Record<string, { configured?: boolean }> } | null,
  contextValues: Record<string, string | number | boolean | undefined> = {},
  effectiveValues: Record<string, string | number | boolean | undefined> = contextValues,
) {
  const group = catalogGroups.find(item => item.id === id)
  if (!group) throw new Error(`Unknown configuration group: ${id}`)
  return {
    id: group.id,
    label: group.title['en-US'],
    description: (group.ui_description ?? group.description)['en-US'],
    fields: Object.entries(group.config).map(([key, field]) => {
      const raw = startupValue(key, snapshot, field.default ?? contextValues[key], effectiveValues[key])
      const description = field.ui_description === null ? undefined : field.ui_description ?? field.description
      if (!field.secret && raw === undefined && !snapshot?.sources?.[key] && !(key in contextValues)) throw new Error(`Missing computed default for ${key}`)
      return {
      key,
      label: field.title['en-US'],
      type: field.secret ? 'secret' as const : field.type === 'enum' || field.control === 'select' ? 'select' as const
        : field.type === 'url' ? 'url' as const : field.type === 'path' ? 'path' as const
        : field.type === 'boolean' ? 'boolean' as const
        : field.control === 'number' || ['integer', 'number'].includes(field.type) ? 'number' as const : 'text' as const,
      value: field.secret ? '' : raw === undefined ? undefined : field.type === 'boolean'
        ? raw === true || (field.true_values || ['1', 'true', 'yes']).includes(String(raw).trim().toLowerCase()) : String(raw),
      ...(description ? { description: description['en-US'] } : {}),
      ...(field.true_values ? { true_values: field.true_values } : {}),
      ...(field.secret ? { configured: Boolean(snapshot?.secrets?.[key]?.configured) } : {}),
      ...(field.options ? { options: field.options.filter(option => typeof option === 'string' || !option.hidden).map(option => typeof option === 'string' ? option
        : { value: option.value, label: option.label['en-US'] }) } : {}),
      ...(field.min !== undefined ? { min: field.min, max: field.max, step: field.step } : {}),
      editable: group.desktop,
      restart_required: catalogApplication(key) === 'backend_restart',
      apply: catalogApplication(key),
    }}),
  }
}
