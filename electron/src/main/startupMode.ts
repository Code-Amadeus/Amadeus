export const DEFAULT_WINDOWS_STARTUP_MODE = 'window'

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
