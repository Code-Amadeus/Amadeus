import fs from 'node:fs'
import path from 'node:path'
import { randomBytes } from 'node:crypto'
import { createConnection } from 'node:net'
import { assertExtensionPath } from './extensionPaths.js'
import type { StoredExtension } from './desktopSettings.js'

/** A timeout/unreachable listener is unknown, never proof of stopped ownership. */
export function localPortIsClosed(port: number): Promise<boolean> {
  return new Promise(resolve => {
    const socket = createConnection({ host: '127.0.0.1', port })
    const finish = (closed: boolean) => { socket.destroy(); resolve(closed) }
    socket.setTimeout(1000, () => finish(false))
    socket.once('connect', () => finish(false))
    socket.once('error', (error: NodeJS.ErrnoException) => finish(error.code === 'ECONNREFUSED'))
  })
}

/** Hold the main-process launch boundary for the entire stopped-backend transaction. */
export class ExtensionUpdateBoundary {
  private held: 'install' | 'recovery' | undefined
  constructor(private readonly stopped: () => Promise<boolean>,
    private readonly recoveryIdle: () => Promise<boolean> = stopped) {}
  assertHeld = (): void => {
    if (this.held !== 'install') throw new Error('Plugin installation requires the stopped-backend update boundary; schedule it for the next app start.')
  }
  assertRecoveryHeld = (): void => {
    if (!this.held) throw new Error('Plugin recovery requires the stopped-backend recovery boundary; schedule it for the next app start.')
  }
  assertBackendMayStart(): void {
    if (this.held) throw new Error('Plugin maintenance is in progress; backend start is blocked.')
  }
  async run<T>(operation: () => Promise<T>): Promise<T> {
    return this.exclusive('install', this.stopped, operation)
  }
  /** Restore retained records without deleting files or claiming child cleanup. */
  async runRecovery<T>(operation: () => Promise<T>): Promise<T> {
    return this.exclusive('recovery', this.recoveryIdle, operation)
  }
  private async exclusive<T>(kind: 'install' | 'recovery', idle: () => Promise<boolean>, operation: () => Promise<T>): Promise<T> {
    if (this.held) throw new Error('Another plugin maintenance transaction is in progress.')
    this.held = kind
    try {
      if (!await idle()) throw new Error(kind === 'install'
        ? 'The backend or its MCP/Work processes are still active or unconfirmed; plugin installation is blocked. After an unexpected exit, restart Windows if cleanup cannot be verified. Retained-version recovery remains available.'
        : 'The backend is still active; stop it before restoring the retained plugin version.')
      return await operation()
    } finally { this.held = undefined }
  }
}

export type ExtensionUpdateRecord = {
  format: 'amadeus.extension-update/v1'; owner: string
  phase: 'pending' | 'startup_confirmed' | 'recovery_required'
  previous: StoredExtension; target: StoredExtension
}

/** Records only Host-owned package records; secret values remain OS-encrypted. */
export class ExtensionUpdateJournal {
  readonly owner = randomBytes(16).toString('hex')
  constructor(private readonly root: string, private readonly validate: (raw: unknown) => StoredExtension) {}
  private get file(): string { return path.join(this.root, 'update-rollback.json') }
  read(validate = this.validate): ExtensionUpdateRecord | undefined {
    assertExtensionPath(this.root, this.file)
    if (!fs.existsSync(this.file)) return
    if (fs.statSync(this.file).size > 2 * 1024 * 1024) throw new Error('Plugin recovery record is too large; installation is blocked.')
    const raw = JSON.parse(fs.readFileSync(this.file, 'utf8')) as ExtensionUpdateRecord
    if (raw?.format !== 'amadeus.extension-update/v1' || !/^[a-f0-9]{32}$/.test(raw.owner)
      || !['pending', 'startup_confirmed', 'recovery_required'].includes(raw.phase)) throw new Error('Plugin recovery record is invalid; installation is blocked.')
    const previous = validate(raw.previous), target = validate(raw.target)
    if (previous.id !== target.id || previous.installId === target.installId) throw new Error('Plugin recovery identities do not match.')
    return { format: raw.format, owner: raw.owner, phase: raw.phase, previous, target }
  }
  write(record: ExtensionUpdateRecord): void {
    fs.mkdirSync(this.root, { recursive: true })
    assertExtensionPath(this.root, this.file)
    const temporary = `${this.file}.${randomBytes(16).toString('hex')}.tmp`
    assertExtensionPath(this.root, temporary)
    const handle = fs.openSync(temporary, 'wx', 0o600)
    try { fs.writeFileSync(handle, JSON.stringify(record) + '\n', 'utf8'); fs.fsyncSync(handle) }
    finally { fs.closeSync(handle) }
    try { fs.renameSync(temporary, this.file) }
    finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary) }
  }
  clear(): void {
    assertExtensionPath(this.root, this.file)
    if (fs.existsSync(this.file)) fs.unlinkSync(this.file)
  }
}
