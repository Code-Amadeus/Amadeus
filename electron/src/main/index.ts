/**
 * Electron main process - spawns Python backend and creates the app window.
 */

import { app, BrowserWindow, Menu, WebContentsView, dialog, ipcMain, screen } from 'electron'
import { spawn, ChildProcess } from 'child_process'
import { randomBytes } from 'crypto'
import http from 'http'
import path from 'path'
import fs from 'fs'
import { fileURLToPath } from 'url'
import {
  DesktopSettingsStore,
  type DesktopSettingsUpdate,
  type McpConnectionUpdate,
} from './desktopSettings.js'
import { ChatAvatarStore, type ChatAvatarRole } from './chatAvatars.js'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const PROJECT_ROOT = path.join(__dirname, '..', '..', '..')
const ELECTRON_ROOT = path.join(__dirname, '..', '..')
const USER_DATA_DIR = process.env.AMADEUS_ELECTRON_USER_DATA_DIR
  ? path.resolve(process.env.AMADEUS_ELECTRON_USER_DATA_DIR)
  : path.join(PROJECT_ROOT, '.electron-user-data')
const CACHE_DIR = process.env.AMADEUS_ELECTRON_CACHE_DIR
  ? path.resolve(process.env.AMADEUS_ELECTRON_CACHE_DIR)
  : path.join(PROJECT_ROOT, '.electron-cache')
const APP_ICON_PATH = path.join(PROJECT_ROOT, 'assets', 'icons', 'app', 'app_icon.ico')
const desktopSettings = new DesktopSettingsStore(
  path.join(USER_DATA_DIR, 'settings.json'),
  path.join(PROJECT_ROOT, '.env'),
)
const chatAvatars = new ChatAvatarStore(
  path.join(USER_DATA_DIR, 'assets', 'chat-avatars'),
)

for (const dir of [USER_DATA_DIR, CACHE_DIR]) {
  try {
    fs.mkdirSync(dir, { recursive: true })
  } catch { /* ignore */ }
}

app.setPath('userData', USER_DATA_DIR)
app.commandLine.appendSwitch('disk-cache-dir', CACHE_DIR)
Menu.setApplicationMenu(null)

// A packaged build must never trust a process that happens to own the Vite
// development port. NODE_ENV is not guaranteed to be set by electron-builder,
// so packaging identity is the owning security boundary.
const isDev = !app.isPackaged && process.env.NODE_ENV !== 'production'

let mainWindow: BrowserWindow | null = null
let workGlowWindow: BrowserWindow | null = null
let workPanelWindow: BrowserWindow | null = null
let electronSliceWindow: BrowserWindow | null = null
let electronSliceBridgeKey = ''
let electronSliceDesktopMonitor: ChildProcess | null = null
let electronSliceMonitorRestartTimer: NodeJS.Timeout | null = null
let electronSlicePlacementReady = false
let electronSliceShape: Electron.Rectangle[] | null = null
let electronSliceLayout = { x: 550 / 1672, y: 195 / 941, width: 586 / 1672, height: 443 / 941 }
const auipAppWindows = new Set<BrowserWindow>()
type AuipHostedSurface =
  | {
      kind: 'window'
      window: BrowserWindow
      hostSurfaceId: string
      appSessionId: string
    }
  | {
      kind: 'work-preview'
      previewId: string
      window: BrowserWindow
      view: WebContentsView
      hostSurfaceId: string
      appSessionId: string
      attemptId: string
      artifactRef: string
    }
const auipAppSurfacesById = new Map<string, AuipHostedSurface>()
type WorkPreviewPresentationPhase =
  | 'preview'
  | 'auip-preloading'
  | 'auip-attached'
  | 'auip-closing'
  | 'auip-conflict'
  | 'auip-ended'
type WorkPreviewDescriptor = {
  previewId: string
  workItemId: string
  attemptId: string
  title: string
  url: string
  revision: number
  contentRevision: number
  status: string
  error: string
  lifecycle: string
  artifactRef: string
  appSessionId: string
  hostSurfaceId: string
  presentedAppSessionId?: string
  presentedHostSurfaceId?: string
  presentationPhase?: WorkPreviewPresentationPhase
}
type PendingAuipHandoff = {
  view: WebContentsView
  hostSurfaceId: string
  attemptId: string
  artifactRef: string
  startRevision: number
  loaded: boolean
  settled: boolean
  timeout: NodeJS.Timeout
  resolve: (result: { ok: boolean; detail: string }) => void
}
type WorkPreviewSurface = {
  descriptor: WorkPreviewDescriptor
  allowedOrigin: string
  loadState: { status: string; detail: string }
  window: BrowserWindow
  view: WebContentsView
  previewAttached: boolean
  viewportBounds: Electron.Rectangle | null
  auipView: WebContentsView | null
  auipHostSurfaceId: string
  auipAppSessionId: string
  auipAttemptId: string
  auipArtifactRef: string
  pendingAuip: PendingAuipHandoff | null
  presentationPhase: WorkPreviewPresentationPhase
  loadedContentRevision: number
  nativeCloseFallback: NodeJS.Timeout | null
}
const workPreviewSurfaces = new Map<string, WorkPreviewSurface>()
const workPreviewIdsByWorkItem = new Map<string, string>()
let workOverlayHitTestTimer: NodeJS.Timeout | null = null
let workOverlayIgnoringMouse = false
let workOverlayPanelBounds: Electron.Rectangle | null = null
let workOverlayHitRegions: Electron.Rectangle[] = []
let pythonProcess: ChildProcess | null = null
let backendStopping: Promise<void> | null = null
let quittingAfterBackendStop = false
let backendOwned = false

const BACKEND_PORT = 17777
const BACKEND_WS = `ws://127.0.0.1:${BACKEND_PORT}/ws`
const BACKEND_AUTH_SCHEME = 'amadeus.local.v1'
const BACKEND_TOKEN_HEADER = 'X-Amadeus-Token'

function localCredential(name: string, bytes: number, minimum: number): string {
  const configured = String(process.env[name] || '').trim()
  const value = configured || randomBytes(bytes).toString('base64url')
  if (value.length < minimum || value.length > 512 || !/^[A-Za-z0-9._~-]+$/.test(value)) {
    throw new Error(`${name} must be ${minimum}-512 URL-safe characters`)
  }
  return value
}

// Process-memory credentials identify this exact desktop/backend pair. They
// are never placed in a URL, persisted to disk, or printed to a log.
const BACKEND_TOKEN = localCredential('AMADEUS_BACKEND_TOKEN', 32, 32)
const BACKEND_INSTANCE_NONCE = localCredential('AMADEUS_BACKEND_INSTANCE_NONCE', 18, 16)
const BACKEND_PROTOCOLS = [BACKEND_AUTH_SCHEME, `amadeus.auth.${BACKEND_TOKEN}`]

type WallpaperBridgeDescriptor = {
  assetPort: number
  bridgePort: number
  assetVersion: string
  sliceBounds: { x: number; y: number; width: number; height: number }
}

function getAppIconPath(): string | undefined {
  if (fs.existsSync(APP_ICON_PATH)) return APP_ICON_PATH
  return undefined
}

function wantsWorkOverlay(args = process.argv): boolean {
  return args.includes('--work-overlay') || process.env.AMADEUS_WORK_OVERLAY === '1'
}

// Python backend management.

function getPythonCommand(): string {
  const envPython = process.env.AMADEUS_PYTHON || process.env.AMADUES_PYTHON
  if (envPython && fs.existsSync(envPython)) return envPython

  // Resolve original git repo from worktree .git file
  function resolveOriginalRepo(): string | null {
    try {
      const gitFile = path.join(PROJECT_ROOT, '.git')
      if (!fs.existsSync(gitFile)) return null
      const content = fs.readFileSync(gitFile, 'utf-8').trim()
      // gitdir: F:/path/to/repo/.git/worktrees/name
      const match = content.match(/^gitdir:\s*(.+?)[/\\]\.git[/\\]worktrees[/\\]/)
      if (match) return match[1]
    } catch { /* ignore */ }
    return null
  }

  const originalRepo = resolveOriginalRepo()

  // 1. Check project root and original repo for venvs
  const roots = [PROJECT_ROOT, originalRepo].filter(Boolean) as string[]
  const venvNames = ['.venv_cu124', '.venv', '.venv_pt251']
  const venvPaths: string[] = []
  for (const root of roots) {
    for (const name of venvNames) {
      venvPaths.push(path.join(root, name, 'Scripts', 'python.exe'))  // Windows
      venvPaths.push(path.join(root, name, 'bin', 'python3'))          // Unix
    }
  }
  for (const p of venvPaths) {
    if (fs.existsSync(p)) return p
  }

  // 2. Common Python 3.x install locations on Windows
  if (process.platform === 'win32') {
    const localAppData = process.env.LOCALAPPDATA ?? 'C:/Users/' + (process.env.USERNAME ?? '') + '/AppData/Local'
    for (const ver of ['312', '311', '310', '39', '38']) {
      const p = path.join(localAppData, 'Programs', 'Python', 'Python' + ver, 'python.exe')
      if (fs.existsSync(p)) return p
    }
    const storePy3 = path.join(localAppData, 'Microsoft', 'WindowsApps', 'python3.exe')
    if (fs.existsSync(storePy3)) return storePy3
  }

  // 3. PATH fallback
  return 'python3'
}

type BackendHealth = 'ready' | 'starting' | 'foreign' | 'unavailable'

function backendHealthStatus(): Promise<BackendHealth> {
  return new Promise((resolve) => {
    const req = http.get(
      {
        hostname: '127.0.0.1',
        port: BACKEND_PORT,
        path: '/health',
        timeout: 800,
      },
      (res) => {
        let body = ''
        res.setEncoding('utf8')
        res.on('data', (chunk: string) => {
          if (body.length < 4096) body += chunk
        })
        res.on('end', () => {
          const successful = Boolean(res.statusCode && res.statusCode >= 200 && res.statusCode < 300)
          if (!successful) {
            // A listener answered on the owned port but did not prove the
            // Amadeus instance identity. Starting another process would only
            // create a collision and may leak traffic to that listener.
            resolve('foreign')
            return
          }
          try {
            const payload = JSON.parse(body) as { status?: unknown; instance_nonce?: unknown }
            if (payload.instance_nonce !== BACKEND_INSTANCE_NONCE) {
              resolve('foreign')
              return
            }
            resolve(payload.status === 'ok' ? 'ready' : 'starting')
          } catch {
            resolve('foreign')
          }
        })
      },
    )
    req.on('timeout', () => {
      req.destroy()
      resolve('unavailable')
    })
    req.on('error', () => resolve('unavailable'))
  })
}

