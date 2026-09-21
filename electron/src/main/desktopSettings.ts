import { safeStorage } from 'electron'
import fs from 'fs'
import path from 'path'

type StoredDesktopSettings = {
  version: 2
  values: Record<string, string>
  encryptedSecrets: Record<string, string>
  mcpConnections: Record<string, StoredMcpConnection>
  pendingRevisions: Record<string, number>
  nextRevision: number
}

export type DesktopSettingsUpdate = {
  values?: Record<string, string | boolean | null>
  secrets?: Record<string, string | null>
}

export type McpConnectionInput = {
  id?: string
  name: string
  enabled?: boolean
  transport: 'stdio' | 'http'
  providerIds?: string[]
  command?: string
  arguments?: string[]
  cwd?: string
  url?: string
  bearerTokenEnvVar?: string
}

export type McpConnectionUpdate = {
  connection: McpConnectionInput
  environment?: Record<string, string | null>
  clearEnvironment?: boolean
}

type StoredMcpConnection = {
  id: string
  name: string
  enabled: boolean
  transport: 'stdio' | 'http'
  providerIds: string[]
  command: string
  arguments: string[]
  cwd: string
  url: string
  bearerTokenEnvVar: string
  encryptedEnvironment: Record<string, string>
}

const VALUE_KEYS = new Set([
  'AMADEUS_UI_LOCALE',
  'AMADEUS_UI_THEME',
  'AMADEUS_PRESENTATION_LOCALE',
  'AMADEUS_WALLPAPER_CAPTION_MODE',
  'AMADEUS_CHAT_TRANSLATION_SUBTITLES_ENABLED',
  'AMADEUS_VISION_ENABLED',
  'AMADEUS_VISION_MODE',
  'AMADEUS_VISION_SCOPE',
  'AMADEUS_VISION_MAX_LONG_SIDE',
  'AMADEUS_VISION_JPEG_QUALITY',
  'AMADEUS_VISION_REGION',
  'AMADEUS_VISION_WINDOW_HANDLE',
  'ENABLE_CUDA_GRAPH',
  'EXP_TTS_MAX_CONCURRENCY',
  'TTS_OUTPUT_LANGUAGE',
  'LLM_PROVIDER',
  'DEEPSEEK_BASE_URL',
  'DEEPSEEK_MODEL_NAME',
  'OPENAI_BASE_URL',
  'OPENAI_MODEL_NAME',
  'GEMINI_MODEL_NAME',
  'BEDROCK_AUTH_MODE',
  'AWS_BEDROCK_REGION',
  'AWS_BEDROCK_MODEL_ID',
  'AWS_BEDROCK_USE_INFERENCE_PROFILE',
  'AWS_BEDROCK_INFERENCE_PROFILE_ID',
  'RAG_ENABLED',
  'RAG_INDEX_DIR',
  'RAG_TOP_K',
  'RAG_MAX_DISTANCE',
  'LOCAL_LLM_TYPE',
  'LOCAL_LLM_LAUNCH_MODE',
  'LOCAL_LLM_MODEL',
  'LOCAL_LLM_URL',
  'LM_STUDIO_URL',
  'LOCAL_LLM_LM_STUDIO_URL',
  'LOCAL_LLM_OLLAMA_URL',
  'HYBRID_LOCAL_LLM_URL',
  'HYBRID_LOCAL_LLM_MODEL',
  'LOCAL_LLM_CLI_PATH',
  'LOCAL_LLM_CLI_MODEL_PATH',
  'LOCAL_LLM_CLI_THREADS',
  'LOCAL_LLM_CLI_CONTEXT',
  'LOCAL_LLM_CLI_NGL',
  'LOCAL_LLM_CUDA_VISIBLE_DEVICES',
  'COOPERATIVE_WORK_PLANNER_MODEL',
  'WORK_OBSERVER_PROVIDER',
  'WORK_OBSERVER_MODEL',
  'AUIP_NARRATION_PROVIDER',
  'AUIP_NARRATION_MODEL',
  'AUIP_ACTION_PROVIDER',
  'AUIP_ACTION_MODEL',
  'AUIP_ACTION_REASONING_EFFORT',
  'AUIP_ACTION_SERVICE_TIER',
  'BROWSER_BRANCH_PROVIDER',
  'BROWSER_BRANCH_MODEL',
  'VN_LLM_PROVIDER',
  'VN_LLM_MODEL',
  'VN_SUBTITLE_TRANSLATE_PROVIDER',
  'VN_SUBTITLE_TRANSLATE_MODEL',
  'VN_TTS_TRANSLATE_PROVIDER',
  'VN_TTS_TRANSLATE_MODEL',
  'COOPERATIVE_CHAT_ENABLED',
  'GRAPHICS_PROFILE',
  'RENDER_MAX_FPS',
  'RENDER_MAX_RESOLUTION',
  'RENDER_TEXTURE_SAMPLING',
  'COOPERATIVE_CHAT_PROVIDER',
  'WORK_CODING_PROVIDER',
  'WORK_EXECUTION_PROVIDER',
  'PI_PROVIDER_ENABLED',
  'PI_NODE_PATH',
  'PI_AGENT_DIR',
  'PI_MODEL_PROVIDER',
  'PI_MODEL',
  'OPENCLAW_BASE_URL',
  'OPENCLAW_PROJECT_DIR',
  'CODEX_PROVIDER_TRANSPORT',
  'CODEX_APP_SERVER_AUTH_MODE',
  'CODEX_APP_SERVER_CODEX_BIN',
  'CODEX_APP_SERVER_MODEL_PROVIDER',
  'CODEX_APP_SERVER_PROVIDER_BASE_URL',
  'CODEX_APP_SERVER_MODEL',
  'CODEX_APP_SERVER_CHATGPT_MODEL',
  'CODEX_APP_SERVER_REASONING_EFFORT',
  'CODEX_APP_SERVER_SERVICE_TIER',
  'DIRECT_CODEX_CLI_PATH',
  'AMADEUS_ACP_PROVIDERS',
  'ASR_BACKEND',
  'ASR_LANGUAGE',
  'ASR_CONTEXT',
  'ASR_API_BASE_URL',
  'ASR_API_MODEL',
  'ASR_LISTEN_TIMEOUT_SECONDS',
  'ASR_VAD_SILENCE_MS',
  'QWEN3_ASR_MODEL_PATH',
  'QWEN3_ASR_DEVICE',
  'QWEN3_ASR_REQUIRE_CUDA',
  'WAKE_ENABLED',
  'WAKE_ASR_BACKEND',
  'WAKE_PHRASES',
  'WAKE_AUTO_SEND_TO_CHAT',
  'WAKE_SENSEVOICE_LANGUAGES',
  'SENSEVOICE_LANGUAGE',
  'SENSEVOICE_MODEL_PATH',
  'MICROPHONE_DEVICE_INDEX',
  'MICROPHONE_PREFERRED_NAME',
  'AEC_REALTIME_ENABLED',
  'AEC_REALTIME_BARGE_IN',
  'AEC_REALTIME_DELAY_MS',
  'TTS_BACKEND',
  'TTS_DEVICE',
  'TTS_VOICE_PROFILE',
  'TTS_GPT_MODEL_PATH',
  'TTS_SOVITS_MODEL_PATH',
  'TTS_REF_AUDIO_JA',
  'TTS_REF_TEXT_JA',
  'TTS_REF_AUDIO_EN',
  'TTS_REF_TEXT_EN',
  'TTS_API_BASE_URL',
  'TTS_API_MODEL',
  'TTS_API_VOICE',
  'TTS_API_STREAM_PROTOCOL',
  'MIMO_TTS_BASE_URL',
  'MIMO_TTS_MODEL',
  'MIMO_TTS_VOICE',
  'FISH_TTS_WS_URL',
  'FISH_TTS_MODEL',
  'FISH_TTS_REFERENCE_ID',
  'FISH_TTS_LATENCY',
  'VTS_ENABLED',
  'AUIP_ARTIFACT_STYLE_ENABLED',
  'VTS_WS_URL',
  'VTS_TOKEN_FILE',
])

