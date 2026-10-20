import { net, session } from 'electron'
import fs from 'node:fs'
import path from 'node:path'
import { assertExtensionPath } from './extensionPaths.js'

export const MAX_WHEEL_BYTES = 256 * 1024 * 1024
export const MAX_INDEX_BYTES = 4 * 1024 * 1024
export function httpsUrl(raw: string): string {
  if (typeof raw !== 'string' || raw.length > 2048 || /[\x00-\x20\x7f]/.test(raw)) throw new Error('Expected a bounded HTTPS URL without credentials')
  const url = new URL(raw)
  if (url.protocol !== 'https:' || !url.hostname || url.username || url.password || url.hash) throw new Error('Expected HTTPS without credentials or fragment')
  return url.href
}
export function artifactUrl(url: string, digest: string, mirror: string): string {
  httpsUrl(url)
  if (!mirror) return url
  const base = new URL(httpsUrl(mirror))
  if (base.search) throw new Error('Artifact mirror must be an HTTPS base directory')
  const name = decodeURIComponent(new URL(url).pathname.split('/').pop()!)
  if (!/^[a-f0-9]{64}$/.test(digest) || !name.endsWith('.whl') || /[\\/:]/.test(name)) throw new Error('Invalid artifact identity')
  return httpsUrl(base.href.replace(/\/$/, '') + '/' + digest + '/' + encodeURIComponent(name))
}
export type Download = (url: string, destination: string, limit: number, signal: AbortSignal, progress?: (bytes: number) => void) => Promise<void>
/** A separate in-memory Chromium session inherits system transport, not application cookies/auth. */
export const downloadExtension: Download = (url, destination, limit, signal, progress) => new Promise((resolve, reject) => {
  httpsUrl(url)
  if (signal.aborted) { reject(new Error('Extension download cancelled')); return }
  const handle = fs.openSync(destination, 'wx', 0o600)
  let request: Electron.ClientRequest | undefined
  let finished = false
  let received = 0
  let redirects = 0
  const timer = setTimeout(() => finish(new Error('Extension download timed out')), 120_000)
  const cancel = () => finish(new Error('Extension download cancelled'))
  const finish = (error?: Error) => {
    if (finished) return
    finished = true
    clearTimeout(timer)
    signal.removeEventListener('abort', cancel)
    try { if (!error) fs.fsyncSync(handle) } catch { error = new Error('Extension download could not be saved') }
    try { fs.closeSync(handle) } catch { error ||= new Error('Extension download could not be saved') }
    if (error) {
      request?.abort()
      try { fs.unlinkSync(destination) } catch { /* The owned staging scope retries cleanup. */ }
      reject(error)
    } else resolve()
  }
  signal.addEventListener('abort', cancel, { once: true })
  try {
    request = net.request({ url, session: session.fromPartition('amadeus-extension-downloads', { cache: false }),
      credentials: 'omit', redirect: 'manual', useSessionCookies: false, cache: 'no-store', bypassCustomProtocolHandlers: true })
    request.on('redirect', (_status, _method, location) => {
      try { httpsUrl(location); if (++redirects > 5) throw new Error('Too many redirects'); request!.followRedirect() }
      catch { finish(new Error('Extension download rejected an unsafe or excessive redirect')) }
    })
    request.on('error', () => finish(new Error('Extension download failed; check network, proxy and certificate settings')))
    request.on('abort', () => finish(new Error('Extension download cancelled')))
    request.on('response', response => {
      if (response.statusCode !== 200) { finish(new Error('Extension download did not return a complete artifact')); return }
      // Chromium emits decoded bytes; Content-Length describes wire bytes when compressed.
      const header = (name: string): string => { const value = response.headers[name]; return Array.isArray(value) ? value.join(',') : value || '' }
      const encoding = header('content-encoding').toLowerCase()
      const length = encoding && encoding !== 'identity' ? 0 : Number(header('content-length') || 0)
      if (!Number.isFinite(length) || length > limit) { finish(new Error('Extension download exceeds size limit')); return }
      response.on('error', () => finish(new Error('Extension download was interrupted')))
      response.on('data', chunk => {
        if (finished) return
        received += chunk.length
        if (received > limit) { finish(new Error('Extension download exceeds size limit')); return }
        try { fs.writeFileSync(handle, chunk); progress?.(received) } catch { finish(new Error('Extension download could not be saved')) }
      })
      response.on('end', () => finish(received === 0 || (length && received !== length) ? new Error('Incomplete extension download') : undefined))
    })
    request.end()
  } catch { finish(new Error('Extension download could not start')) }
})
const activeStages = new Set<string>()
const STAGE_MARKER = '.amadeus-extension-download'
const STAGE_OWNER = 'amadeus.download/v1'
/** The desktop single-instance lock owns this root; active requests share this module. */
export function stagingDirectory(root: string): { directory: string; dispose: () => void } {
  assertExtensionPath(path.dirname(root), root)
  fs.mkdirSync(root, { recursive: true })
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const stale = path.join(root, entry.name)
    if (!/^download-[A-Za-z0-9]{6}$/.test(entry.name) || activeStages.has(stale) || !entry.isDirectory() || entry.isSymbolicLink()) continue
    assertExtensionPath(root, stale)
    const files = fs.readdirSync(stale)
    // Downloads never create subdirectories. Retain unknown or damaged ownership.
    if (!files.includes(STAGE_MARKER) || files.some(name => {
      const stat = fs.lstatSync(path.join(stale, name))
      return !stat.isFile() || stat.isSymbolicLink() || !(name === STAGE_MARKER || name === 'index.json' || name.endsWith('.whl'))
    })) continue
    const marker = path.join(stale, STAGE_MARKER)
    if (fs.statSync(marker).size !== Buffer.byteLength(STAGE_OWNER) || fs.readFileSync(marker, 'utf8') !== STAGE_OWNER) continue
    fs.rmSync(stale, { recursive: true })
  }
  const directory = fs.mkdtempSync(path.join(root, 'download-'))
  fs.writeFileSync(path.join(directory, STAGE_MARKER), STAGE_OWNER, { flag: 'wx', mode: 0o600 })
  activeStages.add(directory)
  return { directory, dispose: () => {
    assertExtensionPath(root, directory)
    fs.rmSync(directory, { recursive: true, force: true })
    activeStages.delete(directory)
  } }
}
