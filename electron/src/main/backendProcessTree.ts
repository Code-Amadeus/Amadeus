import { spawn, type ChildProcess } from 'node:child_process'
import path from 'node:path'

/** Force-stop only the backend process created by this Electron instance. */
export async function forceStopBackendTree(proc: ChildProcess, platform: string = process.platform,
  createProcess: typeof spawn = spawn): Promise<void> {
  if (!Number.isSafeInteger(proc.pid) || Number(proc.pid) <= 0) throw new Error('Owned backend process identity is unavailable')
  if (platform !== 'win32') {
    proc.kill()
    return
  }
  const systemRoot = process.env.SystemRoot || process.env.SYSTEMROOT || 'C:\\Windows'
  await new Promise<void>((resolve, reject) => {
    const child = createProcess(path.join(systemRoot, 'System32', 'taskkill.exe'), ['/PID', String(proc.pid), '/T', '/F'], {
      windowsHide: true, stdio: 'ignore', env: { SystemRoot: systemRoot, WINDIR: systemRoot },
    })
    const timer = setTimeout(() => { child.kill(); reject(new Error('Owned backend process-tree shutdown timed out; extension files were retained')) }, 5000)
    child.on('error', () => { clearTimeout(timer); reject(new Error('Owned backend process-tree shutdown could not start; extension files were retained')) })
    child.on('close', code => {
      clearTimeout(timer)
      if (code === 0) resolve()
      else reject(new Error('Owned backend process-tree shutdown could not be confirmed; extension files were retained'))
    })
  })
}