const SECRET_KEYS = new Set([
  'ANTHROPIC_API_KEY',
  'DEEPSEEK_API_KEY',
  'OPENAI_API_KEY',
  'GEMINI_API_KEY',
  'AWS_BEARER_TOKEN_BEDROCK',
  'OPENCLAW_GATEWAY_TOKEN',
  'ASR_API_KEY',
  'TTS_API_KEY',
  'MIMO_TTS_API_KEY',
  'FISH_TTS_API_KEY',
])

const CODEX_TRANSPORT_KEYS = [
  'CODEX_APP_SERVER_PROVIDER_ENABLED',
  'DIRECT_CODEX_PROVIDER_ENABLED',
] as const

const VALUE_CHOICES: Record<string, ReadonlySet<string>> = {
  AMADEUS_UI_LOCALE: new Set(['en-US', 'zh-CN']),
  AMADEUS_UI_THEME: new Set(['classic', 'wallpaper-slice']),
  AMADEUS_PRESENTATION_LOCALE: new Set(['en-US', 'zh-CN', 'ja-JP']),
  AMADEUS_WALLPAPER_CAPTION_MODE: new Set(['translated', 'source', 'bilingual', 'off']),
  AMADEUS_CHAT_TRANSLATION_SUBTITLES_ENABLED: new Set(['true', 'false']),
  AMADEUS_VISION_ENABLED: new Set(['true', 'false']),
  AMADEUS_VISION_MODE: new Set(['off', 'on_demand', 'watching', 'self_aware']),
  AMADEUS_VISION_SCOPE: new Set(['full_screen', 'current_window', 'selected_window', 'wallpaper_surface', 'region']),
  ENABLE_CUDA_GRAPH: new Set(['1', '0']),
  TTS_OUTPUT_LANGUAGE: new Set(['日文', '英文']),
  TTS_VOICE_PROFILE: new Set(['custom', 'kurisu_v3', 'kurisu_v2pro']),
  LLM_PROVIDER: new Set(['deepseek', 'openai', 'gemini', 'bedrock', 'local', 'hybrid', 'hybrid2', 'hybrid3']),
  BEDROCK_AUTH_MODE: new Set(['auto', 'boto3', 'bearer']),
  AWS_BEDROCK_USE_INFERENCE_PROFILE: new Set(['true', 'false']),
  RAG_ENABLED: new Set(['true', 'false']),
  LOCAL_LLM_TYPE: new Set(['llama_server', 'lmstudio', 'ollama', 'cli']),
  LOCAL_LLM_LAUNCH_MODE: new Set(['external', 'managed']),
  AUIP_ACTION_REASONING_EFFORT: new Set(['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']),
  AUIP_ACTION_SERVICE_TIER: new Set(['auto', 'default', 'fast', 'priority']),
  BROWSER_BRANCH_PROVIDER: new Set(['deepseek', 'openai']),
  VN_LLM_PROVIDER: new Set(['deepseek', 'openai']),
  VN_SUBTITLE_TRANSLATE_PROVIDER: new Set(['deepseek', 'openai']),
  VN_TTS_TRANSLATE_PROVIDER: new Set(['deepseek', 'openai']),
  COOPERATIVE_CHAT_ENABLED: new Set(['true', 'false']),
  COOPERATIVE_CHAT_PROVIDER: new Set(['codex', 'openclaw', 'browser', 'pi']),
  PI_PROVIDER_ENABLED: new Set(['true', 'false']),
  GRAPHICS_PROFILE: new Set(['standard', 'power_saving', 'custom']),
  RENDER_TEXTURE_SAMPLING: new Set(['true', 'false']),
  CODEX_PROVIDER_TRANSPORT: new Set(['app_server', 'direct', 'disabled']),
  CODEX_APP_SERVER_AUTH_MODE: new Set(['model_api', 'chatgpt']),
  CODEX_APP_SERVER_MODEL_PROVIDER: new Set(['deepseek', 'openai']),
  CODEX_APP_SERVER_REASONING_EFFORT: new Set(['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']),
  CODEX_APP_SERVER_SERVICE_TIER: new Set(['', 'auto', 'default', 'flex', 'priority', 'fast', 'ultrafast']),
  QWEN3_ASR_DEVICE: new Set(['auto', 'cpu', 'cuda']),
  QWEN3_ASR_REQUIRE_CUDA: new Set(['true', 'false']),
  WAKE_ENABLED: new Set(['true', 'false']),
  WAKE_AUTO_SEND_TO_CHAT: new Set(['true', 'false']),
  WAKE_ASR_BACKEND: new Set(['sense_voice', 'qwen3_asr']),
  SENSEVOICE_LANGUAGE: new Set(['auto', 'en', 'zh', 'ja', 'yue', 'ko']),
  AEC_REALTIME_ENABLED: new Set(['true', 'false']),
  AEC_REALTIME_BARGE_IN: new Set(['true', 'false']),
  TTS_API_STREAM_PROTOCOL: new Set(['buffered', 'openai_sse']),
  FISH_TTS_LATENCY: new Set(['normal', 'balanced', 'low']),
  VTS_ENABLED: new Set(['true', 'false']),
  AUIP_ARTIFACT_STYLE_ENABLED: new Set(['true', 'false']),
}

