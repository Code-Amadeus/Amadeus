import { BrowserWindow, ipcMain, screen, type WebContents } from 'electron'
import fs from 'node:fs'
import path from 'node:path'
import { clampPanel, dockPanel, type Rect } from './companionPanelLayout.js'
import { readCompanionPortraits } from './companionPortraits.js'

type Bridge = { assetPort: number; bridgePort: number; assetVersion: string }
type Options = {
  userDataDir: string; preload: string; portraitCacheDir: string
  bridge: () => Bridge | null
  target: (workItemId: string) => BrowserWindow | null
  slice: () => (WebContents | null | undefined)[] | WebContents | null | undefined
}
const sameRect = (a: Rect, b: Rect) => ['x', 'y', 'width', 'height'].every(k => a[k as keyof Rect] === b[k as keyof Rect])

/** Owns the card's placement and forwards input controls to the existing Host. */
export class CompanionPanel {
  private window: BrowserWindow | null = null
  private opening = false
  private workItemId = ''
  private bridge: Bridge | null = null
  private game: BrowserWindow | null = null
  private originalGame: Rect | null = null
  private assignedGame: Rect | null = null
  private assignedPanel: Rect | null = null
  private placing = false
  private gameMinimum: [number, number] | null = null
  private gameMaximized = false
  private removeGameListeners: (() => void) | null = null
  private presentationQueue = Promise.resolve()
  private closeTask: Promise<boolean> | null = null

  constructor(private options: Options) {
    const owns = (sender: WebContents) => sender === this.window?.webContents
    const isSliceSender = (sender: WebContents) => this.slices().some(slice => slice === sender)
    ipcMain.handle('companion.toggle', (event, id: unknown) => {
      if (!isSliceSender(event.sender) || event.senderFrame !== event.sender.mainFrame) return false
      return this.toggle(typeof id === 'string' ? id : '')
    })
    ipcMain.handle('companion.state', event => isSliceSender(event.sender) ? Boolean(this.window) : false)
    ipcMain.handle('companion.portraits', event => owns(event.sender)
      ? readCompanionPortraits(options.portraitCacheDir) : {})
    ipcMain.handle('companion.close', event => owns(event.sender) ? this.close() : false)
    ipcMain.handle('companion.input', async (event, action: unknown) => {
      if (!owns(event.sender) || event.senderFrame !== event.sender.mainFrame || !this.bridge
        || typeof action !== 'string' || !['status', 'voice_start', 'voice_stop', 'vision_toggle'].includes(action)) {
        return { ok: false, error: 'input_control_unavailable' }
      }
      try {
        return await this.requestBridge(this.bridge, 'chat-action', { action }, action === 'voice_start' ? 65000 : 12000)
      } catch (error) { return { ok: false, error: String(error instanceof Error ? error.message : error) } }
    })
    ipcMain.handle('companion.connected', (event, connected: unknown) => {
      if (!owns(event.sender)) return false
      // A disconnected card must never leave the original display hidden.
      return this.setSuppressed(connected === true)
    })
    ipcMain.handle('companion.fit-content', (event, height: unknown) => {
      if (!owns(event.sender) || event.senderFrame !== event.sender.mainFrame
        || typeof height !== 'number' || !Number.isFinite(height) || height < 226) return false
      return this.fitContent(height)
    })
  }

  private slices(): WebContents[] {
    const raw = this.options.slice()
    const list = Array.isArray(raw) ? raw : [raw]
    return list.filter((item): item is WebContents => Boolean(item && !item.isDestroyed()))
  }

  private publish() {
    const open = Boolean(this.window)
    for (const slice of this.slices()) {
      slice.send('companion.state', open)
    }
  }

  private setSuppressed(active: boolean): Promise<boolean> {
    const bridge = this.bridge
    const apply = async () => {
      if (!bridge) return false
      try {
        await this.requestBridge(bridge, 'canvas-action', { target: 'presentation', action: 'companion', active })
        return true
      } catch (error) {
        console.error('[companion] presentation update failed', error)
        return false
      }
    }
    const result = this.presentationQueue.then(apply)
    this.presentationQueue = result.then(() => {})
    return result
  }

