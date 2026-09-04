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
      // Canvas controls sit above Finder's desktop-icon surface while remaining
      // far below ordinary application windows.
      interactiveLevel: { level: 'normal', relativeLevel: -2147483598 },
      // The scene sits between the Dock wallpaper surface and Finder's
      // desktop-icon surface. Electron's desktop type alone lands below both.
      visibleLevel: { level: 'normal', relativeLevel: -2147483609 },
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