async function waitForBackendReady(timeoutMs = 120_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const health = await backendHealthStatus()
    if (health === 'ready') {
      backendOwned = true
      return
    }
    if (health === 'foreign') {
      throw new Error(`port ${BACKEND_PORT} is owned by another backend instance`)
    }
    if (pythonProcess && (pythonProcess.exitCode !== null || pythonProcess.signalCode !== null)) {
      throw new Error(`backend exited before readiness (code ${pythonProcess.exitCode})`)
    }
    await new Promise(resolve => setTimeout(resolve, 250))
  }
  throw new Error(`backend did not become ready within ${timeoutMs}ms`)
}

function requestBackendShutdown(): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.request(
      {
        hostname: '127.0.0.1',
        port: BACKEND_PORT,
        path: '/shutdown',
        method: 'POST',
        timeout: 900,
        headers: { [BACKEND_TOKEN_HEADER]: BACKEND_TOKEN },
      },
      (res) => {
        res.resume()
        resolve(Boolean(res.statusCode && res.statusCode >= 200 && res.statusCode < 300))
      },
    )
    req.on('timeout', () => {
      req.destroy()
      resolve(false)
    })
    req.on('error', () => resolve(false))
    req.end()
  })
}

function waitForProcessExit(proc: ChildProcess, timeoutMs: number): Promise<boolean> {
  return new Promise((resolve) => {
    if (proc.exitCode !== null || proc.signalCode !== null) {
      resolve(true)
      return
    }
    const timer = setTimeout(() => {
      proc.off('exit', onExit)
      resolve(false)
    }, timeoutMs)
    const onExit = () => {
      clearTimeout(timer)
      resolve(true)
    }
    proc.once('exit', onExit)
  })
}

async function startBackend(): Promise<void> {
  const health = await backendHealthStatus()
  if (health === 'ready') {
    backendOwned = true
    console.log(`[electron] backend already available on ${BACKEND_WS}; reusing it`)
    return
  }
  if (health === 'starting') {
    backendOwned = true
    console.log(`[electron] backend is assembling on ${BACKEND_WS}; waiting for readiness`)
    await waitForBackendReady()
    return
  }
  if (health === 'foreign') {
    backendOwned = false
    throw new Error(`port ${BACKEND_PORT} is owned by another backend instance`)
  }

  const python = getPythonCommand()

  console.log(`[electron] starting backend: ${python} -m server.app --port ${BACKEND_PORT}`)
  console.log(`[electron] project root: ${PROJECT_ROOT}`)

  const backendEnvironment = desktopSettings.backendEnvironment(process.env, {
    AEC_REALTIME_ENABLED: '1',
    AEC_REALTIME_BARGE_IN: '1',
    AEC_REALTIME_DELAY_MS: '280',
    ASR_ECHO_TAIL_GUARD_MS: '650',
  })
  pythonProcess = spawn(python, ['-m', 'server.app', '--port', String(BACKEND_PORT)], {
    cwd: PROJECT_ROOT,
    env: {
      ...backendEnvironment,
      ...process.env,
      PYTHONUNBUFFERED: '1',
      PYTHONUTF8: '1',
      PYTHONIOENCODING: 'utf-8',
      ...(process.platform === 'darwin' ? { PYTORCH_ENABLE_MPS_FALLBACK: '1' } : {}),
      AMADEUS_BACKEND_AUTH_MODE: 'required',
      AMADEUS_BACKEND_TOKEN: BACKEND_TOKEN,
      AMADEUS_BACKEND_INSTANCE_NONCE: BACKEND_INSTANCE_NONCE,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  })

  pythonProcess.stdout?.on('data', (data: Buffer) => {
    console.log(`[backend] ${data.toString('utf8').trim()}`)
  })
  pythonProcess.stderr?.on('data', (data: Buffer) => {
    console.error(`[backend:err] ${data.toString('utf8').trim()}`)
  })
  pythonProcess.on('exit', (code: number | null) => {
    console.log(`[electron] backend exited with code ${code}`)
    pythonProcess = null
    backendOwned = false
  })
  await waitForBackendReady()
}

async function stopBackend(): Promise<void> {
  if (backendStopping) return backendStopping
  const proc = pythonProcess
  if (!proc) return

  backendStopping = (async () => {
    const requested = await requestBackendShutdown()
    const exited = requested ? await waitForProcessExit(proc, 2500) : false
    if (!exited && proc.exitCode === null && proc.signalCode === null) {
      proc.kill()
      await waitForProcessExit(proc, 1200)
    }
    if (pythonProcess === proc) pythonProcess = null
    backendOwned = false
  })().finally(() => {
    backendStopping = null
  })
  return backendStopping
}

// window management.

const RENDERER_ENTRY_PATH = path.resolve(
  path.join(__dirname, '..', 'renderer', 'index.html'),
)

function isTrustedRendererShellUrl(rawUrl: string): boolean {
  try {
    const target = new URL(rawUrl)
    if (
      isDev
      && target.protocol === 'http:'
      && target.hostname === 'localhost'
      && target.port === '5173'
      && target.pathname === '/'
    ) return true
    return target.protocol === 'file:'
      && path.resolve(fileURLToPath(target)) === RENDERER_ENTRY_PATH
  } catch {
    return false
  }
}

function guardTrustedRendererShell(window: BrowserWindow): void {
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-attach-webview', event => event.preventDefault())
  const guardNavigation = (event: Electron.Event, target: string) => {
    if (!isTrustedRendererShellUrl(target)) event.preventDefault()
  }
  window.webContents.on('will-navigate', guardNavigation)
  window.webContents.on('will-redirect', guardNavigation)
}

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1100,
    height: 800,
    minWidth: 600,
    minHeight: 400,
    icon: getAppIconPath(),
    title: '',
    frame: true,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload', 'index.mjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      webSecurity: false,   // allow file:// iframe for PixiJS renderer
    },
  })
  mainWindow.setMenuBarVisibility(false)
  mainWindow.setTitle('')
  guardTrustedRendererShell(mainWindow)
  mainWindow.on('page-title-updated', (event) => {
    event.preventDefault()
    mainWindow?.setTitle('')
  })

  // load from vite dev server or built files
  if (isDev) {
    mainWindow.loadURL('http://localhost:5173')
      .catch(() => {
        // fallback: try built files
        const p = path.join(__dirname, '..', 'renderer', 'index.html')
        if (fs.existsSync(p)) mainWindow?.loadFile(p)
      })
  } else {
    mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'))
  }

  mainWindow.on('closed', () => { mainWindow = null })
}

function normalizeLocalPort(value: unknown): number {
  const port = Number(value)
  return Number.isInteger(port) && port > 0 && port <= 65535 ? port : -1
}

function normalizeWallpaperBridge(raw: unknown): WallpaperBridgeDescriptor | null {
  const value = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {}
  const assetPort = normalizeLocalPort(value.assetPort)
  const bridgePort = normalizeLocalPort(value.bridgePort)
  if (assetPort < 0 || bridgePort < 0) return null
  const rawBounds = value.sliceBounds && typeof value.sliceBounds === 'object'
    ? value.sliceBounds as Record<string, unknown>
    : {}
  const candidateBounds = {
    x: Number(rawBounds.x),
    y: Number(rawBounds.y),
    width: Number(rawBounds.width),
    height: Number(rawBounds.height),
  }
  const sliceBounds = Object.values(candidateBounds).every(Number.isFinite)
    && candidateBounds.width > 0
    && candidateBounds.height > 0
    ? candidateBounds
    : electronSliceLayout
  return {
    assetPort,
    bridgePort,
    assetVersion: String(value.assetVersion || ''),
    sliceBounds,
  }
}

function electronSliceBounds(): Electron.Rectangle {
  const display = screen.getPrimaryDisplay()
  const displayBounds = display.bounds
  const left = Math.floor(displayBounds.x + displayBounds.width * electronSliceLayout.x)
  const top = Math.floor(displayBounds.y + displayBounds.height * electronSliceLayout.y)
  const right = Math.ceil(left + displayBounds.width * electronSliceLayout.width)
  const bottom = Math.ceil(top + displayBounds.height * electronSliceLayout.height)
  return { x: left, y: top, width: right - left, height: bottom - top }
}

function electronSliceUrl(bridge: WallpaperBridgeDescriptor): string {
  const query = new URLSearchParams({
    bridgePort: String(bridge.bridgePort),
    assetVersion: bridge.assetVersion,
  })
  return `http://127.0.0.1:${bridge.assetPort}/render/web/electron_slice.html?${query.toString()}`
}

function electronNativeHandle(window: BrowserWindow): string {
  const handle = window.getNativeWindowHandle()
  return handle.length >= 8
    ? handle.readBigUInt64LE(0).toString()
    : String(handle.readUInt32LE(0))
}

function stopElectronSliceDesktopMonitor(): void {
  if (electronSliceMonitorRestartTimer) {
    clearTimeout(electronSliceMonitorRestartTimer)
    electronSliceMonitorRestartTimer = null
  }
  const monitor = electronSliceDesktopMonitor
  electronSliceDesktopMonitor = null
  monitor?.kill()
}

function resetElectronSliceRenderReadiness(window: BrowserWindow): void {
  electronSliceShape = null
  if (window.isDestroyed()) return
  window.setIgnoreMouseEvents(true, { forward: true })
  window.setShape([])
  if (window.isVisible()) window.hide()
}

function applyElectronSliceShape(window: BrowserWindow): void {
  if (window.isDestroyed() || electronSliceShape === null) return
  window.setShape(electronSliceShape)
  window.setIgnoreMouseEvents(electronSliceShape.length === 0, { forward: true })
}

function reconcileElectronSliceReadiness(window: BrowserWindow): void {
  if (
    electronSliceWindow !== window
    || window.isDestroyed()
    || !electronSlicePlacementReady
    || electronSliceShape === null
  ) return
  // Desktop placement can rebuild the native frame and discard its region.
  // Reapply the renderer-owned shape after every observed reconciliation.
  if (!window.isVisible()) window.showInactive()
  applyElectronSliceShape(window)
}

function suspendElectronSlicePlacement(window: BrowserWindow): void {
  electronSlicePlacementReady = false
  if (electronSliceWindow !== window || window.isDestroyed()) return
  window.setIgnoreMouseEvents(true, { forward: true })
  if (window.isVisible()) window.hide()
}

