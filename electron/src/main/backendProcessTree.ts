import { execFile, spawn, type ChildProcess } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { usesScheduledExtensionUpdates } from '../shared/extensionCatalog.js'

const CLEANUP_SCHEMA = 'amadeus.backend-cleanup.v1'

/** The packaged frontend and an explicitly supplied Python backend may live
 * in different directories. Bind cleanup to the code that Python will load. */
export async function resolveBackendProjectRoot(python: string, cwd: string, environment: NodeJS.ProcessEnv,
  execute: typeof execFile = execFile): Promise<string> {
  const source = "import importlib.util,json,pathlib; s=importlib.util.find_spec('server'); assert s and s.origin; print(json.dumps(str(pathlib.Path(s.origin).resolve().parent.parent)))"
  const output = await new Promise<string>((resolve, reject) => {
    execute(python, ['-c', source], { cwd, env: environment, windowsHide: true, timeout: 10000, maxBuffer: 32 * 1024 },
      (error, stdout) => error ? reject(error) : resolve(String(stdout).trim()))
  })
  const root: unknown = JSON.parse(output)
  if (typeof root !== 'string' || !path.isAbsolute(root) || root.includes('\0')) throw Error('Python backend code root is unavailable')
  return root
}
type CleanupOwner = { id: string; nonce: string; project_root: string; python_command: string }
type CleanupIndex = { schema: string; unknown: boolean; owners: CleanupOwner[]; bootId?: number }
type ProcessIdentity = { pid: number; created: string }
type BootProbe = () => Promise<number | undefined>

// Desktop is the sole index writer. Hold this boundary across asynchronous
// identity probes and durable writes so compaction cannot erase a new owner.
const cleanupOperations = new Map<string, Promise<void>>()
async function serializeCleanup<T>(statePath: string, action: () => Promise<T>): Promise<T> {
  ordinaryPath(statePath)
  const key = process.platform === 'win32' ? statePath.toLowerCase() : statePath
  const previous = cleanupOperations.get(key) ?? Promise.resolve()
  let release!: () => void
  const current = new Promise<void>(resolve => { release = resolve })
  cleanupOperations.set(key, current)
  await previous
  try { return await action() }
  finally {
    release()
    if (cleanupOperations.get(key) === current) cleanupOperations.delete(key)
  }
}

function ordinaryPath(file: string): void {
  if (!path.isAbsolute(file) || path.normalize(file) !== file) throw Error('Cleanup path must be canonical and absolute')
  for (let item = file; ; item = path.dirname(item)) {
    try { if (fs.lstatSync(item).isSymbolicLink()) throw Error('Cleanup path cannot traverse a link') }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error }
    if (path.dirname(item) === item) break
  }
}

function readJson(file: string): unknown {
  ordinaryPath(file)
  const info = fs.lstatSync(file)
  if (!info.isFile() || info.size > 2 * 1024 * 1024) throw Error('Invalid cleanup record')
  return JSON.parse(fs.readFileSync(file, 'utf8'))
}

function validOwner(value: unknown): value is CleanupOwner {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const item = value as CleanupOwner
  return Object.keys(item).sort().join(',') === 'id,nonce,project_root,python_command'
    && typeof item.id === 'string' && /^[a-f0-9]{32}$/.test(item.id)
    && typeof item.nonce === 'string' && item.nonce.length >= 16 && item.nonce.length <= 256
    && typeof item.project_root === 'string' && path.isAbsolute(item.project_root)
    && typeof item.python_command === 'string' && item.python_command.length > 0
}

function readIndex(file: string): CleanupIndex | null {
  try {
    const value = readJson(file) as CleanupIndex
    if (!value || value.schema !== CLEANUP_SCHEMA || typeof value.unknown !== 'boolean'
      || value.bootId !== undefined && !validBootId(value.bootId)
      || !Array.isArray(value.owners) || value.owners.length > 4096 || !value.owners.every(validOwner)
      || new Set(value.owners.map(owner => owner.id)).size !== value.owners.length) throw Error('Invalid cleanup index')
    return value
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
    // An index lost independently from its receipts is not a fresh installation.
    if (fs.existsSync(`${file}.receipts`)) throw Error('Cleanup index is missing')
    return null
  }
}

function powershell(script: string): Promise<string> {
  const systemRoot = process.env.SystemRoot || process.env.SYSTEMROOT || 'C:\\Windows'
  return new Promise((resolve, reject) => {
    execFile(path.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
      ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')],
      { windowsHide: true, timeout: 10000, maxBuffer: 32 * 1024,
        env: { SystemRoot: systemRoot, WINDIR: systemRoot } },
      (error, stdout) => error ? reject(error) : resolve(String(stdout).trim()))
  })
}

function validBootId(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) >= 0 && Number(value) <= 0xffffffff
}

let bootIdentity: Promise<number | undefined> | undefined
/** Windows' boot counter is independent of wall-clock changes and PID reuse.
 * https://learn.microsoft.com/windows-hardware/design/device-experiences/oem-hvci-enablement
 * Missing evidence never authorizes retirement; ordinary Job receipts still work. */
