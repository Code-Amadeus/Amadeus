export class ApplicationLifecycle {
  private quitting = false
  private startupComplete = false
  private restoreAfterStartup = false

  get isQuitting(): boolean { return this.quitting }

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

  async finishQuit(cleanup: () => Promise<void>, reportFailure: (error: unknown) => void,
    quit: () => void): Promise<void> {
    try { await cleanup() }
    catch (error) { reportFailure(error) }
    finally { quit() }
  }

  beginQuit(hasOwnedBackend: boolean): boolean {
    if (this.quitting) return false
    this.quitting = true
    return hasOwnedBackend
  }
}
