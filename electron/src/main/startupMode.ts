export const DEFAULT_WINDOWS_STARTUP_MODE = 'window'

export type StartupMode = 'window' | 'wallpaper' | 'companion'

/**
 * Resolve this launch's surface set once. Companion-only is its own startup
 * mode, not a wallpaper modifier: it never owns the wallpaper host or its
 * Slice/Canvas surfaces, and it keeps no visible main window.
 */
export function resolveStartupMode(
  args: readonly string[],
  environment: Readonly<Record<string, string | undefined>>,
  platform: string = process.platform,
  savedMode?: string,
): StartupMode {
  if (args.includes('--companion') || environment.AMADEUS_COMPANION === '1') return 'companion'
  if (platform === 'win32') {
    if (args.includes('--no-wallpaper') || environment.AMADEUS_WALLPAPER === '0') return 'window'
    if (args.includes('--wallpaper') || environment.AMADEUS_WALLPAPER === '1') return 'wallpaper'
    const mode = environment.AMADEUS_WINDOWS_STARTUP_MODE ?? savedMode ?? DEFAULT_WINDOWS_STARTUP_MODE
    return mode === 'wallpaper' ? 'wallpaper' : 'window'
  }
  return args.includes('--wallpaper') || environment.AMADEUS_WALLPAPER === '1' ? 'wallpaper' : 'window'
}

export function isCompanionOnlyStartup(
  args: readonly string[],
  environment: Readonly<Record<string, string | undefined>>,
  platform: string = process.platform,
  savedMode?: string,
): boolean {
  return resolveStartupMode(args, environment, platform, savedMode) === 'companion'
}

export function isWallpaperStartup(
  args: readonly string[],
  environment: Readonly<Record<string, string | undefined>>,
  platform: string = process.platform,
  savedMode?: string,
): boolean {
  return resolveStartupMode(args, environment, platform, savedMode) === 'wallpaper'
}
