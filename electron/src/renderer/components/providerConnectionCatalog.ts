import type { ModelConnectionCatalogField, ModelConnectionCatalogGroup } from './modelConnectionCatalog'

interface ProviderDesktopSnapshot {
  values?: Record<string, string>
  secrets?: Record<string, { configured?: boolean }>
}

const field = (
  key: string,
  label: string,
  type: ModelConnectionCatalogField['type'],
  value: string | boolean = '',
  options?: ModelConnectionCatalogField['options'],
  description?: string,
): ModelConnectionCatalogField => ({
  key,
  label,
  type,
  value,
  options,
  description,
  editable: true,
  restart_required: true,
})

export function buildWorkProviderCatalog(
  runtimeSelection: { provider: string; enabled: boolean; codingProvider?: string;
    roleCandidates?: Record<string, string[]> },
  snapshot?: ProviderDesktopSnapshot | null,
): { routing: ModelConnectionCatalogGroup; connections: ModelConnectionCatalogGroup[] } {
  const values = snapshot?.values || {}
  const value = (key: string, fallback = '') => values[key] || fallback
  const bool = (key: string, fallback: boolean) => values[key] === undefined ? fallback : values[key] === 'true'
  const secret = (key: string) => Boolean(snapshot?.secrets?.[key]?.configured)
  const provider = value('WORK_EXECUTION_PROVIDER', value('COOPERATIVE_CHAT_PROVIDER', runtimeSelection.provider || 'pi'))
  const codingProvider = value('WORK_CODING_PROVIDER', runtimeSelection.codingProvider || 'codex')
  const roleOptions = (role: string, assigned: string) => {
    const candidates = runtimeSelection.roleCandidates?.[role] || []
    return [...new Set([...candidates, assigned])].map(id => ({ value: id,
      label: candidates.includes(id) ? id : `${id} · ${runtimeSelection.roleCandidates ? 'Unavailable' : 'Backend status unavailable'}` }))
  }
  const enabled = bool('COOPERATIVE_CHAT_ENABLED', runtimeSelection.enabled)
  const assigned = (id: string) => enabled && (provider === id || codingProvider === id)
  const transport = value('CODEX_PROVIDER_TRANSPORT', 'app_server')
  const codexAuthMode = value('CODEX_APP_SERVER_AUTH_MODE', 'model_api')
  const codexModelConnection = value('CODEX_APP_SERVER_MODEL_PROVIDER', 'deepseek')
  const connectionDefaults = codexModelConnection === 'openai'
    ? { model: value('OPENAI_MODEL_NAME', 'gpt-5.4-mini'), credentialKey: 'OPENAI_API_KEY' }
    : { model: value('DEEPSEEK_MODEL_NAME', 'deepseek-v4-flash'), credentialKey: 'DEEPSEEK_API_KEY' }
  const codexConnectionOptions = [
    ...(secret('DEEPSEEK_API_KEY') || codexModelConnection === 'deepseek'
      ? [{ value: 'deepseek', label: secret('DEEPSEEK_API_KEY') ? 'DeepSeek' : 'DeepSeek · Not configured' }]
      : []),
    ...(secret('OPENAI_API_KEY') || codexModelConnection === 'openai'
      ? [{ value: 'openai', label: secret('OPENAI_API_KEY') ? 'OpenAI-compatible' : 'OpenAI-compatible · Not configured' }]
      : []),
  ]
  const unknown = 'Backend status unavailable'

  const routing: ModelConnectionCatalogGroup = {
    id: 'work_routing',
    label: 'Work role assignments',
    description: 'Assign coding and everyday execution independently. Routing follows these roles after backend restart; existing Work keeps its owner. Registration and connections are configured below.',
    active: enabled,
    configured: true,
    status: enabled ? 'Enabled' : 'Off',
    status_ok: true,
    fields: [
      field('COOPERATIVE_CHAT_ENABLED', 'Work execution', 'boolean', enabled),
      field('WORK_CODING_PROVIDER', 'Coding', 'select', codingProvider, roleOptions('coding', codingProvider)),
      field('WORK_EXECUTION_PROVIDER', 'Everyday execution', 'select', provider, roleOptions('execution', provider)),
    ],
  }

  const codexFields = [
    field('CODEX_PROVIDER_TRANSPORT', 'Transport', 'select', transport, [
      { value: 'app_server', label: 'App Server' },
      { value: 'direct', label: 'Direct CLI' },
      { value: 'disabled', label: 'Disabled' },
    ]),
  ]
  if (transport === 'app_server') {
    codexFields.push(
      field('CODEX_APP_SERVER_CODEX_BIN', 'App Server executable', 'path', value('CODEX_APP_SERVER_CODEX_BIN')),
      field('CODEX_APP_SERVER_AUTH_MODE', 'App Server authentication', 'select', codexAuthMode, [
        { value: 'chatgpt', label: 'ChatGPT subscription' },
        { value: 'model_api', label: 'Model API connection' },
      ], 'Run `codex login` once for subscription use. Model API reuses a connection from Models.'),
    )
    if (codexAuthMode === 'chatgpt') {
      codexFields.push(field(
        'CODEX_APP_SERVER_CHATGPT_MODEL', 'Subscription model override', 'text',
        value('CODEX_APP_SERVER_CHATGPT_MODEL'), undefined,
        'Optional. Leave blank to use the model selected by the signed-in Codex client.',
      ))
    } else {
      codexFields.push(
        field('CODEX_APP_SERVER_MODEL_PROVIDER', 'Model API connection', 'select', codexModelConnection, codexConnectionOptions, 'Reuses the API key and endpoint configured in Models.'),
        field('CODEX_APP_SERVER_MODEL', 'Model', 'text', value('CODEX_APP_SERVER_MODEL', connectionDefaults.model), undefined, 'Defaults to the model from the selected Models connection.'),
      )
    }
    codexFields.push(
      field('CODEX_APP_SERVER_REASONING_EFFORT', 'Reasoning effort', 'select', value('CODEX_APP_SERVER_REASONING_EFFORT', 'max'), ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']),
      field('CODEX_APP_SERVER_SERVICE_TIER', 'Service tier', 'select', value('CODEX_APP_SERVER_SERVICE_TIER'), ['', 'auto', 'default', 'flex', 'priority', 'fast', 'ultrafast']),
    )
  } else if (transport === 'direct') {
    codexFields.push(field('DIRECT_CODEX_CLI_PATH', 'Direct CLI executable', 'path', value('DIRECT_CODEX_CLI_PATH', 'codex'), undefined, 'Direct CLI uses the existing local `codex login` session.'))
  }
  const codexCredentialReady = transport === 'direct' || codexAuthMode === 'chatgpt' || secret(connectionDefaults.credentialKey)

  const connections: ModelConnectionCatalogGroup[] = [
    {
      id: 'pi',
      label: 'Pi · Experimental',
      description: 'Local agent over native RPC. Install the pinned runtime; uses native Pi model credentials. Assign its role above.',
      active: assigned('pi'),
      configured: bool('PI_PROVIDER_ENABLED', true),
      status: bool('PI_PROVIDER_ENABLED', true) ? unknown : 'Off',
      status_ok: false,
      fields: [
        field('PI_PROVIDER_ENABLED', 'Enable Pi', 'boolean', bool('PI_PROVIDER_ENABLED', true)),
        field('PI_NODE_PATH', 'Node executable', 'path', value('PI_NODE_PATH', 'node')),
        field('PI_AGENT_DIR', 'Pi configuration and sessions', 'path', value('PI_AGENT_DIR', 'runtime/pi')),
        field('PI_MODEL_PROVIDER', 'Pi model provider', 'text', value('PI_MODEL_PROVIDER', 'deepseek'), undefined, 'Uses native Pi authentication or the model provider API key in the backend environment.'),
        field('PI_MODEL', 'Pi model', 'text', value('PI_MODEL', value('DEEPSEEK_MODEL_NAME', 'deepseek-v4-flash'))),
      ],
    },
    {
      id: 'browser',
      label: 'Browser',
      description: 'Host-managed browser Work Provider; no user-managed connection settings.',
      active: assigned('browser'),
      configured: true,
      status: enabled && provider === 'browser' ? unknown : 'Optional',
      status_ok: false,
      fields: [],
    },
    {
      id: 'openclaw',
      label: 'OpenClaw',
      description: 'Optional Gateway provider. Assign a role above or select it explicitly for a task; existing sessions remain supported.',
      active: assigned('openclaw'),
      configured: secret('OPENCLAW_GATEWAY_TOKEN'),
      status: enabled && provider === 'openclaw'
        ? secret('OPENCLAW_GATEWAY_TOKEN') ? unknown : 'Needs setup'
        : 'Optional',
      status_ok: false,
      fields: [
        field('OPENCLAW_BASE_URL', 'Gateway URL', 'url', value('OPENCLAW_BASE_URL', 'http://127.0.0.1:18789')),
        field('OPENCLAW_GATEWAY_TOKEN', 'Gateway token', 'secret'),
        field('OPENCLAW_PROJECT_DIR', 'OpenClaw project directory', 'path', value('OPENCLAW_PROJECT_DIR')),
      ],
    },
    {
      id: 'codex',
      label: 'Codex',
      description: 'Coding Work Provider. Exactly one App Server or Direct transport owns this Provider id.',
      active: assigned('codex'),
      configured: transport !== 'disabled' && codexCredentialReady,
      status: transport === 'disabled'
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
