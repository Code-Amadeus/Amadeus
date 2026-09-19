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

interface CatalogSnapshot {
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
  range?: { min?: number; max?: number; step?: number },
): ModelConnectionCatalogField => ({
  key,
  label,
  type,
  value,
  options,
  description,
  ...range,
  editable: true,
  restart_required: true,
})

/**
 * Model services are a stable product catalog. Runtime configuration changes
 * their status; it must not determine whether a first-time user can discover
 * the credential fields needed to configure them.
 */
export function buildRemoteModelConnectionCatalog(
  activeProvider: string,
  snapshot?: CatalogSnapshot | null,
): ModelConnectionCatalogGroup[] {
  const values = snapshot?.values || {}
  const configured = (key: string) => Boolean(snapshot?.secrets?.[key]?.configured)
  const activeConnections = new Set(({
    hybrid: ['bedrock'],
    hybrid2: ['deepseek'],
    hybrid3: ['openai'],
  } as Record<string, string[]>)[activeProvider] || [activeProvider])
  const group = (
    id: string,
    label: string,
    secretKey: string,
    fields: ModelConnectionCatalogField[],
    description?: string,
  ): ModelConnectionCatalogGroup => {
    const isConfigured = configured(secretKey)
    const isActive = activeConnections.has(id)
    return {
      id,
      label,
      description,
      active: isActive,
      configured: isConfigured,
      status: isConfigured ? 'Credential saved' : isActive ? 'Needs setup' : 'Optional',
      status_ok: isConfigured,
      fields,
    }
  }

  return [
    group('deepseek', 'DeepSeek', 'DEEPSEEK_API_KEY', [
      field('DEEPSEEK_API_KEY', 'API key', 'secret'),
      field('DEEPSEEK_BASE_URL', 'Base URL', 'url', values.DEEPSEEK_BASE_URL || 'https://api.deepseek.com'),
      field('DEEPSEEK_MODEL_NAME', 'Model', 'text', values.DEEPSEEK_MODEL_NAME || 'deepseek-v4-flash', undefined, 'Independent from the Codex Work Provider model.'),
    ]),
    group('openai', 'OpenAI-compatible', 'OPENAI_API_KEY', [
      field('OPENAI_API_KEY', 'API key', 'secret'),
      field('OPENAI_BASE_URL', 'Base URL', 'url', values.OPENAI_BASE_URL || 'https://api.openai.com/v1'),
      field('OPENAI_MODEL_NAME', 'Model', 'text', values.OPENAI_MODEL_NAME || 'gpt-5.4-mini'),
    ], 'Supports OpenAI and compatible endpoints through a configurable base URL.'),
    group('gemini', 'Gemini', 'GEMINI_API_KEY', [
      field('GEMINI_API_KEY', 'API key', 'secret'),
      field('GEMINI_MODEL_NAME', 'Model', 'text', values.GEMINI_MODEL_NAME || 'gemini-2.5-flash'),
    ]),
    group('bedrock', 'AWS Bedrock', 'AWS_BEARER_TOKEN_BEDROCK', [
      field('BEDROCK_AUTH_MODE', 'Authentication', 'select', values.BEDROCK_AUTH_MODE || 'auto', ['auto', 'boto3', 'bearer']),
      field('AWS_BEARER_TOKEN_BEDROCK', 'Bearer token', 'secret'),
      field('AWS_BEDROCK_REGION', 'Region', 'text', values.AWS_BEDROCK_REGION || 'us-west-2'),
      field('AWS_BEDROCK_MODEL_ID', 'Model ID', 'text', values.AWS_BEDROCK_MODEL_ID || 'deepseek.v3-v1:0'),
      field('AWS_BEDROCK_USE_INFERENCE_PROFILE', 'Use inference profile', 'boolean', values.AWS_BEDROCK_USE_INFERENCE_PROFILE === 'true'),
      field('AWS_BEDROCK_INFERENCE_PROFILE_ID', 'Inference profile ID', 'text', values.AWS_BEDROCK_INFERENCE_PROFILE_ID || ''),
    ], 'Uses the AWS credential chain or an explicitly stored Bedrock bearer token.'),
  ]
}