const IDENTIFIER_KEYS = new Set(['ASR_BACKEND', 'TTS_BACKEND', 'WORK_CODING_PROVIDER', 'WORK_EXECUTION_PROVIDER'])

const URL_KEYS = new Set([
  'DEEPSEEK_BASE_URL',
  'OPENAI_BASE_URL',
  'LOCAL_LLM_URL',
  'LM_STUDIO_URL',
  'LOCAL_LLM_LM_STUDIO_URL',
  'LOCAL_LLM_OLLAMA_URL',
  'HYBRID_LOCAL_LLM_URL',
  'OPENCLAW_BASE_URL',
  'CODEX_APP_SERVER_PROVIDER_BASE_URL',
  'ASR_API_BASE_URL',
  'TTS_API_BASE_URL',
  'MIMO_TTS_BASE_URL',
])

const WEBSOCKET_URL_KEYS = new Set(['VTS_WS_URL', 'FISH_TTS_WS_URL'])

const NUMBER_RANGES: Record<string, readonly [number, number]> = {
  RENDER_MAX_FPS: [10, 240],
  RENDER_MAX_RESOLUTION: [0.25, 4],
  RAG_TOP_K: [1, 20],
  RAG_MAX_DISTANCE: [0, 4],
  ASR_LISTEN_TIMEOUT_SECONDS: [1, 120],
  ASR_VAD_SILENCE_MS: [100, 3000],
  AEC_REALTIME_DELAY_MS: [0, 2000],
  AMADEUS_VISION_MAX_LONG_SIDE: [320, 4096],
  AMADEUS_VISION_JPEG_QUALITY: [35, 92],
  EXP_TTS_MAX_CONCURRENCY: [1, 16],
}

