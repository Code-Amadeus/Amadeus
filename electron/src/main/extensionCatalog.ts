import fs from 'node:fs'
import path from 'node:path'
import { isDeepStrictEqual } from 'node:util'
import type { CatalogEntry, CatalogIndex, CatalogRequest, CatalogView, CatalogReply } from '../shared/extensionCatalog.js'
import { DesktopSettingsStore } from './desktopSettings.js'
import { ExtensionInstaller, ExtensionPackageHelper, ExtensionChangeCancelled } from './extensions.js'
import { artifactUrl, downloadExtension, httpsUrl, MAX_INDEX_BYTES, MAX_WHEEL_BYTES, stagingDirectory, type Download } from './extensionDownloads.js'
import { securityBlock, securitySnapshot } from './extensionSecurity.js'

export const OFFICIAL_EXTENSION_INDEX = 'https://raw.githubusercontent.com/Code-Amadeus/Amadeus-Extensions/main/catalog/index.json'
export function catalogRequest(raw: unknown): CatalogRequest {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('Invalid catalog request')
  const r = raw as CatalogRequest
  const selected = r.action === 'install' || r.action === 'homepage' || r.action === 'repository'
  if (!['get', 'status', 'refresh', 'recover', 'cancel', 'install', 'homepage', 'repository'].includes(r.action)
    || Object.keys(r).some(k => !(selected ? ['action', 'id', 'version'] : ['action']).includes(k))) throw new Error('Invalid catalog request fields')
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
  constructor(private readonly root: string, private readonly store: DesktopSettingsStore, private readonly helper: ExtensionPackageHelper,
    private readonly installer: ExtensionInstaller, private readonly mirror: () => string, private readonly changed: () => void = () => {},
    private readonly download: Download = downloadExtension) {}
  async dispatch(raw: unknown, environment: NodeJS.ProcessEnv, openExternal: (url: string) => Promise<unknown>): Promise<CatalogReply> {
    try {
      const request = catalogRequest(raw)
      if (request.action === 'status') return { ok: true, progress: this.status() }
      if (request.action === 'install') return { ok: true, settings: await this.install(request.id, request.version), catalog: await this.view() }
      if (request.action === 'homepage' || request.action === 'repository') { await openExternal(this.homepage(request.id, request.version, request.action)); return { ok: true } }
      if (request.action === 'cancel') this.cancel()
      const catalog = request.action === 'refresh' || request.action === 'recover' ? await this.refresh(request.action === 'recover') : await this.view()
      return { ok: true, catalog, settings: this.store.snapshot(environment) }
    } catch (error) {
      return { ok: false, ...(error instanceof ExtensionChangeCancelled ? { cancelled: true } : {}), error: error instanceof Error ? error.message : 'Catalog operation failed', catalog: await this.view(), settings: this.store.snapshot(environment) }
    }
  }
  private get cacheFile(): string { return path.join(this.root, 'index.json') }
  async view(): Promise<CatalogView> {
    if (!this.trusted && !this.cached && fs.existsSync(this.cacheFile)) {
      try { this.cached = (await this.helper.call('validate-catalog', [this.cacheFile])).catalog } catch { this.error ||= 'Catalog display cache is unavailable' }
    }
    const security = this.store.extensionSecurityState()
    const index = this.trusted || this.cached
    return { entries: (index?.entries || []).map(entry => ({ ...entry, blockedReason: securityBlock(security, { id: entry.id, version: entry.version, digest: entry.sha256 }) || undefined })), sequence: index?.sequence, cached: !this.trusted || Boolean(this.error),
      checkedAt: security?.status === 'ready' && security.sequence === index?.sequence ? security.checkedAt : undefined,
      installed: this.store.storedExtensionRecords().map(record => {
        const match = index?.entries.find(e => e.id === record.id && e.version === record.version && e.sha256 === record.digest)
        return { id: record.id, version: record.version, digest: record.digest, catalogState: !index ? 'unavailable' : match ? 'matched' : 'unlisted',
          review: match?.review, withdrawal: match?.withdrawal, blocked: Boolean(securityBlock(security, record)) }
      }),
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
  async install(id: string, version: string): Promise<Record<string, unknown>> {
    if (this.active) throw new Error('Another catalog installation is in progress')
    const controller = new AbortController()
    this.active = controller
    this.progress = { id, version, received: 0, total: 0, stage: 'checking' }
    let stage: ReturnType<typeof stagingDirectory> | undefined
    const cancelledDownload = (error: unknown): never => { if (controller.signal.aborted) throw new ExtensionChangeCancelled(); throw error }
    try {
      await this.refresh().catch(cancelledDownload)
      const selected = structuredClone(this.entry(id, version))
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
