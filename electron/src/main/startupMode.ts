export function isWallpaperStartup(
  args: readonly string[],
  environment: Readonly<Record<string, string | undefined>>,
): boolean {
  return args.includes('--wallpaper') || environment.AMADEUS_WALLPAPER === '1'
}
