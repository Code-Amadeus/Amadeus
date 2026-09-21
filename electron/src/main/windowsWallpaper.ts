import { spawn, type ChildProcess } from 'node:child_process'
import path from 'node:path'

export function managesWindowsWallpaper(platform: string, environment: NodeJS.ProcessEnv): boolean {
  return platform === 'win32' && environment.AMADEUS_WALLPAPER_HOST !== 'external'
}

type HelperSession = { process: ChildProcess; ready: Promise<void>; done: Promise<void> }
type WallpaperDependencies = {
  prepare: () => Promise<void>
  launch: (url: string) => HelperSession
  exited?: (error?: unknown) => void
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
      await this.dependencies.prepare()
      const helper = this.dependencies.launch(url)
      this.current = { url, helper }
      try { await helper.ready }
      catch (error) {
        await this.stopCurrent()
        throw error
      }
      const ended = (error?: unknown) => {
        if (this.current?.helper !== helper) return
        this.current = null
        this.pending = null
        this.dependencies.exited?.(error)
      }
      void helper.done.then(() => ended(), ended)
    })
    this.pending = { url, result }
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
    // Closing stdin also handles a parent crash. Never kill the recovery helper.
    current.helper.process.stdin?.end()
    await current.helper.done
  }
}

export function windowsWallpaperDependencies(projectRoot: string, resourcesPath: string, packaged: boolean): WallpaperDependencies {
  const directory = packaged ? path.join(resourcesPath, 'windows-wallpaper') : path.join(projectRoot, 'build', 'windows-wallpaper', 'host')
  const executable = path.join(directory, 'Amadeus.Wallpaper.exe')
  let prepared: Promise<void> | null = null
  return {
    prepare: () => prepared ??= new Promise<void>((resolve, reject) => {
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
