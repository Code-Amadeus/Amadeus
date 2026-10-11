/** Public extension metadata. Secret values never cross the renderer bridge. */
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
}
export type ExtensionManifest = {
  schema: 'amadeus.extension/v0'
  id: string
  version: string
  display_name: ExtensionText
  description: ExtensionText
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
  id: string
  version: string
  digest: string
  manifest: ExtensionManifest | null
  enabled: boolean
  pendingRemoval: boolean
  values: Record<string, ExtensionConfigValue>
  secretConfigured: Record<string, boolean>
  providers: string[]
  status: 'disabled' | 'enabled' | 'pending_removal' | 'unavailable'
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
export type ExtensionMutationResult = {
  ok: boolean
  cancelled?: boolean
  error?: string
  settings?: Record<string, unknown>
}
