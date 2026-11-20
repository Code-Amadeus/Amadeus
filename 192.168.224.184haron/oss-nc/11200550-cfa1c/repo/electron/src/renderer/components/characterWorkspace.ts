export const CHARACTER_SECTION_KEY = 'amadeus.character.section'
export const CHARACTER_SECTIONS = ['overview', 'identity', 'appearance', 'knowledge'] as const
export type CharacterSection = typeof CHARACTER_SECTIONS[number]

export function characterSection(value: string | null): CharacterSection {
  return CHARACTER_SECTIONS.includes(value as CharacterSection) ? value as CharacterSection : 'overview'
}
