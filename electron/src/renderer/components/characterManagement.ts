import { STARTUP_CHARACTER_KEY, startupCharacterSelection } from '../../main/backendStartup'

export type CharacterRecord = {
  character_id: string
  name: string
  persona: string
  builtin: boolean
  valid: boolean
  editable: boolean
  error?: string
  edit_error?: string
}
export type ActiveCharacter = {
  character_id: string
  name: string
  display_name: string
  short_name: string
  ui_name: string
  accessible_name: string
}
export type CharacterDesktopSettings = {
  values?: Record<string, string>
  sources?: Record<string, string>
  locked?: Record<string, boolean>
}
type Send = (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>

export type CharacterDraft = { characterId: string | null; name: string; persona: string }

export function saveCharacterDraft(draft: CharacterDraft, send: Send): Promise<Record<string, unknown>> {
  return send(draft.characterId ? 'character.update' : 'character.create', {
    ...(draft.characterId ? { character_id: draft.characterId } : {}),
    name: draft.name.trim(), persona: draft.persona,
  })
}

export function characterCatalog(response: Record<string, unknown>): { characters: CharacterRecord[]; active: ActiveCharacter | null } {
  const characters = Array.isArray(response.characters) ? response.characters.filter((record): record is CharacterRecord =>
    Boolean(record && typeof record === 'object' && typeof record.character_id === 'string'
      && typeof record.name === 'string' && typeof record.persona === 'string')) : []
  const active = response.active as ActiveCharacter | undefined
  return { characters, active: active && typeof active.character_id === 'string' && typeof active.name === 'string' ? active : null }
}

export function nextStartupCharacter(settings: CharacterDesktopSettings | null, active: ActiveCharacter | null) {
  const selection = startupCharacterSelection(settings || {})
  // Parent environment cannot change during this desktop process. The active
  // projection is the resolved identity when that environment owns selection.
  return selection.source === 'environment'
    ? { ...selection, characterId: active?.character_id || null } : selection
}

export async function saveStartupCharacter(characterId: string, send: Send, save: (update: {
  values: Record<string, string>
}) => Promise<{ ok: boolean; error?: string; settings?: Record<string, unknown> }>): Promise<Record<string, unknown> | undefined> {
  const validated = await send('character.validate', { character_id: characterId })
  const character = validated.character as CharacterRecord | undefined
  if (!character?.valid || character.character_id !== characterId) {
    throw new Error(character?.error || 'This role is invalid and cannot be selected for startup.')
  }
  const saved = await save({ values: { [STARTUP_CHARACTER_KEY]: characterId } })
  if (!saved.ok) throw new Error(saved.error || 'Could not save the startup role.')
  return saved.settings
}
