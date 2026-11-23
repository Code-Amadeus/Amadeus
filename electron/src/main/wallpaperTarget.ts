import { spawn } from 'node:child_process'
import path from 'node:path'

// The explicit wallpaper display choice is Lively's display DeviceId, the
// device-interface path (\\?\DISPLAY#…#{…}) that Lively's GetScreens RPC
// reports and SetWallpaper resolves. The helper's `screens` command produces
// it with EnumDisplayDevicesW(EDD_GET_DEVICE_INTERFACE_NAME) — the same query
// Lively's DisplayManager makes — and the session journal already stores it.
// Electron's volatile Display.id never crosses this boundary.
export const WALLPAPER_MONITOR_SETTING = 'AMADEUS_WALLPAPER_MONITOR'

export interface WallpaperRect {
  x: number
  y: number
  width: number
  height: number
}

export interface WallpaperMonitorDescriptor extends WallpaperRect {
  // deviceId: the persisted identity Lively resolves (\\?\DISPLAY#…#{…}).
  // monitor: the GDI device name (\\.\DISPLAYn), labels only — it can be
  // renumbered on hot-plug and is not accepted by SetWallpaper.
  monitor: string
  deviceId: string
  primary: boolean
}

export interface WallpaperMonitorOption {
  monitor: string
  label: string
  primary: boolean
}

export function windowsWallpaperHelperScript(projectRoot: string, resourcesPath: string, packaged: boolean): string {
  const directory = packaged
    ? path.join(resourcesPath, 'windows-wallpaper')
    : path.join(projectRoot, 'electron', 'resources', 'windows-wallpaper')
  return path.join(directory, 'helper.py')
}

// An unset or blank persisted value keeps the historical primary-display
// behavior. Environment and .env overrides are resolved by desktopSettings'
// startupValues before this normalization runs.
export function wallpaperMonitorTarget(value: string | undefined): string | undefined {
  const target = value?.trim() ?? ''
  return target || undefined
}

export function parseWallpaperMonitors(raw: unknown): WallpaperMonitorDescriptor[] {
  const monitors = raw && typeof raw === 'object' ? (raw as { monitors?: unknown }).monitors : undefined
  if (!Array.isArray(monitors)) throw new Error('The wallpaper helper did not report connected displays.')
  return monitors.map(item => {
    const source = item && typeof item === 'object' ? item as Record<string, unknown> : {}
    const monitor = typeof source.Monitor === 'string' ? source.Monitor : ''
    const deviceId = typeof source.DeviceId === 'string' ? source.DeviceId : ''
    const geometry = { x: Number(source.X), y: Number(source.Y), width: Number(source.Width), height: Number(source.Height) }
    if (!deviceId || [geometry.x, geometry.y, geometry.width, geometry.height].some(v => !Number.isFinite(v))
      || geometry.width <= 0 || geometry.height <= 0) {
      throw new Error('The wallpaper helper reported an invalid display entry.')
    }
    return { monitor, deviceId, ...geometry, primary: Boolean(source.Primary) }
  })
}

// Read-only enumeration: the helper's `screens` command never touches Lively,
// the session journal, or display configuration.
export function enumerateWindowsWallpaperMonitors(python: string, helperScript: string,
  spawnCommand: typeof spawn = spawn): Promise<WallpaperMonitorDescriptor[]> {
  return new Promise((resolve, reject) => {
    const child = spawnCommand(python, ['-I', '-S', helperScript, 'screens'], {
      windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    })
    let stdout = ''
    let stderr = ''
    child.stdout?.on('data', chunk => { stdout += chunk.toString() })
    child.stderr?.on('data', chunk => { stderr = (stderr + chunk.toString()).slice(-4000) })
    child.once('error', reject)
    child.once('exit', code => {
      if (code !== 0) {
        reject(new Error(stderr.trim() || `Wallpaper display enumeration failed (${code}).`))
        return
      }
      try { resolve(parseWallpaperMonitors(JSON.parse(stdout.trim()))) }
      catch (error) { reject(error) }
    })
  })
}

// Win32 reports physical-pixel rectangles; Electron's screen.dipToScreenRect
// performs that conversion for each display's own DIP bounds.
export function rectsMatchWithin(a: WallpaperRect, b: WallpaperRect, tolerance = 1): boolean {
  return Math.abs(a.x - b.x) <= tolerance && Math.abs(a.y - b.y) <= tolerance
    && Math.abs(a.width - b.width) <= tolerance && Math.abs(a.height - b.height) <= tolerance
}

export type WallpaperTargetResolution<D> =
  | { status: 'default' }
  | { status: 'resolved'; display: D; descriptor: WallpaperMonitorDescriptor }
  | { status: 'disconnected' | 'ambiguous' | 'unmatched' }

// An explicit target resolves to exactly one Win32 monitor record and exactly
// one Electron display, or the session must not mount anywhere else.
export function resolveWallpaperTargetDisplay<D>(target: string | undefined,
  monitors: WallpaperMonitorDescriptor[], displays: D[],
  physicalBounds: (display: D) => WallpaperRect): WallpaperTargetResolution<D> {
  if (!target) return { status: 'default' }
  const descriptors = monitors.filter(monitor => monitor.deviceId === target)
  if (descriptors.length !== 1) return { status: descriptors.length > 1 ? 'ambiguous' : 'disconnected' }
  const descriptor = descriptors[0]
  const matches = displays.filter(display => rectsMatchWithin(physicalBounds(display), descriptor))
  if (matches.length !== 1) return { status: matches.length > 1 ? 'ambiguous' : 'unmatched' }
  return { status: 'resolved', display: matches[0], descriptor }
}

export function wallpaperTargetProblem(status: string, target: string): string {
  switch (status) {
    case 'disconnected':
      return `The selected wallpaper display "${target}" is no longer connected. Choose another display in Settings.`
    case 'ambiguous':
      return `The selected wallpaper display "${target}" matches more than one connected display; Amadeus will not choose a display on its own.`
    default:
      return `The selected wallpaper display "${target}" could not be matched to a connected display.`
  }
}

// Settings lists one choice per connected Win32 monitor. Correlating an
// Electron display only provides its friendly label; the persisted value
// stays the DeviceId and strict correlation still applies when a session
// starts.
export function wallpaperMonitorOptions<D extends { label?: string }>(monitors: WallpaperMonitorDescriptor[],
  displays: D[], physicalBounds: (display: D) => WallpaperRect): WallpaperMonitorOption[] {
  const claimed = new Set<D>()
  return monitors.map(monitor => {
    const display = displays.find(candidate => !claimed.has(candidate) && rectsMatchWithin(physicalBounds(candidate), monitor))
    if (display) claimed.add(display)
    const name = monitor.monitor.replace(/^\\\\\.\\/, '')
    const label = [name, display?.label, `${monitor.width}\u00d7${monitor.height}`, monitor.primary ? 'Primary' : '']
      .filter(Boolean).join(' \u00b7 ')
    return { monitor: monitor.deviceId, label, primary: monitor.primary }
  })
}
