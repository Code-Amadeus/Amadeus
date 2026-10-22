/** Public extension metadata. Secret values never cross the renderer bridge. */
import { extensionRequiresRestart } from './extensionProfile.js'
export type ExtensionText = string | Record<string, string>
export type ExtensionLocalizedText = ExtensionText
export type ExtensionConfigValue = string | boolean | number
export type ExtensionConfigField = {
  type: 'string' | 'url' | 'boolean' | 'integer' | 'number' | 'enum'
  title: ExtensionText
  description?: ExtensionText
  required?: boolean
  default?: ExtensionConfigValue
  secret?: boolean
  options?: Array<string | { value: string; label?: ExtensionText }>
  min?: number
  max?: number
  max_length?: number
  apply?: 'live' | 'reactivate'
}
export type ExtensionContribution = {
  base?: 'classic' | 'wallpaper-slice'
  tokens?: Record<string, string>
  background?: { image: string; fit: 'cover' | 'contain'; dim: number }
  id: string
  contract: string
  title?: ExtensionText
  summary?: ExtensionText
  role?: string
  deployment?: string
  languages?: string[]
  streaming?: boolean
  transport?: 'stdio' | 'http'
  command?: string
  args?: string[]
  env?: Record<string, string>
  url?: string
  bearer_token?: string
  path?: string
}
export type ExtensionManifest = {
  schema: 'amadeus.extension/v0'
  id: string
  version: string
  display_name: ExtensionText
  description: ExtensionText
  icon?: string
  author: { name: string; email?: string; url?: string }
  license?: string
  homepage?: string
  repository?: string
  engines: { amadeus_host_api: string; platforms?: string[] }
  runtime: { kind: 'none' | 'python'; entry?: string; requires_python?: string }
  contributes: Record<string, ExtensionContribution[]>
  config?: Record<string, ExtensionConfigField>
  permissions?: { host?: unknown[]; os?: Record<string, unknown> }
}
export type InstalledExtensionSummary = {
  revision?: number
  appliedRevision?: number
  appliedVersion?: string
  application?: 'frontend' | 'backend_restart'
  backendStillRunning?: boolean
  frontendSuppressed?: boolean
  applicationError?: string
  id: string
  version: string
  digest: string
  manifest: ExtensionManifest | null
  enabled: boolean
  pendingRemoval: boolean
  values: Record<string, ExtensionConfigValue>
  secretConfigured: Record<string, boolean>
  providers: string[]
  status: 'disabled' | 'enabled' | 'pending_removal' | 'unavailable' | 'incompatible'
  restartRequired: boolean
  validationError?: string
}
export type ExtensionSummary = InstalledExtensionSummary
export type ExtensionUpdate = {
  id: string
  enabled?: boolean
  values?: Record<string, ExtensionConfigValue | null>
  secrets?: Record<string, string | null>
  providers?: string[]
}
export type ExtensionRemovalOptions = { deleteData?: boolean }
export type ExtensionDelivery = { digest?: string; sourceFile?: string }
export type ExtensionConfirmation = ExtensionDelivery & { operation: 'install' | 'update' | 'enable'; manifest: ExtensionManifest; previousVersion?: string; previousManifest?: ExtensionManifest }

export function extensionDisclosure(manifest: ExtensionManifest, previous?: ExtensionManifest, delivery: ExtensionDelivery = {}): string {
  const name = typeof manifest.display_name === 'string' ? manifest.display_name : manifest.display_name['en-US'] || manifest.display_name.en || manifest.id
  const restart = extensionRequiresRestart(manifest) || Boolean(previous && extensionRequiresRestart(previous))
  const updates: string[] = []
  if (previous) {
    for (const [kind, items] of Object.entries(manifest.contributes)) for (const item of items) {
      const old = previous.contributes[kind]?.find(other => other.id === item.id)
      if (!old) updates.push(`New contribution: ${kind}/${item.id}`)
      else if (JSON.stringify(item) !== JSON.stringify(old)) updates.push(`Changed contribution: ${kind}/${item.id}`)
      if (item.command && (!old || item.command !== old.command || JSON.stringify(item.args) !== JSON.stringify(old.args))) updates.push(`New or changed command: ${item.command} ${(item.args || []).join(' ')}`)
    }
    for (const [kind, value] of Object.entries(manifest.permissions?.os || {})) {
      const old = previous.permissions?.os?.[kind]
      for (const item of Array.isArray(value) ? value : [value]) {
        if (!(Array.isArray(old) ? old : old === undefined ? [] : [old]).some(other => JSON.stringify(other) === JSON.stringify(item))) updates.push(`New ${kind} disclosure: ${JSON.stringify(item)}`)
      }
    }
    if (JSON.stringify(manifest.runtime) !== JSON.stringify(previous.runtime)) updates.push(`Changed runtime: ${manifest.runtime.kind}${manifest.runtime.entry ? ` · ${manifest.runtime.entry}` : ''}`)
  }
  return [
    `${name}\n${manifest.id} · ${manifest.version}\nAuthor: ${manifest.author.name}`,
    `License: ${manifest.license || 'Not declared'}`,
    ...(delivery.digest ? [`Wheel SHA-256: ${delivery.digest}`] : []),
    ...(delivery.sourceFile ? [`Source: local file ${delivery.sourceFile}`] : []),
    ...(updates.length ? [`NEW OR CHANGED IN THIS UPDATE\n${updates.join('\n')}`] : []),
    `Contributions:\n${Object.entries(manifest.contributes).flatMap(([kind, items]) => items.map(item =>
      `${kind}: ${item.id}${item.command ? `\nCommand: ${item.command} ${(item.args || []).join(' ')}` : ''}${item.url ? `\nURL: ${item.url}` : ''}${item.path ? `\nResource: ${item.path}` : ''}`)).join('\n')}`,
    `Runtime: ${manifest.runtime.kind}${manifest.runtime.entry ? ` · ${manifest.runtime.entry}` : ''}`,
    `OS access declared by the author:\n${JSON.stringify(manifest.permissions?.os || {}, null, 2)}`,
    manifest.runtime.kind === 'none' ? 'Resources are extracted without pip or importing package code. Selected Agents may execute declared MCP services or Skill scripts under their own permissions.'
      : 'These declarations are not an OS sandbox. Package code runs with your local user access. Installation may download dependencies.',
    restart ? 'Changes apply after backend restart. Disabling or removing closes frontend contributions immediately; the previous backend still runs until restart.'
      : 'This frontend-only package applies immediately without restarting the backend.',
  ].join('\n\n')
}

export type ExtensionMutationResult = {
  ok: boolean
  cancelled?: boolean
  error?: string
  settings?: Record<string, unknown>
}
