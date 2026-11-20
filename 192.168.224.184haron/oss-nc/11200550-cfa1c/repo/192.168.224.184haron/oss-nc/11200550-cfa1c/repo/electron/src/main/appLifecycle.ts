export class ApplicationLifecycle {
  private quitting = false
  private startupComplete = false
  private restoreAfterStartup = false
  private sessionCleanup?: Promise<void>

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

  /** Windows may cancel a query-session-end. Only the confirmed event commits
   * quit intent; neither event delays/vetoes the operating system shutdown. */
  handleSessionEnd(confirmed: boolean, cleanup: () => Promise<void>, reportFailure: (error: unknown) => void): Promise<void> {
    if (confirmed) this.quitting = true
    return this.sessionCleanup ??= Promise.resolve().then(cleanup).catch(reportFailure)
      .finally(() => { this.sessionCleanup = undefined })
  }
}
