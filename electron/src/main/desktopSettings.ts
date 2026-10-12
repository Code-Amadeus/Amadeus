import { safeStorage } from 'electron'
import fs from 'fs'
import path from 'path'
import process from 'node:process'
import { isDeepStrictEqual } from 'node:util'
import { catalogApplication, catalogInputKeys, catalogLaunchDefaults, catalogOptionValues, desktopCatalogFields } from '../shared/configCatalog.js'
import type { ExtensionConfigValue, ExtensionManifest, InstalledExtensionSummary } from '../shared/extensions.js'
import { matchesExtensionSelection, extensionRequiresRestart } from '../shared/extensionProfile.js'

export const EXTENSIONS_ENV = 'AMADEUS_EXTENSIONS'
export type StoredExtension = {
  revision?: number
  id: string
  version: string
  digest: string
  installId: string
  packageRoot: string
  sitePackages: string
  manifest: ExtensionManifest
  enabled: boolean
  pendingRemoval: boolean
  deleteDataOnRemoval: boolean
  values: Record<string, ExtensionConfigValue>
  encryptedSecrets: Record<string, string>
  providers: string[]
}
export type ExtensionRecordValidator = (manifest: ExtensionManifest, values: Record<string, ExtensionConfigValue>) => boolean | 'unavailable'

