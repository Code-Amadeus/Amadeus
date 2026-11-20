import { catalogConfiguration, desktopCatalogFields, visibleCatalogFields } from '../../shared/configCatalog.js'
import { startupValue, type StartupSnapshot } from '../../shared/startupSettings.js'
import type { ModelConnectionCatalogGroup } from './modelConnectionCatalog'

interface ProviderDesktopSnapshot extends StartupSnapshot {
  values?: Record<string, string>
  secrets?: Record<string, { configured?: boolean }>
}

export function buildWorkProviderCatalog(
  runtimeSelection: { provider: string; codingProvider?: string;
    roleCandidates?: Record<string, string[]> },
  snapshot?: ProviderDesktopSnapshot | null,
  effectiveValues: Record<string, string | boolean> = {},
): { routing: ModelConnectionCatalogGroup; connections: ModelConnectionCatalogGroup[] } {
  const value = (key: string, fallback?: string) => {
    const raw = startupValue(key, snapshot, fallback ?? desktopCatalogFields[key]?.default, effectiveValues[key])
    return raw === undefined ? undefined : String(raw)
  }
  const secret = (key: string) => Boolean(snapshot?.secrets?.[key]?.configured)
  const provider = value('WORK_EXECUTION_PROVIDER', value('COOPERATIVE_CHAT_PROVIDER', runtimeSelection.provider || undefined)) ?? ''
  const codingProvider = value('WORK_CODING_PROVIDER', runtimeSelection.codingProvider || undefined) ?? ''
  const roleOptions = (role: string, assigned: string) => {
    const candidates = runtimeSelection.roleCandidates?.[role] || []
    return [...new Set([...candidates, assigned])].map(id => ({ value: id,
      label: candidates.includes(id) ? id : `${id} · ${runtimeSelection.roleCandidates ? 'Unavailable' : 'Backend status unavailable'}` }))
  }
  const assigned = (id: string) => provider === id || codingProvider === id
  const transport = value('CODEX_PROVIDER_TRANSPORT') ?? ''
  const codexAuthMode = value('CODEX_APP_SERVER_AUTH_MODE') ?? ''
  const codexModelConnection = value('CODEX_APP_SERVER_MODEL_PROVIDER') ?? ''
  const connectionDefaults = codexModelConnection === 'openai'
    ? { model: value('OPENAI_MODEL_NAME'), credentialKey: 'OPENAI_API_KEY' }
    : { model: value('DEEPSEEK_MODEL_NAME'), credentialKey: 'DEEPSEEK_API_KEY' }
  const codexConnectionOptions = [
    ...(secret('DEEPSEEK_API_KEY') || codexModelConnection === 'deepseek'
      ? [{ value: 'deepseek', label: secret('DEEPSEEK_API_KEY') ? 'DeepSeek' : 'DeepSeek · Not configured' }]
      : []),
    ...(secret('OPENAI_API_KEY') || codexModelConnection === 'openai'
      ? [{ value: 'openai', label: secret('OPENAI_API_KEY') ? 'OpenAI-compatible' : 'OpenAI-compatible · Not configured' }]
      : []),
  ]
  const computed = {
    WORK_CODING_PROVIDER: codingProvider, WORK_EXECUTION_PROVIDER: provider,
    CODEX_APP_SERVER_MODEL: connectionDefaults.model,
    CODEX_APP_SERVER_PROVIDER_BASE_URL: value(codexModelConnection === 'openai' ? 'OPENAI_BASE_URL' : 'DEEPSEEK_BASE_URL'),
    PI_MODEL: value('DEEPSEEK_MODEL_NAME'),
  }
  const unknown = 'Backend status unavailable'
  // Non-desktop clients have runtime selections but no startup source map.
  const routingValues = { ...(!snapshot?.sources ? {
    WORK_CODING_PROVIDER: codingProvider, WORK_EXECUTION_PROVIDER: provider,
  } : {}), ...effectiveValues }

  const routing: ModelConnectionCatalogGroup = {
    ...catalogConfiguration('work_routing', snapshot, computed, routingValues),

    active: true,
    configured: true,
    status: 'Assigned',
    status_ok: true,
    fields: catalogConfiguration('work_routing', snapshot, computed, routingValues).fields.map(item => ({ ...item, type: 'select' as const,
      options: item.key === 'WORK_CODING_PROVIDER' ? roleOptions('coding', codingProvider) : roleOptions('execution', provider) })),
  }

  const codexFields = visibleCatalogFields(catalogConfiguration('codex', snapshot, computed, effectiveValues).fields)
    .map(item => item.key === 'CODEX_APP_SERVER_MODEL_PROVIDER' ? { ...item, options: codexConnectionOptions } : item)
  const codexCredentialReady = transport === 'direct' || codexAuthMode === 'chatgpt' || secret(connectionDefaults.credentialKey)
  const piModelProvider = (value('PI_MODEL_PROVIDER') ?? '').toLowerCase()
  const piCredentialKey = ({ deepseek: 'DEEPSEEK_API_KEY', openai: 'OPENAI_API_KEY',
    google: 'GEMINI_API_KEY', gemini: 'GEMINI_API_KEY' } as Record<string, string>)[piModelProvider]
  // Unknown/custom providers may use Pi's isolated native auth store. Known
  // Amadeus model connections use the same desktop credential as Main Chat.
  const piCredentialReady = piCredentialKey ? secret(piCredentialKey) : true
  const piGroup = catalogConfiguration('pi', snapshot, computed, effectiveValues)
  const piEnabled = piGroup.fields.find(field => field.key === 'PI_PROVIDER_ENABLED')?.value

  const connections: ModelConnectionCatalogGroup[] = [
    {
      ...piGroup,

      active: assigned('pi'),
      configured: piEnabled === true && piCredentialReady,
      status: piEnabled === false ? 'Off' : assigned('pi') && !piCredentialReady ? 'Needs setup' : unknown,
      status_ok: false,

    },
    {
      id: 'browser',
      label: 'Browser',
      description: 'Host-managed browser Work Provider; no user-managed connection settings.',
      active: assigned('browser'),
      configured: true,
      status: provider === 'browser' ? unknown : 'Optional',
      status_ok: false,
      fields: [],
    },
    {
      ...catalogConfiguration('openclaw', snapshot, computed, effectiveValues),

      active: assigned('openclaw'),
      configured: secret('OPENCLAW_GATEWAY_TOKEN'),
      status: provider === 'openclaw'
        ? secret('OPENCLAW_GATEWAY_TOKEN') ? unknown : 'Needs setup'
        : 'Optional',
      status_ok: false,

    },
    {
      ...catalogConfiguration('codex', snapshot, computed, effectiveValues),

      active: assigned('codex'),
      configured: ['app_server', 'direct'].includes(transport) && codexCredentialReady,
      status: !transport ? unknown : transport === 'disabled'
        ? assigned('codex') ? 'Needs setup' : 'Off'
        : assigned('codex')
          ? transport === 'direct' || codexAuthMode === 'chatgpt' ? 'Needs Codex login' : codexCredentialReady ? unknown : 'Needs setup'
          : 'Optional',
      status_ok: false,
      fields: codexFields,
    },
  ]
  return { routing, connections }
}