function startElectronSliceDesktopMonitor(window: BrowserWindow): void {
  stopElectronSliceDesktopMonitor()
  electronSlicePlacementReady = false
  if (process.platform !== 'win32') {
    electronSlicePlacementReady = true
    reconcileElectronSliceReadiness(window)
    return
  }
  const script = path.join(PROJECT_ROOT, 'wallpaper', 'windows_desktop_layer.py')
  const helper = spawn(
    getPythonCommand(),
    [script, '--attach', electronNativeHandle(window), '--watch', '--interval', '1'],
    { cwd: PROJECT_ROOT, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] },
  )
  electronSliceDesktopMonitor = helper
  let stdoutBuffer = ''
  let stderrBuffer = ''
  helper.stdout?.on('data', chunk => {
    stdoutBuffer += String(chunk)
    const lines = stdoutBuffer.split(/\r?\n/)
    stdoutBuffer = lines.pop() || ''
    for (const line of lines) {
      if (!line.trim()) continue
      try {
        const result = JSON.parse(line) as { ok?: boolean; mode?: string; reconciled?: boolean }
        if (result.ok === true) {
          electronSlicePlacementReady = true
          reconcileElectronSliceReadiness(window)
          console.log(`[electron-slice] desktop placement: ${String(result.mode || 'unknown')}`)
        } else {
          suspendElectronSlicePlacement(window)
          console.error(`[electron-slice] desktop placement unavailable: ${String(result.mode || 'unknown')}`)
        }
      } catch {
        console.error(`[electron-slice] invalid desktop monitor event: ${line}`)
      }
    }
  })
  helper.stderr?.on('data', chunk => {
    stderrBuffer = `${stderrBuffer}${String(chunk)}`.slice(-2000)
  })
  helper.once('error', error => {
    console.error('[electron-slice] desktop monitor failed:', error)
  })
  helper.once('exit', code => {
    if (electronSliceDesktopMonitor !== helper) return
    electronSliceDesktopMonitor = null
    suspendElectronSlicePlacement(window)
    if (stderrBuffer.trim()) console.error(`[electron-slice] desktop monitor stderr: ${stderrBuffer.trim()}`)
    if (!electronSliceWindow || electronSliceWindow !== window || window.isDestroyed()) return
    console.error(`[electron-slice] desktop monitor exited (${String(code)}); restarting`)
    electronSliceMonitorRestartTimer = setTimeout(() => {
      electronSliceMonitorRestartTimer = null
      if (electronSliceWindow === window && !window.isDestroyed()) startElectronSliceDesktopMonitor(window)
    }, 750)
  })
}

function updateElectronSliceBounds(): void {
  if (!electronSliceWindow || electronSliceWindow.isDestroyed()) return
  electronSliceWindow.setBounds(electronSliceBounds(), false)
}

function closeElectronSliceWindow(): void {
  stopElectronSliceDesktopMonitor()
  electronSliceWindow?.close()
  electronSliceWindow = null
  electronSliceBridgeKey = ''
  electronSlicePlacementReady = false
  electronSliceShape = null
}

function createElectronSliceWindow(rawBridge: unknown): boolean {
  const bridge = normalizeWallpaperBridge(rawBridge)
  if (!bridge) return false
  electronSliceLayout = bridge.sliceBounds
  const bridgeKey = `${bridge.assetPort}:${bridge.bridgePort}:${bridge.assetVersion}:${JSON.stringify(bridge.sliceBounds)}`
  if (electronSliceWindow && !electronSliceWindow.isDestroyed()) {
    updateElectronSliceBounds()
    if (electronSliceBridgeKey !== bridgeKey) {
      electronSliceBridgeKey = bridgeKey
      resetElectronSliceRenderReadiness(electronSliceWindow)
      void electronSliceWindow.loadURL(electronSliceUrl(bridge))
    }
    return true
  }

  const window = new BrowserWindow({
    ...electronSliceBounds(),
    title: '',
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    show: false,
    paintWhenInitiallyHidden: true,
    focusable: true,
    fullscreenable: false,
    resizable: false,
    movable: false,
    icon: getAppIconPath(),
    hasShadow: false,
    skipTaskbar: true,
    alwaysOnTop: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload', 'slice.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  })
  electronSliceWindow = window
  electronSliceBridgeKey = bridgeKey
  electronSlicePlacementReady = false
  electronSliceShape = null
  window.setMenuBarVisibility(false)
  window.setIgnoreMouseEvents(true, { forward: true })
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-attach-webview', event => event.preventDefault())
  const allowedUrl = new URL(electronSliceUrl(bridge))
  window.webContents.on('will-navigate', (event, target) => {
    try {
      const destination = new URL(target)
      if (destination.origin !== allowedUrl.origin || destination.pathname !== allowedUrl.pathname) {
        event.preventDefault()
      }
    } catch {
      event.preventDefault()
    }
  })
  window.webContents.on('did-start-loading', () => resetElectronSliceRenderReadiness(window))
  window.webContents.on('did-finish-load', () => {
    console.log('[electron-slice] renderer document loaded; awaiting shape commit')
  })
  window.webContents.on('did-fail-load', (_event, code, description, url, isMainFrame) => {
    if (isMainFrame) console.error(`[electron-slice] renderer load failed (${code}) ${description}: ${url}`)
  })
  window.webContents.on('console-message', (_event, level, message, line, sourceId) => {
    if (level >= 2 || message.includes('[ElectronSlice]')) {
      console.error(`[electron-slice:renderer] ${message} (${sourceId}:${line})`)
    }
  })
  window.webContents.on('render-process-gone', () => resetElectronSliceRenderReadiness(window))
  window.once('ready-to-show', () => startElectronSliceDesktopMonitor(window))
  void window.loadURL(allowedUrl.toString()).catch(error => {
    console.error('[electron-slice] failed to load Slice host:', error)
  })
  window.on('closed', () => {
    if (electronSliceWindow === window) {
      electronSliceWindow = null
      electronSliceBridgeKey = ''
    }
  })
  return true
}

function workOverlayUrl(): string {
  const params = 'page=work&desktopProjection=1&panelWindow=1'
  if (isDev) return `http://localhost:5173?${params}`
  return `file://${path.join(__dirname, '..', 'renderer', 'index.html')}?${params}`
}

function workGlowUrl(): string {
  const params = 'desktopProjection=1&glowWindow=1'
  if (isDev) return `http://localhost:5173?${params}`
  return `file://${path.join(__dirname, '..', 'renderer', 'index.html')}?${params}`
}

function workOverlayBounds() {
  const display = screen.getPrimaryDisplay()
  return display.bounds
}

function workSlicePanelBounds() {
  if (workOverlayPanelBounds) return workOverlayPanelBounds

  const { x, y, width, height } = screen.getPrimaryDisplay().bounds
  const panelWidth = Math.min(1500, Math.max(1120, width * 0.39))
  const panelHeight = Math.min(1070, Math.max(800, width * 0.2786))
  return {
    x: Math.round(x + (width - panelWidth) / 2),
    y: Math.round(y + Math.max(54, height * 0.12)),
    width: Math.round(panelWidth),
    height: Math.round(panelHeight),
  }
}

function workOverlayActiveHitRegions(): Electron.Rectangle[] {
  const regions = workOverlayHitRegions.length > 0 ? workOverlayHitRegions : [workSlicePanelBounds()]
  return regions
}

function setWorkOverlayMousePassthrough(ignore: boolean): void {
  if (!workPanelWindow || workOverlayIgnoringMouse === ignore) return
  workOverlayIgnoringMouse = ignore
  workPanelWindow.setIgnoreMouseEvents(ignore, { forward: true })
}

function startWorkOverlayHitTest(): void {
  stopWorkOverlayHitTest()
  workOverlayIgnoringMouse = false
  workOverlayHitTestTimer = setInterval(() => {
    if (!workPanelWindow) return
    const cursor = screen.getCursorScreenPoint()
    const hitSlop = 28
    const insideHitRegion = workOverlayActiveHitRegions().some(region =>
      cursor.x >= region.x - hitSlop &&
      cursor.x <= region.x + region.width + hitSlop &&
      cursor.y >= region.y - hitSlop &&
      cursor.y <= region.y + region.height + hitSlop
    )
    setWorkOverlayMousePassthrough(!insideHitRegion)
  }, 30)
}

function stopWorkOverlayHitTest(): void {
  if (workOverlayHitTestTimer) {
    clearInterval(workOverlayHitTestTimer)
    workOverlayHitTestTimer = null
  }
  workOverlayIgnoringMouse = false
  workOverlayPanelBounds = null
  workOverlayHitRegions = []
}

function createWorkGlowWindow(): void {
  if (workGlowWindow) {
    workGlowWindow.showInactive()
    return
  }

  const bounds = workOverlayBounds()
  workGlowWindow = new BrowserWindow({
    ...bounds,
    title: '',
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    show: false,
    paintWhenInitiallyHidden: true,
    focusable: false,
    fullscreenable: false,
    resizable: false,
    movable: false,
    icon: getAppIconPath(),
    hasShadow: false,
    skipTaskbar: true,
    alwaysOnTop: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload', 'index.mjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      webSecurity: false,
    },
  })

  workGlowWindow.setMenuBarVisibility(false)
  workGlowWindow.setIgnoreMouseEvents(true, { forward: true })
  guardTrustedRendererShell(workGlowWindow)
  workGlowWindow.loadURL(workGlowUrl()).catch(() => {
    const p = path.join(__dirname, '..', 'renderer', 'index.html')
    if (fs.existsSync(p)) workGlowWindow?.loadFile(p, { query: { desktopProjection: '1', glowWindow: '1' } })
  })
  workGlowWindow.once('ready-to-show', () => {
    workGlowWindow?.showInactive()
  })
  workGlowWindow.on('closed', () => {
    workGlowWindow = null
  })
}

function createWorkPanelWindow(): void {
  if (workPanelWindow) {
    workPanelWindow.show()
    workPanelWindow.focus()
    return
  }

  const bounds = workSlicePanelBounds()
  workPanelWindow = new BrowserWindow({
    ...bounds,
    title: '',
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    show: false,
    paintWhenInitiallyHidden: true,
    focusable: true,
    fullscreenable: false,
    resizable: false,
    movable: true,
    icon: getAppIconPath(),
    hasShadow: false,
    skipTaskbar: true,
    alwaysOnTop: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload', 'index.mjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      webSecurity: false,
    },
  })

  workPanelWindow.setMenuBarVisibility(false)
  guardTrustedRendererShell(workPanelWindow)
  workPanelWindow.loadURL(workOverlayUrl()).catch(() => {
    const p = path.join(__dirname, '..', 'renderer', 'index.html')
    if (fs.existsSync(p)) workPanelWindow?.loadFile(p, { query: { page: 'work', desktopProjection: '1', panelWindow: '1' } })
  })
  workPanelWindow.once('ready-to-show', () => {
    workPanelWindow?.show()
  })
  workPanelWindow.on('closed', () => {
    workPanelWindow = null
  })
}

function createWorkOverlayWindow(): void {
  createWorkPanelWindow()
}

function closeWorkOverlayWindow(): void {
  stopWorkOverlayHitTest()
  workPanelWindow?.close()
  workPanelWindow = null
  workGlowWindow?.close()
  workGlowWindow = null
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? value as Record<string, unknown> : {}
}

function normalizeWorkPreviewUrl(rawUrl: unknown): URL | null {
  try {
    const url = new URL(String(rawUrl || ''))
    const hostname = url.hostname.toLowerCase()
    const loopback = hostname === '127.0.0.1' || hostname === '[::1]'
    if (!loopback || !['http:', 'https:'].includes(url.protocol)) return null
    if (url.username || url.password) return null
    return url
  } catch {
    return null
  }
}