  private async requestBridge(bridge: Bridge, route: 'canvas-action' | 'chat-action', payload: Record<string, unknown>, timeout = 3000): Promise<Record<string, unknown>> {
    const info = await fetch(`http://127.0.0.1:${bridge.assetPort}/wallpaper/bridge-info`, { signal: AbortSignal.timeout(3000) })
    const descriptor = await info.json() as { bridgeToken?: string }
    if (!info.ok || !descriptor.bridgeToken) throw new Error('Presentation bridge unavailable')
    const response = await fetch(`http://127.0.0.1:${bridge.bridgePort}/wallpaper/${route}`, {
      method: 'POST', signal: AbortSignal.timeout(timeout),
      headers: { 'Content-Type': 'application/json', 'X-Amadeus-Bridge-Token': descriptor.bridgeToken },
      body: JSON.stringify(payload),
    })
    const result = await response.json() as Record<string, unknown>
    if (!response.ok || (result.ok !== true && result.status !== 'ok')) throw new Error(String(result.error || 'Companion action rejected'))
    return result
  }

  async toggle(workItemId: string): Promise<boolean> {
    if (this.opening) return Boolean(this.window)
    if (this.window) { await this.close(); return false }
    const bridge = this.options.bridge()
    if (!bridge) throw new Error('Start the Electron Slice before opening the companion.')
    this.opening = true
    this.bridge = bridge
    this.workItemId = workItemId
    try {
      const area = screen.getDisplayNearestPoint(screen.getCursorScreenPoint()).workArea
      let bounds: Rect = { x: area.x + area.width - 486, y: area.y + 60, width: 470, height: 226 }
      try {
        const saved = JSON.parse(fs.readFileSync(this.positionFile(), 'utf8'))
        if (['x', 'y', 'width', 'height'].every(key => Number.isFinite(saved[key]))
          && saved.width >= 300 && saved.height >= 226) bounds = saved
      } catch { /* first use */ }
      bounds = clampPanel(bounds, area)
      const window = new BrowserWindow({
        ...bounds, minWidth: 300, minHeight: 226, frame: false, transparent: true,
        backgroundColor: '#00000000', show: false, alwaysOnTop: true, skipTaskbar: true,
        title: 'Amadeus · Companion',
        webPreferences: { preload: this.options.preload, sandbox: true, contextIsolation: true, nodeIntegration: false, backgroundThrottling: true },
      })
      this.window = window
      const url = `http://127.0.0.1:${bridge.assetPort}/render/web/companion_panel.html?bridgePort=${bridge.bridgePort}&v=${encodeURIComponent(bridge.assetVersion)}`
      window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
      window.webContents.on('will-attach-webview', event => event.preventDefault())
      window.webContents.on('will-navigate', (event, target) => { if (target !== url) event.preventDefault() })
      window.webContents.on('render-process-gone', () => { void this.close() })
      window.on('moved', () => {
        if (!this.placing && this.assignedPanel && !sameRect(window.getBounds(), this.assignedPanel)) this.undock()
        this.savePosition()
      })
      window.on('resized', () => { if (this.game) this.place(); this.savePosition() })
      window.on('closed', () => {
        if (this.window !== window) return
        this.undock()
        this.window = null
        void this.setSuppressed(false)
        this.publish()
      })
      await window.loadURL(url)
      if (window.isDestroyed()) return false
      this.dock(this.options.target(workItemId))
      window.showInactive()
      this.publish()
      return true
    } catch (error) {
      await this.close()
      throw error
    } finally { this.opening = false }
  }

  attachPreview(window: BrowserWindow, workItemId: string) {
    if (this.window && this.workItemId === workItemId && !this.game) this.dock(window)
  }

