export interface SettingsEditorState { dirty: boolean; busy: boolean }

// One decision for both a local editor transition and leaving the Settings page.
// A pending write cannot be discarded; an untouched editor needs no confirmation.
export function canLeaveSettingsEditors(states: SettingsEditorState[], confirmDiscard: () => boolean): boolean {
  if (states.some(state => state.busy)) return false
  return !states.some(state => state.dirty) || confirmDiscard()
}

export const DISCARD_CONNECTION_CHANGES = 'Discard unsaved connection changes?'
