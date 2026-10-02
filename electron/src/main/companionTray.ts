import { Menu, Tray, nativeImage } from 'electron'
import fs from 'node:fs'

/**
 * Companion-only keeps no visible main window, so the tray is the only surface
 * that can bring the card back or end the session.
 */
export class CompanionTray {
  readonly hasIcon: boolean
  private tray: Tray
  private visible = true

  constructor(
    iconPath: string,
    private actions: { show: () => void; hide: () => void; quit: () => void },
  ) {
    const icon = fs.existsSync(iconPath) ? nativeImage.createFromPath(iconPath) : nativeImage.createEmpty()
    this.hasIcon = !icon.isEmpty()
    this.tray = new Tray(icon)
    this.tray.on('double-click', () => this.actions.show())
    this.setVisible(true)
  }

  setVisible(visible: boolean): void {
    this.visible = visible
    this.tray.setToolTip(`Amadeus — Companion ${visible ? 'visible' : 'hidden'}`)
    this.tray.setContextMenu(Menu.buildFromTemplate([
      { label: visible ? 'Companion visible' : 'Companion hidden', enabled: false },
      { type: 'separator' },
      { label: 'Show companion', enabled: !visible, click: () => this.actions.show() },
      { label: 'Hide companion', enabled: visible, click: () => this.actions.hide() },
      { type: 'separator' },
      { label: 'Quit Amadeus', click: () => this.actions.quit() },
    ]))
  }

  destroy(): void {
    this.tray.destroy()
  }
}