function normalizeWorkPreviewDescriptor(raw: unknown): WorkPreviewDescriptor | null {
  const envelope = recordValue(raw)
  const nested = recordValue(envelope.descriptor || envelope.preview)
  const value = { ...envelope, ...nested }
  const previewId = String(value.previewId || value.preview_id || '').trim()
  const workItemId = String(value.workItemId || value.work_item_id || '').trim()
  const attemptId = String(value.attemptId || value.attempt_id || '').trim()
  const rawUrl = String(value.url || value.preview_url || '').trim()
  const url = rawUrl ? normalizeWorkPreviewUrl(rawUrl) : null
  const status = String(value.status || 'preparing').trim().slice(0, 80) || 'preparing'
  const lifecycle = String(value.lifecycle || value.preview_lifecycle || 'preview')
    .trim().toLowerCase().slice(0, 48) || 'preview'
  const contentHidden = ['assembling', 'handoff', 'attached', 'holding', 'frozen'].includes(lifecycle)
  const requiresContent = ['ready', 'live', 'complete', 'completed', 'terminal', 'final']
    .includes(status.toLowerCase()) && !contentHidden
  if (
    !previewId || previewId.length > 160
    || !workItemId || workItemId.length > 200
    || !attemptId || attemptId.length > 200
  ) return null
  if (rawUrl && !url) return null
  if (requiresContent && !url) return null
  const rawRevision = Number(value.revision ?? 0)
  const revision = Number.isSafeInteger(rawRevision) && rawRevision >= 0 ? rawRevision : -1
  if (revision < 0) return null
  const rawContentRevision = Number(value.contentRevision ?? value.content_revision ?? revision)
  const contentRevision = Number.isSafeInteger(rawContentRevision) && rawContentRevision >= 0
    ? rawContentRevision
    : -1
  if (contentRevision < 0) return null
  return {
    previewId,
    workItemId,
    attemptId,
    title: String(value.title || 'Work Preview').trim().slice(0, 240) || 'Work Preview',
    url: url?.toString() || '',
    revision,
    contentRevision,
    status,
    error: String(value.error || '').trim().slice(0, 240),
    lifecycle,
    artifactRef: String(value.artifactRef || value.artifact_ref || '').trim().slice(0, 512),
    appSessionId: String(value.appSessionId || value.app_session_id || '').trim().slice(0, 200),
    hostSurfaceId: String(value.hostSurfaceId || value.host_surface_id || '').trim().slice(0, 200),
  }
}

function workPreviewPartitionToken(previewId: string): string {
  const safeId = previewId.replace(/[^a-zA-Z0-9_-]/g, '-').slice(0, 48) || 'preview'
  return `${safeId}-${Date.now().toString(36)}`
}

function isTrustedAmadeusRenderer(sender: Electron.WebContents): boolean {
  if (isPrimaryDesktopRenderer(sender)) return true
  for (const surface of workPreviewSurfaces.values()) {
    if (surface.window.webContents === sender) return true
  }
  return false
}

function isMainRenderer(sender: Electron.WebContents): boolean {
  return mainWindow?.webContents === sender
}

function isWorkPanelRenderer(sender: Electron.WebContents): boolean {
  return workPanelWindow?.webContents === sender
}

function isPrimaryDesktopRenderer(sender: Electron.WebContents): boolean {
  return isMainRenderer(sender) || isWorkPanelRenderer(sender)
}

function senderOwnsWorkPreview(sender: Electron.WebContents, previewId: string): boolean {
  const surface = workPreviewSurfaces.get(previewId)
  return Boolean(surface && surface.window.webContents === sender)
}

function sendWorkPreviewEvent(
  surface: WorkPreviewSurface,
  channel: 'work-preview.descriptor' | 'work-preview.load-state' | 'work-preview.close-requested',
  payload: Record<string, unknown> | WorkPreviewDescriptor,
): void {
  if (surface.window.isDestroyed() || surface.window.webContents.isDestroyed()) return
  surface.window.webContents.send(channel, payload)
}

function guardWorkPreviewNavigation(surface: WorkPreviewSurface, target: string, event: Electron.Event): void {
  try {
    if (new URL(target).origin !== surface.allowedOrigin) event.preventDefault()
  } catch {
    event.preventDefault()
  }
}

function configureWorkPreviewSession(targetSession: Electron.Session): void {
  targetSession.setPermissionCheckHandler(() => false)
  targetSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false))
  targetSession.on('will-download', event => event.preventDefault())
}

function isAllowedWorkPreviewResource(surface: WorkPreviewSurface, rawUrl: string): boolean {
  try {
    const target = new URL(rawUrl)
    if (target.protocol === 'data:' || target.protocol === 'about:') return true
    if (!surface.allowedOrigin) return false
    if (target.protocol === 'blob:') return target.origin === surface.allowedOrigin
    const allowed = new URL(surface.allowedOrigin)
    if (['http:', 'https:'].includes(target.protocol)) return target.origin === allowed.origin
    if (['ws:', 'wss:'].includes(target.protocol)) {
      const matchingProtocol = (target.protocol === 'ws:' && allowed.protocol === 'http:')
        || (target.protocol === 'wss:' && allowed.protocol === 'https:')
      return matchingProtocol && target.host === allowed.host
    }
    return false
  } catch {
    return false
  }
}

function restrictWorkPreviewContentNetwork(targetSession: Electron.Session, surface: WorkPreviewSurface): void {
  targetSession.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, callback) => {
    callback({ cancel: !isAllowedWorkPreviewResource(surface, details.url) })
  })
}

function workPreviewLifecycleHidesPreview(lifecycle: string): boolean {
  return ['assembling', 'handoff', 'attached'].includes(String(lifecycle || '').toLowerCase())
}

function workPreviewReloadAllowed(surface: WorkPreviewSurface): boolean {
  return !['assembling', 'handoff', 'attached', 'holding', 'frozen']
    .includes(surface.descriptor.lifecycle)
    && surface.presentationPhase === 'preview'
    && !surface.pendingAuip
    && !surface.auipView
    && !surface.view.webContents.isDestroyed()
}

function projectedWorkPreviewDescriptor(surface: WorkPreviewSurface): WorkPreviewDescriptor {
  return {
    ...surface.descriptor,
    presentedAppSessionId: surface.auipAppSessionId || undefined,
    presentedHostSurfaceId: surface.auipHostSurfaceId || undefined,
    presentationPhase: surface.presentationPhase,
  }
}

function sendWorkPreviewDescriptor(surface: WorkPreviewSurface): void {
  sendWorkPreviewEvent(surface, 'work-preview.descriptor', projectedWorkPreviewDescriptor(surface))
}

function detachWorkPreviewView(surface: WorkPreviewSurface): void {
  if (!surface.previewAttached || surface.window.isDestroyed()) return
  try { surface.window.contentView.removeChildView(surface.view) } catch { /* already detached */ }
  surface.previewAttached = false
}

function releaseAttachedAuipView(surface: WorkPreviewSurface): void {
  const view = surface.auipView
  if (!view) return
  const hosted = surface.auipHostSurfaceId
    ? auipAppSurfacesById.get(surface.auipHostSurfaceId)
    : undefined
  if (hosted?.kind === 'work-preview' && hosted.view === view) {
    auipAppSurfacesById.delete(surface.auipHostSurfaceId)
  }
  if (!surface.window.isDestroyed()) {
    try { surface.window.contentView.removeChildView(view) } catch { /* already detached */ }
  }
  if (!view.webContents.isDestroyed()) view.webContents.close({ waitForBeforeUnload: false })
  surface.auipView = null
  surface.auipHostSurfaceId = ''
  surface.auipAppSessionId = ''
  surface.auipAttemptId = ''
  surface.auipArtifactRef = ''
}

function attachWorkPreviewView(surface: WorkPreviewSurface): void {
  if (
    surface.previewAttached
    || surface.window.isDestroyed()
    || surface.view.webContents.isDestroyed()
    || surface.auipView
  ) return
  surface.window.contentView.addChildView(surface.view)
  surface.previewAttached = true
  if (surface.viewportBounds) surface.view.setBounds(surface.viewportBounds)
}

function reconcileWorkPreviewLifecycle(surface: WorkPreviewSurface): void {
  if (
    workPreviewLifecycleHidesPreview(surface.descriptor.lifecycle)
    || surface.auipView
    || surface.presentationPhase !== 'preview'
  ) {
    detachWorkPreviewView(surface)
  } else {
    attachWorkPreviewView(surface)
  }
}

function publishWorkPreviewPresentation(
  surface: WorkPreviewSurface,
  phase: WorkPreviewPresentationPhase,
  detail = '',
): void {
  surface.presentationPhase = phase
  reconcileWorkPreviewLifecycle(surface)
  sendWorkPreviewDescriptor(surface)
  if (detail) {
    surface.loadState = { status: 'failed', detail }
    sendWorkPreviewEvent(surface, 'work-preview.load-state', {
      previewId: surface.descriptor.previewId,
      ...surface.loadState,
    })
  }
}

function destroyWorkPreviewSurface(previewId: string, closeWindow = true): boolean {
  const surface = workPreviewSurfaces.get(previewId)
  if (!surface) return false
  if (surface.nativeCloseFallback) {
    clearTimeout(surface.nativeCloseFallback)
    surface.nativeCloseFallback = null
  }
  workPreviewSurfaces.delete(previewId)
  if (workPreviewIdsByWorkItem.get(surface.descriptor.workItemId) === previewId) {
    workPreviewIdsByWorkItem.delete(surface.descriptor.workItemId)
  }
  if (surface.auipHostSurfaceId) {
    const hosted = auipAppSurfacesById.get(surface.auipHostSurfaceId)
    if (hosted?.kind === 'work-preview' && hosted.previewId === previewId) {
      auipAppSurfacesById.delete(surface.auipHostSurfaceId)
    }
  }
  if (surface.pendingAuip) {
    const pending = surface.pendingAuip
    surface.pendingAuip = null
    pending.settled = true
    clearTimeout(pending.timeout)
    if (!pending.view.webContents.isDestroyed()) {
      pending.view.webContents.close({ waitForBeforeUnload: false })
    }
    pending.resolve({ ok: false, detail: 'The Work Preview closed before Host-authorized Attach completed.' })
  }
  if (surface.auipView) {
    if (!surface.window.isDestroyed()) {
      try { surface.window.contentView.removeChildView(surface.auipView) } catch { /* already detached */ }
    }
    if (!surface.auipView.webContents.isDestroyed()) {
      surface.auipView.webContents.close({ waitForBeforeUnload: false })
    }
    surface.auipView = null
  }
  detachWorkPreviewView(surface)
  if (!surface.view.webContents.isDestroyed()) {
    surface.view.webContents.close({ waitForBeforeUnload: false })
  }
  if (closeWindow && !surface.window.isDestroyed()) surface.window.destroy()
  return true
}