const INTEGER_KEYS = new Set(['RENDER_MAX_FPS', 'RAG_TOP_K', 'ASR_VAD_SILENCE_MS', 'AMADEUS_VISION_MAX_LONG_SIDE', 'AMADEUS_VISION_JPEG_QUALITY', 'EXP_TTS_MAX_CONCURRENCY'])

const MCP_CONNECTIONS_ENV = 'AMADEUS_MCP_CONNECTIONS'
const FRONTEND_ONLY_VALUE_KEYS = new Set(['AMADEUS_UI_LOCALE', 'AMADEUS_UI_THEME'])
const MCP_ID_PATTERN = /^[a-z][a-z0-9_-]{0,63}$/
const MCP_ENV_KEY_PATTERN = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/

export function validateAcpProviders(raw: string): Array<Record<string, unknown>> {
  if (Buffer.byteLength(raw, 'utf8') > 65536) throw new Error('ACP configuration exceeds 64 KiB')
  const profiles: unknown = JSON.parse(raw)
  if (!Array.isArray(profiles) || profiles.length > 32) throw new Error('Expected at most 32 ACP agents')
  const ids = new Set<string>()
  for (const profile of profiles) {
    if (!profile || typeof profile !== 'object' || Array.isArray(profile)) throw new Error('Invalid ACP agent')
    const allowed = new Set(['id', 'name', 'command', 'args', 'enabled', 'environment', 'config_options', 'resume'])
    if (Object.keys(profile).some(key => !allowed.has(key))) throw new Error('Unknown ACP configuration field')
    const id = profile.id
    if (typeof id !== 'string' || !MCP_ID_PATTERN.test(id) || ['codex', 'browser', 'openclaw', 'pi'].includes(id) || ids.has(id)) {
      throw new Error('ACP agents require unique ids distinct from built-in Providers')
    }
    ids.add(id)
    for (const [key, limit] of [['name', 80], ['command', 4096]] as const) {
      const value = profile[key] ?? (key === 'name' ? id : '')
      if (typeof value !== 'string' || !value.trim() || value.includes('\0') || value.length > limit) throw new Error(`Invalid ACP ${key}`)
    }
    for (const key of ['enabled', 'resume']) {
      if (profile[key] !== undefined && typeof profile[key] !== 'boolean') throw new Error(`Invalid ACP ${key}`)
    }
    const args = profile.args ?? []
    if (!Array.isArray(args) || args.length > 64 || args.some(arg => typeof arg !== 'string' || arg.includes('\0') || arg.length > 4096)) {
      throw new Error('ACP arguments must be a string array')
    }
    for (const key of ['environment', 'config_options']) {
      const value = profile[key] ?? {}
      if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).length > (key === 'environment' ? 64 : 32)) {
        throw new Error(`Invalid ACP ${key}`)
      }
      for (const [name, entry] of Object.entries(value)) {
        if (typeof entry !== 'string' || !name || !entry || name.includes('\0') || entry.includes('\0')
          || name.length > 128 || entry.length > 512) throw new Error(`Invalid ACP ${key} entry`)
        if (key === 'environment' && (!MCP_ENV_KEY_PATTERN.test(name) || !MCP_ENV_KEY_PATTERN.test(entry))) {
          throw new Error('ACP environment entries reference Host variable names; save secrets in the credential store')
        }
      }
    }
  }
  return profiles
}

function emptyStore(): StoredDesktopSettings {
  return { version: 2, values: {}, encryptedSecrets: {}, mcpConnections: {}, pendingRevisions: {}, nextRevision: 1 }
}

function allowedPendingKey(key: string): boolean {
  return key === MCP_CONNECTIONS_ENV || (VALUE_KEYS.has(key) && !FRONTEND_ONLY_VALUE_KEYS.has(key)) || SECRET_KEYS.has(key)
}

function cleanPendingRevisions(value: unknown, legacyKeys: unknown): Record<string, number> {
  const result: Record<string, number> = {}
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    for (const [key, revision] of Object.entries(value)) {
      if (allowedPendingKey(key) && Number.isSafeInteger(revision) && Number(revision) > 0) result[key] = Number(revision)
    }
  }
  if (!Object.keys(result).length && Array.isArray(legacyKeys)) {
    let revision = 1
    for (const rawKey of legacyKeys) {
      const key = String(rawKey || '').trim()
      if (allowedPendingKey(key)) result[key] = revision++
    }
  }
  return result
}

function markPending(stored: StoredDesktopSettings, keys: Iterable<string>): void {
  for (const key of keys) {
    stored.pendingRevisions[key] = stored.nextRevision++
  }
}

function boundedText(value: unknown, label: string, limit: number): string {
  const text = String(value ?? '').trim()
  if (text.includes('\0') || text.length > limit) throw new Error(`Invalid ${label}`)
  return text
}

