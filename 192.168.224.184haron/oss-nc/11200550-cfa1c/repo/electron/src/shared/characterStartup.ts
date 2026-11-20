// Shared desktop settings facts. Keep this module free of process and UI dependencies.
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

export function settingSourceLabel(source: string): string {
  if (source === 'environment') return 'Process environment'
  if (source === 'user') return 'Desktop settings'
  if (source === 'dotenv') return '.env'
  return 'Built-in default'
}
