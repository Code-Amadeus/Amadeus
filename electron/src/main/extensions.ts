import fs from 'node:fs'
import path from 'node:path'
import { randomBytes, createHash } from 'node:crypto'
import { spawn, spawnSync } from 'node:child_process'
import { pathToFileURL } from 'node:url'
import { forceStopBackendTree } from './backendProcessTree.js'
import { DesktopSettingsStore, EXTENSIONS_ENV, type StoredExtension } from './desktopSettings.js'
import type { ExtensionConfigValue, ExtensionManifest, ExtensionRemovalOptions, ExtensionUpdate } from '../shared/extensions.js'

type HelperReply = { ok: boolean; error?: string; manifest?: ExtensionManifest; digest?: string;
  package_root?: string; manifest_member?: string; distribution?: string; config?: Record<string, ExtensionConfigValue>; missing?: string[] }
type RunOptions = { cwd: string; env: NodeJS.ProcessEnv; input?: string; allowFailureJson?: boolean }
export type ExtensionCommandRunner = (command: string, args: string[], options: RunOptions) => Promise<string>

export class ExtensionPreparationShutdownError extends Error {
  constructor() { super('Extension preparation shutdown could not be confirmed; its files were retained') }
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
  const timeout = setTimeout(() => { timedOut = true; terminate() }, 300_000)
  child.stdout?.on('data', chunk => {
    size += chunk.length
    if (size > 1_048_576) { overflow = true; terminate(); return }
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
    const input = JSON.stringify({ manifest, config, allow_incomplete: true })
    const key = createHash('sha256').update(input).digest('hex')
    if (this.validRecords.has(key)) return this.validRecords.get(key)!
    if (Date.now() < this.unavailableUntil) return 'unavailable'
    let valid: boolean | 'unavailable' = 'unavailable'
    try {
      const result = spawnSync(this.python(), ['-I', this.script, 'validate-config'], {
        cwd: this.projectRoot, env: extensionInstallerEnvironment(this.environment), input,
        encoding: 'utf8', windowsHide: true, timeout: 10_000, maxBuffer: 1_048_576,
      })
      const reply = JSON.parse(result.stdout) as { ok?: unknown }
      if (!result.error && result.status === 0 && reply.ok === true) valid = true
      else if (!result.error && result.status !== null && reply.ok === false) valid = false
    } catch { /* Validator failure is not evidence that saved records are invalid. */ }
    if (valid === 'unavailable') { this.unavailableUntil = Date.now() + 1000; return valid }
    if (this.validRecords.size >= 256) this.validRecords.clear()
    this.validRecords.set(key, valid)
    return valid
  }

  async call(command: 'inspect-wheel' | 'validate-config' | 'validate-installed', args: string[] = [], input?: unknown): Promise<HelperReply> {
    const output = await this.run(this.python(), ['-I', this.script, command, ...args], {
      cwd: this.projectRoot, env: extensionInstallerEnvironment(this.environment),
      allowFailureJson: true,
      ...(input === undefined ? {} : { input: JSON.stringify(input) }),
    })
    let reply: HelperReply
    try { reply = JSON.parse(output) as HelperReply }
    catch { throw new Error('The trusted extension validator returned an invalid response') }
    if (!reply || reply.ok !== true) throw new Error(reply?.error || 'Extension validation failed')
    return reply
  }
}

function safeRelative(value: string): boolean {
  return /^[A-Za-z0-9_][A-Za-z0-9._-]*(\/[A-Za-z0-9_][A-Za-z0-9._-]*)*$/.test(value)
}

/** Verify every ancestor and never traverse a symbolic link or Windows junction. */
export function assertExtensionPath(root: string, target: string): void {
  const absoluteRoot = path.resolve(root)
  const absoluteTarget = path.resolve(target)
  const relative = path.relative(absoluteRoot, absoluteTarget)
  if (!relative || relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error('Extension filesystem target is outside its installation directory')
  }
  const rootParts = path.parse(absoluteRoot)
  let current = rootParts.root
  for (const segment of absoluteTarget.slice(rootParts.root.length).split(path.sep)) {
    current = path.join(current, segment)
    if (!fs.existsSync(current)) continue
    if (fs.lstatSync(current).isSymbolicLink()) throw new Error('Extension filesystem paths must not contain links or junctions')
  }
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
  removeVerifiedDirectory(root, target)
}

function removeVerifiedDirectory(root: string, target: string): void {
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
  // Do not use recursive rm: traversal and deletion both recheck each boundary.
  function remove(directory: string): void {
    assertExtensionPath(root, directory)
    for (const name of fs.readdirSync(directory)) {
      const child = path.join(directory, name)
      assertExtensionPath(root, child)
      const stat = fs.lstatSync(child)
      if (stat.isDirectory()) remove(child)
      else if (stat.isFile()) fs.unlinkSync(child)
      else throw new Error('Extension cleanup encountered an unsupported filesystem entry')
    }
    fs.rmdirSync(directory)
  }
  remove(target)
}

export class ExtensionInstaller {
  private mutation: Promise<unknown> = Promise.resolve()
  private preparing = new Set<string>()
  private uncertainPreparations = new Set<string>()
  constructor(private readonly root: string, private readonly projectRoot: string, private readonly store: DesktopSettingsStore,
    private readonly helper: ExtensionPackageHelper, private readonly python: () => string,
    private readonly environment: NodeJS.ProcessEnv, private readonly run: ExtensionCommandRunner = runExtensionCommand) {}

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
    removeVerifiedDirectory(this.dataRoot, directory)
  }

