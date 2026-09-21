export function isWallpaperStartup(
  args: readonly string[],
  environment: Readonly<Record<string, string | undefined>>,
  platform: string = process.platform,
): boolean {
  if (platform === 'win32') {
    if (args.includes('--no-wallpaper') || environment.AMADEUS_WALLPAPER === '0') return false
    if (environment.AMADEUS_WALLPAPER_HOST !== 'external') return true
  }
  return args.includes('--wallpaper') || environment.AMADEUS_WALLPAPER === '1'
}
