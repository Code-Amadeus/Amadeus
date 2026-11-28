import { desktopCatalogFields } from '../shared/configCatalog.js'

export const DEFAULT_WINDOWS_STARTUP_MODE = String(desktopCatalogFields.AMADEUS_WINDOWS_STARTUP_MODE.default)
export const DEFAULT_WALLPAPER_DRAGGABLE_CONTROLS = String(desktopCatalogFields.AMADEUS_WALLPAPER_DRAGGABLE_CONTROLS.default)
const DRAGGABLE_CONTROLS_TRUE_VALUES = new Set(['1', 'true', 'yes'])

export function isWallpaperStartup(
  args: readonly string[],
  environment: Readonly<Record<string, string | undefined>>,
  platform: string = process.platform,
  savedMode?: string,
): boolean {
  if (platform === 'win32') {
    if (args.includes('--no-wallpaper') || environment.AMADEUS_WALLPAPER === '0') return false
    if (args.includes('--wallpaper') || environment.AMADEUS_WALLPAPER === '1') return true
    const mode = environment.AMADEUS_WINDOWS_STARTUP_MODE ?? savedMode ?? DEFAULT_WINDOWS_STARTUP_MODE
    return mode === 'wallpaper'
  }
  return args.includes('--wallpaper') || environment.AMADEUS_WALLPAPER === '1'
}

// The optional draggable-controls layout is Windows-only: it relies on the
// shaped transparent slice window the Windows desktop layer hosts.
export function wantsDraggableSliceControls(
  environment: Readonly<Record<string, string | undefined>>,
  platform: string = process.platform,
  savedValue?: string,
): boolean {
  if (platform !== 'win32') return false
  const raw = environment.AMADEUS_WALLPAPER_DRAGGABLE_CONTROLS ?? savedValue ?? DEFAULT_WALLPAPER_DRAGGABLE_CONTROLS
  return DRAGGABLE_CONTROLS_TRUE_VALUES.has(String(raw).trim().toLowerCase())
}
