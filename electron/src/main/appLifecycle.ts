export class ApplicationLifecycle {
  private quitting = false
  private startupComplete = false
  private restoreAfterStartup = false

  requestMainWindow(hasMainWindow: boolean): 'defer' | 'create' | 'restore' {
    if (hasMainWindow) return 'restore'
    if (this.startupComplete) return 'create'
    this.restoreAfterStartup = true
    return 'defer'
  }

  completeStartup(): boolean {
    this.startupComplete = true
    const shouldRestore = this.restoreAfterStartup
    this.restoreAfterStartup = false
    return shouldRestore
  }

  shouldHideWallpaperWindow(wallpaperMode: boolean): boolean {
    return wallpaperMode && !this.quitting
  }

  beginQuit(hasOwnedBackend: boolean): boolean {
    if (this.quitting) return false
    this.quitting = true
    return hasOwnedBackend
  }
}