type StoredDesktopSettings = {
  version: 2
  values: Record<string, string>
  encryptedSecrets: Record<string, string>
  mcpConnections: Record<string, StoredMcpConnection>
  extensions: Record<string, StoredExtension>
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

// Structured records and retired-key removal have dedicated validators below.
const STRUCTURED_VALUE_KEYS = new Set(['AMADEUS_ACP_PROVIDERS', 'COOPERATIVE_CHAT_ENABLED'])
const VALUE_KEYS = new Set([
  ...STRUCTURED_VALUE_KEYS,
  ...Object.keys(desktopCatalogFields).filter(key => !desktopCatalogFields[key].secret),
])

const SECRET_KEYS = new Set([
  ...Object.keys(desktopCatalogFields).filter(key => desktopCatalogFields[key].secret),
])

const CODEX_TRANSPORT_KEYS = [
  'CODEX_APP_SERVER_PROVIDER_ENABLED',
  'DIRECT_CODEX_PROVIDER_ENABLED',
] as const

const VALUE_CHOICES: Record<string, ReadonlySet<string>> = Object.fromEntries(Object.entries(desktopCatalogFields)
  .filter(([, field]) => field.type === 'enum' || field.type === 'boolean')
  .map(([key, field]) => [key, new Set(field.type === 'boolean' ? field.accepted_values ?? ['true', 'false'] : catalogOptionValues(field))]))

const IDENTIFIER_KEYS = new Set(Object.keys(desktopCatalogFields).filter(key => desktopCatalogFields[key].identifier))
const MCP_CONNECTIONS_ENV = 'AMADEUS_MCP_CONNECTIONS'
const FRONTEND_ONLY_VALUE_KEYS = new Set(Object.keys(desktopCatalogFields).filter(key => desktopCatalogFields[key].scope === 'desktop'))
const RETIRED_ROUTE_KEY = 'COOPERATIVE_CHAT_ENABLED'
const MCP_ID_PATTERN = /^[a-z][a-z0-9_-]{0,63}$/
const MCP_ENV_KEY_PATTERN = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/

export function validateAcpProviders(raw: string): Array<Record<string, unknown>> {
  if (Buffer.byteLength(raw, 'utf8') > 65536) throw new Error('ACP configuration exceeds 64 KiB')
  const profiles: unknown = JSON.parse(raw)
  if (!Array.isArray(profiles) || profiles.length > 32) throw new Error('Expected at most 32 ACP agents')
  const ids = new Set<string>()
  for (const profile of profiles) {
    if (!profile || typeof profile !== 'object' || Array.isArray(profile)) throw new Error('Invalid ACP agent')
    const allowed = new Set(['id', 'name', 'command', 'args', 'enabled', 'environment', 'config_options', 'resume', 'skill_discovery'])
    if (Object.keys(profile).some(key => !allowed.has(key))) throw new Error('Unknown ACP configuration field')
    const id = profile.id
    if (typeof id !== 'string' || !MCP_ID_PATTERN.test(id) || ['codex', 'browser', 'openclaw', 'pi'].includes(id) || ids.has(id)) {
      throw new Error('ACP agents require unique ids distinct from built-in Providers')
    }
    ids.add(id)
    if (profile.skill_discovery !== undefined && !['catalog', 'dsh'].includes(profile.skill_discovery)) {
      throw new Error('ACP Skill discovery must be catalog or dsh')
    }
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
  return { version: 2, values: {}, encryptedSecrets: {}, mcpConnections: {}, extensions: {}, pendingRevisions: {}, nextRevision: 1 }
}

function allowedPendingKey(key: string): boolean {
  return key === MCP_CONNECTIONS_ENV || key === EXTENSIONS_ENV || (VALUE_KEYS.has(key) && !FRONTEND_ONLY_VALUE_KEYS.has(key)) || SECRET_KEYS.has(key)
}

// Paths are identities relative to the installation owner, never arbitrary
// filesystem targets. Manifest/config semantics belong to the Python helper.
function cleanStoredExtensions(value: unknown, validate: ExtensionRecordValidator): { records: Record<string, StoredExtension>; errors: string[] } {
  const records: Record<string, StoredExtension> = {}
  const errors: string[] = []
  if (value === undefined) return { records, errors }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return { records, errors: ['Stored extension records are invalid; no extensions were enabled.'] }
  for (const [key, raw] of Object.entries(value)) {
    try {
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error()
      const record = raw as StoredExtension
      if (Object.keys(records).length >= 64 || record.id !== key || typeof record.id !== 'string'
        || !/^[a-z][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)+$/.test(record.id) || record.id.length > 128
        || typeof record.version !== 'string' || record.version.length > 128
        || !record.manifest || record.manifest.id !== record.id || record.manifest.version !== record.version
        || typeof record.digest !== 'string' || !/^[a-f0-9]{64}$/.test(record.digest)
        || typeof record.installId !== 'string' || !/^[a-f0-9]{32}$/.test(record.installId)
        || typeof record.packageRoot !== 'string' || !/^[A-Za-z0-9_][A-Za-z0-9._-]*(\/[A-Za-z0-9_][A-Za-z0-9._-]*)*$/.test(record.packageRoot)
        || typeof record.sitePackages !== 'string' || !(record.manifest.runtime?.kind === 'none' && record.sitePackages === ''
          || /^(Lib\/site-packages|lib\/python\d+\.\d+\/site-packages)$/.test(record.sitePackages))
        || (record.revision !== undefined && (!Number.isSafeInteger(record.revision) || record.revision < 1))
        || typeof record.enabled !== 'boolean' || typeof record.pendingRemoval !== 'boolean'
        || typeof record.deleteDataOnRemoval !== 'boolean'
        || !record.values || typeof record.values !== 'object' || Array.isArray(record.values)
        || !record.encryptedSecrets || typeof record.encryptedSecrets !== 'object' || Array.isArray(record.encryptedSecrets)
        || !Array.isArray(record.providers) || record.providers.length > 32
        || record.providers.some(id => typeof id !== 'string' || !MCP_ID_PATTERN.test(id))) throw new Error()
      for (const [name, entry] of Object.entries(record.values)) {
        if (!record.manifest.config?.[name] || record.manifest.config[name].secret || !['string', 'number', 'boolean'].includes(typeof entry)
          || (typeof entry === 'string' && entry.length > 16_384) || (typeof entry === 'number' && !Number.isFinite(entry))) throw new Error()
      }
      for (const [name, encrypted] of Object.entries(record.encryptedSecrets)) {
        if (!record.manifest.config?.[name]?.secret || typeof encrypted !== 'string' || !encrypted || encrypted.length > 32_768) throw new Error()
      }
      const validated = validate(record.manifest, record.values)
      if (validated === false) errors.push('Extension is incompatible with this Host or configuration; saved records were retained and activation was skipped.')
      if (validated === 'unavailable') errors.push('Extension validation is temporarily unavailable; saved records were retained and activation was skipped.')
      records[key] = { ...record, revision: record.revision ?? 1, enabled: record.enabled && !record.pendingRemoval,
        deleteDataOnRemoval: record.deleteDataOnRemoval,
        values: { ...record.values }, encryptedSecrets: { ...record.encryptedSecrets }, providers: [...new Set(record.providers)].sort() }
    } catch {
      errors.push('A stored extension record could not be validated and was excluded from startup.')
    }
  }
  return { records, errors }
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
  return catalogInputKeys(key).some(candidate => environment[candidate] !== undefined)
}

function sourceFor(
  key: string,
  environment: NodeJS.ProcessEnv,
  stored: StoredDesktopSettings,
  dotenvKeys: ReadonlySet<string>,
): 'environment' | 'user' | 'dotenv' | 'default' {
  if (explicitEnvironmentHas(environment, key)) return 'environment'
  if (catalogInputKeys(key).some(candidate => stored.values[candidate] !== undefined || stored.encryptedSecrets[candidate] !== undefined)) return 'user'
  if (key === 'CODEX_PROVIDER_TRANSPORT') {
    if (CODEX_TRANSPORT_KEYS.some(candidate => dotenvKeys.has(candidate))) return 'dotenv'
  } else if (catalogInputKeys(key).some(candidate => dotenvKeys.has(candidate))) {
    return 'dotenv'
  }
  return 'default'
}

function storedSettingsFrom(text: string, validate: ExtensionRecordValidator, errors: string[]): StoredDesktopSettings {
  const parsed = JSON.parse(text) as Record<string, unknown>
  const pendingRevisions = cleanPendingRevisions(parsed.pendingRevisions, parsed.pendingKeys)
  const highestRevision = Math.max(0, ...Object.values(pendingRevisions))
  const storedNextRevision = Number.isSafeInteger(parsed.nextRevision) && Number(parsed.nextRevision) > 0
    ? Number(parsed.nextRevision)
    : 1
  const extensions = cleanStoredExtensions(parsed.extensions, validate)
  errors.push(...extensions.errors)
  return {
    version: 2,
    values: cleanRecord(parsed.values, VALUE_KEYS),
    encryptedSecrets: cleanRecord(parsed.encryptedSecrets, SECRET_KEYS),
    mcpConnections: cleanStoredMcpConnections(parsed.mcpConnections),
    extensions: extensions.records,
    pendingRevisions,
    nextRevision: Math.max(storedNextRevision, highestRevision + 1),
  }
}

// Flush before rename: after a power loss the target holds either its previous
// or its new content, never a renamed file whose data never reached the disk.
function writeFileDurably(target: string, text: string): void {
  const temporary = `${target}.tmp`
  const handle = fs.openSync(temporary, 'w', 0o600)
  try {
    fs.writeFileSync(handle, text, 'utf8')
    fs.fsyncSync(handle)
  } finally {
    fs.closeSync(handle)
  }
  fs.renameSync(temporary, target)
}

export class DesktopSettingsStore {
  private appliedExtensions = new Map<string, StoredExtension>()
  private extensionErrors: string[] = []
  private extensionWarnings = new Set<string>()
  private validatedExtensionIds = new Set<string>()
  private incompatibleExtensionIds = new Set<string>()
  constructor(
    private readonly filePath: string,
    private readonly dotenvPath: string,
    private readonly platform: string = process.platform,
    private readonly validateExtensionRecord: ExtensionRecordValidator = () => false,
  ) {}

  private get backupPath(): string {
    return `${this.filePath}.bak`
  }

  // Every save rewrites the store, so reading must never turn settings it
  // could not read into an empty store that the next save makes permanent.
  private read(): StoredDesktopSettings {
    this.extensionErrors = []
    this.validatedExtensionIds.clear()
    this.incompatibleExtensionIds.clear()
    const validate: ExtensionRecordValidator = (manifest, values) => {
      const result = this.validateExtensionRecord(manifest, values)
      if (result === true) this.validatedExtensionIds.add(manifest.id)
      if (result === false) this.incompatibleExtensionIds.add(manifest.id)
      return result
    }
    let failure: unknown
    try {
      return storedSettingsFrom(fs.readFileSync(this.filePath, 'utf8'), validate, this.extensionErrors)
    } catch (error) {
      failure = error
    }
    const code = (failure as NodeJS.ErrnoException).code
    // A missing file is a first launch or a deliberate reset, not damage.
    if (code === 'ENOENT') return emptyStore()
    try {
      const saved = storedSettingsFrom(fs.readFileSync(this.backupPath, 'utf8'), validate, this.extensionErrors)
      console.error(`[desktop-settings] ${this.filePath} is unreadable; using the copy from its last save`, failure)
      return saved
    } catch {
      // No usable copy; decide below without guessing.
    }
    // An I/O error says nothing about the content, so it is not replaced.
    if (code) throw failure
    // Unparseable content without a usable copy: keep the bytes for inspection
    // and start from defaults instead of silently overwriting them.
    const setAside = `${this.filePath}.corrupt-${Date.now()}`
    fs.renameSync(this.filePath, setAside)
    console.error(`[desktop-settings] unreadable settings were moved to ${setAside}`, failure)
    return emptyStore()
  }

  private write(value: StoredDesktopSettings): void {
    fs.mkdirSync(path.dirname(this.filePath), { recursive: true })
    const text = `${JSON.stringify(value, null, 2)}\n`
    // Commit the primary file last. If backup persistence fails, the caller
    // must not observe a failed confirmation whose retired key already vanished.
    writeFileDurably(this.backupPath, text)
    writeFileDurably(this.filePath, text)
  }

  private retiredSettings(environment: NodeJS.ProcessEnv, stored: StoredDesktopSettings): Array<Record<string, unknown>> {
    const facts: Array<Record<string, unknown>> = []
    const add = (raw: string, source: string) => facts.push({
      key: RETIRED_ROUTE_KEY,
      value: ['1', 'true', 'yes'].includes(raw.trim().toLowerCase()),
      source,
      effective_behavior: 'cooperative_only',
    })
    if (stored.values[RETIRED_ROUTE_KEY] !== undefined) add(stored.values[RETIRED_ROUTE_KEY], 'user')
    if (environment[RETIRED_ROUTE_KEY] !== undefined) {
      add(environment[RETIRED_ROUTE_KEY]!, 'environment')
    } else if (stored.values[RETIRED_ROUTE_KEY] === undefined) {
      // Only this retired boolean is read; no secret or general config value
      // is projected. Match the existing dotenv key inventory syntax.
      try {
        const lines = fs.readFileSync(this.dotenvPath, 'utf8').split(/\r?\n/)
        let raw: string | undefined
        for (const line of lines) {
          const match = line.match(/^\s*(?:export\s+)?COOPERATIVE_CHAT_ENABLED\s*=\s*(.*)$/)
          if (match) raw = match[1].replace(/\s+#.*$/, '').trim().replace(/^(['"])(.*)\1$/, '$2')
        }
        // Interpolation needs the backend's dotenv parser. Without those
        // resolved facts, do not guess that a nonliteral value means Off.
        if (raw !== undefined && !raw.includes('${')) add(raw, 'dotenv')
      } catch { /* No readable dotenv means no known retired fact. */ }
    }
    return facts
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
        configured: sources[key] === 'environment' ? Boolean(environment[key])
          : sources[key] === 'user' ? Boolean(stored.encryptedSecrets[key]) : dotenvKeys.has(key),
        source: sources[key],
        locked: locked[key],
      }]),
    )
    const pendingKeys = Object.keys(stored.pendingRevisions)
    const launchDefaults = catalogLaunchDefaults(this.platform)
    const startupValues = Object.fromEntries([...VALUE_KEYS].flatMap(key => {
      // Compound transport and dotenv interpolation need their owning parser.
      const inputs = catalogInputKeys(key)
      const value = sources[key] === 'environment' ? (key === 'CODEX_PROVIDER_TRANSPORT' ? undefined
        : inputs.map(candidate => environment[candidate]).find(value => value !== undefined))
        : sources[key] === 'user' ? inputs.map(candidate => stored.values[candidate]).find(value => value !== undefined)
        : sources[key] === 'default' ? launchDefaults[key] : undefined
      return value === undefined ? [] : [[key, value]]
    }))
    return {
      platform: process.platform,
      values: { ...stored.values, ...(environment.AMADEUS_WINDOWS_STARTUP_MODE !== undefined
        ? { AMADEUS_WINDOWS_STARTUP_MODE: environment.AMADEUS_WINDOWS_STARTUP_MODE } : {}) },
      sources,
      startupValues,
      retired_settings: this.retiredSettings(environment, stored),
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
      extensions: Object.values(stored.extensions).map(record => this.extensionSummary(record, pendingKeys.includes(EXTENSIONS_ENV), this.validatedExtensionIds.has(record.id))),
      extensionErrors: [...this.extensionErrors, ...this.extensionWarnings, ...(environment[EXTENSIONS_ENV] !== undefined
        ? ['Parent AMADEUS_EXTENSIONS was ignored; extension startup is owned by desktop installation records.'] : [])],
    }
  }

  private extensionSummary(record: StoredExtension, restartRequired: boolean, validated: boolean): InstalledExtensionSummary {
    const applied = this.appliedExtensions.get(record.id)
    const application = this.extensionChangeNeedsRestart(record) ? 'backend_restart' : 'frontend'
    restartRequired = restartRequired && application === 'backend_restart' && (record.pendingRemoval || applied?.revision !== record.revision)
    const revisions = { revision: record.revision ?? 1, appliedRevision: applied?.revision, appliedVersion: applied?.version, application,
      backendStillRunning: Boolean(applied?.enabled && extensionRequiresRestart(applied.manifest)) } as const
    if (!validated) return { id: record.id, version: record.version, digest: record.digest, manifest: null,
      ...revisions,
      enabled: record.enabled, pendingRemoval: record.pendingRemoval, values: {}, secretConfigured: {}, providers: [],
      status: this.incompatibleExtensionIds.has(record.id) ? 'incompatible' : 'unavailable', restartRequired,
      validationError: this.incompatibleExtensionIds.has(record.id)
        ? 'Extension is incompatible with this Host or configuration; saved configuration and credentials were retained.'
        : 'Trusted extension validation is temporarily unavailable; saved configuration and credentials were retained.' }
    return { id: record.id, version: record.version, digest: record.digest, manifest: record.manifest,
      ...revisions,
      enabled: record.enabled, pendingRemoval: record.pendingRemoval, values: { ...record.values },
      secretConfigured: Object.fromEntries(Object.entries(record.manifest.config || {}).filter(([, field]) => field.secret)
        .map(([key]) => [key, Boolean(record.encryptedSecrets[key])])), providers: [...record.providers],
      status: record.pendingRemoval ? 'pending_removal' : record.enabled ? 'enabled' : 'disabled', restartRequired }
  }

  extensionRecords(): StoredExtension[] {
    const records = Object.values(this.read().extensions)
    return records.filter(record => this.validatedExtensionIds.has(record.id))
  }
  storedExtensionRecords(): StoredExtension[] { return Object.values(this.read().extensions) }
  appliedExtensionRecords(): StoredExtension[] { return structuredClone([...this.appliedExtensions.values()]) }
  acceptExtensionApplication(record: StoredExtension): void { this.appliedExtensions.set(record.id, structuredClone(record)) }
  releaseExtensionApplication(id: string): void { this.appliedExtensions.delete(id) }
  releaseBackendExtensionApplications(): void {
    for (const record of this.appliedExtensions.values()) if (extensionRequiresRestart(record.manifest)) this.appliedExtensions.delete(record.id)
  }
  extensionChangeNeedsRestart(record: StoredExtension, previous?: StoredExtension): boolean {
    return [record, previous, this.appliedExtensions.get(record.id)].some(item => item && extensionRequiresRestart(item.manifest))
  }
  extensionIsInstalled(id: string): boolean { return Object.hasOwn(this.read().extensions, id) }
  deferIncompleteExtensionApplication(): boolean {
    const stored = this.read()
    const incomplete = Object.values(stored.extensions).some(record => this.extensionChangeNeedsRestart(record)
      && (record.pendingRemoval || record.enabled && !this.validatedExtensionIds.has(record.id)))
    if (incomplete && !stored.pendingRevisions[EXTENSIONS_ENV]) {
      markPending(stored, [EXTENSIONS_ENV])
      this.write(stored)
    }
    return incomplete
  }
  reportExtensionWarning(message: string): void { this.extensionWarnings.add(message) }

  // The durable writer commits the backup before the primary. A failed save
  // may therefore leave a recoverable record only in the existing backup.
  extensionInstallationReferences(): Set<string> {
    const result = new Set<string>()
    for (const file of [this.filePath, this.backupPath]) {
      try {
        const parsed = JSON.parse(fs.readFileSync(file, 'utf8')) as Record<string, unknown>
        const records = cleanStoredExtensions(parsed.extensions, () => 'unavailable').records
        for (const record of Object.values(records)) result.add(record.installId)
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code && (error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
        // Missing or malformed copies cannot restore valid install authority.
      }
    }
    return result
  }

  extensionSecrets(record: StoredExtension, replacedKeys: ReadonlySet<string> = new Set()): Record<string, string> {
    const result: Record<string, string> = {}
    for (const [key, encrypted] of Object.entries(record.encryptedSecrets)) {
      if (replacedKeys.has(key)) continue
      if (!safeStorage.isEncryptionAvailable()) throw new Error('System credential encryption is unavailable; extension secrets cannot be loaded')
      try { result[key] = safeStorage.decryptString(Buffer.from(encrypted, 'base64')) }
      catch { throw new Error('An extension credential could not be decrypted; configure it again before enabling the extension') }
    }
    return result
  }

  /** Called only by the installation owner after canonical validation. */
  saveExtension(environment: NodeJS.ProcessEnv, record: StoredExtension, secrets: Record<string, string | null> = {}): Record<string, unknown> {
    const stored = this.read()
    const previous = stored.extensions[record.id]
    const encryptedSecrets = { ...record.encryptedSecrets }
    for (const [key, value] of Object.entries(secrets)) {
      if (!record.manifest.config?.[key]?.secret) throw new Error('Unknown extension secret field')
      if (value === null) { delete encryptedSecrets[key]; continue }
      if (!safeStorage.isEncryptionAvailable()) throw new Error('System credential encryption is unavailable; extension secrets were not saved')
      const text = secretText(value, 'extension secret', 16_384)
      let unchanged = false
      try { unchanged = Boolean(encryptedSecrets[key]) && safeStorage.decryptString(Buffer.from(encryptedSecrets[key], 'base64')) === text } catch { /* replacement repairs an unreadable credential */ }
      if (!unchanged) encryptedSecrets[key] = safeStorage.encryptString(text).toString('base64')
    }
    const clean = cleanStoredExtensions({ [record.id]: { ...record, encryptedSecrets } }, this.validateExtensionRecord)
    if (clean.errors.length) throw new Error('Extension record validation failed')
    const next = { ...clean.records[record.id], revision: previous?.revision ?? 1 }
    if (previous && isDeepStrictEqual(previous, next)) return this.snapshot(environment)
    next.revision = previous ? (previous.revision ?? 1) + 1 : 1
    if (!Number.isSafeInteger(next.revision)) throw new Error('Extension revision limit reached')
    stored.extensions[record.id] = next
    if (this.extensionChangeNeedsRestart(next, previous)) markPending(stored, [EXTENSIONS_ENV])
    this.write(stored)
    return this.snapshot(environment)
  }

  requestExtensionRemoval(environment: NodeJS.ProcessEnv, id: string, deleteData: boolean = false): Record<string, unknown> {
    const stored = this.read()
    if (typeof id !== 'string' || !Object.hasOwn(stored.extensions, id)) throw new Error('Extension was not found')
    const record = stored.extensions[id]
    if (!record) throw new Error('Extension was not found')
    if (record.pendingRemoval && record.deleteDataOnRemoval === deleteData) return this.snapshot(environment)
    record.revision = (record.revision ?? 1) + 1
    record.enabled = false
    record.pendingRemoval = true
    record.deleteDataOnRemoval = deleteData
    if (this.extensionChangeNeedsRestart(record)) markPending(stored, [EXTENSIONS_ENV])
    this.write(stored)
    return this.snapshot(environment)
  }

  /** Physical deletion has already succeeded while the backend was stopped. */
  finishExtensionRemoval(id: string, installId: string): void {
    const stored = this.read()
    if (stored.extensions[id]?.pendingRemoval && stored.extensions[id].installId === installId) {
      delete stored.extensions[id]
      this.write(stored)
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
      if (key === RETIRED_ROUTE_KEY) {
        if (rawValue !== null) throw new Error(`${key} is retired and read-only; confirmation may only remove the stored key`)
        // Deleting a stored retired key never edits an overriding environment
        // source. It is allowed even when that source locks ordinary settings.
        delete stored.values[key]
        delete stored.pendingRevisions[key]
        continue
      }
      if (explicitEnvironmentHas(environment, key)) {
        throw new Error(`${key} is locked by the parent process environment`)
      }
      // Keep an explicit empty character prompt so restoring the built-in
      // role also overrides any project .env value on the next start.
      if (rawValue === null || (rawValue === '' && !desktopCatalogFields[key]?.allow_empty)) {
        for (const candidate of catalogInputKeys(key)) {
          if (stored.values[candidate] !== undefined) {
            changedKeys.add(candidate)
            changedKeys.add(catalogInputKeys(key)[0])
          }
          delete stored.values[candidate]
        }
        continue
      }
      if (desktopCatalogFields[key]?.trim && typeof rawValue !== 'string') throw new Error(`${key} must be a string`)
      const value = desktopCatalogFields[key]?.trim ? String(rawValue).trim()
        : typeof rawValue === 'boolean' ? (rawValue ? 'true' : 'false') : String(rawValue)
      const maxLength = key === 'AMADEUS_ACP_PROVIDERS' ? 65536 : desktopCatalogFields[key]?.max_length ?? 4096
      if (value.includes('\0') || String(rawValue).length > maxLength) throw new Error(`Invalid value for ${key}`)
      if (key === 'AMADEUS_ACP_PROVIDERS') validateAcpProviders(value)
      const extensionSelection = Object.values(stored.extensions).some(record => this.validatedExtensionIds.has(record.id) && !record.pendingRemoval
        && matchesExtensionSelection(record.manifest, desktopCatalogFields[key]?.extension_selection, value))
      const choices = VALUE_CHOICES[key]
      if (choices && !choices.has(value) && !extensionSelection) throw new Error(`Invalid value for ${key}: ${value}`)
      const schemes = desktopCatalogFields[key]?.schemes
      if (schemes) {
        let protocol = ''
        try { protocol = new URL(value).protocol.slice(0, -1) } catch { /* rejected below */ }
        if (!schemes.includes(protocol)) throw new Error(`${key} must use ${schemes.join(' or ')} URL`)
      }
      if (IDENTIFIER_KEYS.has(key) && !/^[a-z][a-z0-9_-]{0,63}$/.test(value) && !extensionSelection) {
        throw new Error(`Invalid backend identifier for ${key}`)
      }
      const definition = desktopCatalogFields[key]
      if (definition && ['integer', 'number'].includes(definition.type)) {
        const parsed = Number(value)
        if (!Number.isFinite(parsed)) throw new Error(`${key} must be a number`)
        if (definition.type === 'integer' && !Number.isInteger(parsed)) throw new Error(`${key} must be an integer`)
        if (definition.min !== undefined && parsed < definition.min || definition.max !== undefined && parsed > definition.max) {
          throw new Error(`${key} must be between ${definition.min} and ${definition.max}`)
        }
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

    markPending(stored, [...changedKeys].filter(key => !['frontend', 'desktop_restart'].includes(catalogApplication(key))))
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
  ): NodeJS.ProcessEnv {
    const stored = this.read()
    const dotenvKeys = this.dotenvKeys()
    const result: NodeJS.ProcessEnv = {}
    // Canonicalize both parent and stored legacy inputs before Python loads
    // dotenv. Within one source the canonical name wins; every parent input
    // precedes every stored input, and both precede the project file.
    for (const key of VALUE_KEYS) {
      if (FRONTEND_ONLY_VALUE_KEYS.has(key)) continue
      const inputs = catalogInputKeys(key)
      if (inputs.length < 2 || inputs[0] !== key || environment[key] !== undefined) continue
      const inherited = inputs.slice(1).map(alias => environment[alias]).find(value => value !== undefined)
        ?? inputs.map(candidate => stored.values[candidate]).find(value => value !== undefined)
      if (inherited !== undefined) result[key] = inherited
    }
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
    for (const [key, value] of Object.entries(catalogLaunchDefaults(this.platform))) {
      if (sourceFor(key, environment, stored, dotenvKeys) === 'default') {
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
