import type { BrowserWindowConstructorOptions } from 'electron'

type WallpaperWindowPolicy = {
  constructorOptions: Pick<
    BrowserWindowConstructorOptions,
    'focusable' | 'hiddenInMissionControl' | 'type'
  >
  hostMode: 'scene' | 'slice'
  joinAllWorkspaces: boolean
  interactiveLevel: { level: 'normal'; relativeLevel: number } | null
  visibleLevel: { level: 'normal'; relativeLevel: number } | null
  supportsWindowShape: boolean
}

// desktop, desktop-icon, and normal come from CGWindowLevelForKey on macOS
// 26.6.2 (25G83); dockWallpaper and the two host levels were confirmed through
// CGWindowListCopyWindowInfo with Electron 44.0.0. Electron exposes named
// levels plus a relative offset but no CGWindowLevelForKey binding, so the
// community candidate records the queried references and verified offsets.
export const MACOS_WINDOW_LEVELS = Object.freeze({
  dockWallpaper: -2147483624,
  desktop: -2147483623,
  scene: -2147483609,
  finderDesktopIcons: -2147483603,
  canvas: -2147483598,
  normalApplication: 0,
})

export function wallpaperWindowPolicy(platform: NodeJS.Platform): WallpaperWindowPolicy {
  if (platform === 'darwin') {
    return {
      constructorOptions: {
        type: 'desktop',
        focusable: false,
        hiddenInMissionControl: true,
      },
      hostMode: 'scene',
      joinAllWorkspaces: true,
      interactiveLevel: { level: 'normal', relativeLevel: MACOS_WINDOW_LEVELS.canvas },
      visibleLevel: { level: 'normal', relativeLevel: MACOS_WINDOW_LEVELS.scene },
      supportsWindowShape: false,
    }
  }

  return {
    constructorOptions: { focusable: true },
    hostMode: 'slice',
    joinAllWorkspaces: false,
    interactiveLevel: null,
    visibleLevel: null,
    supportsWindowShape: platform === 'win32' || platform === 'linux',
  }
}