function loadWorkPreviewContent(surface: WorkPreviewSurface, reload = false): void {
  if (surface.view.webContents.isDestroyed() || !surface.descriptor.url) return
  surface.loadedContentRevision = surface.descriptor.contentRevision
  if (reload && surface.view.webContents.getURL()) {
    surface.view.webContents.reloadIgnoringCache()
    return
  }
  void surface.view.webContents.loadURL(surface.descriptor.url).catch(error => {
    sendWorkPreviewEvent(surface, 'work-preview.load-state', {
      previewId: surface.descriptor.previewId,
      status: 'failed',
      detail: error instanceof Error ? error.message : String(error),
    })
  })
}

function updateWorkPreviewSurface(descriptor: WorkPreviewDescriptor): {
  ok: boolean
  detail: string
  descriptor?: WorkPreviewDescriptor
} {
  const surface = workPreviewSurfaces.get(descriptor.previewId)
  if (!surface) return { ok: false, detail: 'The preview surface is not open.' }
  if (surface.descriptor.workItemId !== descriptor.workItemId) {
    return { ok: false, detail: 'A preview cannot be rebound to a different WorkItem.' }
  }
  if (descriptor.revision < surface.descriptor.revision) {
    return { ok: true, detail: 'Stale preview update ignored.', descriptor: projectedWorkPreviewDescriptor(surface) }
  }
  if (
    descriptor.revision === surface.descriptor.revision
    && descriptor.contentRevision < surface.descriptor.contentRevision
  ) {
    return { ok: true, detail: 'Stale preview content update ignored.', descriptor: projectedWorkPreviewDescriptor(surface) }
  }
  const nextUrl = descriptor.url ? normalizeWorkPreviewUrl(descriptor.url) : null
  if (descriptor.url && !nextUrl) return { ok: false, detail: 'Preview URL must use a loopback HTTP origin.' }
  const previous = surface.descriptor
  const resolvedDescriptor = { ...descriptor }
  const urlChanged = Boolean(descriptor.url && previous.url !== descriptor.url)
  surface.descriptor = resolvedDescriptor
  if (nextUrl) surface.allowedOrigin = nextUrl.origin
  surface.window.setTitle(resolvedDescriptor.title)
  if (surface.auipView && resolvedDescriptor.lifecycle === 'frozen') {
    // ``frozen`` is a Host fact emitted only after the exact AppSession ended
    // (including the managed surface receipt or a verified disconnect). It is
    // therefore the authority to release the retained AUIP child.
    releaseAttachedAuipView(surface)
    surface.presentationPhase = 'auip-ended'
  }
  reconcilePendingAuipHandoff(surface)
  reconcileAttachedAuipAuthority(surface)
  if (!surface.pendingAuip && !surface.auipView) {
    if (surface.presentationPhase === 'auip-closing' && resolvedDescriptor.lifecycle === 'frozen') {
      surface.presentationPhase = 'auip-ended'
    } else if (resolvedDescriptor.attemptId !== previous.attemptId) {
      // A newer Host Attempt is the only authority that thaws a completed
      // App surface back into Preview. The retained Preview child is reused.
      surface.presentationPhase = 'preview'
    }
  }
  reconcileWorkPreviewLifecycle(surface)
  sendWorkPreviewDescriptor(surface)
  if (workPreviewReloadAllowed(surface)) {
    if (urlChanged) loadWorkPreviewContent(surface)
    else if (resolvedDescriptor.contentRevision > surface.loadedContentRevision) {
      loadWorkPreviewContent(surface, true)
    }
  }
  return { ok: true, detail: '', descriptor: projectedWorkPreviewDescriptor(surface) }
}

function workPreviewShellUrl(previewId: string): string {
  const query = new URLSearchParams({ previewWindow: '1', previewId })
  if (isDev) return `http://localhost:5173?${query.toString()}`
  return `file://${path.join(__dirname, '..', 'renderer', 'index.html')}?${query.toString()}`
}

function createWorkPreviewSurface(descriptor: WorkPreviewDescriptor): {
  ok: boolean
  detail: string
  descriptor?: WorkPreviewDescriptor
} {
  const existing = workPreviewSurfaces.get(descriptor.previewId)
  if (existing) {
    const result = updateWorkPreviewSurface(descriptor)
    if (result.ok && !existing.window.isDestroyed()) {
      if (existing.window.isMinimized()) existing.window.restore()
      existing.window.show()
      existing.window.focus()
    }
    return result
  }
  const existingPreviewId = workPreviewIdsByWorkItem.get(descriptor.workItemId)
  if (existingPreviewId && existingPreviewId !== descriptor.previewId) {
    const bound = workPreviewSurfaces.get(existingPreviewId)
    bound?.window.show()
    bound?.window.focus()
    return { ok: false, detail: 'This WorkItem is already bound to another preview surface.' }
  }

  const parsedUrl = descriptor.url ? normalizeWorkPreviewUrl(descriptor.url) : null
  if (descriptor.url && !parsedUrl) return { ok: false, detail: 'Preview URL must use a loopback HTTP origin.' }
  const partitionToken = workPreviewPartitionToken(descriptor.previewId)
  const window = new BrowserWindow({
    width: 1024,
    height: 768,
    minWidth: 720,
    minHeight: 520,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    title: descriptor.title,
    icon: getAppIconPath(),
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload', 'index.mjs'),
      contextIsolation: true,
      nodeIntegration: false,
      // The outer shell is trusted Amadeus UI and uses the existing ESM
      // preload. The untrusted project remains in the separate sandboxed
      // WebContentsView below; keeping these trust domains distinct also
      // avoids granting preview content any renderer IPC capability.
      sandbox: false,
      webSecurity: true,
      partition: `work-preview-shell-${partitionToken}`,
    },
  })
  const view = new WebContentsView({
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      partition: `work-preview-content-${partitionToken}`,
    },
  })
  const surface: WorkPreviewSurface = {
    descriptor,
    allowedOrigin: parsedUrl?.origin || '',
    loadState: { status: descriptor.url ? 'loading' : 'idle', detail: '' },
    window,
    view,
    previewAttached: false,
    viewportBounds: null,
    auipView: null,
    auipHostSurfaceId: '',
    auipAppSessionId: '',
    auipAttemptId: '',
    auipArtifactRef: '',
    pendingAuip: null,
    presentationPhase: 'preview',
    loadedContentRevision: -1,
    nativeCloseFallback: null,
  }
  workPreviewSurfaces.set(descriptor.previewId, surface)
  workPreviewIdsByWorkItem.set(descriptor.workItemId, descriptor.previewId)
  if (!workPreviewLifecycleHidesPreview(descriptor.lifecycle)) {
    window.contentView.addChildView(view)
    surface.previewAttached = true
  }
  view.setBackgroundColor('#050708')
  window.setMenuBarVisibility(false)
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-attach-webview', event => event.preventDefault())
  window.webContents.on('will-navigate', (event, target) => {
    const expected = new URL(workPreviewShellUrl(descriptor.previewId))
    try {
      const destination = new URL(target)
      if (destination.origin !== expected.origin || destination.pathname !== expected.pathname) event.preventDefault()
    } catch {
      event.preventDefault()
    }
  })
  configureWorkPreviewSession(window.webContents.session)

  view.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  view.webContents.on('will-attach-webview', event => event.preventDefault())
  view.webContents.on('will-navigate', (event, target) => guardWorkPreviewNavigation(surface, target, event))
  view.webContents.on('will-redirect', (event, target) => guardWorkPreviewNavigation(surface, target, event))
  view.webContents.on('did-start-loading', () => {
    surface.loadState = { status: 'loading', detail: '' }
    sendWorkPreviewEvent(surface, 'work-preview.load-state', {
      previewId: descriptor.previewId,
      ...surface.loadState,
    })
  })
  view.webContents.on('did-stop-loading', () => {
    if (surface.loadState.status === 'failed') return
    surface.loadState = { status: 'loaded', detail: '' }
    sendWorkPreviewEvent(surface, 'work-preview.load-state', {
      previewId: descriptor.previewId,
      ...surface.loadState,
    })
  })
  view.webContents.on('did-fail-load', (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
    if (!isMainFrame || errorCode === -3) return
    surface.loadState = {
      status: 'failed',
      detail: `${errorDescription} (${errorCode}) ${validatedURL}`,
    }
    sendWorkPreviewEvent(surface, 'work-preview.load-state', {
      previewId: descriptor.previewId,
      ...surface.loadState,
    })
  })
  configureWorkPreviewSession(view.webContents.session)
  restrictWorkPreviewContentNetwork(view.webContents.session, surface)

  window.once('ready-to-show', () => window.show())
  window.on('close', event => {
    // A native close (Alt+F4) must traverse the trusted renderer so the Host
    // can reclaim its watcher and loopback server before this surface dies.
    // destroyWorkPreviewSurface removes the map entry before calling destroy,
    // so an acknowledged close is not intercepted here a second time.
    if (!workPreviewSurfaces.has(descriptor.previewId)) return
    event.preventDefault()
    if (surface.nativeCloseFallback) return
    sendWorkPreviewEvent(surface, 'work-preview.close-requested', {
      previewId: descriptor.previewId,
    })
    surface.nativeCloseFallback = setTimeout(() => {
      surface.nativeCloseFallback = null
      if (!workPreviewSurfaces.has(descriptor.previewId)) return
      // Native close must remain escapable even if the renderer/backend lane
      // is unavailable. Destroying an attached child makes its disconnect a
      // Host-observable AUIP fact instead of pretending the close succeeded.
      console.error('[work-preview] forcing native close after acknowledgement timeout')
      destroyWorkPreviewSurface(descriptor.previewId)
    }, 15_000)
  })
  window.on('closed', () => destroyWorkPreviewSurface(descriptor.previewId, false))
  window.on('page-title-updated', event => event.preventDefault())
  window.webContents.on('did-finish-load', () => {
    // The page can finish loading before the trusted shell has mounted its IPC
    // listeners. Replay current state once so READY never looks like WAITING.
    sendWorkPreviewDescriptor(surface)
    sendWorkPreviewEvent(surface, 'work-preview.load-state', {
      previewId: surface.descriptor.previewId,
      ...surface.loadState,
    })
  })
  void window.loadURL(workPreviewShellUrl(descriptor.previewId)).catch(error => {
    console.error('[work-preview] failed to load trusted shell:', error)
    destroyWorkPreviewSurface(descriptor.previewId)
  })
  loadWorkPreviewContent(surface)
  return { ok: true, detail: '', descriptor: projectedWorkPreviewDescriptor(surface) }
}

function closeAllWorkPreviewSurfaces(): void {
  for (const previewId of [...workPreviewSurfaces.keys()]) destroyWorkPreviewSurface(previewId)
}

type AuipLaunchPolicy = {
  entryUrl: string
  entryPath: string
  entryRoot: string
  webSocketUrl: string
}

