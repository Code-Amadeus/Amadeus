import { useEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n'
import VNAbilities, { type VNCapabilityPresets } from './VNAbilities'

export type VNProfileSettings = {
  id?: string; name: string; textSource: 'agent' | 'luna'; gameExe: string; hookHelper: string
  scriptPath: string; lunaWsUrl: string; launchGame: boolean; launchMethod: 'exe' | 'steam'; steamAppId: string
  launchOverlay: boolean; stopWallpaper: boolean; closeGameOnStop: boolean
  promptPack: 'base' | 'mystery'; voiceInput: boolean
}
type Props = {
  initial?: Partial<VNProfileSettings> & { overlayExists?: boolean }
  overlayAvailable?: boolean; capabilityPresets: VNCapabilityPresets
  agentExe: string; onClose: () => void
  onSave: (profile: VNProfileSettings, agentExe: string, test: boolean) => Promise<void>
}

export default function VNProfileEditor({ initial, overlayAvailable = false, capabilityPresets, agentExe, onClose, onSave }: Props) {
  const { t } = useI18n()
  const [profile, setProfile] = useState<VNProfileSettings>(() => ({
    name: '', textSource: 'agent', gameExe: '', hookHelper: '', scriptPath: '', lunaWsUrl: '',
    launchGame: true, launchMethod: 'exe', steamAppId: '', launchOverlay: false,
    stopWallpaper: true, closeGameOnStop: false, voiceInput: false,
    promptPack: initial?.id === 'paranormasight' ? 'mystery' : 'base', ...initial,
  }))
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
    <div className="vn-field"><label htmlFor={`vn-file-${kind}`}>{t(label)}</label><div className="vn-file-field">
      <input id={`vn-file-${kind}`} required={required} value={value} placeholder={t('Choose a file…')} onChange={e => update(e.target.value)} />
      <button type="button" aria-label={`${t('Browse')}: ${t(label)}`} onClick={() => void browse(kind, update)}>{t('Browse')}</button>
    </div></div>
  )
  return <dialog ref={dialog} className="vn-profile-dialog" aria-labelledby="vn-profile-title"
    onCancel={event => { event.preventDefault(); if (!busy) onClose() }}>
    <form onInvalidCapture={event => {
      const target = event.target as HTMLElement
      target.closest('details')?.setAttribute('open', '')
    }} onSubmit={async event => {
      event.preventDefault()
      const test = (event.nativeEvent as SubmitEvent).submitter?.getAttribute('value') === 'test'
      setBusy(true); setError('')
      try {
        // Send editable settings only; capabilities are derived by the host.
        const { id, name, textSource, gameExe, hookHelper, scriptPath, lunaWsUrl, launchGame, launchMethod, steamAppId, launchOverlay, stopWallpaper, closeGameOnStop, promptPack, voiceInput } = profile
        await onSave({ ...(id ? { id } : {}), name, textSource, gameExe, hookHelper, scriptPath, lunaWsUrl, launchGame, launchMethod, steamAppId, launchOverlay, stopWallpaper, closeGameOnStop, promptPack, voiceInput }, agent, test)
      } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
      finally { setBusy(false) }
    }}>
      <header className="vn-editor-heading">
        <div><h2 id="vn-profile-title">{t(initial?.id ? 'Edit game profile' : 'Add game')}</h2>
          <p>{t('Set up once. Next time, just start playing.')}</p></div>
        <button type="button" className="vn-icon-button" aria-label={t('Close')} disabled={busy} onClick={onClose}>×</button>
      </header>
      <fieldset disabled={busy} className="vn-editor-fields">
        <div className="vn-editor-body">
          <section className="vn-editor-section" aria-labelledby="vn-game-heading">
            <h3 id="vn-game-heading"><span>1</span>{t('Your game')}</h3>
            <label className="vn-field">{t('Game name')}<input autoFocus required maxLength={120} value={profile.name} onChange={e => set('name', e.target.value)} /></label>
            <fieldset className="vn-type-picker"><legend>{t('Game type')}</legend>
              {(['base', 'mystery'] as const).map(kind => <label key={kind} className={profile.promptPack === kind ? 'selected' : ''}>
                <input type="radio" name="game-type" value={kind} checked={profile.promptPack === kind} onChange={() => set('promptPack', kind)} />
                <span><strong>{t(kind === 'base' ? 'General VN' : 'Mystery VN')}</strong>
                  <small>{t(kind === 'base' ? 'Follow the story, characters and everyday conversations.' : 'Follow the story and connect clues, hypotheses and earlier events.')}</small></span>
              </label>)}
            </fieldset>
            <div className="vn-type-abilities"><p>{t('Companion abilities follow the game type automatically.')}</p>
              <VNAbilities compact preset={capabilityPresets[profile.promptPack]} /></div>
          </section>
          <section className="vn-editor-section" aria-labelledby="vn-connection-heading">
            <h3 id="vn-connection-heading"><span>2</span>{t('Connect game text')}</h3>
            <label className="vn-field">{t('Text source')}<select value={profile.textSource} onChange={e => set('textSource', e.target.value as 'agent' | 'luna')}>
              <option value="agent">0xDC00 Agent</option><option value="luna">{t('Luna original text (experimental)')}</option>
            </select></label>
            {profile.textSource === 'agent' ? <>
              {fileField('Game executable', 'game', profile.gameExe, value => set('gameExe', value))}
              <label className="vn-field">{t('Launch game with')}<select value={!profile.launchGame ? 'manual' : profile.launchMethod} onChange={e => setProfile(previous => ({ ...previous, launchGame: e.target.value !== 'manual', launchMethod: e.target.value === 'steam' ? 'steam' : 'exe' }))}>
                <option value="exe">{t('Game executable')}</option><option value="steam">Steam</option><option value="manual">{t('I will start the game')}</option>
              </select></label>
              {profile.launchGame && profile.launchMethod === 'steam' && <label className="vn-field">{t('Steam app ID')}
                <input required inputMode="numeric" pattern="[1-9][0-9]{0,9}" value={profile.steamAppId} onChange={e => set('steamAppId', e.target.value)} placeholder="3345060" />
                <small>{t('The number in the game’s Steam store URL. Use the demo’s own ID when playing a demo.')}</small></label>}
              {!profile.launchGame && <p className="vn-help">{t('Start the game yourself. Agent will attach automatically when you click Start.')}</p>}
              {fileField('Game hook script (.js)', 'hook', profile.hookHelper, value => set('hookHelper', value))}
              <p className="vn-help">{t('Use the Agent script for this game. The full story script is a separate, optional file.')}</p>
              <details className="vn-editor-details" open={!agent || undefined}>
                <summary>{t('Agent installation')}<span>{t(agent ? 'Already configured · shared by all games' : 'Choose once for all games')}</span></summary>
                {fileField('Agent installation (shared by all games)', 'agent', agent, setAgent)}
              </details>
            </> : <>
              <label className="vn-field">{t('Luna WebSocket URL')}<input required value={profile.lunaWsUrl} onChange={e => set('lunaWsUrl', e.target.value)} placeholder="ws://127.0.0.1:port/api/ws/text/origin" /></label>
              <p className="vn-help">{t('Start the game and configure extraction in Luna first. VN Player connects to its original-text stream.')}</p>
              {fileField('Game executable (optional for game view)', 'game', profile.gameExe, value => set('gameExe', value), false)}
            </>}
          </section>
          <details className="vn-editor-details">
            <summary>{t('Story script and play preferences')}<span>{t('Optional')}</span></summary>
            <div className="vn-preferences">
              {fileField('Full script for alignment', 'script', profile.scriptPath, value => set('scriptPath', value), false)}
              <p className="vn-help">{t('Live text is enough to begin. Mystery lookahead becomes available when a full script is aligned.')}</p>
              <label><input type="checkbox" checked={profile.voiceInput} onChange={e => set('voiceInput', e.target.checked)} /> {t('Voice input when play starts')}</label>
              <label><input type="checkbox" checked={profile.launchOverlay} disabled={!canLaunchOverlay} onChange={e => set('launchOverlay', e.target.checked)} /> {t('Portrait overlay')}</label>
              {!canLaunchOverlay && <p className="vn-help">{t('Overlay helper is unavailable for this game.')}</p>}
              {profile.textSource === 'agent' && <>
                <label><input type="checkbox" checked={profile.stopWallpaper} onChange={e => set('stopWallpaper', e.target.checked)} /> {t('Exit wallpaper before game')}</label>
                <label><input type="checkbox" checked={profile.closeGameOnStop} onChange={e => set('closeGameOnStop', e.target.checked)} /> {t('Close games launched by VN Player on stop')}</label>
              </>}
            </div>
          </details>
          {error && <p role="alert" className="vn-error">{error}</p>}
        </div>
        <footer className="vn-editor-actions">
          <p>{t('Test a few lines before your first play session.')}</p>
          <button type="button" onClick={onClose}>{t('Cancel')}</button>
          <button type="submit" value="save">{t('Save')}</button>
          <button type="submit" value="test" className="vn-primary">{t(busy ? 'Saving…' : 'Save and test text')}</button>
        </footer>
      </fieldset>
    </form>
  </dialog>
}
