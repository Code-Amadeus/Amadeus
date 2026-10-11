import type { ExtensionSummary, ExtensionConfigField, ExtensionConfigValue, ExtensionLocalizedText, ExtensionUpdate } from '../../shared/extensions.js'

export interface ExtensionRuntimeStatus {
  id: string
  version: string
  enabled: boolean
  state: string
  detail?: string
  contributions: Array<{ kind: string; id: string; qualified_id: string; health: string; detail?: string }>
}

export interface ExtensionProvider {
  provider_id: string
  display_name: string
  capabilities?: { capability_projections?: string[] }
}

export interface ExtensionDraft {
  enabled: boolean
  values: Record<string, string | boolean>
  secrets: Record<string, string | null>
  providers: string[]
}

export function extensionText(value: ExtensionLocalizedText | undefined, locale = 'en-US', fallback = ''): string {
  if (typeof value === 'string') return value
  return value?.[locale] || value?.[locale.split('-')[0]] || value?.en || Object.values(value || {})[0] || fallback
}

export function extensionDraft(extension: ExtensionSummary): ExtensionDraft {
  const values: ExtensionDraft['values'] = {}
  for (const [key, field] of Object.entries(extension.manifest?.config || {})) {
    if (field.secret) continue
    const value = extension.values[key] ?? field.default ?? ''
    values[key] = field.type === 'boolean' ? value === true : String(value)
  }
  return { enabled: extension.enabled, values, secrets: {}, providers: [...extension.providers] }
}

function typedValue(value: string | boolean, field: ExtensionConfigField): ExtensionConfigValue | null {
  if (field.type === 'boolean') return value === true
  if (field.type === 'integer' || field.type === 'number') return value === '' ? null : Number(value)
  return value === '' && !field.required ? null : String(value)
}

/** Only explicit edits cross the persistence boundary; saved secrets are never read. */
export function extensionUpdate(extension: ExtensionSummary, draft: ExtensionDraft): ExtensionUpdate {
  if (!extension.manifest) throw new Error('Extension metadata is unavailable.')
  const initial = extensionDraft(extension)
  const values: Record<string, ExtensionConfigValue | null> = {}
  for (const [key, value] of Object.entries(draft.values)) {
    if (value !== initial.values[key]) values[key] = typedValue(value, extension.manifest.config![key])
  }
  const secrets = Object.fromEntries(Object.entries(draft.secrets).filter(([, value]) => value === null || value !== ''))
  return { id: extension.id, enabled: draft.enabled, values, secrets, providers: [...draft.providers] }
}

export function compatibleExtensionProviders(providers: ExtensionProvider[]): ExtensionProvider[] {
  return providers.filter(provider => provider.capabilities?.capability_projections?.includes('mcp_connection'))
}

export function extensionLanguageSupported(languages: string[], language: string): boolean {
  return languages.some(tag => tag.split('-')[0].toLowerCase() === language.split('-')[0].toLowerCase())
}

export function extensionVoiceOptions(extensions: ExtensionSummary[], locale = 'en-US', selected = ''): Array<{ value: string; label: string; disabled: boolean; unavailable?: boolean }> {
  const options = extensions.filter(extension => !extension.pendingRemoval && extension.manifest).flatMap(extension =>
    (extension.manifest!.contributes.voice_backend || []).filter(voice => voice.role === 'tts').map(voice => ({
      value: `${extension.id}/${voice.id}`,
      label: `${extensionText(voice.title, locale, extensionText(extension.manifest!.display_name, locale, extension.id))} · ${extension.id}`,
      disabled: !extension.enabled,
      unavailable: !extension.enabled,
    })),
  )
  // The selected durable identity remains diagnosable after disable or uninstall.
  if (selected.includes('/') && !options.some(option => option.value === selected)) {
    options.push({ value: selected, label: selected, disabled: true, unavailable: true })
  }
  return options
}

export function extensionRuntimeLabel(state?: string): string {
  return ({ active: 'Active', ready: 'Ready', degraded: 'Degraded', starting: 'Starting', needs_config: 'Needs setup', incompatible: 'Incompatible',
    error: 'Failed', inactive: 'Inactive', disabled: 'Inactive' } as Record<string, string>)[state || ''] || 'Backend status unavailable'
}