function parseAuipLaunchPolicy(launchUrl: URL): AuipLaunchPolicy | null {
  try {
    if (launchUrl.protocol !== 'file:' || launchUrl.hostname !== '') return null
    const prefix = '#amadeus-auip='
    if (!launchUrl.hash.startsWith(prefix)) return null
    const encoded = launchUrl.hash.slice(prefix.length)
    const base64 = encoded.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - encoded.length % 4) % 4)
    const descriptor = JSON.parse(Buffer.from(base64, 'base64').toString('utf8')) as Record<string, unknown>
    if (descriptor.schema !== 'amadeus.auip/launch-v0') return null
    const webSocket = new URL(String(descriptor.webSocketUrl || ''))
    const loopback = ['127.0.0.1', 'localhost', '[::1]'].includes(webSocket.hostname.toLowerCase())
    if (
      !loopback
      || !['ws:', 'wss:'].includes(webSocket.protocol)
      || webSocket.pathname !== '/auip/ws'
      || webSocket.search
      || webSocket.hash
      || webSocket.username
      || webSocket.password
      || !webSocket.port
    ) return null
    const entry = new URL(launchUrl.toString())
    entry.hash = ''
    const entryPath = path.resolve(fileURLToPath(entry))
    if (!['.html', '.htm'].includes(path.extname(entryPath).toLowerCase())) return null
    return {
      entryUrl: entry.toString(),
      entryPath,
      entryRoot: path.dirname(entryPath),
      webSocketUrl: webSocket.toString(),
    }
  } catch {
    return null
  }
}

function pathIsWithin(root: string, candidate: string): boolean {
  const relative = path.relative(path.resolve(root), path.resolve(candidate))
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative))
}

function isAllowedAuipResource(policy: AuipLaunchPolicy, rawUrl: string): boolean {
  try {
    const target = new URL(rawUrl)
    if (target.protocol === 'data:' || target.protocol === 'blob:') return true
    if (target.protocol === 'file:' && target.hostname === '') {
      return pathIsWithin(policy.entryRoot, fileURLToPath(target))
    }
    if (target.protocol === 'ws:' || target.protocol === 'wss:') {
      return target.toString() === policy.webSocketUrl
    }
    return false
  } catch {
    return false
  }
}

function restrictAuipContentNetwork(targetSession: Electron.Session, policy: AuipLaunchPolicy): void {
  targetSession.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, callback) => {
    callback({ cancel: !isAllowedAuipResource(policy, details.url) })
  })
}

function guardAuipEntryNavigation(policy: AuipLaunchPolicy, target: string, event: Electron.Event): void {
  try {
    const destination = new URL(target)
    destination.hash = ''
    const sameEntry = destination.protocol === 'file:'
      && destination.hostname === ''
      && path.resolve(fileURLToPath(destination)).toLowerCase() === policy.entryPath.toLowerCase()
    if (!sameEntry) event.preventDefault()
  } catch {
    event.preventDefault()
  }
}

function settlePendingAuip(
  surface: WorkPreviewSurface,
  result: { ok: boolean; detail: string },
): void {
  const pending = surface.pendingAuip
  if (!pending || pending.settled) return
  pending.settled = true
  clearTimeout(pending.timeout)
  surface.pendingAuip = null
  if (!result.ok && !pending.view.webContents.isDestroyed()) {
    pending.view.webContents.close({ waitForBeforeUnload: false })
  }
  if (!result.ok) {
    surface.presentationPhase = surface.descriptor.lifecycle === 'frozen' ? 'auip-ended' : 'preview'
    publishWorkPreviewPresentation(surface, surface.presentationPhase, result.detail)
  }
  pending.resolve(result)
}

function commitPendingAuip(surface: WorkPreviewSurface, pending: PendingAuipHandoff): void {
  const descriptor = surface.descriptor
  if (surface.pendingAuip !== pending || pending.settled) return
  pending.settled = true
  clearTimeout(pending.timeout)
  surface.pendingAuip = null
  surface.auipView = pending.view
  surface.auipHostSurfaceId = descriptor.hostSurfaceId
  surface.auipAppSessionId = descriptor.appSessionId
  surface.auipAttemptId = descriptor.attemptId
  surface.auipArtifactRef = descriptor.artifactRef
  detachWorkPreviewView(surface)
  surface.window.contentView.addChildView(pending.view)
  if (surface.viewportBounds) pending.view.setBounds(surface.viewportBounds)
  auipAppSurfacesById.set(descriptor.hostSurfaceId, {
    kind: 'work-preview',
    previewId: descriptor.previewId,
    window: surface.window,
    view: pending.view,
    hostSurfaceId: descriptor.hostSurfaceId,
    appSessionId: descriptor.appSessionId,
    attemptId: descriptor.attemptId,
    artifactRef: descriptor.artifactRef,
  })
  publishWorkPreviewPresentation(surface, 'auip-attached')
  if (surface.window.isMinimized()) surface.window.restore()
  surface.window.show()
  surface.window.focus()
  pending.resolve({ ok: true, detail: '' })
}

function reconcilePendingAuipHandoff(surface: WorkPreviewSurface): void {
  const pending = surface.pendingAuip
  if (!pending || pending.settled) return
  const descriptor = surface.descriptor
  if (descriptor.attemptId !== pending.attemptId) {
    settlePendingAuip(surface, {
      ok: false,
      detail: 'Host changed the active Attempt before AUIP Attach completed.',
    })
    return
  }
  if (descriptor.revision <= pending.startRevision) return
  if (descriptor.lifecycle === 'attached') {
    const exactIdentity = descriptor.attemptId === pending.attemptId
      && descriptor.artifactRef === pending.artifactRef
      && descriptor.hostSurfaceId === pending.hostSurfaceId
      && Boolean(descriptor.appSessionId)
    if (!exactIdentity) {
      settlePendingAuip(surface, {
        ok: false,
        detail: 'Host attached a different AUIP Attempt, artifact, AppSession, or surface.',
      })
      return
    }
    if (pending.loaded) commitPendingAuip(surface, pending)
    return
  }
  if (descriptor.lifecycle !== 'handoff') {
    settlePendingAuip(surface, {
      ok: false,
      detail: `Host withdrew AUIP handoff with lifecycle ${descriptor.lifecycle}.`,
    })
  }
}

function reconcileAttachedAuipAuthority(surface: WorkPreviewSurface): void {
  if (!surface.auipView) return
  const descriptor = surface.descriptor
  const exactIdentity = descriptor.lifecycle === 'attached'
    && descriptor.attemptId === surface.auipAttemptId
    && descriptor.artifactRef === surface.auipArtifactRef
    && descriptor.hostSurfaceId === surface.auipHostSurfaceId
    && descriptor.appSessionId === surface.auipAppSessionId
  if (exactIdentity) {
    surface.presentationPhase = 'auip-attached'
    return
  }
  const detail = descriptor.lifecycle === 'frozen'
    ? 'Host ended this AppSession; waiting for the exact surface-close receipt.'
    : 'Host moved to a different Work lifecycle while the prior AppSession still owns this surface.'
  publishWorkPreviewPresentation(
    surface,
    descriptor.lifecycle === 'frozen' ? 'auip-closing' : 'auip-conflict',
    detail,
  )
}

async function waitForMatchingHostHandoff(
  surface: WorkPreviewSurface,
  hostSurfaceId: string,
): Promise<{ attemptId: string; artifactRef: string; revision: number }> {
  const initialAttemptId = surface.descriptor.attemptId
  const deadline = Date.now() + 4000
  while (Date.now() < deadline) {
    if (
      surface.window.isDestroyed()
      || workPreviewSurfaces.get(surface.descriptor.previewId) !== surface
    ) throw new Error('The Work Preview closed before AUIP handoff was authorized.')
    const descriptor = surface.descriptor
    if (descriptor.attemptId !== initialAttemptId) {
      throw new Error('Host changed the active Attempt before AUIP handoff was authorized.')
    }
    if (
      descriptor.lifecycle === 'handoff'
      && descriptor.artifactRef
      && descriptor.hostSurfaceId === hostSurfaceId
    ) {
      return {
        attemptId: descriptor.attemptId,
        artifactRef: descriptor.artifactRef,
        revision: descriptor.revision,
      }
    }
    await new Promise(resolve => setTimeout(resolve, 25))
  }
  throw new Error('Host did not authorize this exact AUIP handoff.')
}

async function waitForWorkPreviewSurface(workItemId: string): Promise<WorkPreviewSurface | undefined> {
  if (!workItemId) return undefined
  const deadline = Date.now() + 4000
  while (Date.now() < deadline) {
    const previewId = workPreviewIdsByWorkItem.get(workItemId)
    const surface = previewId ? workPreviewSurfaces.get(previewId) : undefined
    if (surface?.descriptor.workItemId === workItemId) return surface
    await new Promise(resolve => setTimeout(resolve, 25))
  }
  return undefined
}

async function openAuipInWorkPreview(
  surface: WorkPreviewSurface,
  launchUrl: URL,
  hostSurfaceId: string,
): Promise<{ ok: boolean; detail: string }> {
  if (surface.auipView || surface.pendingAuip) {
    return { ok: false, detail: 'This Work Preview is already attaching an AUIP application.' }
  }
  if (!hostSurfaceId) return { ok: false, detail: 'Missing AUIP host surface identity.' }
  const policy = parseAuipLaunchPolicy(launchUrl)
  if (!policy) return { ok: false, detail: 'Invalid or unsafe AUIP launch descriptor.' }
  let binding: { attemptId: string; artifactRef: string; revision: number }
  try {
    binding = await waitForMatchingHostHandoff(surface, hostSurfaceId)
  } catch (error) {
    return { ok: false, detail: error instanceof Error ? error.message : String(error) }
  }

  const partitionToken = workPreviewPartitionToken(`${surface.descriptor.previewId}-auip`)
  const appView = new WebContentsView({
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      partition: `auip-work-preview-${partitionToken}`,
    },
  })
  appView.setBackgroundColor('#050708')
  appView.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  appView.webContents.on('will-attach-webview', event => event.preventDefault())
  appView.webContents.on('will-navigate', (event, target) => {
    guardAuipEntryNavigation(policy, target, event)
  })
  appView.webContents.on('will-redirect', (event, target) => {
    guardAuipEntryNavigation(policy, target, event)
  })
  configureWorkPreviewSession(appView.webContents.session)
  restrictAuipContentNetwork(appView.webContents.session, policy)

  return await new Promise(resolve => {
    const pending: PendingAuipHandoff = {
      view: appView,
      hostSurfaceId,
      attemptId: binding.attemptId,
      artifactRef: binding.artifactRef,
      startRevision: binding.revision,
      loaded: false,
      settled: false,
      timeout: setTimeout(() => {
        settlePendingAuip(surface, {
          ok: false,
          detail: 'Host did not commit AUIP Attach before the handoff deadline.',
        })
      }, 65_000),
      resolve,
    }
    surface.pendingAuip = pending
    publishWorkPreviewPresentation(surface, 'auip-preloading')
    // Loading is presentation readiness only. It never commits authority;
    // reconcilePendingAuipHandoff requires a newer exact Host descriptor.
    void appView.webContents.loadURL(launchUrl.toString()).then(() => {
      if (surface.pendingAuip !== pending || pending.settled) return
      pending.loaded = true
      reconcilePendingAuipHandoff(surface)
    }).catch(error => {
      settlePendingAuip(surface, {
        ok: false,
        detail: error instanceof Error ? error.message : String(error),
      })
    })
  })
}

