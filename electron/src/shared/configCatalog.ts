import { catalogGroups as generatedGroups } from './configCatalog.generated.js'
import { startupValue, type StartupSnapshot } from './startupSettings.js'

type LocalizedText = { 'en-US': string; 'zh-CN': string }
interface CatalogField {
  type: 'string' | 'path' | 'url' | 'enum' | 'boolean' | 'integer' | 'number'
  title: LocalizedText
  description?: LocalizedText
  default?: string | boolean | number
  computed_default?: boolean
  example?: string | number | boolean
  example_active?: boolean
  secret?: boolean
  options?: Array<string | { value: string; label: LocalizedText }>
  schemes?: string[]
  min?: number
  max?: number
  step?: number
}
export interface CatalogGroup {
  id: string
  title: LocalizedText
  description: LocalizedText
  desktop: boolean
  restart_required: boolean
  config: Record<string, CatalogField>
  section?: 'output' | 'remote'
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
export const desktopCatalogFields = Object.fromEntries(
  catalogGroups.filter(group => group.desktop).flatMap(group => Object.entries(group.config)),
)
export const catalogTranslations = Object.fromEntries(catalogGroups.flatMap(group =>
  [group.title, group.description, ...(group.voice_backend ? [group.voice_backend.label] : []),
    ...Object.values(group.config).flatMap(field => [field.title, ...(field.description ? [field.description] : []),
      ...(field.options || []).flatMap(option => typeof option === 'string' ? [] : [option.label])])]
    .map(text => [text['en-US'], text['zh-CN']]),
))

export const voiceBackendGroups = catalogGroups.filter(group => group.voice_backend)
  .sort((left, right) => left.voice_backend!.order - right.voice_backend!.order)

export function catalogOptionValues(field: CatalogField): string[] | undefined {
  return field.options?.map(option => typeof option === 'string' ? option : option.value)
}

export const voiceBackendOptions = [
  ...voiceBackendGroups.map(group => ({ value: group.voice_backend!.id, label: group.voice_backend!.label['en-US'] })),
  { value: 'disabled', label: 'Disabled' },
]

export function catalogConfiguration(
  id: string,
  snapshot?: StartupSnapshot & { secrets?: Record<string, { configured?: boolean }> } | null,
  effectiveValues: Record<string, string | number | boolean> = {},
) {
  const group = catalogGroups.find(item => item.id === id)
  if (!group) throw new Error(`Unknown configuration group: ${id}`)
  return {
    id: group.id,
    label: group.title['en-US'],
    description: group.description['en-US'],
    fields: Object.entries(group.config).map(([key, field]) => {
      const raw = startupValue(key, snapshot, field.default ?? effectiveValues[key], effectiveValues[key])
      if (!field.secret && raw === undefined && !snapshot?.sources?.[key]) throw new Error(`Missing computed default for ${key}`)
      return {
      key,
      label: field.title['en-US'],
      type: field.secret ? 'secret' as const : field.type === 'enum' ? 'select' as const
        : field.type === 'url' ? 'url' as const : field.type === 'path' ? 'path' as const
        : field.type === 'boolean' ? 'boolean' as const
        : ['integer', 'number'].includes(field.type) ? 'number' as const : 'text' as const,
      value: field.secret ? '' : raw === undefined ? undefined : field.type === 'boolean'
        ? raw === true || ['1', 'true', 'yes'].includes(String(raw).toLowerCase()) : String(raw),
      ...(field.description ? { description: field.description['en-US'] } : {}),
      ...(field.secret ? { configured: Boolean(snapshot?.secrets?.[key]?.configured) } : {}),
      ...(field.options ? { options: field.options.map(option => typeof option === 'string' ? option
        : { value: option.value, label: option.label['en-US'] }) } : {}),
      ...(field.min !== undefined ? { min: field.min, max: field.max, step: field.step } : {}),
      editable: group.desktop,
      restart_required: group.restart_required,
    }}),
  }
}
