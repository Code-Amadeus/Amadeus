import { spawn, type ChildProcess } from 'node:child_process'
import path from 'node:path'

export function managesWindowsWallpaper(platform: string, environment: NodeJS.ProcessEnv): boolean {
  return platform === 'win32' && environment.AMADEUS_WALLPAPER_HOST !== 'external'
}

type HelperSession = { process: ChildProcess; ready: Promise<void>; done: Promise<void> }
export type WindowsWallpaperStatus = 'preparing' | 'mounting' | 'active' | 'restoring' | 'idle'
type WallpaperDependencies = {
  prepare: () => Promise<void>
  launch: (url: string) => HelperSession
  exited?: (error?: unknown) => void
  status?: (status: WindowsWallpaperStatus) => void
}

// The helper owns the before-image and cleanup; Electron owns when a session
// starts/stops. Serialize duplicate ready events, explicit stops and shutdown.
export class WindowsWallpaperSession {
  private tail: Promise<unknown> = Promise.resolve()
  private current: { url: string; helper: HelperSession } | null = null
  private pending: { url: string; result: Promise<void> } | null = null
  private dependencies: WallpaperDependencies

  constructor(dependencies: WallpaperDependencies) { this.dependencies = dependencies }

  private enqueue(action: () => Promise<void>): Promise<void> {
    const result = this.tail.then(action)
    this.tail = result.catch(() => {})
    return result
  }

  start(url: string): Promise<void> {
    if (this.pending?.url === url) return this.pending.result
    const result = this.enqueue(async () => {
      if (this.current?.url === url) return
      await this.stopCurrent()
      this.dependencies.status?.('preparing')
      await this.dependencies.prepare()
      this.dependencies.status?.('mounting')
      const helper = this.dependencies.launch(url)
      this.current = { url, helper }
      try { await helper.ready }
      catch (error) {
        await this.stopCurrent()
        throw error
      }
      this.dependencies.status?.('active')
      const ended = (error?: unknown) => {
        if (this.current?.helper !== helper) return
        this.current = null
        this.pending = null
        this.dependencies.status?.('idle')
        this.dependencies.exited?.(error)
      }
      void helper.done.then(() => ended(), ended)
    })
    this.pending = { url, result }
    // Only an in-flight start is shared. A repaired installation must be able
    // to retry the same URL, and an older attempt must not clear a newer one.
    const settled = () => { if (this.pending?.result === result) this.pending = null }
    void result.then(settled, () => {
      settled()
      this.dependencies.status?.('idle')
    })
    return result
  }

  stop(): Promise<void> {
    this.pending = null
    return this.enqueue(() => this.stopCurrent())
  }

  private async stopCurrent(): Promise<void> {
    const current = this.current
    if (!current) return
    this.current = null
    this.dependencies.status?.('restoring')
    // Closing stdin also handles a parent crash. Never kill the recovery helper.
    current.helper.process.stdin?.end()
    try { await current.helper.done }
    finally { this.dependencies.status?.('idle') }
  }
}

// IPC's existing boolean contract: report a recovery failure at the host
// boundary instead of rejecting into fire-and-forget renderer event handlers.
export async function stopWallpaperForRenderer(
  session: Pick<WindowsWallpaperSession, 'stop'> | null,
  reportError: (error: unknown) => void,
): Promise<boolean> {
  try { await session?.stop(); return true }
  catch (error) { reportError(error); return false }
}

// A dead wallpaper host must not leave its backend-owned microphone session
// alive. If the owned backend cannot acknowledge cleanup, shut that runtime
// down using Electron's existing graceful-stop/owned-process fallback.
export async function stopBackendWallpaperAfterHostExit(
  requestStop: () => Promise<boolean>,
  stopBackend: () => Promise<void>,
): Promise<void> {
  let stopped = false
  try { stopped = await requestStop() } catch { /* transport is unavailable */ }
  if (!stopped) await stopBackend()
}

export function windowsWallpaperDependencies(projectRoot: string, resourcesPath: string, packaged: boolean): WallpaperDependencies {
  const directory = packaged ? path.join(resourcesPath, 'windows-wallpaper') : path.join(projectRoot, 'build', 'windows-wallpaper', 'host')
  const executable = path.join(directory, 'Amadeus.Wallpaper.exe')
  return {
    // Recheck on each new session: installation state is external, and failed
    // setup must be retryable without restarting Amadeus.
    prepare: () => new Promise<void>((resolve, reject) => {
      // Packaged builds contain the published helper. Source runs build it once.
      const script = packaged
        ? path.join(directory, 'setup_windows_wallpaper.ps1')
        : path.join(projectRoot, 'scripts', 'setup_windows_wallpaper.ps1')
      const child = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', script], {
        windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
      })
      let detail = ''
      child.stdout?.on('data', chunk => { detail = (detail + chunk.toString()).slice(-4000) })
      child.stderr?.on('data', chunk => { detail = (detail + chunk.toString()).slice(-4000) })
      child.once('error', reject)
      child.once('exit', code => code === 0 ? resolve() : reject(new Error(`Windows wallpaper setup failed: ${detail}`)))
    }),
    launch: url => {
      const child = spawn(executable, ['run', url], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] })
      let resolveReady: () => void
      let rejectReady: (error: Error) => void
      const ready = new Promise<void>((resolve, reject) => { resolveReady = resolve; rejectReady = reject })
      let detail = ''
      let buffer = ''
      child.stdout?.on('data', chunk => {
        buffer += chunk.toString()
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        for (const line of lines) {
          try {
            const message = JSON.parse(line)
            if (message.status === 'mounted') resolveReady()
            if (message.status === 'error') { detail = message.error; rejectReady(new Error(detail)) }
          } catch { /* only JSON status messages are part of the helper protocol */ }
        }
      })
      child.stderr?.on('data', chunk => { detail = (detail + chunk.toString()).slice(-4000) })
      child.stdin?.on('error', error => { detail = error.message; rejectReady(error) })
      const done = new Promise<void>((resolve, reject) => {
        child.once('error', error => { rejectReady(error); reject(error) })
        child.once('exit', code => {
          rejectReady(new Error(detail || `Wallpaper helper exited before mount (${code}).`))
          if (code === 0) resolve()
          else reject(new Error(detail || `Wallpaper restoration failed (${code}); recovery journal retained.`))
        })
      })
      // Cleanup may fail before Electron asks to stop. Attach a handler now;
      // callers still observe the original rejection when awaiting done.
      void done.catch(error => console.error('[windows-wallpaper]', error))
      return { process: child, ready, done }
    },
  }
}
