import { Menu, Tray, nativeImage } from 'electron'
import fs from 'node:fs'
import type { WindowsWallpaperStatus } from './windowsWallpaper.js'

const labels: Record<WindowsWallpaperStatus, string> = {
  idle: 'Wallpaper inactive',
  preparing: 'Preparing wallpaper components…',
  mounting: 'Starting wallpaper…',
  active: 'Wallpaper active',
  restoring: 'Restoring your previous wallpaper…',
}

// Created only on Windows. This is also exercised from the packaged ASAR by
// the native tray smoke, without requiring Lively or a character package.
export class WindowsWallpaperTray {
  readonly hasIcon: boolean
  private tray: Tray
  private notice: ReturnType<typeof setTimeout> | null = null
  private open: () => void
  private quit: () => void

  constructor(iconPath: string, open: () => void, quit: () => void) {
    const icon = fs.existsSync(iconPath) ? nativeImage.createFromPath(iconPath) : nativeImage.createEmpty()
    this.hasIcon = !icon.isEmpty()
    this.tray = new Tray(icon)
    this.open = open
    this.quit = quit
    this.tray.on('double-click', open)
    this.setStatus('idle')
  }

  setStatus(status: WindowsWallpaperStatus): void {
    if (this.notice) clearTimeout(this.notice)
    this.notice = null
    this.tray.setToolTip(`Amadeus — ${labels[status]}`)
    this.tray.setContextMenu(Menu.buildFromTemplate([
      { label: labels[status], enabled: false },
      { type: 'separator' },
      { label: 'Open Amadeus', click: this.open },
      { label: 'Quit Amadeus', enabled: status !== 'restoring', click: this.quit },
    ]))
    // Fast, ordinary starts/stops stay quiet. Slow setup and restoration remain
    // visible even while the main window is hidden. Do not kill the restorer.
    if (status === 'preparing' || status === 'restoring') {
      this.notice = setTimeout(() => {
        this.tray.displayBalloon({
          title: 'Amadeus',
          content: status === 'preparing'
            ? 'Preparing wallpaper components. First use may download and install Lively Wallpaper. Please wait.'
            : 'Restoring your previous wallpaper. Amadeus is still cleaning up; please wait.',
          respectQuietTime: true,
        })
      }, 3000)
      this.notice.unref()
    }
  }

  destroy(): void {
    if (this.notice) clearTimeout(this.notice)
    this.tray.destroy()
  }
}