  private serialize<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.mutation.then(operation)
    this.mutation = result.catch(() => {})
    return result
  }

  install(wheel: string): Promise<Record<string, unknown>> {
    return this.serialize(async () => {
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
      try {
        // Copy before inspection: pip consumes the same immutable wheel bytes
        // inspected by the trusted helper even if the selected source changes.
        const copied = path.join(directory, path.basename(wheel))
        fs.copyFileSync(wheel, copied, fs.constants.COPYFILE_EXCL)
        const inspection = await this.helper.call('inspect-wheel', [copied])
        const manifest = inspection.manifest!
        if (!manifest || !inspection.digest || !inspection.package_root || !safeRelative(inspection.package_root)) {
          throw new Error('The wheel inspector returned incomplete installation metadata')
        }
        const existing = this.store.extensionRecords().find(record => record.id === manifest.id)
        if (this.store.extensionIsInstalled(manifest.id) && (!existing || existing.pendingRemoval || existing.version === manifest.version)) {
          throw new Error('This extension is already installed; select a different version to update, or finish its pending removal')
        }
        const env = extensionInstallerEnvironment(this.environment)
        const venv = path.join(directory, 'venv')
        await this.run(this.python(), ['-I', '-m', 'venv', '--copies', venv], { cwd: directory, env })
        const privatePython = path.join(venv, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
        // Resolve stdlib-owned paths before any third-party wheel is installed.
        const purelib = (await this.run(privatePython, ['-I', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'], { cwd: directory, env })).trim()
        assertExtensionPath(directory, purelib)
        const sitePackages = path.relative(venv, purelib).split(path.sep).join('/')
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
        const sdkWheel = path.join(this.projectRoot, 'build', 'extension-sdk', 'amadeus_extension_sdk-0.1.0-py3-none-any.whl')
        if (!fs.existsSync(sdkWheel)) throw new Error('The bundled extension SDK wheel is unavailable')
        await this.run(privatePython, ['-I', '-m', 'pip', 'install', '--target', purelib, '--no-user', '--no-deps', '--only-binary=:all:', sdkWheel], { cwd: directory, env })
        const constraints = path.join(directory, 'constraints.txt')
        fs.writeFileSync(constraints, `amadeus-extension-sdk @ ${pathToFileURL(sdkWheel).href}\n`, { mode: 0o600 })
        await this.run(privatePython, ['-I', '-m', 'pip', 'install', '--target', purelib, '--no-user', '--only-binary=:all:', '--constraint', constraints, copied], { cwd: directory, env })
        const packagePath = path.join(purelib, inspection.package_root)
        assertExtensionPath(directory, packagePath)
        if (!fs.lstatSync(packagePath).isDirectory()) throw new Error('The installed extension package directory is missing')
        if (!inspection.manifest_member || !safeRelative(inspection.manifest_member)) throw new Error('The installed extension manifest location is missing')
        const installedManifestPath = path.join(purelib, inspection.manifest_member)
        assertExtensionPath(directory, installedManifestPath)
        const installedManifest = JSON.parse(fs.readFileSync(installedManifestPath, 'utf8')) as unknown
        if (JSON.stringify(installedManifest) !== JSON.stringify(manifest)) throw new Error('The installed extension manifest differs from the inspected wheel')
        await this.helper.call('validate-installed', [packagePath], { manifest })
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
        const snapshot = this.store.saveExtension(this.environment, record)
        committed = true
        return snapshot
      } catch (error) {
        retainFiles = error instanceof ExtensionPreparationShutdownError
        if (retainFiles) {
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
      if (raw.enabled !== undefined && typeof raw.enabled !== 'boolean') throw new Error('Invalid extension enabled value')
      const providers = raw.providers === undefined ? record.providers : raw.providers
      if (!Array.isArray(providers) || providers.length > 32 || providers.some(id => typeof id !== 'string' || !/^[a-z][a-z0-9_-]{0,63}$/.test(id))) throw new Error('Invalid extension Work Provider binding')
      const enabled = raw.enabled ?? record.enabled
      if (enabled && (record.manifest.contributes.mcp_server?.length || record.manifest.contributes.skill?.length) && !providers.length) throw new Error('Select a compatible Work Provider before enabling this extension')
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
      return this.store.saveExtension(this.environment, { ...record, values: validated.config || {}, enabled,
        providers: [...new Set(providers)] }, changedSecrets)
    })
  }

  remove(id: string, options: ExtensionRemovalOptions = {}): Promise<Record<string, unknown>> {
    return this.serialize(async () => {
      if (!options || typeof options !== 'object' || Array.isArray(options)
        || Object.keys(options).some(key => key !== 'deleteData')
        || (options.deleteData !== undefined && typeof options.deleteData !== 'boolean')) throw new Error('Invalid extension data removal choice')
      return this.store.requestExtensionRemoval(this.environment, id, options.deleteData === true)
    })
  }

  /** Called only after confirmed backend shutdown, before starting a new one. */
  cleanupStoppedBackend(): Promise<void> {
    return this.serialize(async () => {
      const records = this.store.extensionRecords()
      for (const record of records.filter(item => item.pendingRemoval)) {
        try {
          removeExtensionDirectory(this.root, path.join(this.root, record.installId), record.installId)
          if (record.deleteDataOnRemoval) this.removePackageData(record.id)
          this.store.finishExtensionRemoval(record.id, record.installId)
        } catch {
          this.store.reportExtensionWarning('Extension cleanup could not verify a pending removal; its files were retained.')
        }
      }
      if (!fs.existsSync(this.root)) return
      try {
        assertExtensionPath(path.dirname(this.root), this.root)
        if (!fs.lstatSync(this.root).isDirectory()) throw new Error('invalid root')
      } catch {
        this.store.reportExtensionWarning('Extension cleanup could not verify the installation root; files were retained.')
        return
      }
      const retained = this.store.extensionInstallationReferences()
      for (const entry of fs.readdirSync(this.root)) {
        if (!/^[a-f0-9]{32}$/.test(entry) || retained.has(entry) || this.preparing.has(entry) || this.uncertainPreparations.has(entry)) continue
        // A marker proves installation ownership; directory discovery cannot
        // create records, enable packages, or infer trust.
        try { removeExtensionDirectory(this.root, path.join(this.root, entry), entry) }
        catch { this.store.reportExtensionWarning('Extension orphan cleanup could not verify installation ownership; its files were retained.') }
      }
    })
  }

  launchSnapshot(): Promise<string> { return this.serialize(() => this.buildLaunchSnapshot()) }

  prepareLaunch(): Promise<{ extensions: string; environment: NodeJS.ProcessEnv; pendingRevisions: Record<string, number> }> {
    return this.serialize(async () => {
      const incomplete = this.store.deferIncompleteExtensionApplication()
      const pendingRevisions = this.store.pendingRevisionSnapshot()
      // Failed/retained removals have not completed the requested lifecycle,
      // even though their code is absent from this new backend snapshot.
      if (incomplete) delete pendingRevisions[EXTENSIONS_ENV]
      const environment = this.store.backendEnvironment(this.environment)
      return { extensions: await this.buildLaunchSnapshot(), environment, pendingRevisions }
    })
  }

  private async buildLaunchSnapshot(): Promise<string> {
    const snapshot: Array<Record<string, unknown>> = []
    for (const record of this.store.extensionRecords().filter(item => item.enabled && !item.pendingRemoval)) {
      const directory = path.join(this.root, record.installId)
      const packagePath = path.join(directory, 'venv', record.sitePackages, record.packageRoot)
      const privatePython = path.join(directory, 'venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
      const dataPath = this.dataPath(record.id)
      let secrets: Record<string, string> = {}
      let config: Record<string, ExtensionConfigValue> = { ...record.values }
      let startupError = ''
      try {
        assertExtensionPath(this.root, packagePath)
        assertExtensionPath(this.root, privatePython)
        if (!fs.existsSync(packagePath) || !fs.existsSync(privatePython)) throw new Error('missing installation')
        secrets = this.store.extensionSecrets(record)
        const checked = await this.helper.call('validate-config', [], { manifest: record.manifest, config: { ...record.values, ...secrets }, allow_incomplete: false })
        config = checked.config || {}
        this.packageData(record.id)
      } catch {
        // Saved enable intent remains visible. Python retains the contribution
        // in its unavailable catalog and never executes a startup-error entry.
        startupError = 'Extension installation, credentials, configuration, or package data could not be validated'
        secrets = {}
        this.store.reportExtensionWarning(startupError)
      }
      snapshot.push({ id: record.id, version: record.version, digest: record.digest, manifest: record.manifest,
        package_path: packagePath, python_path: privatePython, data_path: dataPath, enabled: true,
        config, secrets, providers: [...record.providers], ...(startupError ? { startup_error: startupError } : {}) })
    }
    return JSON.stringify(snapshot)
  }
}