  private fitContent(height: number): boolean {
    if (!this.window || this.window.isDestroyed()) return false
    const before = this.window.getBounds()
    const area = screen.getDisplayMatching(before).workArea
    const bounds = clampPanel({ ...before, height: Math.ceil(height) }, area)
    if (sameRect(before, bounds)) return true
    // Content growth can move the card into the work area without detaching it.
    this.placing = true
    try { this.window.setBounds(bounds) } finally { this.placing = false }
    if (this.game) this.place()
    this.savePosition()
    return true
  }

  private positionFile() { return path.join(this.options.userDataDir, 'companion-position.json') }
  private savePosition() {
    if (!this.window || this.window.isDestroyed()) return
    try { fs.writeFileSync(this.positionFile(), JSON.stringify(this.window.getBounds())) } catch { /* layout persistence is optional */ }
  }

  private dock(game: BrowserWindow | null) {
    this.undock()
    if (!game || game.isDestroyed() || !this.window) return
    this.gameMaximized = game.isMaximized()
    if (this.gameMaximized) game.unmaximize()
    this.game = game
    const [minWidth, minHeight] = game.getMinimumSize()
    this.gameMinimum = [minWidth, minHeight]
    this.originalGame = game.getBounds()
    const place = () => this.place()
    const leave = () => this.undock()
    game.on('move', place)
    game.on('resize', place)
    game.on('closed', leave)
    this.removeGameListeners = () => { game.removeListener('move', place); game.removeListener('resize', place); game.removeListener('closed', leave) }
    this.place()
  }

  private place() {
    if (this.placing || !this.game || this.game.isDestroyed() || !this.window || this.window.isDestroyed()) return
    this.placing = true
    try {
      const bounds = this.game.getBounds()
      if (this.assignedGame && !sameRect(bounds, this.assignedGame)) {
        // A later user move/resize becomes the new baseline for restoration.
        this.originalGame = bounds
        this.gameMaximized = this.game.isMaximized()
      }
      const panel = this.window.getBounds()
      const placement = dockPanel(bounds, screen.getDisplayMatching(bounds).workArea, panel.width, panel.height)
      this.assignedPanel = placement.panel
      if (!sameRect(bounds, placement.game)) {
        this.assignedGame = placement.game
        if (this.gameMinimum) this.game.setMinimumSize(Math.min(this.gameMinimum[0], placement.game.width), Math.min(this.gameMinimum[1], placement.game.height))
        this.game.setBounds(placement.game)
      }
      if (!sameRect(panel, placement.panel)) this.window.setBounds(placement.panel)
    } finally { this.placing = false }
  }

  private undock() {
    this.removeGameListeners?.()
    this.removeGameListeners = null
    if (this.game && !this.game.isDestroyed()) {
      const unchanged = this.assignedGame ? sameRect(this.game.getBounds(), this.assignedGame)
        : this.originalGame && sameRect(this.game.getBounds(), this.originalGame)
      if (this.gameMinimum) this.game.setMinimumSize(...this.gameMinimum)
      if (unchanged && this.originalGame) {
        this.game.setBounds(this.originalGame)
        if (this.gameMaximized) this.game.maximize()
      }
    }
    this.game = null
    this.originalGame = this.assignedGame = this.assignedPanel = null
    this.gameMinimum = null
    this.gameMaximized = false
  }

  close(): Promise<boolean> {
    if (this.closeTask) return this.closeTask
    const window = this.window
    if (!window) return this.presentationQueue.then(() => true)
    this.savePosition()
    this.closeTask = this.setSuppressed(false).then(() => {
      // Release ownership before the asynchronous closed event. A retired card
      // must not restore presentation or undock a subsequently opened card.
      if (this.window === window) {
        this.window = null
        this.undock()
        this.publish()
      }
      if (!window.isDestroyed()) window.destroy()
      return true
    }).finally(() => { this.closeTask = null })
    return this.closeTask
  }
}
