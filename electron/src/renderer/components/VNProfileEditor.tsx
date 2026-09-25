import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { flushSync } from 'react-dom'
import { useI18n } from '../i18n'
import FluentIcon, { type FluentIconName } from './FluentIcon'
import { CardShell, SettingsGroup } from './SettingsPrimitives'
import VNAbilities, { type VNCapabilityPresets } from './VNAbilities'

export type VNProfileSettings = {
  id?: string; name: string; textSource: 'agent' | 'luna'; gameExe: string; hookHelper: string
  scriptPath: string; lunaWsUrl: string; launchGame: boolean; launchMethod: 'exe' | 'steam'; steamAppId: string
  launchOverlay: boolean; stopWallpaper: boolean; closeGameOnStop: boolean
  promptPack: 'base' | 'mystery'; voiceInput: boolean; visionMode: 'off' | 'on_question'
  commentaryFrequency: 'quiet' | 'balanced' | 'frequent'
}
type EditorSection = 'game' | 'connection' | 'preferences'
const sections: Array<{ id: EditorSection; title: string; icon: FluentIconName }> = [
  { id: 'game', title: 'Game and companion', icon: 'Movie' },
  { id: 'connection', title: 'Text connection', icon: 'CommandPrompt' },
  { id: 'preferences', title: 'Play preferences', icon: 'Setting' },
]
type Props = {
  initial?: Partial<VNProfileSettings> & { overlayExists?: boolean }
  overlayAvailable?: boolean; capabilityPresets: VNCapabilityPresets
  agentExe: string; onClose: () => void
  inspectGame: (gameExe: string) => Promise<Record<string, unknown>>
  onSave: (profile: VNProfileSettings, agentExe: string, test: boolean) => Promise<void>
}

