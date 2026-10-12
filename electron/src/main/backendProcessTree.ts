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

function hasExited(proc: ChildProcess): boolean {
  return proc.exitCode !== null || proc.signalCode !== null
}

export function waitForProcessExit(proc: ChildProcess, timeoutMs: number): Promise<boolean> {
  return new Promise(resolve => {
    if (hasExited(proc)) { resolve(true); return }
    const onExit = () => { clearTimeout(timer); resolve(true) }
    const timer = setTimeout(() => { proc.off('exit', onExit); resolve(false) }, timeoutMs)
    proc.once('exit', onExit)
  })
}

/** Returns whether child lifetime was confirmed, after the owned backend exits. */
export async function stopOwnedBackend(proc: ChildProcess, requestShutdown: () => Promise<boolean>,
  { platform = process.platform, forceStop = forceStopBackendTree, waitForExit = waitForProcessExit }: {
    platform?: string; forceStop?: typeof forceStopBackendTree; waitForExit?: typeof waitForProcessExit;
  } = {}): Promise<boolean> {
  const requested = await requestShutdown()
  if (requested && await waitForExit(proc, 2500)) return true
  // The process may have exited between the request/timeout and escalation.
  // That proves the backend stopped, but not that a forced tree cleanup ran.
  if (hasExited(proc)) return false
  try { await forceStop(proc, platform) }
  catch (error) {
    if (hasExited(proc)) return false
    throw error
  }
  if (!await waitForExit(proc, 1200) && !hasExited(proc)) {
    throw new Error('Owned backend shutdown did not complete')
  }
  return platform === 'win32'
}
