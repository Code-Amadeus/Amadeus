import type { CharacterRecord } from './characterManagement'

type SessionCharacter = Pick<CharacterRecord, 'character_id' | 'name' | 'valid'>
type Translate = (source: string, variables?: Record<string, string | number>) => string

export function foreignSessionCharacter(
  characterId: unknown, currentCharacterId: string, characters: readonly SessionCharacter[] | null,
): { id: string; name: string; available: boolean | null } | null {
  if (!currentCharacterId || typeof characterId !== 'string' || !characterId || characterId === currentCharacterId) return null
  const character = characters?.find(record => record.character_id === characterId)
  return { id: characterId, name: character?.name || characterId, available: characters === null ? null : character?.valid === true }
}

export function sessionCharacterNotice(
  characterId: unknown, currentCharacterId: string, characters: readonly SessionCharacter[] | null,
  connected: boolean, startupCharacterLocked: boolean, t: Translate,
): string {
  if (!connected || !currentCharacterId || typeof characterId !== 'string' || !characterId) {
    return t('Chat character identity is unavailable. Reconnect to the backend.')
  }
  const character = foreignSessionCharacter(characterId, currentCharacterId, characters)
  if (!character) return ''
  const name = character.name === character.id ? character.id : `${character.name} (${character.id})`
  return [
    t('This chat belongs to {character}.', { character: name }),
    character.available === false ? t('This role is unavailable. Restore or repair it before switching.') : '',
    startupCharacterLocked
      ? t('Change AMADEUS_CHARACTER_ID to {characterId} in your launch environment and restart the backend to open, rename, or delete this chat.', { characterId: character.id })
      : t('In Settings → Characters → Identity & persona, select this role with Use at next start, then restart the backend to open, rename, or delete this chat.'),
  ].filter(Boolean).join(' ')
}