function secretText(value: unknown, label: string, limit: number): string {
  const text = String(value ?? '')
  if (!text || text.includes('\0') || text.length > limit) throw new Error(`Invalid ${label}`)
  return text
}

function cleanStoredMcpConnection(value: unknown): StoredMcpConnection | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const source = value as Record<string, unknown>
  try {
    const id = boundedText(source.id, 'MCP connection id', 64).toLowerCase()
    const name = boundedText(source.name, 'MCP connection name', 80)
    const transport = source.transport === 'http' ? 'http' : source.transport === 'stdio' ? 'stdio' : ''
    if (!MCP_ID_PATTERN.test(id) || !name || !transport) return null
    const providerIds = Array.isArray(source.providerIds)
      ? [...new Set(source.providerIds.map(value => String(value || '').trim().toLowerCase()).filter(value => MCP_ID_PATTERN.test(value)))]
      : []
    const argumentsValue = Array.isArray(source.arguments)
      ? source.arguments.slice(0, 64).map(value => boundedText(value, 'MCP argument', 4096))
      : []
    const encryptedEnvironment: Record<string, string> = {}
    if (source.encryptedEnvironment && typeof source.encryptedEnvironment === 'object' && !Array.isArray(source.encryptedEnvironment)) {
      for (const [key, encrypted] of Object.entries(source.encryptedEnvironment)) {
        if (MCP_ENV_KEY_PATTERN.test(key) && typeof encrypted === 'string' && encrypted.length <= 32_768) {
          encryptedEnvironment[key] = encrypted
        }
      }
    }
    return {
      id,
      name,
      enabled: Boolean(source.enabled) && providerIds.length > 0,
      transport,
      providerIds,
      command: boundedText(source.command, 'MCP command', 4096),
      arguments: argumentsValue,
      cwd: boundedText(source.cwd, 'MCP working directory', 4096),
      url: boundedText(source.url, 'MCP URL', 4096),
      bearerTokenEnvVar: boundedText(source.bearerTokenEnvVar, 'MCP bearer token environment variable', 128),
      encryptedEnvironment,
    }
  } catch {
    return null
  }
}

function cleanStoredMcpConnections(value: unknown): Record<string, StoredMcpConnection> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const result: Record<string, StoredMcpConnection> = {}
  for (const item of Object.values(value)) {
    const connection = cleanStoredMcpConnection(item)
    if (connection && Object.keys(result).length < 64) result[connection.id] = connection
  }
  return result
}

function mcpConnectionSnapshot(connection: StoredMcpConnection): Record<string, unknown> {
  return {
    id: connection.id,
    name: connection.name,
    enabled: connection.enabled,
    transport: connection.transport,
    providerIds: [...connection.providerIds],
    command: connection.command,
    arguments: [...connection.arguments],
    cwd: connection.cwd,
    url: connection.url,
    bearerTokenEnvVar: connection.bearerTokenEnvVar,
    environmentKeys: Object.keys(connection.encryptedEnvironment).sort(),
    mainChatAccess: false,
  }
}

function cleanRecord(
  value: unknown,
  allowed: ReadonlySet<string>,
): Record<string, string> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const result: Record<string, string> = {}
  for (const [key, raw] of Object.entries(value)) {
    if (!allowed.has(key) || typeof raw !== 'string' || raw.length > 16_384) continue
    result[key] = raw
  }
  return result
}

function explicitEnvironmentHas(
  environment: NodeJS.ProcessEnv,
  key: string,
): boolean {
  if (key === 'CODEX_PROVIDER_TRANSPORT') {
    return CODEX_TRANSPORT_KEYS.some(candidate => environment[candidate] !== undefined)
  }
  return environment[key] !== undefined
}

function sourceFor(
  key: string,
  environment: NodeJS.ProcessEnv,
  stored: StoredDesktopSettings,
  dotenvKeys: ReadonlySet<string>,
): 'environment' | 'user' | 'dotenv' | 'default' {
  if (explicitEnvironmentHas(environment, key)) return 'environment'
  if (stored.values[key] !== undefined || stored.encryptedSecrets[key] !== undefined) return 'user'
  if (key === 'CODEX_PROVIDER_TRANSPORT') {
    if (CODEX_TRANSPORT_KEYS.some(candidate => dotenvKeys.has(candidate))) return 'dotenv'
  } else if (dotenvKeys.has(key)) {
    return 'dotenv'
  }
  return 'default'
}

export class DesktopSettingsStore {
  constructor(
    private readonly filePath: string,
    private readonly dotenvPath: string,
  ) {}