export function readWindowsBootId(): Promise<number | undefined> {
  if (process.platform !== 'win32') return Promise.resolve(undefined)
  return bootIdentity ??= powershell(`$ErrorActionPreference='Stop'
$key=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Memory Management\\PrefetchParameters')
try { if($null -eq $key -or $key.GetValueKind('BootId') -ne [Microsoft.Win32.RegistryValueKind]::DWord){exit 3}; $key.GetValue('BootId')|ConvertTo-Json -Compress } finally { if($key){$key.Dispose()} }`)
    .then(output => {
      const value: unknown = JSON.parse(output)
      if (!validBootId(value)) throw Error('Windows boot identity is unavailable')
      return value
    }).catch(() => { bootIdentity = undefined; return undefined })
}

function reconcileBoot(index: CleanupIndex, bootId: number | undefined): CleanupIndex {
  if (bootId === undefined) return index
  if (!validBootId(bootId)) throw Error('Invalid Windows boot identity')
  // Existing PR profiles lack a boot binding. Adopt this boot without granting
  // cleanup authority: their first positively observed later boot can retire it.
  if (index.bootId === undefined) return { ...index, bootId }
  if (bootId < index.bootId) throw Error('Windows boot identity moved backwards; cleanup remains unconfirmed')
  if (bootId === index.bootId) return index
  // No process from the earlier boot can still hold package files. This says
  // nothing about Work outcomes; their durable recovery records are untouched.
  return { schema: CLEANUP_SCHEMA, bootId, unknown: false, owners: [] }
}

async function atomicIndex(file: string, value: CleanupIndex): Promise<void> {
  ordinaryPath(file)
  fs.mkdirSync(path.dirname(file), { recursive: true })
  ordinaryPath(file)
  const temporary = `${file}.${randomBytes(16).toString('hex')}.tmp`
  const handle = fs.openSync(temporary, 'wx', 0o600)
  try { fs.writeFileSync(handle, JSON.stringify(value) + '\n', 'utf8'); fs.fsyncSync(handle) }
  finally { fs.closeSync(handle) }
  ordinaryPath(file)
  if (process.platform === 'win32') {
    const quote = (value: string) => "'" + value.replaceAll("'", "''") + "'"
    // Declare the native call in memory. Add-Type starts a C# compiler for each
    // write, adding an avoidable process and cold-start cost to this deadline.
    await powershell(`$ErrorActionPreference='Stop'
$assembly=[AppDomain]::CurrentDomain.DefineDynamicAssembly([Reflection.AssemblyName]::new('BackendCleanupMove'),[Reflection.Emit.AssemblyBuilderAccess]::Run)
$module=$assembly.DefineDynamicModule('Native')
$type=$module.DefineType('BackendCleanupMove',[Reflection.TypeAttributes]'Public,Abstract,Sealed')
$method=$type.DefinePInvokeMethod(
  'MoveFileExW','kernel32.dll',[Reflection.MethodAttributes]'Public,Static,PinvokeImpl',
  [Reflection.CallingConventions]::Standard,[bool],[Type[]]@([string],[string],[int]),
  [Runtime.InteropServices.CallingConvention]::Winapi,[Runtime.InteropServices.CharSet]::Unicode)
$method.SetImplementationFlags($method.GetMethodImplementationFlags() -bor [Reflection.MethodImplAttributes]::PreserveSig)
$native=$type.CreateType()
if(-not $native::MoveFileExW(${quote(temporary)},${quote(file)},9)){exit 3}`)
  } else {
    fs.renameSync(temporary, file)
    const directory = fs.openSync(path.dirname(file), 'r')
    try { fs.fsyncSync(directory) } finally { fs.closeSync(directory) }
  }
}

/** Register before spawn. Never replace an earlier unresolved launch with a new one. */
export async function prepareBackendCleanup(statePath: string,
  { nonce, projectRoot, pythonCommand }: { nonce: string; projectRoot: string; pythonCommand: string },
  platform: NodeJS.Platform = process.platform, getBootId: BootProbe = readWindowsBootId): Promise<{ environment: Record<string, string> }> {
  if (!usesScheduledExtensionUpdates(platform)) return { environment: {} }
  return serializeCleanup(statePath, async () => {
    const index = reconcileBoot(readIndex(statePath) ?? { schema: CLEANUP_SCHEMA, unknown: false, owners: [] }, await getBootId())
    const owner: CleanupOwner = { id: randomBytes(16).toString('hex'), nonce,
      project_root: path.resolve(projectRoot), python_command: pythonCommand }
    if (!validOwner(owner) || index.owners.length >= 4096) throw Error('Invalid backend cleanup launch')
    // First persist the owner. If directory creation/spawn later fails, retain it.
    await atomicIndex(statePath, { ...index, owners: [...index.owners, owner] })
    const directory = `${statePath}.receipts`
    ordinaryPath(directory)
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 })
    ordinaryPath(directory)
    return { environment: { AMADEUS_BACKEND_CLEANUP_LAUNCH: JSON.stringify({
      schema: CLEANUP_SCHEMA, state_path: statePath, owner,
    }) } }
  })
}

