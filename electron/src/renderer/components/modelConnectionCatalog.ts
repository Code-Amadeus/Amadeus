import { catalogConfiguration, visibleCatalogFields } from '../../shared/configCatalog.js'
import type { StartupSnapshot } from '../../shared/startupSettings.js'

export interface ModelConnectionCatalogField {
  key: string
  label: string
  type: 'text' | 'url' | 'path' | 'number' | 'select' | 'boolean' | 'secret'
  description?: string
  value?: string | boolean
  configured?: boolean
  options?: Array<string | { value: string; label: string }>
  min?: number
  max?: number
  step?: number
  editable: boolean
  restart_required: boolean
}

export interface ModelConnectionCatalogGroup {
  id: string
  label: string
  description?: string
  active: boolean
  configured: boolean
  status: string
  status_ok: boolean
  fields: ModelConnectionCatalogField[]
}

interface CatalogSnapshot extends StartupSnapshot {
  secrets?: Record<string, { configured?: boolean }>
}

/** Runtime readiness does not decide whether connection setup is discoverable. */
export function buildRemoteModelConnectionCatalog(
  activeProvider: string, snapshot?: CatalogSnapshot | null,
): ModelConnectionCatalogGroup[] {
  const activeConnections = new Set(({
    hybrid: ['bedrock'], hybrid2: ['deepseek'], hybrid3: ['openai'],
  } as Record<string, string[]>)[activeProvider] || [activeProvider])
  return ['deepseek', 'openai', 'gemini', 'bedrock'].map(id => {
    const group = catalogConfiguration(id, snapshot)
    const configured = group.fields.filter(field => field.type === 'secret').every(field => field.configured)
    const active = activeConnections.has(id)
    return { ...group, active, configured,
      status: configured ? 'Credential saved' : active ? 'Needs setup' : 'Optional', status_ok: configured }
  })
}

export function buildLocalModelConnectionCatalog(
  activeProvider: string, snapshot?: CatalogSnapshot | null,
  effectiveValues: Record<string, string | boolean> = {},
): ModelConnectionCatalogGroup[] {
  const local = catalogConfiguration('local', snapshot, effectiveValues)
  const value = (key: string) => local.fields.find(field => field.key === key)?.value
  const hybrid = catalogConfiguration('hybrid_local', snapshot, {
    HYBRID_LOCAL_LLM_URL: value('LOCAL_LLM_URL'),
    HYBRID_LOCAL_LLM_MODEL: value('LOCAL_LLM_MODEL'),
  }, effectiveValues)
  return [local, hybrid].map(group => {
    const active = group.id === 'local' ? activeProvider === 'local' : ['hybrid', 'hybrid2', 'hybrid3'].includes(activeProvider)
    return { ...group, active, configured: false,
      status: active ? 'Backend status unavailable' : 'Optional', status_ok: false,
      fields: visibleCatalogFields(group.fields),
    }
  })
}

export function buildOptionalModelServiceCatalog(snapshot?: CatalogSnapshot | null): ModelConnectionCatalogGroup[] {
  const group = catalogConfiguration('character_rag', snapshot)
  const enabled = group.fields.find(field => field.key === 'RAG_ENABLED')?.value === true
  return [{ ...group, active: enabled, configured: false,
    status: enabled ? 'Backend status unavailable' : 'Off', status_ok: !enabled }]
}