export function buildLocalModelConnectionCatalog(
  activeProvider: string,
  snapshot?: CatalogSnapshot | null,
): ModelConnectionCatalogGroup[] {
  const values = snapshot?.values || {}
  const localType = values.LOCAL_LLM_TYPE || 'llama_server'
  const localFields: ModelConnectionCatalogField[] = [
    field('LOCAL_LLM_TYPE', 'Backend type', 'select', localType, ['llama_server', 'lmstudio', 'ollama', 'cli']),
    field('LOCAL_LLM_MODEL', 'Model', 'text', values.LOCAL_LLM_MODEL || 'qwen3-30b-a3b-instruct-2507@q4_k_m'),
  ]
  if (localType === 'llama_server') {
    localFields.push(
      field('LOCAL_LLM_LAUNCH_MODE', 'Server ownership', 'select', values.LOCAL_LLM_LAUNCH_MODE || 'external', [
        { value: 'external', label: 'External server' },
        { value: 'managed', label: 'Managed by Amadeus' },
      ], 'External reuses an existing llama.cpp server; managed starts and stops it with Amadeus.'),
      field('LOCAL_LLM_URL', 'llama.cpp server URL', 'url', values.LOCAL_LLM_URL || 'http://127.0.0.1:8080/v1'),
      field('LOCAL_LLM_CLI_PATH', 'llama-server executable', 'path', values.LOCAL_LLM_CLI_PATH || ''),
      field('LOCAL_LLM_CLI_MODEL_PATH', 'GGUF model file', 'path', values.LOCAL_LLM_CLI_MODEL_PATH || ''),
      field('LOCAL_LLM_CLI_CONTEXT', 'Context size', 'number', values.LOCAL_LLM_CLI_CONTEXT || '4096', undefined, undefined, { min: 1, step: 1 }),
      field('LOCAL_LLM_CLI_THREADS', 'CPU threads', 'number', values.LOCAL_LLM_CLI_THREADS || '4', undefined, undefined, { min: 1, step: 1 }),
      field('LOCAL_LLM_CLI_NGL', 'GPU layers', 'number', values.LOCAL_LLM_CLI_NGL || '99', undefined, undefined, { min: 0, step: 1 }),
      field('LOCAL_LLM_CUDA_VISIBLE_DEVICES', 'Visible GPU IDs', 'text', values.LOCAL_LLM_CUDA_VISIBLE_DEVICES || '', undefined, 'Optional nvidia-smi indices, for example 1. Leave blank for automatic visibility.'),
    )
  } else if (localType === 'lmstudio') {
    localFields.push(field('LOCAL_LLM_LM_STUDIO_URL', 'LM Studio URL', 'url', values.LOCAL_LLM_LM_STUDIO_URL || 'http://127.0.0.1:1234'))
  } else if (localType === 'ollama') {
    localFields.push(field('LOCAL_LLM_OLLAMA_URL', 'Ollama URL', 'url', values.LOCAL_LLM_OLLAMA_URL || 'http://127.0.0.1:11434'))
  } else {
    localFields.push(
      field('LOCAL_LLM_CLI_PATH', 'llama-cli executable', 'path', values.LOCAL_LLM_CLI_PATH || ''),
      field('LOCAL_LLM_CLI_MODEL_PATH', 'GGUF model file', 'path', values.LOCAL_LLM_CLI_MODEL_PATH || ''),
    )
  }

  const unavailable = 'Backend status unavailable'
  return [
    {
      id: 'local',
      label: 'Pure-local model',
      description: 'Choose and configure the local runtime used by the pure-local Main conversation profile.',
      active: activeProvider === 'local',
      configured: false,
      status: activeProvider === 'local' ? unavailable : 'Optional',
      status_ok: false,
      fields: localFields,
    },
    {
      id: 'hybrid_local',
      label: 'Hybrid local head',
      description: 'Shared fast first-sentence endpoint. Hybrid pairs it with Bedrock, Hybrid2 with DeepSeek, and Hybrid3 with OpenAI-compatible.',
      active: ['hybrid', 'hybrid2', 'hybrid3'].includes(activeProvider),
      configured: false,
      status: ['hybrid', 'hybrid2', 'hybrid3'].includes(activeProvider) ? unavailable : 'Optional',
      status_ok: false,
      fields: [
        field('HYBRID_LOCAL_LLM_URL', 'Head endpoint', 'url', values.HYBRID_LOCAL_LLM_URL || values.LOCAL_LLM_URL || 'http://127.0.0.1:8080/v1'),
        field('HYBRID_LOCAL_LLM_MODEL', 'Head model', 'text', values.HYBRID_LOCAL_LLM_MODEL || values.LOCAL_LLM_MODEL || 'qwen3-30b-a3b-instruct-2507@q4_k_m'),
      ],
    },
  ]
}

export function buildOptionalModelServiceCatalog(
  snapshot?: CatalogSnapshot | null,
): ModelConnectionCatalogGroup[] {
  const values = snapshot?.values || {}
  const enabled = values.RAG_ENABLED === 'true'
  return [{
    id: 'character_rag',
    label: 'Character knowledge (optional RAG)',
    description: 'Local retrieval shared by all Main conversation providers. Retrieved excerpts may be sent to the selected remote model.',
    active: enabled,
    configured: false,
    status: enabled ? 'Backend status unavailable' : 'Off',
    status_ok: !enabled,
    fields: [
      field('RAG_ENABLED', 'Enable character knowledge', 'boolean', enabled),
      field('RAG_INDEX_DIR', 'Built index directory', 'path', values.RAG_INDEX_DIR || '.amadeus/character-rag'),
      field('RAG_TOP_K', 'Maximum results', 'number', values.RAG_TOP_K || '3', undefined, undefined, { min: 1, max: 20, step: 1 }),
      field('RAG_MAX_DISTANCE', 'Maximum squared L2 distance', 'number', values.RAG_MAX_DISTANCE || '0.33', undefined, undefined, { min: 0, max: 4, step: 0.01 }),
    ],
  }]
}
