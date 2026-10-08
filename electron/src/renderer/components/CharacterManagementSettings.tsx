import { useCallback, useEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n'
import { CardShell } from './SettingsPrimitives'
import { characterDraftLengths, characterCatalog, nextStartupCharacter, saveCharacterDraft, saveStartupCharacter, type CharacterLimits, type CharacterDraft, type CharacterRecord, type ActiveCharacter, type CharacterDesktopSettings } from './characterManagement'

interface Props {
  send: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
  connected: boolean
  restarting: boolean
  onRestart: () => Promise<void>
  desktop: CharacterDesktopSettings | null
  kurisuPreview: unknown
  onSettingsChanged: (settings: Record<string, unknown>) => void
}

export function CharacterRoleLabel({ character, active, nextStart }: {
  character: CharacterRecord; active: boolean; nextStart: boolean
}) {
  const { t } = useI18n()
  return <>
    <div className="settings-field-label">{character.name}
      {character.builtin ? ` · ${t('Built-in')}` : ''}
      {active ? ` · ${t('Active')}` : ''}
      {nextStart ? ` · ${t('Next start')}` : ''}
    </div>
    <div className="settings-field-description"><code style={{ overflowWrap: 'anywhere' }}>{character.character_id}</code></div>
    {active && character.valid && character.pending_restart
      ? <div role="status" className="settings-field-description">{t('Modified. Restart the backend to apply.')}</div> : null}
  </>
}

export default function CharacterManagementSettings({ send, connected, restarting, onRestart, desktop, kurisuPreview, onSettingsChanged }: Props) {
  const { t } = useI18n()
  const [characters, setCharacters] = useState<CharacterRecord[]>([])
  const [active, setActive] = useState<ActiveCharacter | null>(null)
  const [limits, setLimits] = useState<CharacterLimits | null>(null)
  const [editor, setEditor] = useState<CharacterDraft | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const generation = useRef(0)
  const runtimeActive = connected ? active : null
  const selection = nextStartupCharacter(desktop, runtimeActive)
  const next = characters.find(character => character.character_id === selection.characterId)
  const preview = kurisuPreview && typeof kurisuPreview === 'object' ? kurisuPreview as Record<string, unknown> : {}
  const kurisuPersona = typeof preview.effective === 'string' ? preview.effective : ''
  const disabled = busy || restarting || !connected
  const lengths = editor ? characterDraftLengths(editor) : null
  const nameTooLong = Boolean(limits && lengths && lengths.name > limits.name_max_chars)
  const personaTooLong = Boolean(limits && lengths && lengths.persona > limits.persona_max_chars)

  const refresh = useCallback(async () => {
    const current = generation.current
    const catalog = characterCatalog(await send('character.list'))
    if (current !== generation.current) return
    setCharacters(catalog.characters)
    setActive(catalog.active)
    setLimits(catalog.limits)
  }, [send])

  useEffect(() => {
    generation.current += 1
    setActive(null)
    setLimits(null)
    if (!connected) return
    let current = true
    void send('character.list').then(response => {
      if (!current) return
      const catalog = characterCatalog(response)
      setCharacters(catalog.characters)
      setActive(catalog.active)
      setLimits(catalog.limits)
    }).catch(reason => { if (current) setError(reason instanceof Error ? reason.message : 'Could not load roles.') })
    return () => { current = false }
  }, [connected, send])

  const perform = async (action: () => Promise<void>) => {
    setBusy(true)
    setError('')
    setNotice('')
    try { await action() }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not save role.') }
    finally { setBusy(false) }
  }

  const select = (characterId: string) => perform(async () => {
    if (!window.amadeus) throw new Error('Startup role selection is available in the Electron app.')
    const settings = await saveStartupCharacter(characterId, send, window.amadeus.updateDesktopSettings)
    if (settings) onSettingsChanged(settings)
    setNotice('Startup role saved. Restart the backend to apply.')
    await refresh()
  })

  const save = () => perform(async () => {
    if (!editor || !limits || nameTooLong || personaTooLong) return
    await saveCharacterDraft(editor, send)
    setEditor(null)
    setNotice('Role saved for the next backend start. The active role remains unchanged.')
    await refresh()
  })

  return <CardShell vertical>
    <div className="character-prompt-editor">
      <div className="settings-field-description">{t('All roles follow the application’s appearance and voice settings.')}</div>
      <div className="settings-field-label">{runtimeActive ? t('Active in this backend: {name}', { name: runtimeActive.name }) : t('Active role unavailable until the backend connects.')}</div>
      <div className="settings-field-description">{selection.characterId
        ? t('Next backend start: {name}', { name: next?.name || selection.characterId })
        : t('Next backend start: resolved from {source} at startup.', { source: selection.source === 'dotenv' ? '.env' : t('launch environment') })}</div>
      <div className="settings-field-description">{selection.locked
        ? t('Startup role is controlled by your launch environment (AMADEUS_CHARACTER_ID).')
        : t('Selecting a startup role or editing a user role takes effect after a backend restart.')}</div>
      {!connected ? <div className="settings-field-description">{t('Connect the backend to list, create or edit roles.')}</div> : null}
      <div className="character-prompt-actions">
        <button type="button" disabled={disabled} onClick={() => { setError(''); setNotice(''); setEditor({ characterId: null, name: '', persona: '' }) }}>{t('New role')}</button>
        <button type="button" disabled={disabled} onClick={() => void perform(refresh)}>{t('Refresh roles')}</button>
        <button type="button" disabled={disabled || !desktop} onClick={() => void onRestart()}>{t(restarting ? 'Restarting…' : 'Restart backend to apply')}</button>
      </div>
      <ul style={{ listStyle: 'none', padding: 0, margin: '8px 0' }} aria-label={t('Character roles')}>
        {characters.map(character => <li key={character.character_id} data-character-id={character.character_id} style={{ padding: '10px 0', borderTop: '1px solid var(--card-border)' }}>
          <CharacterRoleLabel character={character} active={runtimeActive?.character_id === character.character_id}
            nextStart={selection.characterId === character.character_id} />
          {!character.valid ? <div role="alert" className="settings-field-description">{t('Invalid role')}: {character.error || t('Validation failed.')}</div> : null}
          {character.edit_error ? <div className="settings-field-description">{t(character.edit_error)}</div> : null}
          <div className="character-prompt-actions">
            <button type="button" disabled={disabled || !character.valid || selection.locked || !desktop}
              onClick={() => void select(character.character_id)}>{t('Use at next start')}</button>
            {!character.builtin ? <button type="button" disabled={disabled || !character.valid || !character.editable}
              onClick={() => { setError(''); setNotice(''); setEditor({ characterId: character.character_id, name: character.name, persona: character.persona }) }}>{t('Edit role')}</button> : null}
          </div>
        </li>)}
      </ul>
      {editor ? <div>
        <div className="settings-field-label">{t(editor.characterId ? 'Edit user role' : 'Create user role')}</div>
        <label className="settings-field-label" htmlFor="character-role-name">{t('Role name')}</label>
        <input id="character-role-name" type="text" aria-describedby="character-name-limits" aria-invalid={nameTooLong} value={editor.name} disabled={disabled}
          onChange={event => setEditor({ ...editor, name: event.target.value })}
          className="w-full min-w-0 text-[12px] bg-[var(--surface-alt)] border border-[var(--border)] rounded-lg px-3 outline-none focus:border-[var(--accent)] disabled:opacity-50"
          style={{ height: 34, marginBottom: 8 }} />
        {limits && lengths ? <div id="character-name-limits" className="settings-field-description">
          {t('Name: {count}/{limit} characters. Use a single line.', { count: lengths.name, limit: limits.name_max_chars })}
        </div> : null}
        <label className="settings-field-label" htmlFor="character-role-persona">{t('Persona')}</label>
        <div className="settings-field-description">{t('Write the role’s personality and speaking style. An empty persona uses the application’s ordinary defaults.')}</div>
        <textarea id="character-role-persona" rows={8} aria-describedby="character-persona-limits" aria-invalid={personaTooLong} value={editor.persona} disabled={disabled}
          onChange={event => setEditor({ ...editor, persona: event.target.value })} />
        {limits && lengths ? <div id="character-persona-limits" className="settings-field-description">
          {t('Persona: {count}/{limit} characters. Leading and trailing whitespace is removed.', { count: lengths.persona, limit: limits.persona_max_chars })}
        </div> : null}
        {nameTooLong || personaTooLong ? <div role="alert" className="settings-field-description">{t('Shorten the highlighted field before saving.')}</div> : null}
        {!editor.characterId ? <div className="character-prompt-actions">
          <button type="button" disabled={disabled || !kurisuPersona} onClick={() => setEditor({ ...editor, persona: kurisuPersona })}>{t('Prefill from current Kurisu Japanese persona')}</button>
          <span className="settings-field-description">{t('This text may contain Kurisu’s name. Review and edit the visible text before saving your new role.')}</span>
        </div> : null}
        <div className="character-prompt-actions">
          <button type="button" disabled={disabled || !limits || !editor.name.trim() || nameTooLong || personaTooLong} onClick={() => void save()}>{t(busy ? 'Saving…' : 'Save role')}</button>
          <button type="button" disabled={busy} onClick={() => setEditor(null)}>{t('Cancel')}</button>
        </div>
      </div> : null}
      {error ? <div role="alert" className="settings-field-description">{t(error)}</div> : null}
      {notice ? <div role="status" className="settings-field-description">{t(notice)}</div> : null}
    </div>
  </CardShell>
}