// IPC.

function isTrustedBackendRenderer(sender: Electron.WebContents): boolean {
  // Work Preview's outer shell is Amadeus-owned and uses the normal preload;
  // its sandboxed project WebContentsView is deliberately absent here.
  return isTrustedAmadeusRenderer(sender)
    || sender === workGlowWindow?.webContents
}

ipcMain.handle('get-backend-connection', (event) => {
  if (!isTrustedBackendRenderer(event.sender) || !backendOwned) return null
  return {
    url: BACKEND_WS,
    protocols: [...BACKEND_PROTOCOLS],
    instanceNonce: BACKEND_INSTANCE_NONCE,
    authScheme: BACKEND_AUTH_SCHEME,
  }
})
ipcMain.handle('restart-backend', async (event) => {
  if (!isTrustedBackendRenderer(event.sender)) return false
  try {
    await stopBackend()
    await startBackend()
    return true
  } catch (error) {
    console.error('[electron] backend restart failed', error)
    return false
  }
})
ipcMain.handle('desktop-settings.get', (event) => {
  if (!isTrustedBackendRenderer(event.sender)) return null
  return desktopSettings.snapshot(process.env)
})
ipcMain.handle('desktop-settings.update', (event, update: DesktopSettingsUpdate) => {
  if (!isTrustedBackendRenderer(event.sender)) {
    return { ok: false, error: 'Untrusted desktop settings requester.' }
  }
  try {
    return { ok: true, settings: desktopSettings.update(process.env, update || {}) }
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    }
  }
})
ipcMain.handle('mcp-connections.upsert', (event, update: McpConnectionUpdate) => {
  if (!isTrustedBackendRenderer(event.sender)) {
    return { ok: false, error: 'Untrusted MCP connection requester.' }
  }
  try {
    return { ok: true, settings: desktopSettings.upsertMcpConnection(process.env, update) }
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    }
  }
})
ipcMain.handle('mcp-connections.remove', (event, connectionId: string) => {
  if (!isTrustedBackendRenderer(event.sender)) {
    return { ok: false, error: 'Untrusted MCP connection requester.' }
  }
  try {
    return { ok: true, settings: desktopSettings.removeMcpConnection(process.env, connectionId) }
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    }
  }
})
ipcMain.handle('chat-avatars.get', (event) => {
  if (!isTrustedBackendRenderer(event.sender)) return null
  return chatAvatars.snapshot()
})
ipcMain.handle('chat-avatars.select', async (event, role: ChatAvatarRole) => {
  if (!isTrustedBackendRenderer(event.sender)) {
    return { ok: false, cancelled: false, error: 'Untrusted chat avatar requester.' }
  }
  const options: Electron.OpenDialogOptions = {
    title: role === 'assistant' ? 'Choose Kurisu avatar' : 'Choose your avatar',
    buttonLabel: 'Use this image',
    properties: ['openFile'],
    filters: [{ name: 'Images', extensions: ['png', 'jpg', 'jpeg', 'webp', 'bmp'] }],
  }
  const result = mainWindow
    ? await dialog.showOpenDialog(mainWindow, options)
    : await dialog.showOpenDialog(options)
  if (result.canceled || !result.filePaths[0]) {
    return { ok: true, cancelled: true, avatars: chatAvatars.snapshot() }
  }
  try {
    return {
      ok: true,
      cancelled: false,
      avatars: chatAvatars.save(role, result.filePaths[0]),
    }
  } catch (error) {
    return {
      ok: false,
      cancelled: false,
      error: error instanceof Error ? error.message : String(error),
    }
  }
})
ipcMain.handle('chat-avatars.clear', (event, role: ChatAvatarRole) => {
  if (!isTrustedBackendRenderer(event.sender)) {
    return { ok: false, error: 'Untrusted chat avatar requester.' }
  }
  try {
    return { ok: true, avatars: chatAvatars.clear(role) }
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    }
  }
})
ipcMain.handle('main-window.focus', (event) => {
  if (!isTrustedBackendRenderer(event.sender)) return false
  if (!mainWindow) return false
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.show()
  mainWindow.focus()
  return true
})
ipcMain.handle('project-directory.select', async (event) => {
  if (!isTrustedAmadeusRenderer(event.sender)) {
    return { ok: false, cancelled: false, path: '', detail: 'Untrusted Project directory requester.' }
  }
  const options: Electron.OpenDialogOptions = {
    title: 'New Project',
    buttonLabel: 'Use this folder',
    properties: ['openDirectory', 'createDirectory'],
  }
  const result = mainWindow
    ? await dialog.showOpenDialog(mainWindow, options)
    : await dialog.showOpenDialog(options)
  const selectedPath = String(result.filePaths[0] || '')
  return {
    ok: !result.canceled && Boolean(selectedPath),
    cancelled: result.canceled || !selectedPath,
    path: selectedPath,
    detail: result.canceled ? '' : selectedPath ? '' : 'No Project directory was selected.',
  }
})
ipcMain.handle('work-preview.open', (event, rawDescriptor: unknown) => {
  if (!isTrustedAmadeusRenderer(event.sender)) return { ok: false, detail: 'Untrusted preview opener.' }
  const descriptor = normalizeWorkPreviewDescriptor(rawDescriptor)
  if (!descriptor) return { ok: false, detail: 'Invalid work preview descriptor.' }
  return createWorkPreviewSurface(descriptor)
})
ipcMain.handle('work-preview.update', (event, rawDescriptor: unknown) => {
  if (!isTrustedAmadeusRenderer(event.sender)) return { ok: false, detail: 'Untrusted preview updater.' }
  const descriptor = normalizeWorkPreviewDescriptor(rawDescriptor)
  if (!descriptor) return { ok: false, detail: 'Invalid work preview descriptor.' }
  return updateWorkPreviewSurface(descriptor)
})
ipcMain.handle('work-preview.get', (event, rawPreviewId: unknown) => {
  const previewId = String(rawPreviewId || '').trim()
  if (!isTrustedAmadeusRenderer(event.sender)) return { ok: false, detail: 'Untrusted preview reader.' }
  const surface = workPreviewSurfaces.get(previewId)
  return surface
    ? { ok: true, detail: '', descriptor: projectedWorkPreviewDescriptor(surface) }
    : { ok: false, detail: 'The preview surface is not open.' }
})
ipcMain.handle('work-preview.reload', (event, rawPreviewId: unknown) => {
  const previewId = String(rawPreviewId || '').trim()
  if (!senderOwnsWorkPreview(event.sender, previewId) && event.sender !== mainWindow?.webContents) {
    return { ok: false, detail: 'Untrusted preview reload.' }
  }
  const surface = workPreviewSurfaces.get(previewId)
  if (!surface) return { ok: false, detail: 'The preview surface is not open.' }
  if (!workPreviewReloadAllowed(surface)) {
    return { ok: false, detail: `Reload is unavailable while Preview is ${surface.descriptor.lifecycle}.` }
  }
  loadWorkPreviewContent(surface, true)
  return { ok: true, detail: '' }
})
ipcMain.handle('work-preview.close', (event, rawPreviewId: unknown) => {
  const previewId = String(rawPreviewId || '').trim()
  if (!senderOwnsWorkPreview(event.sender, previewId) && event.sender !== mainWindow?.webContents) {
    return { ok: false, detail: 'Untrusted preview closer.' }
  }
  const surface = workPreviewSurfaces.get(previewId)
  if (!surface) return { ok: false, detail: 'The preview surface is not open.' }
  const cancellableDetachedPreload = Boolean(
    surface.pendingAuip
    && surface.presentationPhase === 'auip-preloading'
    && !surface.descriptor.appSessionId
    && surface.descriptor.lifecycle !== 'attached',
  )
  if (
    (surface.pendingAuip && !cancellableDetachedPreload)
    || surface.auipView
    || (
      !cancellableDetachedPreload
      && ['auip-preloading', 'auip-attached', 'auip-closing', 'auip-conflict']
        .includes(surface.presentationPhase)
    )
  ) {
    return {
      ok: false,
      detail: 'Leave the exact AppSession and wait for its surface-close receipt before closing this window.',
    }
  }
  return destroyWorkPreviewSurface(previewId)
    ? { ok: true, detail: '' }
    : { ok: false, detail: 'The preview surface is not open.' }
})
ipcMain.handle('work-preview.set-bounds', (event, rawPreviewId: unknown, rawBounds: unknown) => {
  const previewId = String(rawPreviewId || '').trim()
  if (!senderOwnsWorkPreview(event.sender, previewId)) return false
  const surface = workPreviewSurfaces.get(previewId)
  if (!surface || surface.window.isDestroyed()) return false
  const source = recordValue(rawBounds)
  const content = surface.window.getContentBounds()
  const x = Math.max(0, Math.round(Number(source.x || 0)))
  const y = Math.max(0, Math.round(Number(source.y || 0)))
  const width = Math.min(content.width - x, Math.max(0, Math.round(Number(source.width || 0))))
  const height = Math.min(content.height - y, Math.max(0, Math.round(Number(source.height || 0))))
  if (![x, y, width, height].every(Number.isFinite) || width < 32 || height < 32) return false
  surface.viewportBounds = { x, y, width, height }
  if (surface.auipView && !surface.auipView.webContents.isDestroyed()) {
    surface.auipView.setBounds(surface.viewportBounds)
  } else if (surface.previewAttached && !surface.view.webContents.isDestroyed()) {
    surface.view.setBounds(surface.viewportBounds)
  }
  return true
})
ipcMain.handle('electron-slice.open', (event, bridge: unknown) => {
  if (!isMainRenderer(event.sender)) return false
  return createElectronSliceWindow(bridge)
})
ipcMain.handle('electron-slice.close', (event) => {
  if (!isMainRenderer(event.sender)) return false
  closeElectronSliceWindow()
  return true
})
ipcMain.handle('electron-slice.set-shape', (event, boundsList: Electron.Rectangle[]) => {
  const window = electronSliceWindow
  if (!window || window.isDestroyed() || event.sender !== window.webContents) return false
  if (!Array.isArray(boundsList)) return false
  const firstCommit = electronSliceShape === null
  const bounds = window.getContentBounds()
  electronSliceShape = boundsList.map(item => {
    const left = Math.max(0, Math.round(Number(item?.x || 0)))
    const top = Math.max(0, Math.round(Number(item?.y || 0)))
    const right = Math.min(bounds.width, left + Math.max(0, Math.round(Number(item?.width || 0))))
    const bottom = Math.min(bounds.height, top + Math.max(0, Math.round(Number(item?.height || 0))))
    return { x: left, y: top, width: right - left, height: bottom - top }
  }).filter(item => item.width > 0 && item.height > 0)
  if (firstCommit) console.log(`[electron-slice] renderer shape committed: ${electronSliceShape.length} region(s)`)
  reconcileElectronSliceReadiness(window)
  return true
})
ipcMain.handle('auip-app.open', async (
  event,
  rawUrl: string,
  rawSurfaceId?: string,
  rawWorkItemId?: string,
) => {
  let appWindow: BrowserWindow | null = null
  try {
    if (!isPrimaryDesktopRenderer(event.sender)) {
      return { ok: false, detail: 'Untrusted AUIP surface opener.' }
    }
    const hostSurfaceId = String(rawSurfaceId || '').trim()
    const workItemId = String(rawWorkItemId || '').trim()
    if (!hostSurfaceId) return { ok: false, detail: 'Missing AUIP host surface identity.' }
    if (hostSurfaceId && auipAppSurfacesById.has(hostSurfaceId)) {
      return { ok: false, detail: 'The AUIP host surface is already open.' }
    }
    const launchUrl = new URL(String(rawUrl || ''))
    const policy = parseAuipLaunchPolicy(launchUrl)
    if (!policy) {
      return { ok: false, detail: 'Invalid AUIP launch descriptor.' }
    }
    const previewSurface = await waitForWorkPreviewSurface(workItemId)
    if (previewSurface) {
      return await openAuipInWorkPreview(previewSurface, launchUrl, hostSurfaceId)
    }
    if (workItemId) {
      // Every current Host prepare owns a WorkItem and synchronously ensures
      // its unified App Surface before returning. Falling back here could
      // race a delayed open event and create two windows for one authority.
      return {
        ok: false,
        detail: 'The Host did not deliver the exact WorkItem App Surface.',
      }
    }
    // A file URL with an Attach descriptor cannot be opened with openPath
    // (which discards the fragment), and shell.openExternal rejects file URLs
    // on Windows.  Keep the application in its own sandboxed web surface: it
    // receives no preload, Node API, or Amadeus renderer privileges.  The
    // restricted AUIP WebSocket remains its only host capability. This branch
    // exists only for legacy callers that predate WorkItem-bound prepare.
    appWindow = new BrowserWindow({
      width: 1100,
      height: 800,
      minWidth: 480,
      minHeight: 360,
      show: false,
      autoHideMenuBar: true,
      title: '',
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: true,
        partition: `auip-app-${workPreviewPartitionToken(hostSurfaceId || 'standalone')}`,
      },
    })
    appWindow.setMenuBarVisibility(false)
    appWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    appWindow.webContents.on('will-attach-webview', event => event.preventDefault())
    appWindow.webContents.on('will-navigate', (navigationEvent, url) => {
      guardAuipEntryNavigation(policy, url, navigationEvent)
    })
    appWindow.webContents.on('will-redirect', (navigationEvent, url) => {
      guardAuipEntryNavigation(policy, url, navigationEvent)
    })
    configureWorkPreviewSession(appWindow.webContents.session)
    restrictAuipContentNetwork(appWindow.webContents.session, policy)
    auipAppWindows.add(appWindow)
    if (hostSurfaceId) {
      auipAppSurfacesById.set(hostSurfaceId, {
        kind: 'window',
        window: appWindow,
        hostSurfaceId,
        appSessionId: '',
      })
    }
    appWindow.on('closed', () => {
      auipAppWindows.delete(appWindow as BrowserWindow)
      const hosted = hostSurfaceId ? auipAppSurfacesById.get(hostSurfaceId) : undefined
      if (hosted?.kind === 'window' && hosted.window === appWindow) {
        auipAppSurfacesById.delete(hostSurfaceId)
      }
    })
    await appWindow.loadURL(launchUrl.toString())
    appWindow.show()
    return { ok: true, detail: '' }
  } catch (error) {
    console.error('[electron] AUIP app launch failed:', error)
    if (appWindow && !appWindow.isDestroyed()) appWindow.destroy()
    return {
      ok: false,
      detail: error instanceof Error ? error.message : String(error),
    }
  }
})
ipcMain.handle('auip-app.close', async (
  event,
  rawSurfaceId: string,
  rawAppSessionId?: string,
) => {
  if (!isPrimaryDesktopRenderer(event.sender)) {
    return { ok: false, status: 'failed', detail: 'Untrusted AUIP surface closer.' }
  }
  const hostSurfaceId = String(rawSurfaceId || '').trim()
  const appSessionId = String(rawAppSessionId || '').trim()
  if (!hostSurfaceId) return { ok: false, status: 'failed', detail: 'Missing host surface id.' }
  let hosted = auipAppSurfacesById.get(hostSurfaceId)
  if (!hosted && appSessionId) {
    // A very short AppSession can request closure after Host registration but
    // before the detached view finishes loading. The exact Host pair is still
    // sufficient to cancel that pending presentation and acknowledge closure.
    const pendingSurface = [...workPreviewSurfaces.values()].find(surface => (
      surface.pendingAuip?.hostSurfaceId === hostSurfaceId
      && (
        !surface.descriptor.appSessionId
        || surface.descriptor.appSessionId === appSessionId
      )
    ))
    if (pendingSurface?.pendingAuip) {
      const pending = pendingSurface.pendingAuip
      pending.settled = true
      clearTimeout(pending.timeout)
      pendingSurface.pendingAuip = null
      if (!pending.view.webContents.isDestroyed()) {
        pending.view.webContents.close({ waitForBeforeUnload: false })
      }
      pendingSurface.presentationPhase = 'auip-closing'
      publishWorkPreviewPresentation(pendingSurface, 'auip-closing')
      pending.resolve({ ok: false, detail: 'AppSession closed before its presentation committed.' })
      return { ok: true, status: 'closed', detail: '' }
    }
    hosted = auipAppSurfacesById.get(hostSurfaceId)
  }
  if (!hosted || hosted.window.isDestroyed()) {
    auipAppSurfacesById.delete(hostSurfaceId)
    return { ok: false, status: 'not_found', detail: 'The AUIP host surface is not open.' }
  }
  if (hosted.hostSurfaceId !== hostSurfaceId) {
    return { ok: false, status: 'failed', detail: 'AUIP surface identity mismatch.' }
  }
  if (appSessionId && hosted.appSessionId && hosted.appSessionId !== appSessionId) {
    return { ok: false, status: 'failed', detail: 'AUIP AppSession identity mismatch.' }
  }
  // This is a Host-owned sandboxed surface, not an arbitrary external process.
  // Destroying it avoids an untrusted beforeunload handler defeating an
  // explicit leave request. AppSession truth has already moved to closed.
  auipAppSurfacesById.delete(hostSurfaceId)
  if (hosted.kind === 'window') {
    hosted.window.destroy()
  } else {
    const surface = workPreviewSurfaces.get(hosted.previewId)
    if (surface && surface.auipView === hosted.view) {
      releaseAttachedAuipView(surface)
      const nextPhase: WorkPreviewPresentationPhase = surface.descriptor.lifecycle === 'frozen'
        ? 'auip-ended'
        : surface.descriptor.lifecycle === 'attached'
          ? 'auip-closing'
          : 'preview'
      publishWorkPreviewPresentation(surface, nextPhase)
      if (nextPhase === 'preview' && surface.descriptor.url) {
        if (!surface.view.webContents.getURL()) loadWorkPreviewContent(surface)
        else if (surface.loadedContentRevision < surface.descriptor.contentRevision) {
          loadWorkPreviewContent(surface, true)
        }
      }
    } else if (!hosted.view.webContents.isDestroyed()) {
      hosted.view.webContents.close({ waitForBeforeUnload: false })
    }
  }
  return { ok: true, status: 'closed', detail: '' }
})
ipcMain.handle('work-overlay.open', (event) => {
  if (!isMainRenderer(event.sender)) return false
  createWorkOverlayWindow()
  return true
})
ipcMain.handle('work-overlay.close', (event) => {
  if (!isPrimaryDesktopRenderer(event.sender)) return false
  closeWorkOverlayWindow()
  return true
})
ipcMain.handle('work-overlay.set-mouse-ignore', (event, ignore: boolean) => {
  if (!isWorkPanelRenderer(event.sender)) return false
  if (!workPanelWindow) return false
  setWorkOverlayMousePassthrough(Boolean(ignore))
  return true
})
ipcMain.handle('work-overlay.set-panel-bounds', (event, bounds: Electron.Rectangle) => {
  if (!isWorkPanelRenderer(event.sender)) return false
  if (!bounds) return false
  const display = screen.getPrimaryDisplay().bounds
  const next = {
    x: Math.round(display.x + Number(bounds.x || 0)),
    y: Math.round(display.y + Number(bounds.y || 0)),
    width: Math.round(Number(bounds.width || 0)),
    height: Math.round(Number(bounds.height || 0)),
  }
  if (next.width <= 0 || next.height <= 0) return false
  workOverlayPanelBounds = next
  workOverlayHitRegions = [next]
  return true
})
ipcMain.handle('work-overlay.set-hit-regions', (event, boundsList: Electron.Rectangle[]) => {
  if (!isWorkPanelRenderer(event.sender)) return false
  if (!Array.isArray(boundsList)) return false
  const display = screen.getPrimaryDisplay().bounds
  const next = boundsList
    .map(bounds => ({
      x: Math.round(display.x + Number(bounds.x || 0)),
      y: Math.round(display.y + Number(bounds.y || 0)),
      width: Math.round(Number(bounds.width || 0)),
      height: Math.round(Number(bounds.height || 0)),
    }))
    .filter(bounds => bounds.width > 0 && bounds.height > 0)
  if (next.length === 0) return false
  workOverlayHitRegions = next
  workOverlayPanelBounds = next[0]
  return true
})

