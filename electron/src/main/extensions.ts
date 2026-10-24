import fs from 'node:fs'
import { securityBlock, type ArtifactIdentity } from './extensionSecurity.js'
import type { CatalogIndex } from '../shared/extensionCatalog.js'
import { assertExtensionPath } from './extensionPaths.js'
export { assertExtensionPath } from './extensionPaths.js'
import path from 'node:path'
import { randomBytes, createHash } from 'node:crypto'
import { spawn } from 'node:child_process'
import { pathToFileURL } from 'node:url'
import { forceStopBackendTree } from './backendProcessTree.js'
import { bundledExtensionSdk } from './extensionSdk.js'
import { DesktopSettingsStore, EXTENSIONS_ENV, type StoredExtension } from './desktopSettings.js'
import type { ExtensionConfigValue, ExtensionManifest, ExtensionRemovalOptions, ExtensionUpdate, ExtensionConfirmation } from '../shared/extensions.js'
import { ExtensionThemeResources } from './extensionThemes.js'
import { readExtensionImage } from './extensionImages.js'
import { startupValue } from '../shared/startupSettings.js'
import { ExtensionUpdateJournal } from './extensionUpdates.js'
import type { DesktopThemeState } from '../shared/extensionTheme.js'
import { contributionProfile, extensionNeedsAgent, extensionRequiresRestart } from '../shared/extensionProfile.js'

type HelperReply = { catalog?: CatalogIndex; dependency_mode?: 'offline' | 'resolve'; ok: boolean; error?: string; unavailable?: boolean; manifest?: ExtensionManifest; digest?: string;
  package_root?: string; manifest_member?: string; distribution?: string; config?: Record<string, ExtensionConfigValue>; missing?: string[] }
type RunOptions = { cwd: string; env: NodeJS.ProcessEnv; input?: string; allowFailureJson?: boolean; timeoutMs?: number; maxOutputBytes?: number }
export type ExtensionCommandRunner = (command: string, args: string[], options: RunOptions) => Promise<string>

export class ExtensionPreparationShutdownError extends Error {
  constructor() { super('Extension preparation shutdown could not be confirmed; its files were retained') }
}
export class ExtensionChangeCancelled extends Error {
  constructor() { super('Extension change cancelled') }
}

