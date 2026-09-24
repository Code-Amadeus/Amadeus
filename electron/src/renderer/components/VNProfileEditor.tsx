import { useEffect, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { useI18n } from '../i18n'

export type VNCapability = 'immediate' | 'interaction' | 'summary' | 'retrospective' | 'lookahead' | 'reasoning'
export type VNCapabilities = Partial<Record<VNCapability, boolean>>
export const BASE_CAPABILITIES: VNCapabilities = { immediate: true, interaction: true, summary: true, retrospective: true, lookahead: false, reasoning: false }
export const MYSTERY_CAPABILITIES: VNCapabilities = { immediate: true, interaction: true, summary: true, retrospective: true, lookahead: true, reasoning: true }
export type VNProfileSettings = {
  id?: string; name: string; textSource: 'agent' | 'luna'; gameExe: string; hookHelper: string
  scriptPath: string; lunaWsUrl: string; launchGame: boolean; launchOverlay: boolean
  stopWallpaper: boolean; closeGameOnStop: boolean; promptPack: 'base' | 'mystery'
  capabilities: VNCapabilities; voiceInput: boolean
}
type Props = {
  initial?: Partial<VNProfileSettings> & { overlayExists?: boolean }
  overlayAvailable?: boolean
  agentExe: string; onClose: () => void
  onSave: (profile: VNProfileSettings, agentExe: string, test: boolean) => Promise<void>
}
const choices: Array<{ key: VNCapability; title: string; detail: string }> = [
  { key: 'immediate', title: 'Immediate commentary', detail: 'React to the current story moment.' },
  { key: 'interaction', title: 'Player interaction', detail: 'Answer your questions and respond to choices.' },
  { key: 'summary', title: 'Story summaries', detail: 'Summarize recent story events.' },
  { key: 'retrospective', title: 'Reflection', detail: 'Reflect on earlier story details.' },
  { key: 'lookahead', title: 'Lookahead', detail: 'Use a complete, aligned script to anticipate later events.' },
  { key: 'reasoning', title: 'Detective reasoning', detail: 'Reason about clues using the Mystery companion.' },
]
const fieldStyle = { width: '100%', minWidth: 0, padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 7, background: 'var(--bg)', color: 'var(--text)' }
const buttonStyle = { padding: '8px 12px', border: '1px solid var(--border)', borderRadius: 7, background: 'var(--surface)', color: 'var(--text)' }

export default function VNProfileEditor({ initial, overlayAvailable = false, agentExe, onClose, onSave }: Props) {
  const { t } = useI18n()
  const [profile, setProfile] = useState<VNProfileSettings>(() => {
    const promptPack = initial?.promptPack || (initial?.id === 'paranormasight' ? 'mystery' : 'base')
    return {
      name: '', textSource: 'agent', gameExe: '', hookHelper: '', scriptPath: '', lunaWsUrl: '',
      launchGame: true, launchOverlay: false, stopWallpaper: true, closeGameOnStop: false,
      voiceInput: promptPack === 'mystery', promptPack, ...initial,
      capabilities: { ...(promptPack === 'mystery' ? MYSTERY_CAPABILITIES : BASE_CAPABILITIES), ...initial?.capabilities },
    }
  })
  const [tab, setTab] = useState<'connection' | 'companion'>('connection')
  const [agent, setAgent] = useState(agentExe)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const dialog = useRef<HTMLDialogElement>(null)
  const canLaunchOverlay = initial?.overlayExists ?? overlayAvailable
  useEffect(() => { dialog.current?.showModal() }, [])
  const set = <K extends keyof VNProfileSettings>(key: K, value: VNProfileSettings[K]) =>
    setProfile(previous => ({ ...previous, [key]: value }))
  const browse = async (kind: 'game' | 'agent' | 'hook' | 'script', update: (path: string) => void) => {
    setError('')
    try {
      if (!window.amadeus?.selectVNFile) throw new Error(t('File selection is available in the desktop app. You can also enter an absolute path.'))
      const result = await window.amadeus.selectVNFile(kind)
      if (result.ok) update(result.path)
      else if (!result.cancelled) throw new Error(result.detail)
    } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
  }
  const fileField = (label: string, kind: 'game' | 'agent' | 'hook' | 'script', value: string, update: (path: string) => void, required = true) => (
    <label className="vn-field"><span>{t(label)}</span><span className="vn-file-field">
      <input aria-label={t(label)} required={required} value={value} onChange={e => update(e.target.value)} style={fieldStyle} />
      <button type="button" onClick={() => void browse(kind, update)} style={buttonStyle}>{t('Browse')}</button>
    </span></label>
  )
  return (
    <dialog ref={dialog} className="vn-profile-dialog" aria-labelledby="vn-profile-title"
      onCancel={event => { event.preventDefault(); if (!busy) onClose() }}>
      <form onInvalidCapture={event => {
        const field = event.target
        if (field instanceof HTMLElement && field.closest('.vn-editor-section')?.hasAttribute('hidden')) {
          flushSync(() => setTab('connection'))
          field.focus()
        }
      }} onSubmit={async event => {
        event.preventDefault()
        const test = (event.nativeEvent as SubmitEvent).submitter?.getAttribute('value') === 'test'
        setBusy(true); setError('')
        try {
          const { id, name, textSource, gameExe, hookHelper, scriptPath, lunaWsUrl, launchGame, launchOverlay, stopWallpaper, closeGameOnStop, promptPack, capabilities, voiceInput } = profile
          await onSave({ ...(id ? { id } : {}), name, textSource, gameExe, hookHelper, scriptPath, lunaWsUrl, launchGame, launchOverlay, stopWallpaper, closeGameOnStop, promptPack, capabilities, voiceInput }, agent, test)
        } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
        finally { setBusy(false) }
      }}>
        <h3 id="vn-profile-title">{t(initial?.id ? 'Edit game profile' : 'Add game')}</h3>
        <nav className="vn-editor-tabs" aria-label={t('Profile settings')}>
          <button type="button" aria-current={tab === 'connection' ? 'page' : undefined} onClick={() => setTab('connection')}>{t('Game connection')}</button>
          <button type="button" aria-current={tab === 'companion' ? 'page' : undefined} onClick={() => setTab('companion')}>{t('Companion')}</button>
        </nav>
        <fieldset disabled={busy} className="vn-editor-fields">
          <div hidden={tab !== 'connection'} className="vn-editor-section">
            <label className="vn-field">{t('Game name')}<input autoFocus required maxLength={120} value={profile.name} onChange={e => set('name', e.target.value)} style={fieldStyle} /></label>
            <label className="vn-field">{t('Text source')}<select value={profile.textSource} onChange={e => set('textSource', e.target.value as 'agent' | 'luna')} style={fieldStyle}>
              <option value="agent">0xDC00 Agent</option><option value="luna">{t('Luna original text (experimental)')}</option>
            </select></label>
            {profile.textSource === 'agent' ? <>
              {fileField('Game executable', 'game', profile.gameExe, value => set('gameExe', value))}
              {fileField('Agent installation (shared by all games)', 'agent', agent, setAgent)}
              {fileField('Game hook script (.js)', 'hook', profile.hookHelper, value => set('hookHelper', value))}
              <p className="vn-help">{t('Choose an existing script compatible with this game. Save and test to preview its extracted text.')}</p>
              <label><input type="checkbox" checked={profile.launchGame} onChange={e => set('launchGame', e.target.checked)} /> {t('Launch game if it is not running')}</label>
              {!profile.launchGame && <p className="vn-help">{t('Start the game yourself. Agent will attach automatically when you click Start.')}</p>}
              <label><input type="checkbox" checked={profile.stopWallpaper} onChange={e => set('stopWallpaper', e.target.checked)} /> {t('Exit wallpaper before game')}</label>
              <label><input type="checkbox" checked={profile.closeGameOnStop} onChange={e => set('closeGameOnStop', e.target.checked)} /> {t('Close games launched by VN Player on stop')}</label>
            </> : <>
              <label className="vn-field">{t('Luna WebSocket URL')}<input required value={profile.lunaWsUrl} onChange={e => set('lunaWsUrl', e.target.value)} placeholder="ws://127.0.0.1:<port>/api/ws/text/origin" style={fieldStyle} /></label>
              {fileField('Game executable (optional for game view)', 'game', profile.gameExe, value => set('gameExe', value), false)}
              <p className="vn-help">{t('Start the game and configure extraction in Luna first. VN Player connects to its original-text stream.')}</p>
            </>}
          </div>
          <div hidden={tab !== 'companion'} className="vn-editor-section">
            <label className="vn-field">{t('Companion type')}<select value={profile.promptPack} onChange={e => {
              const next = e.target.value as 'base' | 'mystery'
              setProfile(previous => ({ ...previous, promptPack: next, capabilities: { ...previous.capabilities, reasoning: next === 'mystery' && !!previous.capabilities.reasoning } }))
            }} style={fieldStyle}><option value="base">{t('Base companion')}</option><option value="mystery">{t('Mystery companion')}</option></select></label>
            <p className="vn-help">{t('Choose the companion style, then choose what it may do while you play.')}</p>
            <div className="vn-capability-list">{choices.map(choice => {
              const unavailable = choice.key === 'reasoning' && profile.promptPack !== 'mystery'
              return <label key={choice.key} className="vn-capability-option">
                <input type="checkbox" checked={!unavailable && !!profile.capabilities[choice.key]} disabled={unavailable}
                  onChange={e => set('capabilities', { ...profile.capabilities, [choice.key]: e.target.checked })} />
                <span><strong>{t(choice.title)}</strong><small>{t(choice.detail)}</small>
                  {choice.key === 'lookahead' && <small>{t('Requires a full script and verified alignment during play.')}</small>}
                  {unavailable && <small>{t('Available with Mystery companion.')}</small>}</span>
              </label>
            })}</div>
            {fileField('Full script for alignment', 'script', profile.scriptPath, value => set('scriptPath', value), false)}
            <label><input type="checkbox" checked={profile.voiceInput} onChange={e => set('voiceInput', e.target.checked)} /> {t('Voice input when play starts')}</label>
            <p className="vn-help">{t('You can also start or stop the microphone during play.')}</p>
            <label title={!canLaunchOverlay ? t('Overlay helper is unavailable for this game.') : undefined}>
              <input type="checkbox" checked={profile.launchOverlay} disabled={!canLaunchOverlay} onChange={e => set('launchOverlay', e.target.checked)} /> {t('Portrait overlay')}
            </label>
            {!canLaunchOverlay && <p className="vn-help">{t('Overlay helper is unavailable for this game.')}</p>}
          </div>
          {error && <p role="alert" className="vn-error">{error}</p>}
          <div className="vn-editor-actions">
            <button type="button" onClick={onClose} style={buttonStyle}>{t('Cancel')}</button>
            <button type="submit" value="save" style={buttonStyle}>{t('Save')}</button>
            <button type="submit" value="test" style={{ ...buttonStyle, color: 'var(--accent)' }}>{t('Save and test text')}</button>
          </div>
        </fieldset>
      </form>
    </dialog>
  )
}