// app lifecycle.

const gotSingleInstanceLock = app.requestSingleInstanceLock()
if (!gotSingleInstanceLock) {
  app.quit()
}

app.on('second-instance', (_event, commandLine) => {
  if (wantsWorkOverlay(commandLine)) {
    createWorkOverlayWindow()
    return
  }
  if (!mainWindow) return
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.focus()
})

app.whenReady().then(async () => {
  try {
    await startBackend()
  } catch (error) {
    console.error('[electron] backend failed to become ready', error)
  }
  createWindow()
  if (wantsWorkOverlay()) createWorkOverlayWindow()
  screen.on('display-metrics-changed', updateElectronSliceBounds)
  screen.on('display-added', updateElectronSliceBounds)
  screen.on('display-removed', updateElectronSliceBounds)

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  app.quit()
})

app.on('before-quit', (event) => {
  closeElectronSliceWindow()
  closeWorkOverlayWindow()
  closeAllWorkPreviewSurfaces()
  for (const appWindow of auipAppWindows) appWindow.close()
  auipAppWindows.clear()
  auipAppSurfacesById.clear()
  if (quittingAfterBackendStop || !pythonProcess) return
  event.preventDefault()
  quittingAfterBackendStop = true
  void stopBackend().finally(() => {
    app.quit()
  })
})