/** Reusing a backend without its original lifetime handle cannot prove cleanup. */
export async function markBackendCleanupUnconfirmed(statePath: string, { getBootId = readWindowsBootId }: { getBootId?: BootProbe } = {}): Promise<void> {
  return serializeCleanup(statePath, async () => {
    const index = reconcileBoot(readIndex(statePath) ?? { schema: CLEANUP_SCHEMA, unknown: false, owners: [] }, await getBootId())
    await atomicIndex(statePath, { ...index, unknown: true })
  })
}

async function processIdentityGone(identity: ProcessIdentity): Promise<boolean> {
  if (process.platform !== 'win32') return false
  const result = JSON.parse(await powershell(`$ErrorActionPreference='Stop'; try { $p=[System.Diagnostics.Process]::GetProcessById(${identity.pid}); $created=$p.StartTime.ToUniversalTime().ToFileTimeUtc().ToString(); @{state='running';created=$created}|ConvertTo-Json -Compress } catch [System.ArgumentException] { '{"state":"absent"}' } catch { exit 3 }`))
  return result.state === 'absent' || (result.state === 'running'
    && typeof result.created === 'string' && /^\d+$/.test(result.created) && result.created !== identity.created)
}

/** Fresh means only that this protocol has never registered an owner. Legacy
 * orphan processes require an operator migration check; absence cannot prove them.
 * Fully verified owners are discharged from the live index; receipts stay archived. */
export async function verifyPreviousBackendCleanup(statePath: string,
  { isProcessIdentityGone = processIdentityGone, getBootId = readWindowsBootId }: {
    isProcessIdentityGone?: (identity: ProcessIdentity) => Promise<boolean>;
    getBootId?: BootProbe;
  } = {}): Promise<boolean> {
  try {
    return await serializeCleanup(statePath, async () => {
      const saved = readIndex(statePath)
      if (saved === null) return true
      const index = reconcileBoot(saved, await getBootId())
      if (index !== saved) await atomicIndex(statePath, index)
      if (index.unknown) return false
      for (const owner of index.owners) {
        const receipt = readJson(path.join(`${statePath}.receipts`, `${owner.id}.json`)) as {
          schema: string; state: string; owner: CleanupOwner; process: ProcessIdentity; observer: ProcessIdentity;
          proof: string; job_name: string; providers_settled: boolean; work_settled: boolean;
        }
        if (!receipt || receipt.schema !== CLEANUP_SCHEMA || !['exited', 'settled'].includes(receipt.state)
          || !validOwner(receipt.owner) || Object.keys(owner).some(key => owner[key as keyof CleanupOwner] !== receipt.owner[key as keyof CleanupOwner])
          || receipt.proof !== 'windows-job-drained'
          // Old receipts remain valid only with their original complete shape.
          || receipt.state === 'settled' && (receipt.providers_settled !== true || receipt.work_settled !== true)
          || receipt.job_name !== `Global\\AmadeusBackendCleanup-${owner.id}`
          || ![receipt.process, receipt.observer].every(identity => identity
            && Number.isSafeInteger(identity.pid) && identity.pid > 0
            && typeof identity.created === 'string' && /^[1-9]\d{10,20}$/.test(identity.created))
          || !await isProcessIdentityGone(receipt.process) || !await isProcessIdentityGone(receipt.observer)) return false
      }
      // Every registered root and observer was positively verified. Receipts stay
      // archived; future checks only inspect owners not yet discharged here.
      if (index.owners.length) await atomicIndex(statePath, { ...index, owners: [] })
      return true
    })
  } catch { return false }
}

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
  { platform = process.platform, forceStop = forceStopBackendTree, waitForExit = waitForProcessExit,
    verifyCleanup }: {
    platform?: string; forceStop?: typeof forceStopBackendTree; waitForExit?: typeof waitForProcessExit;
    verifyCleanup?: () => Promise<boolean>;
  } = {}): Promise<boolean> {
  const requested = await requestShutdown()
  if (requested && await waitForExit(proc, usesScheduledExtensionUpdates(platform) ? 15000 : 2500)) {
    // Preserve the pre-existing POSIX graceful-root shutdown contract. This is
    // not descendant proof and is never used for the Windows update boundary.
    if (!usesScheduledExtensionUpdates(platform)) return true
    if (!verifyCleanup) return false
    // The out-of-Job observer publishes only after root exit. Give that final
    // durable write a bounded verification grace period; never infer success.
    const deadline = Date.now() + 2000
    do {
      try { if (await verifyCleanup()) return true } catch { return false }
      if (Date.now() >= deadline) return false
      await new Promise(resolve => setTimeout(resolve, 50))
    } while (true)
  }
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
  // This stop call does not infer child exit from taskkill. The independent Job
  // observer may subsequently publish physical exit proof, even after a crash.
  return false
}
