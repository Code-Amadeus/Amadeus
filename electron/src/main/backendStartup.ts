import type { ChildProcess } from 'node:child_process'

export const CHARACTER_STARTUP_EXIT_CODE = 78
export const STARTUP_CHARACTER_KEY = 'AMADEUS_CHARACTER_ID'

export type CharacterSelectionSource = 'environment' | 'user' | 'dotenv' | 'default'
export type StartupCharacterSelection = {
  characterId: string | null
  source: CharacterSelectionSource
  locked: boolean
}

export type BackendStartupFailure = {
  kind: 'character' | 'backend'
  detail: string
  selection: StartupCharacterSelection
}

export class BackendStartupExitError extends Error {
  constructor(readonly exitCode: number | null) {
    super(`Backend exited before readiness (code ${exitCode}).`)
  }
}

export async function waitForBackendReadiness(proc: ChildProcess | null, health: () => Promise<'ready' | 'starting' | 'foreign' | 'unavailable'>,
  port: number, timeoutMs = 120_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  let spawnError: Error | null = null
  const onError = (error: Error) => { spawnError = error }
  proc?.on('error', onError)
  try {
    while (Date.now() < deadline) {
      if (spawnError) throw spawnError
      const status = await health()
      if (status === 'ready') return
      if (status === 'foreign') throw new Error(`port ${port} is owned by another backend instance`)
      // Keep this exact launched process, even after the owner clears its slot.
      if (proc && (proc.exitCode !== null || proc.signalCode !== null)) throw new BackendStartupExitError(proc.exitCode)
      await new Promise(resolve => setTimeout(resolve, 250))
    }
    throw new Error(`backend did not become ready within ${timeoutMs}ms`)
  } finally { proc?.off('error', onError) }
}

export function startupCharacterSelection(snapshot: {
  values?: Record<string, unknown>
  sources?: Record<string, unknown>
  locked?: Record<string, unknown>
}): StartupCharacterSelection {
  const source = snapshot.sources?.[STARTUP_CHARACTER_KEY] as CharacterSelectionSource || 'default'
  const value = snapshot.values?.[STARTUP_CHARACTER_KEY]
  return {
    characterId: typeof value === 'string' ? value : source === 'default' ? 'kurisu' : null,
    source,
    locked: snapshot.locked?.[STARTUP_CHARACTER_KEY] === true,
  }
}

export function backendStartupFailure(exitCode: number | null | undefined, detail: string, selection: StartupCharacterSelection): BackendStartupFailure {
  return {
    kind: exitCode === CHARACTER_STARTUP_EXIT_CODE ? 'character' : 'backend',
    detail,
    selection,
  }
}

/** A deliberate recovery saves an explicit built-in choice before a new launch. */
export async function recoverCharacterStartup<T>(failure: BackendStartupFailure | null, actions: {
  save: (update: { values: Record<string, string> }) => T
  stop: () => Promise<void>
  start: () => Promise<void>
}): Promise<T> {
  if (failure?.kind !== 'character') throw new Error('No character startup failure is available to recover.')
  if (failure.selection.locked) throw new Error('Startup character is locked by the parent process environment.')
  const settings = actions.save({ values: { [STARTUP_CHARACTER_KEY]: 'kurisu' } })
  await actions.stop()
  await actions.start()
  return settings
}