  private read(): StoredDesktopSettings {
    try {
      const parsed = JSON.parse(fs.readFileSync(this.filePath, 'utf8')) as Record<string, unknown>
      const pendingRevisions = cleanPendingRevisions(parsed.pendingRevisions, parsed.pendingKeys)
      const highestRevision = Math.max(0, ...Object.values(pendingRevisions))
      const storedNextRevision = Number.isSafeInteger(parsed.nextRevision) && Number(parsed.nextRevision) > 0
        ? Number(parsed.nextRevision)
        : 1
      return {
        version: 2,
        values: cleanRecord(parsed.values, VALUE_KEYS),
        encryptedSecrets: cleanRecord(parsed.encryptedSecrets, SECRET_KEYS),
        mcpConnections: cleanStoredMcpConnections(parsed.mcpConnections),
        pendingRevisions,
        nextRevision: Math.max(storedNextRevision, highestRevision + 1),
      }
    } catch {
      return emptyStore()
    }
  }

  private write(value: StoredDesktopSettings): void {
    fs.mkdirSync(path.dirname(this.filePath), { recursive: true })
    const temporary = `${this.filePath}.tmp`
    fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {
      encoding: 'utf8',
      mode: 0o600,
    })
    fs.renameSync(temporary, this.filePath)
  }

  private dotenvKeys(): Set<string> {
    try {
      const keys = new Set<string>()
      for (const line of fs.readFileSync(this.dotenvPath, 'utf8').split(/\r?\n/)) {
        const match = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=/)
        if (match) keys.add(match[1])
      }
      return keys
    } catch {
      return new Set()
    }
  }

  snapshot(environment: NodeJS.ProcessEnv): Record<string, unknown> {
    const stored = this.read()
    const dotenvKeys = this.dotenvKeys()
    const allKeys = [...VALUE_KEYS, ...SECRET_KEYS]
    const sources = Object.fromEntries(
      allKeys.map(key => [key, sourceFor(key, environment, stored, dotenvKeys)]),
    )
    const locked = Object.fromEntries(
      allKeys.map(key => [key, explicitEnvironmentHas(environment, key)]),
    )
    const secrets = Object.fromEntries(
      [...SECRET_KEYS].map(key => [key, {
        configured: Boolean(
          environment[key]
          || stored.encryptedSecrets[key]
          || dotenvKeys.has(key)
        ),
        source: sources[key],
        locked: locked[key],
      }]),
    )
    const pendingKeys = Object.keys(stored.pendingRevisions)
    return {
      values: { ...stored.values },
      sources,
      locked,
      secrets,
      encryptionAvailable: safeStorage.isEncryptionAvailable(),
      restartRequired: pendingKeys.length > 0,
      pendingKeys,
      pendingRevisions: { ...stored.pendingRevisions },
      mcpConnections: Object.values(stored.mcpConnections)
        .sort((left, right) => left.name.localeCompare(right.name))
        .map(mcpConnectionSnapshot),
      mcpConnectionsLocked: environment[MCP_CONNECTIONS_ENV] !== undefined,
    }
  }

  upsertMcpConnection(
    environment: NodeJS.ProcessEnv,
    raw: McpConnectionUpdate,
  ): Record<string, unknown> {
    if (environment[MCP_CONNECTIONS_ENV] !== undefined) {
      throw new Error('MCP connections are locked by the parent process environment')
    }
    const stored = this.read()
    const input = raw?.connection
    if (!input || typeof input !== 'object') throw new Error('MCP connection is required')
    const name = boundedText(input.name, 'MCP connection name', 80)
    if (!name) throw new Error('MCP connection name is required')
    let id = boundedText(input.id, 'MCP connection id', 64).toLowerCase()
    if (!id) {
      const base = `amadeus_${name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'mcp'}`.slice(0, 56)
      id = base
      let suffix = 2
      while (stored.mcpConnections[id]) id = `${base.slice(0, 58)}_${suffix++}`
    }
    if (!MCP_ID_PATTERN.test(id)) throw new Error('MCP connection id must use lowercase letters, numbers, hyphens, or underscores')
    const existing = stored.mcpConnections[id]
    const transport = input.transport
    if (!['stdio', 'http'].includes(transport)) throw new Error('Unsupported MCP transport')
    const providerIds = [...new Set((input.providerIds || []).map(value => String(value || '').trim().toLowerCase()))]
    // Persist explicit target identity, not a second Provider catalog. The UI
    // offers live compatible manifests; Runtime/adapter projection enforces
    // availability. This also supports agents configured through backend .env.
    if (providerIds.some(value => !MCP_ID_PATTERN.test(value))) throw new Error('Invalid MCP Provider binding')
    const enabled = Boolean(input.enabled)
    if (enabled && providerIds.length === 0) throw new Error('Select a compatible Work Provider before enabling this connection')
    const command = boundedText(input.command, 'MCP command', 4096)
    const url = boundedText(input.url, 'MCP URL', 4096)
    if (transport === 'stdio' && !command) throw new Error('A stdio MCP connection requires a command')
    if (transport === 'http') {
      let protocol = ''
      try { protocol = new URL(url).protocol } catch { /* rejected below */ }
      if (!['http:', 'https:'].includes(protocol)) throw new Error('MCP URL must use HTTP(S)')
    }
    const argumentsValue = (input.arguments || []).map(value => boundedText(value, 'MCP argument', 4096))
    if (argumentsValue.length > 64) throw new Error('MCP connection accepts at most 64 arguments')
    const bearerTokenEnvVar = boundedText(input.bearerTokenEnvVar, 'MCP bearer token environment variable', 128)
    if (bearerTokenEnvVar && !MCP_ENV_KEY_PATTERN.test(bearerTokenEnvVar)) {
      throw new Error('Invalid MCP bearer token environment variable')
    }
    const encryptedEnvironment = raw.clearEnvironment
      ? {}
      : { ...(existing?.encryptedEnvironment || {}) }
    const environmentUpdate = raw.environment && typeof raw.environment === 'object' ? raw.environment : {}
    for (const [key, rawValue] of Object.entries(environmentUpdate)) {
      if (!MCP_ENV_KEY_PATTERN.test(key)) throw new Error(`Invalid MCP environment key: ${key}`)
      if (rawValue === null || rawValue === '') {
        delete encryptedEnvironment[key]
        continue
      }
      const value = secretText(rawValue, `MCP environment value for ${key}`, 16_384)
      if (!safeStorage.isEncryptionAvailable()) {
        throw new Error('System credential encryption is unavailable; MCP environment values were not saved')
      }
      encryptedEnvironment[key] = safeStorage.encryptString(value).toString('base64')
    }
    stored.mcpConnections[id] = {
      id,
      name,
      enabled,
      transport,
      providerIds,
      command: transport === 'stdio' ? command : '',
      arguments: transport === 'stdio' ? argumentsValue : [],
      cwd: transport === 'stdio' ? boundedText(input.cwd, 'MCP working directory', 4096) : '',
      url: transport === 'http' ? url : '',
      bearerTokenEnvVar: transport === 'http' ? bearerTokenEnvVar : '',
      encryptedEnvironment,
    }
    markPending(stored, [MCP_CONNECTIONS_ENV])
    this.write(stored)
    return this.snapshot(environment)
  }

  removeMcpConnection(environment: NodeJS.ProcessEnv, connectionId: string): Record<string, unknown> {
    if (environment[MCP_CONNECTIONS_ENV] !== undefined) {
      throw new Error('MCP connections are locked by the parent process environment')
    }
    const id = String(connectionId || '').trim().toLowerCase()
    const stored = this.read()
    if (!stored.mcpConnections[id]) throw new Error('MCP connection was not found')
    delete stored.mcpConnections[id]
    markPending(stored, [MCP_CONNECTIONS_ENV])
    this.write(stored)
    return this.snapshot(environment)
  }

  update(
    environment: NodeJS.ProcessEnv,
    raw: DesktopSettingsUpdate,
  ): Record<string, unknown> {
    const stored = this.read()
    const values = raw?.values && typeof raw.values === 'object' ? raw.values : {}
    const secrets = raw?.secrets && typeof raw.secrets === 'object' ? raw.secrets : {}
    const changedKeys = new Set<string>()

    for (const [key, rawValue] of Object.entries(values)) {
      if (!VALUE_KEYS.has(key)) throw new Error(`Unsupported desktop setting: ${key}`)
      if (explicitEnvironmentHas(environment, key)) {
        throw new Error(`${key} is locked by the parent process environment`)
      }
      if (rawValue === null || rawValue === '') {
        if (stored.values[key] !== undefined) changedKeys.add(key)
        delete stored.values[key]
        continue
      }
      const value = typeof rawValue === 'boolean' ? (rawValue ? 'true' : 'false') : String(rawValue)
      if (value.includes('\0') || value.length > (key === 'AMADEUS_ACP_PROVIDERS' ? 65536 : 4096)) throw new Error(`Invalid value for ${key}`)
      if (key === 'AMADEUS_ACP_PROVIDERS') validateAcpProviders(value)
      const choices = VALUE_CHOICES[key]
      if (choices && !choices.has(value)) throw new Error(`Invalid value for ${key}: ${value}`)
      if (IDENTIFIER_KEYS.has(key) && !/^[a-z][a-z0-9_-]{0,63}$/.test(value)) {
        throw new Error(`Invalid backend identifier for ${key}`)
      }
      const numberRange = NUMBER_RANGES[key]
      if (numberRange) {
        const parsed = Number(value)
        if (!Number.isFinite(parsed) || parsed < numberRange[0] || parsed > numberRange[1]) {
          throw new Error(`${key} must be between ${numberRange[0]} and ${numberRange[1]}`)
        }
        if (INTEGER_KEYS.has(key) && !Number.isInteger(parsed)) {
          throw new Error(`${key} must be an integer`)
        }
      }
      if (URL_KEYS.has(key)) {
        let protocol = ''
        try { protocol = new URL(value).protocol } catch { /* rejected below */ }
        if (!['http:', 'https:'].includes(protocol)) throw new Error(`${key} must be an HTTP(S) URL`)
      }
      if (WEBSOCKET_URL_KEYS.has(key)) {
        let protocol = ''
        try { protocol = new URL(value).protocol } catch { /* rejected below */ }
        if (!['ws:', 'wss:'].includes(protocol)) throw new Error(`${key} must be a WebSocket URL`)
      }
      if (stored.values[key] !== value) changedKeys.add(key)
      stored.values[key] = value
    }

    for (const [key, rawValue] of Object.entries(secrets)) {
      if (!SECRET_KEYS.has(key)) throw new Error(`Unsupported desktop secret: ${key}`)
      if (explicitEnvironmentHas(environment, key)) {
        throw new Error(`${key} is locked by the parent process environment`)
      }
      if (rawValue === null) {
        if (stored.encryptedSecrets[key] !== undefined) changedKeys.add(key)
        delete stored.encryptedSecrets[key]
        continue
      }
      const value = String(rawValue)
      if (!value || value.includes('\0') || value.length > 16_384) {
        throw new Error(`Invalid secret value for ${key}`)
      }
      if (!safeStorage.isEncryptionAvailable()) {
        throw new Error('System credential encryption is unavailable; the secret was not saved')
      }
      stored.encryptedSecrets[key] = safeStorage.encryptString(value).toString('base64')
      changedKeys.add(key)
    }

    markPending(stored, [...changedKeys].filter(key => !FRONTEND_ONLY_VALUE_KEYS.has(key)))
    this.write(stored)
    return this.snapshot(environment)
  }

  pendingRevisionSnapshot(): Record<string, number> {
    return { ...this.read().pendingRevisions }
  }

  markApplied(environment: NodeJS.ProcessEnv, revisions?: Readonly<Record<string, number>>): Record<string, unknown> {
    const stored = this.read()
    if (revisions === undefined) {
      stored.pendingRevisions = {}
    } else {
      for (const [key, revision] of Object.entries(revisions)) {
        if (stored.pendingRevisions[key] === revision) delete stored.pendingRevisions[key]
      }
    }
    this.write(stored)
    return this.snapshot(environment)
  }

  backendEnvironment(
    environment: NodeJS.ProcessEnv,
    launchDefaults: Readonly<Record<string, string>> = {},
  ): NodeJS.ProcessEnv {
    const stored = this.read()
    const dotenvKeys = this.dotenvKeys()
    const result: NodeJS.ProcessEnv = {}
    for (const [key, value] of Object.entries(stored.values)) {
      if (key === 'CODEX_PROVIDER_TRANSPORT' || FRONTEND_ONLY_VALUE_KEYS.has(key) || explicitEnvironmentHas(environment, key)) continue
      result[key] = value
    }

    const transport = stored.values.CODEX_PROVIDER_TRANSPORT
    if (transport && !explicitEnvironmentHas(environment, 'CODEX_PROVIDER_TRANSPORT')) {
      result.CODEX_APP_SERVER_PROVIDER_ENABLED = transport === 'app_server' ? 'true' : 'false'
      result.DIRECT_CODEX_PROVIDER_ENABLED = transport === 'direct' ? 'true' : 'false'
    }

    for (const [key, encrypted] of Object.entries(stored.encryptedSecrets)) {
      if (explicitEnvironmentHas(environment, key)) continue
      try {
        if (safeStorage.isEncryptionAvailable()) {
          result[key] = safeStorage.decryptString(Buffer.from(encrypted, 'base64'))
        }
      } catch (error) {
        console.error(`[electron] could not decrypt desktop secret ${key}`, error)
      }
    }
    for (const [key, value] of Object.entries(launchDefaults)) {
      if (
        environment[key] === undefined
        && stored.values[key] === undefined
        && stored.encryptedSecrets[key] === undefined
        && !dotenvKeys.has(key)
      ) {
        result[key] = value
      }
    }
    if (environment[MCP_CONNECTIONS_ENV] === undefined) {
      const connections = Object.values(stored.mcpConnections).map(connection => {
        const connectionEnvironment: Record<string, string> = {}
        for (const [key, encrypted] of Object.entries(connection.encryptedEnvironment)) {
          try {
            if (safeStorage.isEncryptionAvailable()) {
              connectionEnvironment[key] = safeStorage.decryptString(Buffer.from(encrypted, 'base64'))
            }
          } catch (error) {
            console.error(`[electron] could not decrypt MCP environment value ${connection.id}:${key}`, error)
          }
        }
        return {
          id: connection.id,
          name: connection.name,
          enabled: connection.enabled,
          transport: connection.transport,
          provider_ids: connection.providerIds,
          command: connection.command,
          arguments: connection.arguments,
          cwd: connection.cwd,
          url: connection.url,
          bearer_token_env_var: connection.bearerTokenEnvVar,
          environment: connectionEnvironment,
        }
      })
      if (connections.length) {
        result[MCP_CONNECTIONS_ENV] = JSON.stringify({ version: 1, connections })
      }
    }
    return result
  }
}