export default function VNProfileEditor({ initial, overlayAvailable = false, capabilityPresets, agentExe, inspectGame, onClose, onSave }: Props) {
  const { t } = useI18n()
  const [profile, setProfile] = useState<VNProfileSettings>(() => ({
    name: '', textSource: 'agent', gameExe: '', hookHelper: '', scriptPath: '', lunaWsUrl: '',
    launchGame: true, launchMethod: 'exe', steamAppId: '', launchOverlay: overlayAvailable,
    stopWallpaper: true, closeGameOnStop: false, voiceInput: false, visionMode: 'off',
    commentaryFrequency: 'balanced',
    promptPack: initial?.id === 'paranormasight' ? 'mystery' : 'base', ...initial,
  }))
  const [section, setSection] = useState<EditorSection>('game')
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
      const startPath = kind === 'hook' && agent ? agent.replace(/[\\/][^\\/]+$/, '/data/scripts') : undefined
      const result = await window.amadeus.selectVNFile(kind, startPath)
      if (result.ok) {
        update(result.path)
        if (kind === 'game') await detectSteam(result.path)
      }
      else if (!result.cancelled) throw new Error(result.detail)
    } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
  }
  const detectSteam = async (gameExe: string) => {
    const detected = await inspectGame(gameExe)
    if (detected.steamAppId) setProfile(current => current.gameExe === gameExe ? {
      ...current, name: current.name || String(detected.name || ''),
      launchMethod: 'steam', steamAppId: String(detected.steamAppId),
    } : current)
  }
  const openHelp = async (page: 'agent' | 'scripts') => {
    try {
      if (!window.amadeus?.openVNHelp) throw new Error('Official help links are available in the desktop app.')
      await window.amadeus.openVNHelp(page)
    }
    catch (err) { setError(err instanceof Error ? err.message : String(err)) }
  }
  const moveSection = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? sections.length - 1
      : ['ArrowDown', 'ArrowRight'].includes(event.key) ? (index + 1) % sections.length
      : ['ArrowUp', 'ArrowLeft'].includes(event.key) ? (index + sections.length - 1) % sections.length : -1
    if (next < 0) return
    event.preventDefault()
    setSection(sections[next].id)
    event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role=tab]')[next]?.focus()
  }
  const fileField = (label: string, kind: 'game' | 'agent' | 'hook' | 'script', value: string, update: (path: string) => void, required = true) => (
    <CardShell vertical><div className="vn-field"><label className="settings-field-label" htmlFor={`vn-file-${kind}`}>{t(label)}</label><div className="vn-file-field">
      <input id={`vn-file-${kind}`} required={required} value={value} placeholder={t('Choose a file…')} onChange={e => update(e.target.value)} />
      <button type="button" aria-label={`${t('Browse')}: ${t(label)}`} onClick={() => void browse(kind, update)}>{t('Browse')}</button>
    </div></div></CardShell>
  )
  return <dialog ref={dialog} className="vn-profile-dialog" aria-labelledby="vn-profile-title"
    onCancel={event => { event.preventDefault(); if (!busy) onClose() }}>
    <form noValidate onSubmit={async event => {
      event.preventDefault()
      const invalid = event.currentTarget.querySelector<HTMLInputElement | HTMLSelectElement>('input:invalid, select:invalid')
      if (invalid) {
        const panel = invalid.closest<HTMLElement>('[data-vn-panel]')?.dataset.vnPanel as EditorSection | undefined
        if (panel) flushSync(() => setSection(panel))
        invalid.closest('details')?.setAttribute('open', '')
        invalid.focus()
        invalid.reportValidity()
        return
      }
      const test = (event.nativeEvent as SubmitEvent).submitter?.getAttribute('value') === 'test'
      setBusy(true); setError('')
      try {
        // Send editable settings only; capabilities are derived by the host.
        const { id, name, textSource, gameExe, hookHelper, scriptPath, lunaWsUrl, launchGame, launchMethod, steamAppId, launchOverlay, stopWallpaper, closeGameOnStop, promptPack, voiceInput, visionMode, commentaryFrequency } = profile
        await onSave({ ...(id ? { id } : {}), name, textSource, gameExe, hookHelper, scriptPath, lunaWsUrl, launchGame, launchMethod, steamAppId, launchOverlay, stopWallpaper, closeGameOnStop, promptPack, voiceInput, visionMode, commentaryFrequency }, agent, test)
      } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
      finally { setBusy(false) }
    }}>
      <header className="vn-editor-heading">
        <div><h2 id="vn-profile-title" className="settings-panel-title">{t(initial?.id ? 'Edit game profile' : 'Add game')}</h2>
          <p>{t('Set up once. Next time, just start playing.')}</p></div>
        <button type="button" className="vn-icon-button" aria-label={t('Close')} disabled={busy} onClick={onClose}>×</button>
      </header>
      <fieldset disabled={busy} className="vn-editor-fields">
        <div className="vn-editor-layout">
          <nav className="settings-section-nav vn-editor-nav" role="tablist" aria-label={t('Profile settings')} aria-orientation="vertical">
            {sections.map((item, index) => <button key={item.id} type="button" role="tab" id={`vn-tab-${item.id}`} aria-controls={`vn-panel-${item.id}`}
              aria-selected={section === item.id} tabIndex={section === item.id ? 0 : -1} onKeyDown={event => moveSection(event, index)} onClick={() => setSection(item.id)}>
              <FluentIcon name={item.icon} size={15} aria-hidden="true" />{t(item.title)}
            </button>)}
          </nav>
          <div className="vn-editor-body settings-scroll-area">
          <div className="vn-editor-panel" role="tabpanel" id="vn-panel-game" data-vn-panel="game" aria-labelledby="vn-tab-game" hidden={section !== 'game'}>
          <SettingsGroup title="Game and companion" detail="Choose the game type. Its companion abilities are configured for you.">
            <CardShell vertical>
            <label className="vn-field">{t('Game name')}<input autoFocus required maxLength={120} value={profile.name} onChange={e => set('name', e.target.value)} /></label></CardShell>
            <fieldset className="vn-type-picker"><legend>{t('Game type')}</legend>
              {(['base', 'mystery'] as const).map(kind => <label key={kind} className={profile.promptPack === kind ? 'selected' : ''}>
                <input type="radio" name="game-type" value={kind} checked={profile.promptPack === kind} onChange={() => set('promptPack', kind)} />
                <span><strong>{t(kind === 'base' ? 'General VN' : 'Mystery VN')}</strong>
                  <small>{t(kind === 'base' ? 'Follow the story, characters and everyday conversations.' : 'Follow the story and connect clues, hypotheses and earlier events.')}</small></span>
              </label>)}
            </fieldset>
            <CardShell vertical><div className="vn-type-abilities"><p>{t('Companion abilities follow the game type automatically.')}</p>
              <VNAbilities compact preset={capabilityPresets[profile.promptPack]} /></div></CardShell>
          </SettingsGroup></div>
          <div className="vn-editor-panel" role="tabpanel" id="vn-panel-connection" data-vn-panel="connection" aria-labelledby="vn-tab-connection" hidden={section !== 'connection'}>
          <SettingsGroup title="Text connection" detail="Use your existing extraction tool and a script that matches this game.">
            <CardShell vertical>
            <label className="vn-field">{t('Text source')}<select value={profile.textSource} onChange={e => setProfile(current => ({ ...current, textSource: e.target.value as 'agent' | 'luna', launchGame: current.gameExe ? current.launchGame : e.target.value === 'agent' }))}>
              <option value="agent">0xDC00 Agent</option><option value="luna">{t('Luna original text (experimental)')}</option>
            </select></label></CardShell>
              {fileField('Game executable', 'game', profile.gameExe, value => set('gameExe', value), profile.textSource === 'agent' || profile.launchGame)}
              <CardShell vertical><label className="vn-field">{t('Launch game with')}<select value={!profile.launchGame ? 'manual' : profile.launchMethod} onChange={e => setProfile(previous => ({ ...previous, launchGame: e.target.value !== 'manual', launchMethod: e.target.value === 'steam' ? 'steam' : 'exe' }))}>
                <option value="exe">{t('Game executable')}</option><option value="steam">Steam</option><option value="manual">{t('I will start the game')}</option>
              </select></label></CardShell>
              {profile.launchGame && profile.launchMethod === 'steam' && <label className="vn-field">{t('Steam app ID')}
                <input required inputMode="numeric" pattern="[1-9][0-9]{0,9}" value={profile.steamAppId} onChange={e => set('steamAppId', e.target.value)} placeholder="3345060" />
                <small>{t('The number in the game’s Steam store URL. Use the demo’s own ID when playing a demo.')}</small></label>}
              {!profile.launchGame && <p className="vn-help">{t('Start the game first. VN Player will connect to the selected text source.')}</p>}
            {profile.textSource === 'agent' ? <>
              {fileField('Game hook script (.js)', 'hook', profile.hookHelper, value => set('hookHelper', value))}
              <p className="vn-help">{t('Use the Agent script for this game. The full story script is a separate, optional file.')}</p>
              <button type="button" onClick={() => void openHelp('scripts')}>{t('Find a game script on the official repository')}</button>
              <details className="vn-editor-details setting-card" open={!agent || undefined}>
                <summary>{t('Agent installation')}<span>{t(agent ? 'Already configured · shared by all games' : 'Choose once for all games')}</span></summary>
                {fileField('Agent installation (shared by all games)', 'agent', agent, setAgent)}
                <p className="vn-help">{t('Download and extract Agent, then select agent.exe. Keep its data folder beside it. This is needed only once.')}</p>
                <button type="button" onClick={() => void openHelp('agent')}>{t('Open official Agent downloads')}</button>
              </details>
            </> : <>
              <label className="vn-field">{t('Luna WebSocket URL')}<input required value={profile.lunaWsUrl} onChange={e => set('lunaWsUrl', e.target.value)} placeholder="ws://127.0.0.1:port/api/ws/text/origin" /></label>
              <p className="vn-help">{t('Start the game and configure extraction in Luna first. VN Player connects to its original-text stream.')}</p>
            </>}
          </SettingsGroup></div>
          <div className="vn-editor-panel" role="tabpanel" id="vn-panel-preferences" data-vn-panel="preferences" aria-labelledby="vn-tab-preferences" hidden={section !== 'preferences'}>
          <SettingsGroup title="Play preferences" detail="Optional story context and controls for this game.">
            <div className="vn-preferences">
              <CardShell vertical><label className="vn-field">{t('Commentary frequency')}<select value={profile.commentaryFrequency} onChange={e => set('commentaryFrequency', e.target.value as VNProfileSettings['commentaryFrequency'])}>
                <option value="quiet">{t('Occasional')}</option><option value="balanced">{t('Balanced')}</option><option value="frequent">{t('Frequent')}</option>
              </select></label><p className="vn-help">{t('Controls spontaneous comments, not answers to your questions.')}</p></CardShell>
              {fileField('Full script for alignment', 'script', profile.scriptPath, value => set('scriptPath', value), false)}
              <p className="vn-help">{t('Live text is enough to begin. Mystery lookahead becomes available when a full script is aligned.')}</p>
              <label className="vn-preference-row setting-card"><input type="checkbox" checked={profile.voiceInput} onChange={e => set('voiceInput', e.target.checked)} /> {t('Voice input when play starts')}</label>
              <CardShell vertical><div className="vn-field"><label htmlFor="vn-start-vision">{t('VN vision at session start')}</label>
                <select id="vn-start-vision" value={profile.visionMode} onChange={e => set('visionMode', e.target.value as 'off' | 'on_question')}>
                  <option value="off">{t('Off')}</option><option value="on_question">{t('Read the game view when I ask')}</option>
                </select></div><p className="vn-help">{t('VN only. Typed and spoken questions use a fresh game-window image. General vision settings stay separate.')}</p></CardShell>
              <p className="vn-help">{t('These are startup defaults. Voice and vision can be changed independently during each play session.')}</p>
              <label className="vn-preference-row setting-card"><input type="checkbox" checked={profile.launchOverlay} disabled={!canLaunchOverlay && !profile.launchOverlay} onChange={e => set('launchOverlay', e.target.checked)} /> {t('Portrait overlay')}</label>
              <p className="vn-help">{t('Portrait art is optional. Without the Companion Lite pack, the window shows a simple avatar and captions.')}</p>
              {!canLaunchOverlay && <p className="vn-help">{t('Overlay helper is unavailable for this game.')}</p>}
              {profile.launchGame && <>
                <label className="vn-preference-row setting-card"><input type="checkbox" checked={profile.stopWallpaper} onChange={e => set('stopWallpaper', e.target.checked)} /> {t('Exit wallpaper before game')}</label>
                <label className="vn-preference-row setting-card"><input type="checkbox" checked={profile.closeGameOnStop} onChange={e => set('closeGameOnStop', e.target.checked)} /> {t('Close games launched by VN Player on stop')}</label>
              </>}
            </div>
          </SettingsGroup></div>
          {error && <p role="alert" className="vn-error settings-feedback">{error}</p>}
        </div></div>
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