// Only trusted inspection/install helpers receive transport configuration.
// Third-party Runner environments are constructed separately by Python.
export function extensionInstallerEnvironment(environment: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const allowed = new Set(['SystemRoot', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'Path', 'PATHEXT', 'TEMP', 'TMP',
    'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
    'PIP_CONFIG_FILE', 'PIP_INDEX_URL', 'PIP_EXTRA_INDEX_URL', 'PIP_TRUSTED_HOST', 'PIP_PROXY', 'PIP_CERT', 'PIP_CLIENT_CERT',
    'PIP_NO_INDEX', 'PIP_FIND_LINKS', 'PIP_TIMEOUT', 'PIP_DEFAULT_TIMEOUT', 'PIP_RETRIES',
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
    'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'SSL_CERT_FILE', 'SSL_CERT_DIR'])
  return { ...Object.fromEntries(Object.entries(environment).filter(([key]) => allowed.has(key))),
    PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', PIP_DISABLE_PIP_VERSION_CHECK: '1', PIP_NO_INPUT: '1' }
}

export const runExtensionCommand: ExtensionCommandRunner = (command, args, options) => new Promise((resolve, reject) => {
  const child = spawn(command, args, { cwd: options.cwd, env: options.env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] })
  let stdout = ''
  let size = 0
  let overflow = false
  let timedOut = false
  let termination: Promise<void> | null = null
  const terminate = () => {
    if (termination) return
    termination = forceStopBackendTree(child)
    void termination.catch(() => reject(new ExtensionPreparationShutdownError()))
  }
  const timeout = setTimeout(() => { timedOut = true; terminate() }, options.timeoutMs ?? 300_000)
  child.stdout?.on('data', chunk => {
    size += chunk.length
    if (size > (options.maxOutputBytes ?? 1_048_576)) { overflow = true; terminate(); return }
    stdout += String(chunk)
  })
  // Pip may include authenticated index/proxy URLs. Never forward its output.
  child.stderr?.on('data', () => {})
  child.on('error', () => { clearTimeout(timeout); reject(new Error('The trusted extension preparation command could not start')) })
  child.on('close', async code => {
    clearTimeout(timeout)
    if (termination) {
      try { await termination }
      catch { reject(new ExtensionPreparationShutdownError()); return }
      if (process.platform !== 'win32') { reject(new ExtensionPreparationShutdownError()); return }
    }
    if (timedOut) reject(new Error('Extension preparation timed out'))
    else if (overflow) reject(new Error('Extension preparation returned too much output'))
    else if (code !== 0 && !options.allowFailureJson) reject(new Error(`Extension preparation failed (exit ${code ?? 'unknown'}); check Python and pip transport settings`))
    else resolve(stdout)
  })
  child.stdin?.on('error', () => {})
  child.stdin?.end(options.input || '')
})

export class ExtensionPackageHelper {
  private readonly validRecords = new Map<string, boolean>()
  private unavailableUntil = 0
  constructor(private readonly projectRoot: string, private readonly python: () => string,
    private readonly environment: NodeJS.ProcessEnv, private readonly run: ExtensionCommandRunner = runExtensionCommand) {}

  private get script(): string { return path.join(this.projectRoot, 'tools', 'extension_package.py') }

  validateRecord = (manifest: ExtensionManifest, config: Record<string, ExtensionConfigValue>): boolean | 'unavailable' => {
    return this.validRecords.get(this.recordKey(manifest, config)) ?? 'unavailable'
  }

  private recordKey(manifest: ExtensionManifest, config: Record<string, ExtensionConfigValue>): string {
    return createHash('sha256').update(JSON.stringify({ manifest, config, allow_incomplete: true })).digest('hex')
  }

  async refreshRecords(records: StoredExtension[]): Promise<void> {
    for (const record of records) {
      if (Date.now() < this.unavailableUntil) return
      const key = this.recordKey(record.manifest, record.values)
      if (this.validRecords.has(key)) continue
      try {
        const output = await this.run(this.python(), ['-I', this.script, 'validate-config'], {
          cwd: this.projectRoot, env: extensionInstallerEnvironment(this.environment),
          input: JSON.stringify({ manifest: record.manifest, config: record.values, allow_incomplete: true }),
          allowFailureJson: true, timeoutMs: 10_000,
        })
        const reply = JSON.parse(output) as HelperReply
        if (reply.unavailable || typeof reply.ok !== 'boolean') throw new Error('validator unavailable')
        this.validRecords.set(key, reply.ok)
      } catch { this.unavailableUntil = Date.now() + 1000 }
    }
  }

  async call(command: 'validate-catalog' | 'inspect-catalog-wheel' | 'inspect-wheel' | 'prepare-data-wheel' | 'validate-config' | 'validate-installed', args: string[] = [], input?: unknown): Promise<HelperReply> {
    const output = await this.run(this.python(), ['-I', this.script, command, ...args], {
      cwd: this.projectRoot, env: extensionInstallerEnvironment(this.environment),
      allowFailureJson: true, maxOutputBytes: command === 'validate-catalog' ? 8 * 1024 * 1024 : undefined,
      ...(input === undefined ? {} : { input: JSON.stringify(input) }),
    })
    let reply: HelperReply
    try { reply = JSON.parse(output) as HelperReply }
    catch { throw new Error('The trusted extension validator returned an invalid response') }
    if (!reply || reply.ok !== true) throw new Error(reply?.error || 'Extension validation failed')
    if (command === 'validate-config' && input && typeof input === 'object' && 'manifest' in input && reply.config) {
      const definition = (input as { manifest: ExtensionManifest }).manifest
      this.validRecords.set(this.recordKey(definition, reply.config), true)
    }
    return reply
  }
}

function safeRelative(value: string): boolean {
  return /^[A-Za-z0-9_][A-Za-z0-9._-]*(\/[A-Za-z0-9_][A-Za-z0-9._-]*)*$/.test(value)
}

/** Unlinks files recursively only after verifying the entire tree has no links. */
export function removeExtensionDirectory(root: string, target: string, installId: string): void {
  assertExtensionPath(root, target)
  if (!/^[a-f0-9]{32}$/.test(installId) || path.basename(target) !== installId) throw new Error('Invalid extension installation identity')
  if (!fs.existsSync(target)) return
  const markerPath = path.join(target, '.amadeus-extension-install.json')
  assertExtensionPath(root, markerPath)
  let marker: { installId?: unknown }
  try { marker = JSON.parse(fs.readFileSync(markerPath, 'utf8')) }
  catch { throw new Error('Extension installation ownership could not be verified') }
  if (marker.installId !== installId) throw new Error('Extension installation ownership does not match')
  removeVerifiedDirectory(root, target, '.amadeus-extension-install.json')
}

function removeVerifiedDirectory(root: string, target: string, markerName: string): void {
  function verify(directory: string): void {
    assertExtensionPath(root, directory)
    for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
      const child = path.join(directory, item.name)
      const stat = fs.lstatSync(child)
      if (stat.isSymbolicLink()) throw new Error('Extension cleanup refused a link or junction')
      if (stat.isDirectory()) verify(child)
    }
  }
  verify(target)
  const markerPath = path.join(target, markerName)
  const marker = fs.readFileSync(markerPath)
  // Do not use recursive rm: traversal and deletion both recheck each boundary.
  function remove(directory: string): void {
    assertExtensionPath(root, directory)
    for (const name of fs.readdirSync(directory)) {
      if (directory === target && name === markerName) continue
      const child = path.join(directory, name)
      assertExtensionPath(root, child)
      const stat = fs.lstatSync(child)
      if (stat.isDirectory()) remove(child)
      else if (stat.isFile()) fs.unlinkSync(child)
      else throw new Error('Extension cleanup encountered an unsupported filesystem entry')
    }
    if (directory !== target) fs.rmdirSync(directory)
  }
  remove(target)
  // A transient lock must not erase the proof needed by the next cleanup.
  fs.unlinkSync(markerPath)
  try { fs.rmdirSync(target) }
  catch (error) {
    fs.writeFileSync(markerPath, marker, { mode: 0o600, flag: 'wx' })
    throw error
  }
}

export class ExtensionInstaller {
  private mutation: Promise<unknown> = Promise.resolve()
  private preparing = new Set<string>()
  private backendReferences = new Set<string>()
  private cleanupReady = false
  private uncertainPreparations = new Set<string>()
  private readonly updates: ExtensionUpdateJournal
  private updateRecoveryRequired = false
  constructor(private readonly root: string, private readonly projectRoot: string, private readonly store: DesktopSettingsStore,
    private readonly helper: ExtensionPackageHelper, private readonly python: () => string,
    private readonly environment: NodeJS.ProcessEnv, private readonly run: ExtensionCommandRunner = runExtensionCommand,
    private readonly confirm: (review: ExtensionConfirmation) => Promise<boolean> = async () => false,
    private readonly sdkDirectory = path.join(projectRoot, 'vendor', 'extension-sdk'),
    private readonly themes = new ExtensionThemeResources(),
    private readonly changed: () => void = () => {},
    private readonly assertInstallIdle: () => void = () => {}) {
    this.updates = new ExtensionUpdateJournal(root, raw => store.validateExtensionRollbackRecord(raw))
  }

  async prepareStartup(): Promise<void> {
    // Structural validation permits loading records for the asynchronous Python
    // validator. It grants no activation or rollback authority: re-read with the
    // strict validator before the app can apply a queued change or launch Host.
    const update = this.updates.read(raw => this.store.validateExtensionRollbackRecord(raw, () => true))
    await this.helper.refreshRecords([...this.store.storedExtensionRecords(),
      ...(update ? [update.previous, update.target] : [])])
    if (update) this.updates.read()
  }

  pendingUpdate(): { id: string; version: string; previousVersion: string; canConfirm: boolean; recoveryRequired: boolean } | undefined {
    const update = this.updates.read()
    if (!update) return
    return { id: update.target.id, version: update.target.version, previousVersion: update.previous.version,
      canConfirm: !this.updateRecoveryRequired && update.owner === this.updates.owner && update.phase === 'startup_confirmed',
      recoveryRequired: this.updateRecoveryRequired || update.owner !== this.updates.owner || update.phase === 'recovery_required' }
  }
  assertUpdateCanLaunch(): void {
    if (this.updateRecoveryRequired || this.pendingUpdate()?.recoveryRequired) throw new Error('An unconfirmed plugin update requires rollback before the backend can start. Open Manage plugins.')
  }
  private observeUpdateHealth(statuses: unknown): void {
    const update = this.updates.read()
    if (!update || update.owner !== this.updates.owner || this.updateRecoveryRequired) return
    const target = this.store.storedExtensionRecords().find(record => record.id === update.target.id
      && record.installId === update.target.installId && record.revision === update.target.revision)
    const rows = Array.isArray(statuses) ? statuses.filter(row => row?.id === update.target.id) : []
    const row = rows.length === 1 ? rows[0] : undefined
    // Projection readiness is not a successful remote MCP call. User keep is
    // still separate, and an earlier process's receipt is never reusable.
    const expected = target ? Object.entries(target.manifest.contributes).flatMap(([kind, items]) => items
      .filter(item => contributionProfile(kind, item.role)?.apply !== 'frontend').map(item => `${kind}/${item.id}`)).sort() : []
    const reported = Array.isArray(row?.contributions) ? row.contributions : []
    const backendReady = row?.version === target?.version && row?.revision === target?.revision
      && row?.startup === 'accepted' && row?.state === 'active'
      && reported.every((item: { health?: string }) => item?.health === 'ready')
      && JSON.stringify(reported.map((item: { kind?: string; id?: string }) => `${item?.kind}/${item?.id}`).sort()) === JSON.stringify(expected)
    const frontend = this.store.appliedExtensionRecords().find(record => record.id === target?.id
      && record.installId === target.installId && record.revision === target.revision)
    const frontendReady = frontend?.enabled && target && !this.store.extensionFrontendClosed(target.id)
    const healthy = target && (!target.enabled || (expected.length ? backendReady : frontendReady))
    this.updates.write({ ...update, phase: healthy ? 'startup_confirmed' : 'recovery_required' })
  }
  refreshUpdateHealth(statuses: unknown): Promise<void> {
    return this.serialize(async () => { this.observeUpdateHealth(statuses); this.changed() })
  }
  markUpdateFailed(): void {
    const update = this.updates.read()
    if (update?.owner === this.updates.owner) { this.updateRecoveryRequired = true; this.updates.write({ ...update, phase: 'recovery_required' }) }
  }
  confirmPendingUpdate(): Promise<Record<string, unknown>> {
    return this.serialize(async () => {
      const update = this.updates.read()
      if (!update || !this.pendingUpdate()?.canConfirm) throw new Error('Plugin startup has not been confirmed; the previous version is retained.')
      const current = this.store.storedExtensionRecords().find(record => record.id === update.target.id)
      if (current?.installId !== update.target.installId || current.revision !== update.target.revision) throw new Error('Plugin state changed; confirmation was refused.')
      this.store.assertExtensionAllowed(update.target)
      this.updates.clear()
      this.changed()
      return this.store.snapshot(this.environment)
    })
  }
  rollbackPendingUpdate(): Promise<Record<string, unknown>> {
    return this.serialize(async () => {
      this.assertInstallIdle()
      const update = this.updates.read()
      if (!update) throw new Error('No plugin update is waiting for rollback.')
      const previous = update.previous
      const directory = path.join(this.root, previous.installId)
      assertExtensionPath(this.root, directory)
      const originalWheel = fs.readdirSync(directory).find(name => {
        const file = path.join(directory, name)
        assertExtensionPath(this.root, file)
        return name.endsWith('.whl') && fs.lstatSync(file).isFile() && createHash('sha256').update(fs.readFileSync(file)).digest('hex') === previous.digest
      })
      if (!originalWheel) throw new Error('The previous verified wheel is unavailable; rollback is blocked without deleting either version.')
      await this.helper.call('validate-installed', [this.packagePath(previous)], { manifest: previous.manifest })
      if (!await this.confirm({ operation: 'update', manifest: previous.manifest, previousVersion: update.target.version,
        previousManifest: update.target.manifest, digest: previous.digest, sourceFile: originalWheel })) throw new ExtensionChangeCancelled()
      this.assertInstallIdle()
      const confirmedWheel = path.join(directory, originalWheel)
      assertExtensionPath(this.root, confirmedWheel)
      if (!fs.lstatSync(confirmedWheel).isFile() || createHash('sha256').update(fs.readFileSync(confirmedWheel)).digest('hex') !== previous.digest) throw new Error('The previous verified wheel changed during confirmation; rollback is blocked.')
      this.store.restoreExtension(previous, update.target.installId)
      this.updates.clear()
      this.updateRecoveryRequired = false
      await this.applyFrontendChanges()
      this.changed()
      return this.store.snapshot(this.environment)
    })
  }

  private get dataRoot(): string { return path.join(path.dirname(this.root), 'extension-data') }

  private dataPath(id: string): string { return path.join(this.dataRoot, createHash('sha256').update(id).digest('hex')) }

  private packageData(id: string): string {
    const directory = this.dataPath(id)
    assertExtensionPath(this.dataRoot, directory)
    const markerPath = path.join(directory, '.amadeus-extension-data.json')
    if (!fs.existsSync(directory)) {
      fs.mkdirSync(directory, { recursive: true })
      fs.writeFileSync(markerPath, JSON.stringify({ id }), { mode: 0o600, flag: 'wx' })
    } else {
      assertExtensionPath(this.dataRoot, markerPath)
      let marker: { id?: unknown }
      try { marker = JSON.parse(fs.readFileSync(markerPath, 'utf8')) }
      catch { throw new Error('Extension package data ownership could not be verified') }
      if (marker.id !== id) throw new Error('Extension package data ownership does not match')
    }
    return directory
  }

  private removePackageData(id: string): void {
    const directory = this.dataPath(id)
    assertExtensionPath(this.dataRoot, directory)
    if (!fs.existsSync(directory)) return
    const markerPath = path.join(directory, '.amadeus-extension-data.json')
    assertExtensionPath(this.dataRoot, markerPath)
    let marker: { id?: unknown }
    try { marker = JSON.parse(fs.readFileSync(markerPath, 'utf8')) }
    catch { throw new Error('Extension data deletion ownership could not be verified') }
    if (marker.id !== id) throw new Error('Extension data deletion ownership does not match')
    removeVerifiedDirectory(this.dataRoot, directory, '.amadeus-extension-data.json')
  }

  private serialize<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.mutation.then(operation)
    this.mutation = result.catch(() => {})
    return result
  }

  install(wheel: string, delivery?: { catalog?: boolean; identity: ArtifactIdentity; assertCurrent: () => void; onStage?: (stage: 'verifying' | 'confirming' | 'preparing') => void }): Promise<Record<string, unknown>> {
    return this.serialize(async () => {
      this.assertInstallIdle()
      if (this.updates.read()) throw new Error('Confirm or roll back the previous plugin update before installing another package.')
      this.store.assertExtensionAllowed()
      delivery?.assertCurrent()
      if (!path.isAbsolute(wheel) || path.extname(wheel).toLowerCase() !== '.whl' || !fs.lstatSync(wheel).isFile()) {
        throw new Error('Select a local Python wheel (.whl)')
      }
      const installId = randomBytes(16).toString('hex')
      const directory = path.join(this.root, installId)
      assertExtensionPath(this.root, directory)
      fs.mkdirSync(directory, { recursive: true })
      fs.writeFileSync(path.join(directory, '.amadeus-extension-install.json'), JSON.stringify({ installId }), { mode: 0o600, flag: 'wx' })
      this.preparing.add(installId)
      let committed = false
      let retainFiles = false
      let rollback: { previous: StoredExtension; target: StoredExtension } | undefined
      try {
        // Copy before inspection: pip consumes the same immutable wheel bytes
        // inspected by the trusted helper even if the selected source changes.
        const copied = path.join(directory, path.basename(wheel))
        fs.copyFileSync(wheel, copied, fs.constants.COPYFILE_EXCL)
        delivery?.onStage?.('verifying')
        const inspection = await this.helper.call(delivery && delivery.catalog !== false ? 'inspect-catalog-wheel' : 'inspect-wheel', [copied])
        const manifest = inspection.manifest!
        if (!manifest || !inspection.digest || !inspection.package_root || !safeRelative(inspection.package_root)) {
          throw new Error('The wheel inspector returned incomplete installation metadata')
        }
        const identity = { id: manifest.id, version: manifest.version, digest: inspection.digest }
        const assertAllowed = () => {
          this.assertInstallIdle()
          this.store.assertExtensionAllowed(identity)
          if (delivery && (identity.id !== delivery.identity.id || identity.version !== delivery.identity.version || identity.digest !== delivery.identity.digest)) throw new Error('Prepared artifact identity or digest differs from the selected bytes')
          delivery?.assertCurrent()
        }
        assertAllowed()
        if (!['offline', 'resolve'].includes(inspection.dependency_mode!)) throw new Error('Dependency classification is unavailable')
        if (delivery && delivery.catalog !== false && inspection.dependency_mode !== 'offline') throw new Error('Third-party dependencies require local installation')
        await this.themes.validate(manifest)
        let sdkWheel = ''
        if (manifest.runtime.kind === 'python') {
          const bundledSdk = bundledExtensionSdk(this.projectRoot, this.sdkDirectory)
          fs.copyFileSync(bundledSdk, path.join(directory, path.basename(bundledSdk)), fs.constants.COPYFILE_EXCL)
          sdkWheel = bundledExtensionSdk(this.projectRoot, directory)
        }
        const existing = this.store.storedExtensionRecords().find(record => record.id === manifest.id)
        if (this.store.extensionIsInstalled(manifest.id) && (!existing || existing.pendingRemoval || existing.version === manifest.version)) {
          throw new Error('This extension is already installed; select a different version to update, or finish its pending removal')
        }
        delivery?.onStage?.('confirming')
        if (!await this.confirm({ operation: existing ? 'update' : 'install', manifest, previousVersion: existing?.version, previousManifest: existing?.manifest,
          digest: inspection.digest, sourceFile: path.basename(wheel) })) {
          throw new ExtensionChangeCancelled()
        }
        assertAllowed()
        delivery?.onStage?.('preparing')
        const env = extensionInstallerEnvironment(this.environment)
        const venv = path.join(directory, 'venv')
        let purelib: string
        let sitePackages = ''
        if (manifest.runtime.kind === 'none') {
          purelib = path.join(directory, 'package')
          await this.helper.call('prepare-data-wheel', [copied, '--destination', purelib])
        } else {
          await this.run(this.python(), ['-I', '-m', 'venv', '--copies', venv], { cwd: directory, env })
          const privatePython = path.join(venv, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
          // Resolve stdlib-owned paths before any third-party wheel is installed.
          purelib = (await this.run(privatePython, ['-I', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'], { cwd: directory, env })).trim()
          assertExtensionPath(directory, purelib)
          sitePackages = path.relative(venv, purelib).split(path.sep).join('/')
          if (!/^(Lib\/site-packages|lib\/python\d+\.\d+\/site-packages)$/.test(sitePackages)) throw new Error('Unsupported private Python environment layout')
          // Transport settings are supported; a configured root/prefix/target
          // must not redirect this transaction outside its private environment.
          const pipConfigurationText = await this.run(privatePython, ['-I', '-c',
            'import json; from pip._internal.configuration import Configuration; c=Configuration(isolated=False); c.load(); print(json.dumps(dict(c.items())))'], { cwd: directory, env })
          let pipConfiguration: Record<string, unknown>
          try { pipConfiguration = JSON.parse(pipConfigurationText) as Record<string, unknown> }
          catch { throw new Error('The trusted pip configuration could not be inspected') }
          if (!pipConfiguration || typeof pipConfiguration !== 'object' || Array.isArray(pipConfiguration)
            || Object.keys(pipConfiguration).some(key => /\.(root|prefix|target|user)$/.test(key))) {
            throw new Error('Configured pip installation path overrides are unsupported; use index, proxy, and certificate transport settings')
          }
          await this.run(privatePython, ['-I', '-m', 'pip', 'install', '--target', purelib, '--no-user', '--no-index', '--no-deps', '--only-binary=:all:', sdkWheel], { cwd: directory, env })
          const constraints = path.join(directory, 'constraints.txt')
          fs.writeFileSync(constraints, `amadeus-extension-sdk @ ${pathToFileURL(sdkWheel).href}\n`, { mode: 0o600 })
          await this.run(privatePython, ['-I', '-m', 'pip', 'install', '--target', purelib, '--no-user', '--only-binary=:all:', '--constraint', constraints, ...(inspection.dependency_mode === 'offline' ? ['--no-index', '--no-deps'] : []), copied], { cwd: directory, env })
        }
        const packagePath = path.join(purelib, inspection.package_root)
        assertExtensionPath(directory, packagePath)
        if (!fs.lstatSync(packagePath).isDirectory()) throw new Error('The installed extension package directory is missing')
        if (!inspection.manifest_member || !safeRelative(inspection.manifest_member)) throw new Error('The installed extension manifest location is missing')
        const installedManifestPath = path.join(purelib, inspection.manifest_member)
        assertExtensionPath(directory, installedManifestPath)
        const installedManifest = JSON.parse(fs.readFileSync(installedManifestPath, 'utf8')) as unknown
        if (JSON.stringify(installedManifest) !== JSON.stringify(manifest)) throw new Error('The installed extension manifest differs from the inspected wheel')
        await this.helper.call('validate-installed', [packagePath], { manifest })
        await this.themes.validate(manifest, packagePath)
        if (manifest.icon) readExtensionImage(packagePath, manifest.icon, { label: 'Package icon', maxMiB: 1, maxDimension: 1024 })
        // A replacement is prepared beside the running version. Only a complete
        // validated installation replaces desired state; old code is collected
        // after confirmed backend stop, using the existing ownership markers.
        const retainedValues = Object.fromEntries(Object.entries(existing?.values || {})
          .filter(([key]) => manifest.config?.[key] && !manifest.config[key].secret))
        const retainedSecrets = Object.fromEntries(Object.entries(existing?.encryptedSecrets || {})
          .filter(([key]) => manifest.config?.[key]?.secret))
        const checked = await this.helper.call('validate-config', [], { manifest, config: retainedValues, allow_incomplete: true })
        const record: StoredExtension = { id: manifest.id, version: manifest.version, digest: inspection.digest,
          installId, packageRoot: inspection.package_root, sitePackages, manifest, enabled: existing?.enabled || false, pendingRemoval: false,
          deleteDataOnRemoval: false,
          values: checked.config || {}, encryptedSecrets: retainedSecrets, providers: existing?.providers || [] }
        if (delivery && existing) {
          record.revision = (existing.revision ?? 1) + 1
          rollback = { previous: structuredClone(existing), target: record }
          this.updates.write({ format: 'amadeus.extension-update/v1', owner: this.updates.owner, phase: 'pending', ...rollback })
        }
        if (existing && (extensionRequiresRestart(existing.manifest) || extensionRequiresRestart(manifest))) this.backendReferences.add(existing.installId)
        assertAllowed()
        this.store.saveExtension(this.environment, record)
        committed = true
        await this.applyFrontendChanges()
        this.changed()
        return this.store.snapshot(this.environment)
      } catch (error) {
        if (rollback) {
          try {
            this.store.restoreExtension(rollback.previous, installId)
            this.updates.clear()
            committed = false
          } catch {
            this.updateRecoveryRequired = true
            retainFiles = true
            try { this.updates.write({ format: 'amadeus.extension-update/v1', owner: this.updates.owner, phase: 'recovery_required', ...rollback }) }
            catch { /* The previous durable journal still blocks a new process; this owner remains fenced in memory. */ }
            this.store.reportExtensionWarning('Plugin update failed and rollback persistence needs recovery. Both package versions and encrypted credentials were retained.')
          }
        }
        const shutdownUnconfirmed = error instanceof ExtensionPreparationShutdownError
        retainFiles ||= shutdownUnconfirmed
        if (shutdownUnconfirmed) {
          this.uncertainPreparations.add(installId)
          this.store.reportExtensionWarning('Extension preparation shutdown could not be confirmed; staging files were retained.')
        }
        throw error
      } finally {
        this.preparing.delete(installId)
        // Persistence may have committed the backup before a failed primary
        // write. Retain that environment if recovery can read its record.
        if (!committed && !retainFiles && !this.store.extensionInstallationReferences().has(installId)) {
          removeExtensionDirectory(this.root, directory, installId)
        }
      }
    })
  }

  update(raw: ExtensionUpdate): Promise<Record<string, unknown>> {
    return this.serialize(async () => {
      const record = this.store.extensionRecords().find(item => item.id === raw?.id)
      if (!record || record.pendingRemoval) throw new Error('Extension was not found or is pending removal')
      const pending = this.updates.read()
      if (pending?.target.id === raw?.id) {
        const unchangedConfig = Object.keys(raw.values || {}).length === 0 && Object.keys(raw.secrets || {}).length === 0
          && (raw.providers === undefined || JSON.stringify(raw.providers) === JSON.stringify(record.providers))
        if (raw.enabled !== false || !unchangedConfig || Object.keys(raw).some(key => !['id', 'enabled', 'values', 'secrets', 'providers'].includes(key))) throw new Error('Confirm or roll back this plugin update before changing its configuration.')
        // Revocation remains available even during recovery. Persist it in the
        // rollback record first so recovery can never re-enable the package.
        this.updates.write({ ...pending, phase: 'recovery_required', previous: { ...pending.previous, enabled: false },
          target: { ...record, enabled: false, revision: (record.revision ?? 1) + 1 } })
        this.store.saveExtension(this.environment, { ...record, enabled: false })
        await this.applyFrontendChanges()
        this.changed()
        return this.store.snapshot(this.environment)
      }
      if (raw.enabled !== undefined && typeof raw.enabled !== 'boolean') throw new Error('Invalid extension enabled value')
      const providers = raw.providers === undefined ? record.providers : raw.providers
      if (!Array.isArray(providers) || providers.length > 32 || providers.some(id => typeof id !== 'string' || !/^[a-z][a-z0-9_-]{0,63}$/.test(id))) throw new Error('Invalid extension Work Provider binding')
      const enabled = raw.enabled ?? record.enabled
      if (enabled) this.store.assertExtensionAllowed(record)
      if (enabled && !record.enabled && !await this.confirm({ operation: 'enable', manifest: record.manifest, digest: record.digest })) {
        throw new ExtensionChangeCancelled()
      }
      if (enabled && extensionNeedsAgent(record.manifest) && !providers.length) throw new Error('Select a compatible Work Provider before enabling this extension')
      const values = { ...record.values }
      for (const [key, value] of Object.entries(raw.values || {})) {
        if (record.manifest.config?.[key]?.secret) throw new Error('Extension secrets must use the credential field')
        if (value === null) delete values[key]
        else values[key] = value
      }
      const changedSecrets = raw.secrets || {}
      const secrets = enabled ? this.store.extensionSecrets(record, new Set(Object.keys(changedSecrets))) : {}
      for (const [key, value] of Object.entries(changedSecrets)) {
        if (!record.manifest.config?.[key]?.secret) throw new Error('Unknown extension secret field')
        if (value === null) delete secrets[key]
        else if (typeof value === 'string' && value.length > 0) secrets[key] = value
        else throw new Error('Invalid extension secret value')
      }
      // Values and secrets enter only the trusted validator via stdin, never
      // command arguments, environment variables, or plaintext temporary files.
      const validated = await this.helper.call('validate-config', [], { manifest: record.manifest, config: { ...values, ...secrets }, allow_incomplete: !enabled })
      if (enabled) await this.themes.validate(record.manifest, this.packagePath(record))
      if (enabled) this.store.assertExtensionAllowed(record)
      this.store.saveExtension(this.environment, { ...record, values: validated.config || {}, enabled,
        providers: [...new Set(providers)] }, changedSecrets)
      await this.applyFrontendChanges()
      this.changed()
      return this.store.snapshot(this.environment)
    })
  }

  remove(id: string, options: ExtensionRemovalOptions = {}): Promise<Record<string, unknown>> {
    return this.serialize(async () => {
      if (this.updates.read()?.target.id === id) throw new Error('Confirm or roll back this plugin update before removal.')
      if (!options || typeof options !== 'object' || Array.isArray(options)
        || Object.keys(options).some(key => key !== 'deleteData')
        || (options.deleteData !== undefined && typeof options.deleteData !== 'boolean')) throw new Error('Invalid extension data removal choice')
      this.store.requestExtensionRemoval(this.environment, id, options.deleteData === true)
      await this.applyFrontendChanges()
      this.changed()
      return this.store.snapshot(this.environment)
    })
  }

  /** Called only after confirmed backend shutdown, before starting a new one. */
  cleanupStoppedBackend(): Promise<void> {
    return this.serialize(async () => {
      this.backendReferences.clear()
      this.cleanupReady = true
      this.store.releaseBackendExtensionApplications()
      const records = this.store.storedExtensionRecords()
      for (const record of records.filter(item => item.pendingRemoval)) {
        try {
          this.store.releaseExtensionApplication(record.id)
          removeExtensionDirectory(this.root, path.join(this.root, record.installId), record.installId)
          if (record.deleteDataOnRemoval) this.removePackageData(record.id)
          this.store.finishExtensionRemoval(record.id, record.installId)
        } catch {
          this.store.reportExtensionWarning('Extension cleanup did not complete; ownership records were retained for retry.')
        }
      }
      await this.applyFrontendChanges()
      this.changed()
    })
  }

  private collectUnusedInstallations(releasedFrontend: ReadonlySet<string> = new Set()): void {
      // Before the first confirmed stop, discovered old directories may still
      // belong to a backend from a prior desktop lifetime.
      if (!this.cleanupReady && !releasedFrontend.size) return
      if (!fs.existsSync(this.root)) return
      try {
        const rootStat = fs.lstatSync(this.root)
        if (rootStat.isSymbolicLink() || !rootStat.isDirectory()) throw new Error('invalid root')
      } catch {
        this.store.reportExtensionWarning('Extension cleanup could not verify the installation root; files were retained.')
        return
      }
      const retained = this.store.extensionInstallationReferences()
      const update = this.updates.read()
      if (update) { retained.add(update.previous.installId); retained.add(update.target.installId) }
      for (const applied of this.store.appliedExtensionRecords()) retained.add(applied.installId)
      for (const installId of this.backendReferences) retained.add(installId)
      for (const entry of fs.readdirSync(this.root)) {
        if (!this.cleanupReady && !releasedFrontend.has(entry)) continue
        if (!/^[a-f0-9]{32}$/.test(entry) || retained.has(entry) || this.preparing.has(entry) || this.uncertainPreparations.has(entry)) continue
        // A marker proves installation ownership; directory discovery cannot
        // create records, enable packages, or infer trust.
        try { removeExtensionDirectory(this.root, path.join(this.root, entry), entry) }
        catch { this.store.reportExtensionWarning('Extension orphan cleanup could not verify installation ownership; its files were retained.') }
      }
  }

  refreshValidation(): Promise<void> {
    return this.serialize(async () => { await this.helper.refreshRecords(this.store.storedExtensionRecords()); await this.applyFrontendChanges() })
  }

  launchSnapshot(): Promise<string> { return this.serialize(async () => {
    await this.helper.refreshRecords(this.store.storedExtensionRecords())
    return this.buildLaunchSnapshot()
  }) }

  prepareLaunch(): Promise<{ extensions: string; environment: NodeJS.ProcessEnv; pendingRevisions: Record<string, number>; records: StoredExtension[] }> {
    return this.serialize(async () => {
      this.assertUpdateCanLaunch()
      // A fresh process cannot inherit a receipt from the previous process.
      // This retires logical state only; cleanup still requires confirmed stop.
      this.store.releaseBackendExtensionApplications()
      await this.helper.refreshRecords(this.store.storedExtensionRecords())
      await this.applyFrontendChanges()
      for (const record of this.store.extensionRecords()) if (record.enabled && !record.pendingRemoval && extensionRequiresRestart(record.manifest)) this.backendReferences.add(record.installId)
      const incomplete = this.store.deferIncompleteExtensionApplication()
      const pendingRevisions = this.store.pendingRevisionSnapshot()
      // Failed/retained removals have not completed the requested lifecycle,
      // even though their code is absent from this new backend snapshot.
      if (incomplete) delete pendingRevisions[EXTENSIONS_ENV]
      const environment = this.store.backendEnvironment(this.environment)
      return { extensions: await this.buildLaunchSnapshot(), environment, pendingRevisions, records: this.store.extensionRecords() }
    })
  }

  acceptLaunch(launch: { records: StoredExtension[]; extensions: string }, statuses: unknown): Promise<boolean> {
    return this.serialize(async () => {
      let complete = true
      const receipts = new Map<string, { revision: number; startup: 'accepted' | 'rejected' }>()
      const seen = new Set<string>()
      for (const row of Array.isArray(statuses) ? statuses : []) {
        if (!row || typeof row !== 'object' || typeof row.id !== 'string') continue
        if (seen.has(row.id)) { receipts.delete(row.id); continue }
        seen.add(row.id)
        if (Number.isSafeInteger(row.revision) && row.revision > 0 && ['accepted', 'rejected'].includes(row.startup)) receipts.set(row.id, row)
      }
      const sent = JSON.parse(launch.extensions) as Array<{ id: string; revision: number; startup_error?: string }>
      this.observeUpdateHealth(statuses)
      const blocked = new Map(sent.filter(item => item.startup_error).map(item => [item.id, item.revision]))
      for (const record of launch.records.filter(item => extensionRequiresRestart(item.manifest) && !item.pendingRemoval)) {
        // Host-preflight rejection is a handled revision, not a loaded receipt.
        if (blocked.get(record.id) === (record.revision ?? 1)) continue
        const receipt = receipts.get(record.id)
        if (!record.enabled) this.store.acceptExtensionApplication(record)
        else if (receipt?.revision !== (record.revision ?? 1)) complete = false
        else if (receipt.startup === 'accepted') this.store.acceptExtensionApplication(record)
        // Explicit rejection settles pending work without creating a loaded receipt.
        // Missing/invalid/ambiguous status above never implies a rejection.
      }
      await this.applyFrontendChanges()
      this.changed()
      return complete
    })
  }

  icon(id: string): string | null {
    const record = this.store.extensionRecords().find(item => item.id === id)
    if (!record?.manifest.icon) return null
    try {
      const image = readExtensionImage(this.packagePath(record), record.manifest.icon,
        { label: 'Package icon', maxMiB: 1, maxDimension: 1024 })
      return `data:${image.mime};base64,${image.base64}`
    } catch {
      // Cosmetic resources do not grant authority or block the package card.
      console.warn('[extensions] Package icon unavailable:', record.id)
      return null
    }
  }

  private packagePath(record: StoredExtension): string {
    const directory = path.join(this.root, record.installId)
    const packagePath = record.sitePackages === '' ? path.join(directory, 'package', record.packageRoot)
      : path.join(directory, 'venv', record.sitePackages, record.packageRoot)
    assertExtensionPath(this.root, packagePath)
    return packagePath
  }

  private async applyFrontendChanges(): Promise<void> {
    const released = new Set<string>()
    const valid = new Set(this.store.extensionRecords().map(record => record.id))
    for (const record of this.store.storedExtensionRecords()) {
      if (this.store.extensionChangeNeedsRestart(record)) continue
      if (record.pendingRemoval) {
        this.store.releaseExtensionApplication(record.id)
        try {
          removeExtensionDirectory(this.root, path.join(this.root, record.installId), record.installId)
          if (record.deleteDataOnRemoval) this.removePackageData(record.id)
          this.store.finishExtensionRemoval(record.id, record.installId)
        } catch { this.store.reportExtensionApplicationError(record.id, 'Extension cleanup is incomplete; ownership records were retained for retry.') }
        continue
      }
      if (!valid.has(record.id) || this.store.extensionSecurityBlock(record)) continue
      const applied = this.store.appliedExtensionRecords().find(item => item.id === record.id)
      if (applied?.revision === record.revision) continue
      try {
        if (record.enabled) {
          const packagePath = this.packagePath(record)
          await this.helper.call('validate-installed', [packagePath], { manifest: record.manifest })
          await this.themes.validate(record.manifest, packagePath)
          this.packageData(record.id)
          this.store.assertExtensionAllowed(record)
        }
        this.store.acceptExtensionApplication(record)
        if (applied && applied.installId !== record.installId) released.add(applied.installId)
        this.changed()
      } catch (error) {
        this.store.reportExtensionApplicationError(record.id, error instanceof Error ? error.message : 'Theme application failed')
      }
    }
    this.collectUnusedInstallations(released)
  }

  restoreBuiltinTheme(): Promise<void> {
    return this.serialize(async () => {
      this.store.update(this.environment, { values: { AMADEUS_UI_THEME: 'classic' } })
      this.store.rememberThemeBase('classic')
      this.changed()
    })
  }

  themeState(): Promise<DesktopThemeState> {
    return this.serialize(async () => {
      const selected = String(startupValue('AMADEUS_UI_THEME', this.store.snapshot(this.environment), 'wallpaper-slice'))
      if (selected === 'classic' || selected === 'wallpaper-slice') { this.store.rememberThemeBase(selected); return { selected, base: selected, reason: '' } }
      const fallback = (reason: string): DesktopThemeState => ({ selected, base: this.store.verifiedThemeBase(), reason })
      const [packageId] = selected.split('/')
      const policyState = this.store.extensionSecurityState()
      if (securityBlock(policyState)) return fallback(securityBlock(policyState))
      const desired = this.store.extensionRecords().find(record => record.id === packageId)
      if (!desired) return fallback('The saved theme is unavailable. Its last verified base is shown.')
      if (!desired.enabled || desired.pendingRemoval) return fallback('The saved theme is disabled or being removed. Its base is shown.')
      const applied = this.store.appliedExtensionRecords().find(record => record.id === desired.id && record.enabled)
      const policyError = securityBlock(policyState, desired) || (applied ? securityBlock(policyState, applied) : '')
      if (policyError) return fallback(policyError)
      if (this.store.extensionFrontendClosed(desired.id)) return fallback('Frontend access is closed until the updated package is applied after restart.')
      if (!applied) return fallback('This theme is waiting for its package to be applied.')
      const item = (applied.manifest.contributes.theme || []).find(theme => selected === `${applied.id}/${theme.id}`)
      if (!item) {
        const pendingTheme = applied.revision !== desired.revision && this.store.extensionChangeNeedsRestart(desired)
          && (desired.manifest.contributes.theme || []).some(theme => selected === `${desired.id}/${theme.id}`)
        return fallback(pendingTheme
          ? 'This theme is in the saved update. Restart the backend to apply it.'
          : 'The selected theme is no longer available in this package. Choose another theme in Appearance.')
      }
      try {
        const palettes = await this.themes.validate(applied.manifest, this.packagePath(applied), item.id)
        const palette = palettes[item.id]
        this.store.rememberThemeBase(palette.base)
        return { selected, base: palette.base, reason: applied.revision === desired.revision ? '' : 'Saved changes are pending restart; the loaded theme is unchanged.', theme: { ...palette, id: selected, version: applied.version, revision: applied.revision ?? 1 } }
      } catch (error) { return fallback(error instanceof Error ? error.message : 'Theme resources are unavailable') }
    })
  }

  private async buildLaunchSnapshot(): Promise<string> {
    const snapshot: Array<Record<string, unknown>> = []
    for (const record of this.store.extensionRecords().filter(item => item.enabled && !item.pendingRemoval && extensionRequiresRestart(item.manifest))) {
      const directory = path.join(this.root, record.installId)
      const packagePath = record.sitePackages === '' ? path.join(directory, 'package', record.packageRoot)
        : path.join(directory, 'venv', record.sitePackages, record.packageRoot)
      const privatePython = record.sitePackages === '' ? ''
        : path.join(directory, 'venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
      const dataPath = this.dataPath(record.id)
      let secrets: Record<string, string> = {}
      let config: Record<string, ExtensionConfigValue> = { ...record.values }
      let startupError = ''
      try {
        this.store.assertExtensionAllowed(record)
        assertExtensionPath(this.root, packagePath)
        if (privatePython) assertExtensionPath(this.root, privatePython)
        if (!fs.existsSync(packagePath) || (privatePython && !fs.existsSync(privatePython))) throw new Error('missing installation')
        secrets = this.store.extensionSecrets(record)
        const checked = await this.helper.call('validate-config', [], { manifest: record.manifest, config: { ...record.values, ...secrets }, allow_incomplete: false })
        config = checked.config || {}
        this.packageData(record.id)
        this.store.assertExtensionAllowed(record)
      } catch {
        // Saved enable intent remains visible. Python retains the contribution
        // in its unavailable catalog and never executes a startup-error entry.
        startupError = this.store.extensionSecurityBlock(record) || 'Extension installation, credentials, configuration, or package data could not be validated'
        secrets = {}
        this.store.reportExtensionWarning(startupError)
      }
      snapshot.push({ id: record.id, version: record.version, revision: record.revision ?? 1, digest: record.digest, manifest: record.manifest,
        package_path: packagePath, python_path: privatePython, data_path: dataPath, enabled: true,
        config, secrets, providers: [...record.providers], ...(startupError ? { startup_error: startupError } : {}) })
    }
    return JSON.stringify(snapshot)
  }
}
