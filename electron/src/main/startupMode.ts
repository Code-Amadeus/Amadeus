export const DEFAULT_WINDOWS_STARTUP_MODE = 'wallpaper'

export function isWallpaperStartup(
  args: readonly string[],
  environment: Readonly<Record<string, string | undefined>>,
  platform: string = process.platform,
  savedMode?: string,
): boolean {
  if (platform === 'win32') {
    if (args.includes('--no-wallpaper') || environment.AMADEUS_WALLPAPER === '0') return false
    if (args.includes('--wallpaper') || environment.AMADEUS_WALLPAPER === '1') return true
    const mode = environment.AMADEUS_WINDOWS_STARTUP_MODE ?? savedMode
    if (mode === 'window' || mode === 'wallpaper') return mode === 'wallpaper'
    return environment.AMADEUS_WALLPAPER_HOST !== 'external' && DEFAULT_WINDOWS_STARTUP_MODE === 'wallpaper'
  }
  return args.includes('--wallpaper') || environment.AMADEUS_WALLPAPER === '1'
}
