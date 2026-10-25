import fs from 'node:fs'
import path from 'node:path'
import { createHash, randomBytes } from 'node:crypto'
import { assertExtensionPath } from './extensionPaths.js'
import { isDeepStrictEqual } from 'node:util'
import type { CatalogEntry, CatalogIndex, CatalogRequest, CatalogView, CatalogReply } from '../shared/extensionCatalog.js'
import { usesScheduledExtensionUpdates } from '../shared/extensionCatalog.js'
import { DesktopSettingsStore } from './desktopSettings.js'
import { ExtensionInstaller, ExtensionPackageHelper, ExtensionChangeCancelled, removeExtensionDirectory } from './extensions.js'
import { artifactUrl, downloadExtension, httpsUrl, MAX_INDEX_BYTES, MAX_WHEEL_BYTES, stagingDirectory, type Download } from './extensionDownloads.js'
import { securityBlock, securitySnapshot } from './extensionSecurity.js'

export const OFFICIAL_EXTENSION_INDEX = 'https://raw.githubusercontent.com/Code-Amadeus/Amadeus-Extensions/main/catalog/v1/index.json'
type LocalSelection = { directory: string; file: string; id: string; version: string; digest: string }
type UpdatePreferences = { enabled: boolean; attemptedAt: number; notified: string; pending?: { id: string; version: string; digest: string }; local?: LocalSelection; rollback?: boolean }
export function catalogRequest(raw: unknown): CatalogRequest {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('Invalid catalog request')
  const r = raw as CatalogRequest
  const selected = r.action === 'install' || r.action === 'schedule' || r.action === 'homepage' || r.action === 'repository'
  if (!['get', 'status', 'refresh', 'recover', 'cancel', 'install', 'schedule', 'auto-check', 'cancel-scheduled', 'rollback', 'confirm-update', 'health', 'homepage', 'repository'].includes(r.action)
    || Object.keys(r).some(k => !(selected ? ['action', 'id', 'version'] : r.action === 'auto-check' ? ['action', 'enabled'] : ['action']).includes(k))) throw new Error('Invalid catalog request fields')
  if (r.action === 'auto-check' && typeof r.enabled !== 'boolean') throw new Error('Invalid automatic-check preference')
  if (selected && (typeof r.id !== 'string' || !r.id || r.id.length > 128 || typeof r.version !== 'string' || !/^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(r.version))) throw new Error('Invalid catalog selection')
  return r
}
export class ExtensionCatalog {
  private trusted?: CatalogIndex
  private cached?: CatalogIndex
  private error = ''
  private refreshTask?: Promise<CatalogView>
  private refreshController?: AbortController
  private active?: AbortController
  private progress?: CatalogView['downloading']
  private timer?: ReturnType<typeof setInterval>
  constructor(private readonly root: string, private readonly store: DesktopSettingsStore, private readonly helper: ExtensionPackageHelper,
    private readonly installer: ExtensionInstaller, private readonly mirror: () => string, private readonly changed: () => void = () => {},
    private readonly download: Download = downloadExtension,
    private readonly notifyUpdates: (updates: Array<{ id: string; version: string }>) => void = () => {},
    private readonly platform: NodeJS.Platform = process.platform) {}
  async dispatch(raw: unknown, environment: NodeJS.ProcessEnv, openExternal: (url: string) => Promise<unknown>): Promise<CatalogReply> {
    try {
      const request = catalogRequest(raw)
      if (['schedule', 'rollback', 'confirm-update', 'health'].includes(request.action) && !usesScheduledExtensionUpdates(this.platform)) {
        throw new Error('Scheduled plugin updates are only available on Windows.')
      }
      if (request.action === 'status') return { ok: true, progress: this.status() }
      if (request.action === 'install') return { ok: true, settings: await this.install(request.id, request.version), catalog: await this.view() }
      if (request.action === 'schedule') {
        if (this.installer.pendingUpdate()) throw new Error('Confirm or roll back the previous plugin update first.')
        await this.refresh()
        const selected = this.entry(request.id, request.version)
        this.admitted(selected)
        const preferences = this.preferences()
        this.savePreferences({ ...preferences, pending: { id: selected.id, version: selected.version, digest: selected.sha256 }, local: undefined, rollback: false })
        if (preferences.local) this.removeLocal(preferences.local)
        return { ok: true, scheduled: true, catalog: await this.view() }
      }
      if (request.action === 'auto-check') this.savePreferences({ ...this.preferences(), enabled: request.enabled })
      if (request.action === 'cancel-scheduled') {
        const preferences = this.preferences()
        this.savePreferences({ ...preferences, pending: undefined, local: undefined, rollback: false })
        if (preferences.local) this.removeLocal(preferences.local)
      }
      if (request.action === 'rollback') {
        if (!this.installer.pendingUpdate()) throw new Error('No plugin update is waiting for rollback.')
        const preferences = this.preferences()
        this.savePreferences({ ...preferences, pending: undefined, local: undefined, rollback: true })
        if (preferences.local) this.removeLocal(preferences.local)
        return { ok: true, scheduled: true, catalog: await this.view() }
      }
      if (request.action === 'confirm-update') return { ok: true, settings: await this.installer.confirmPendingUpdate(), catalog: await this.view() }
      if (request.action === 'homepage' || request.action === 'repository') { await openExternal(this.homepage(request.id, request.version, request.action)); return { ok: true } }
      if (request.action === 'cancel') this.cancel()
      const catalog = request.action === 'refresh' || request.action === 'recover' ? await this.refresh(request.action === 'recover') : await this.view()
      return { ok: true, catalog, settings: this.store.snapshot(environment) }
    } catch (error) {
      return { ok: false, ...(error instanceof ExtensionChangeCancelled ? { cancelled: true } : {}), error: error instanceof Error ? error.message : 'Catalog operation failed', catalog: await this.view(), settings: this.store.snapshot(environment) }
    }
  }
  private get cacheFile(): string { return path.join(this.root, 'index.json') }
  private preferences(): UpdatePreferences {
    const file = path.join(this.root, 'update-preferences.json')
    assertExtensionPath(this.root, file)
    if (!fs.existsSync(file)) return { enabled: false, attemptedAt: 0, notified: '' }
    if (fs.statSync(file).size > 64 * 1024) throw new Error('Plugin update preferences are too large; automatic checking is blocked.')
    const raw = JSON.parse(fs.readFileSync(file, 'utf8')) as UpdatePreferences
    if (typeof raw.enabled !== 'boolean' || !Number.isFinite(raw.attemptedAt) || raw.attemptedAt < 0 || typeof raw.notified !== 'string'
      || raw.rollback !== undefined && typeof raw.rollback !== 'boolean') throw new Error('Plugin update preferences are invalid; automatic checking and scheduled installation are blocked.')
    if (raw.pending) {
      catalogRequest({ action: 'install', id: raw.pending.id, version: raw.pending.version })
      if (!/^[a-f0-9]{64}$/.test(raw.pending.digest)) throw new Error('Scheduled plugin digest is invalid.')
    }
    if (raw.local) {
      if (typeof raw.local.id !== 'string' || !raw.local.id || raw.local.id.length > 128 || typeof raw.local.version !== 'string' || !raw.local.version || raw.local.version.length > 128
        || !/^[a-f0-9]{32}$/.test(raw.local.directory) || !/^[A-Za-z0-9_][A-Za-z0-9_.+-]*\.whl$/.test(raw.local.file)
        || !/^[a-f0-9]{64}$/.test(raw.local.digest)) throw new Error('Scheduled local wheel identity is invalid.')
    }
    if ([raw.pending, raw.local, raw.rollback].filter(Boolean).length > 1) throw new Error('Conflicting scheduled plugin changes are blocked.')
    return raw
  }
  private localFile(selection: LocalSelection): string {
    const file = path.join(this.root, 'pending-local', selection.directory, selection.file)
    assertExtensionPath(this.root, file)
    return file
  }
  private removeLocal(selection: LocalSelection): void {
    const root = path.join(this.root, 'pending-local')
    removeExtensionDirectory(root, path.join(root, selection.directory), selection.directory)
  }
  /** Native file picker only; renderer and Runtime never supply a source path. */
  async scheduleLocal(wheel: string): Promise<void> {
    if (!usesScheduledExtensionUpdates(this.platform)) throw new Error('Scheduled plugin updates are only available on Windows.')
    if (this.installer.pendingUpdate()) throw new Error('Confirm or roll back the previous plugin update first.')
    if (!path.isAbsolute(wheel) || path.extname(wheel).toLowerCase() !== '.whl' || !fs.statSync(wheel).isFile() || fs.statSync(wheel).size > MAX_WHEEL_BYTES) throw new Error('Select a bounded local Python wheel (.whl).')
    const directory = randomBytes(16).toString('hex'), file = path.basename(wheel)
    const target = this.localFile({ directory, file, id: '', version: '', digest: '' })
    fs.mkdirSync(path.dirname(target), { recursive: true })
    fs.writeFileSync(path.join(path.dirname(target), '.amadeus-extension-install.json'), JSON.stringify({ installId: directory }), { flag: 'wx', mode: 0o600 })
    let persisted = false
    try {
      fs.copyFileSync(wheel, target, fs.constants.COPYFILE_EXCL)
      if (fs.statSync(target).size > MAX_WHEEL_BYTES) throw new Error('The selected local wheel exceeds the size limit.')
      const inspected = await this.helper.call('inspect-wheel', [target])
      const digest = createHash('sha256').update(fs.readFileSync(target)).digest('hex')
      if (!inspected.manifest || inspected.digest !== digest) throw new Error('The selected local wheel identity could not be verified.')
      const selection = { directory, file, id: inspected.manifest.id, version: inspected.manifest.version, digest }
      const preferences = this.preferences()
      this.savePreferences({ ...preferences, pending: undefined, local: selection, rollback: false })
      persisted = true
      if (preferences.local) this.removeLocal(preferences.local)
    } finally { if (!persisted) this.removeLocal({ directory, file, id: '', version: '', digest: '' }) }
  }
  private savePreferences(value: UpdatePreferences): void {
    assertExtensionPath(this.root, path.join(this.root, 'update-preferences.json'))
    fs.mkdirSync(this.root, { recursive: true })
    const temporary = path.join(this.root, `update-${randomBytes(16).toString('hex')}.tmp`)
    const handle = fs.openSync(temporary, 'wx', 0o600)
    try { fs.writeFileSync(handle, JSON.stringify(value) + '\n'); fs.fsyncSync(handle) }
    finally { fs.closeSync(handle) }
    try { fs.renameSync(temporary, path.join(this.root, 'update-preferences.json')) }
    finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary) }
    this.changed()
  }
  startAutomaticChecking(): void {
    if (this.timer) return
    void this.checkAutomatic().catch(() => {})
    this.timer = setInterval(() => { void this.checkAutomatic().catch(() => {}) }, 60_000)
    this.timer.unref?.()
  }
  stopAutomaticChecking(): void { if (this.timer) clearInterval(this.timer); this.timer = undefined }
  async checkAutomatic(now = Date.now()): Promise<void> {
    const preferences = this.preferences()
    if (!preferences.enabled || this.active || this.refreshTask || now - preferences.attemptedAt < 86_400_000) return
    this.savePreferences({ ...preferences, attemptedAt: now })
    const view = await this.refresh()
    const updates = this.store.storedExtensionRecords().flatMap(installed => {
      if (installed.pendingRemoval || !/^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(installed.version)) return []
      const compare = (left: string, right: string) => {
        const a = left.split('.').map(BigInt), b = right.split('.').map(BigInt)
        for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i] ? 1 : -1
        return 0
      }
      const next = view.entries.filter(entry => entry.id === installed.id && !entry.incompatible.length && !entry.withdrawal && !entry.blockedReason
        && compare(entry.version, installed.version) > 0).sort((a, b) => compare(b.version, a.version))[0]
      return next ? [{ id: next.id, version: next.version }] : []
    })
    const signature = JSON.stringify(updates.sort((a, b) => a.id.localeCompare(b.id)))
    const current = this.preferences()
    if (current.enabled && updates.length && signature !== current.notified) {
      this.notifyUpdates(updates)
      this.savePreferences({ ...this.preferences(), notified: signature })
    }
  }
  /** Only the app's stopped-backend boundary calls this before launching Host. */
  hasScheduled(): boolean {
    if (!usesScheduledExtensionUpdates(this.platform)) return false
    const preferences = this.preferences()
    return Boolean(preferences.pending || preferences.local || preferences.rollback)
  }
  async applyScheduled(): Promise<void> {
    if (!usesScheduledExtensionUpdates(this.platform)) throw new Error('Scheduled plugin updates are only available on Windows.')
    const preferences = this.preferences()
    if (preferences.rollback) {
      this.savePreferences({ ...preferences, rollback: false })
      await this.installer.rollbackPendingUpdate()
    } else if (preferences.local) {
      const selection = preferences.local
      this.savePreferences({ ...preferences, local: undefined })
      const file = this.localFile(selection)
      const assertCurrent = () => {
        assertExtensionPath(this.root, file)
        if (fs.statSync(file).size > MAX_WHEEL_BYTES || createHash('sha256').update(fs.readFileSync(file)).digest('hex') !== selection.digest) throw new Error('Scheduled local wheel bytes changed; select the wheel again.')
      }
      try { assertCurrent(); await this.installer.install(file, { catalog: false, identity: selection, assertCurrent }) }
      finally { this.removeLocal(selection) }
    } else if (preferences.pending) {
      // Consume intent before attempting: never retry a failed/cancelled install
      // on another boot without a new explicit user selection.
      this.savePreferences({ ...preferences, pending: undefined })
      await this.install(preferences.pending.id, preferences.pending.version, preferences.pending.digest)
    }
  }
  async view(): Promise<CatalogView> {
    if (!this.trusted && !this.cached && fs.existsSync(this.cacheFile)) {
      try { this.cached = (await this.helper.call('validate-catalog', [this.cacheFile])).catalog } catch { this.error ||= 'Catalog display cache is unavailable' }
    }
    const security = this.store.extensionSecurityState()
    const index = this.trusted || this.cached
    let preferences: UpdatePreferences | undefined
    let pendingUpdate: CatalogView['pendingUpdate']
    try { preferences = this.preferences(); pendingUpdate = this.installer.pendingUpdate?.() }
    catch (error) { this.error ||= error instanceof Error ? error.message : 'Plugin recovery state is unavailable' }
    const scheduled = preferences?.pending || preferences?.local
    return { entries: (index?.entries || []).map(entry => ({ ...entry, blockedReason: securityBlock(security, { id: entry.id, version: entry.version, digest: entry.sha256 }) || undefined })), sequence: index?.sequence, cached: !this.trusted || Boolean(this.error),
      checkedAt: security?.status === 'ready' && security.sequence === index?.sequence ? security.checkedAt : undefined,
      installed: this.store.storedExtensionRecords().map(record => {
        const match = index?.entries.find(e => e.id === record.id && e.version === record.version && e.sha256 === record.digest)
        return { id: record.id, version: record.version, digest: record.digest, catalogState: !index ? 'unavailable' : match ? 'matched' : 'unlisted',
          review: match?.review, withdrawal: match?.withdrawal, blocked: Boolean(securityBlock(security, record)) }
      }),
      autoCheck: preferences?.enabled ?? false, scheduledInstall: scheduled ? { id: scheduled.id, version: scheduled.version, digest: scheduled.digest } : undefined, rollbackScheduled: preferences?.rollback, pendingUpdate,
      error: this.error || this.store.extensionSecurityBlock() || undefined, downloading: this.progress }
  }
  refresh(recovery = false): Promise<CatalogView> {
    if (this.refreshTask) return this.refreshTask
    this.refreshTask = this.fetchIndex(recovery).finally(() => { this.refreshTask = undefined })
    return this.refreshTask
  }
  private async fetchIndex(recovery: boolean): Promise<CatalogView> {
    const stage = stagingDirectory(path.join(this.root, 'staging'))
    const controller = new AbortController()
    this.refreshController = controller
    try {
      const file = path.join(stage.directory, 'index.json')
      await this.download(OFFICIAL_EXTENSION_INDEX, file, MAX_INDEX_BYTES, controller.signal)
      const index = (await this.helper.call('validate-catalog', [file])).catalog
      if (!index) throw new Error('Catalog validator returned no catalog')
      this.store.commitExtensionSecurity(securitySnapshot(index.sequence, index.security.map(d => ({ id: d.id, version: d.version, digest: d.sha256, blocked: d.status === 'blocked', sequence: d.sequence })), new Date().toISOString()), recovery)
      // Publish only after the durable security transaction. Cache never authorizes installation.
      this.trusted = index
      this.error = ''
      try { fs.copyFileSync(file, this.cacheFile + '.tmp'); fs.renameSync(this.cacheFile + '.tmp', this.cacheFile) }
      catch { this.error = 'Security updated; catalog display cache could not be saved' }
      return await this.view()
    } catch (error) {
      this.error = error instanceof Error ? error.message : 'Catalog refresh failed'
      throw error
    } finally { this.refreshController = undefined; stage.dispose(); this.changed() }
  }
  private entry(id: string, version: string): CatalogEntry {
    const entry = this.trusted?.entries.find(e => e.id === id && e.version === version)
    if (!entry) throw new Error('Select a version from a freshly checked official catalog')
    return entry
  }
  private admitted(entry: CatalogEntry): void {
    this.store.assertExtensionAllowed({ id: entry.id, version: entry.version, digest: entry.sha256 })
    if (entry.withdrawal) throw new Error('This catalog version has been withdrawn: ' + entry.withdrawal.reason)
    if (entry.incompatible.length) throw new Error('Incompatible catalog package: ' + entry.incompatible.join('; '))
  }
  status(): CatalogView['downloading'] { return this.progress ? { ...this.progress } : undefined }
  cancel(): void { if (this.progress) this.progress.cancelRequested = true; this.active?.abort(); this.refreshController?.abort() }
  async install(id: string, version: string, expectedDigest?: string): Promise<Record<string, unknown>> {
    if (this.active) throw new Error('Another catalog installation is in progress')
    const controller = new AbortController()
    this.active = controller
    this.progress = { id, version, received: 0, total: 0, stage: 'checking' }
    let stage: ReturnType<typeof stagingDirectory> | undefined
    const cancelledDownload = (error: unknown): never => { if (controller.signal.aborted) throw new ExtensionChangeCancelled(); throw error }
    try {
      await this.refresh().catch(cancelledDownload)
      const selected = structuredClone(this.entry(id, version))
      if (expectedDigest && expectedDigest !== selected.sha256) throw new Error('Scheduled plugin bytes changed; select and review the version again.')
      const assertCurrent = () => {
        if (controller.signal.aborted) throw new ExtensionChangeCancelled()
        const current = this.entry(id, version)
        if (!isDeepStrictEqual(selected, current)) throw new Error('Catalog selection changed; review the current version again')
        this.admitted(current)
      }
      assertCurrent()
      stage = stagingDirectory(path.join(this.root, 'staging'))
      const filename = decodeURIComponent(new URL(selected.url).pathname.split('/').pop()!)
      if (!filename.endsWith('.whl') || /[\\/:]/.test(filename)) throw new Error('Invalid catalog wheel filename')
      const wheel = path.join(stage.directory, filename)
      const url = artifactUrl(selected.url, selected.sha256, this.mirror())
      this.progress = { id, version, received: 0, total: selected.size, stage: 'downloading' }
      await this.download(url, wheel, Math.min(MAX_WHEEL_BYTES, selected.size), controller.signal, received => { this.progress = { id, version, received, total: selected.size, stage: 'downloading', cancelRequested: controller.signal.aborted } }).catch(cancelledDownload)
      if (fs.statSync(wheel).size !== selected.size) throw new Error('Downloaded wheel size differs from catalog')
      assertCurrent()
      return await this.installer.install(wheel, { identity: { id, version, digest: selected.sha256 }, assertCurrent, onStage: stage => { this.progress = { id, version, received: selected.size, total: selected.size, stage, cancelRequested: controller.signal.aborted } } })
    } finally { stage?.dispose(); this.progress = undefined; this.active = undefined }
  }
  homepage(id: string, version: string, field: 'homepage' | 'repository' = 'homepage'): string {
    const value = (this.trusted || this.cached)?.entries.find(entry => entry.id === id && entry.version === version)?.display[field]
    if (typeof value !== 'string') throw new Error('This package has no HTTPS homepage')
    return httpsUrl(value)
  }
}
