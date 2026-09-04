type Point = { x: number; y: number }
type Rectangle = { x: number; y: number; width: number; height: number }

export type WallpaperShapeSender = 'canvas' | 'scene' | null

export type WallpaperCanvasWindow = {
  close: () => void
  getBounds: () => Rectangle
  hide: () => void
  isDestroyed: () => boolean
  isVisible: () => boolean
  reload: () => void
  setIgnoreMouseEvents: (ignore: boolean, options: { forward: boolean }) => void
  showInactive: () => void
}

type Scheduler = {
  setInterval: (callback: () => void, milliseconds: number) => unknown
  clearInterval: (handle: unknown) => void
}

type LifecycleOptions = {
  getCursorScreenPoint: () => Point
  pointHitsWindowRegions: (point: Point, windowBounds: Rectangle, regions: Rectangle[]) => boolean
  scheduler?: Scheduler
}

const defaultScheduler: Scheduler = {
  setInterval: (callback, milliseconds) => setInterval(callback, milliseconds),
  clearInterval: handle => clearInterval(handle as NodeJS.Timeout),
}

export function wallpaperShapeSender(
  sender: unknown,
  sceneSender: unknown,
  canvasSender: unknown,
): WallpaperShapeSender {
  if (canvasSender !== null && canvasSender !== undefined && sender === canvasSender) return 'canvas'
  if (sceneSender !== null && sceneSender !== undefined && sender === sceneSender) return 'scene'
  return null
}

export class WallpaperCanvasLifecycle<TWindow extends WallpaperCanvasWindow> {
  private currentWindow: TWindow | null = null
  private currentBridgeKey = ''
  private hitRegions: Rectangle[] = []
  private hitRegionSignature = ''
  private hitTestTimer: unknown = null
  private ignoringMouse = true
  private rendererLoadPending = false
  private readonly getCursorScreenPoint: () => Point
  private readonly pointHitsWindowRegions: LifecycleOptions['pointHitsWindowRegions']
  private readonly scheduler: Scheduler

  constructor(options: LifecycleOptions) {
    this.getCursorScreenPoint = options.getCursorScreenPoint
    this.pointHitsWindowRegions = options.pointHitsWindowRegions
    this.scheduler = options.scheduler || defaultScheduler
  }

  get window(): TWindow | null {
    return this.currentWindow
  }

  get bridgeKey(): string {
    return this.currentBridgeKey
  }

  snapshot() {
    return {
      hasWindow: this.currentWindow !== null,
      bridgeKey: this.currentBridgeKey,
      hitRegions: this.hitRegions.map(region => ({ ...region })),
      hitTestActive: this.hitTestTimer !== null,
      ignoringMouse: this.ignoringMouse,
      rendererLoadPending: this.rendererLoadPending,
    }
  }

  attach(window: TWindow, bridgeKey: string): void {
    if (this.currentWindow && this.currentWindow !== window) this.close()
    this.currentWindow = window
    this.currentBridgeKey = bridgeKey
    this.hitRegions = []
    this.hitRegionSignature = ''
    this.rendererLoadPending = true
    this.stopHitTest()
    this.ignoringMouse = true
    if (!window.isDestroyed()) window.setIgnoreMouseEvents(true, { forward: true })
  }

  prepareReload(window: TWindow, bridgeKey: string): boolean {
    if (this.currentWindow !== window || window.isDestroyed()) return false
    this.currentBridgeKey = bridgeKey
    this.rendererLoadPending = true
    return this.reset(window)
  }

  reloadRenderer(window: TWindow | null = this.currentWindow): boolean {
    if (!window || !this.reset(window)) return false
    if (this.rendererLoadPending) return true
    this.rendererLoadPending = true
    window.reload()
    return true
  }

  reset(window: TWindow | null = this.currentWindow): boolean {
    if (!window || this.currentWindow !== window || window.isDestroyed()) return false
    this.hitRegions = []
    this.hitRegionSignature = ''
    this.stopHitTest()
    if (window.isVisible()) window.hide()
    return true
  }

  commitRegions(window: TWindow, regions: Rectangle[]) {
    if (this.currentWindow !== window || window.isDestroyed()) {
      return { accepted: false, changed: false, firstCommit: false, count: 0 }
    }
    const firstCommit = this.hitRegions.length === 0
    this.rendererLoadPending = false
    this.hitRegions = regions.map(region => ({ ...region }))
    const nextSignature = this.hitRegions
      .map(region => `${region.x},${region.y},${region.width},${region.height}`)
      .join(';')
    const changed = nextSignature !== this.hitRegionSignature
    this.hitRegionSignature = nextSignature

    if (this.hitRegions.length === 0) {
      this.stopHitTest()
      if (window.isVisible()) window.hide()
      return { accepted: true, changed, firstCommit, count: 0 }
    }

    if (!window.isVisible()) window.showInactive()
    this.startHitTest()
    return {
      accepted: true,
      changed,
      firstCommit,
      count: this.hitRegions.length,
    }
  }

  close(): void {
    const window = this.currentWindow
    this.stopHitTest()
    this.currentWindow = null
    this.currentBridgeKey = ''
    this.hitRegions = []
    this.hitRegionSignature = ''
    this.ignoringMouse = true
    this.rendererLoadPending = false
    if (window && !window.isDestroyed()) window.close()
  }

  detach(window: TWindow): boolean {
    if (this.currentWindow !== window) return false
    this.stopHitTest()
    this.currentWindow = null
    this.currentBridgeKey = ''
    this.hitRegions = []
    this.hitRegionSignature = ''
    this.ignoringMouse = true
    this.rendererLoadPending = false
    return true
  }

  private startHitTest(): void {
    if (this.hitTestTimer !== null) return
    this.hitTestTimer = this.scheduler.setInterval(() => {
      const window = this.currentWindow
      if (!window || window.isDestroyed() || !window.isVisible()) return
      const hitsControl = this.pointHitsWindowRegions(
        this.getCursorScreenPoint(),
        window.getBounds(),
        this.hitRegions,
      )
      this.setMousePassthrough(!hitsControl)
    }, 40)
  }

  private stopHitTest(): void {
    if (this.hitTestTimer !== null) {
      this.scheduler.clearInterval(this.hitTestTimer)
      this.hitTestTimer = null
    }
    this.setMousePassthrough(true)
  }

  private setMousePassthrough(ignore: boolean): void {
    const window = this.currentWindow
    if (!window || window.isDestroyed() || this.ignoringMouse === ignore) return
    this.ignoringMouse = ignore
    window.setIgnoreMouseEvents(ignore, { forward: true })
  }
}
