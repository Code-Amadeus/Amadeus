import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { flushSync } from 'react-dom'
import { useI18n } from '../i18n'
import FluentIcon, { type FluentIconName } from './FluentIcon'
import { CardShell, SettingsGroup } from './SettingsPrimitives'
import VNAbilities, { type VNCapabilityPresets } from './VNAbilities'
import VNCropOverlay, { type CropRect } from './VNCropOverlay'

export type VNProfileSettings = {
  id?: string; name: string; textSource: 'agent' | 'luna' | 'ocr'; gameExe: string; hookHelper: string
  scriptPath: string; lunaWsUrl: string; launchGame: boolean; launchMethod: 'exe' | 'steam'; steamAppId: string
  launchOverlay: boolean; stopWallpaper: boolean; closeGameOnStop: boolean
  promptPack: 'base' | 'mystery'; voiceInput: boolean; visionMode: 'off' | 'on_question'
  commentaryFrequency: 'quiet' | 'balanced' | 'frequent'
  terminology: string
  ocrCropRect?: CropRect
  ocrWindowName?: string
  ocrWindowId?: number
  ocrAppName?: string
  ocrLanguage?: string
  ocrIntervalMs?: number
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
  send?: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
}

export default function VNProfileEditor({ initial, overlayAvailable = false, capabilityPresets, agentExe, inspectGame, onClose, onSave, send }: Props) {
  const { t } = useI18n()
  const [profile, setProfile] = useState<VNProfileSettings>(() => ({
    name: '', textSource: 'agent', gameExe: '', hookHelper: '', scriptPath: '', lunaWsUrl: '',
    launchGame: initial?.textSource === 'ocr' ? false : true, launchMethod: 'exe', steamAppId: '', launchOverlay: overlayAvailable,
    stopWallpaper: true, closeGameOnStop: false, voiceInput: false, visionMode: 'off',
    commentaryFrequency: 'balanced', terminology: '',
    promptPack: initial?.id === 'paranormasight' ? 'mystery' : 'base',
    ocrCropRect: initial?.ocrCropRect || { x: 0, y: 0.65, w: 1, h: 0.35 },
    ocrWindowName: initial?.ocrWindowName || '',
    ocrWindowId: initial?.ocrWindowId,
    ocrAppName: initial?.ocrAppName || '',
    ocrLanguage: initial?.ocrLanguage || 'ja,zh-Hans,zh-Hant,en-US',
    ocrIntervalMs: initial?.ocrIntervalMs || 700,
    ...initial,
    ...(initial?.textSource === 'ocr' ? { launchGame: false, gameExe: '' } : {}),
  }))
  const [windows, setWindows] = useState<Array<{ id: number; windowId: number; title: string; app: string; appName: string; width: number; height: number }>>([])
  const [fetchingWindows, setFetchingWindows] = useState(false)
  const [previewImage, setPreviewImage] = useState<string>('')
  const [capturingPreview, setCapturingPreview] = useState(false)
  const previewRequest = useRef(0)
  const [section, setSection] = useState<EditorSection>('game')
  const [agent, setAgent] = useState(agentExe)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const dialog = useRef<HTMLDialogElement>(null)
  const canLaunchOverlay = initial?.overlayExists ?? overlayAvailable
  useEffect(() => { dialog.current?.showModal() }, [])
  const set = <K extends keyof VNProfileSettings>(key: K, value: VNProfileSettings[K]) =>
    setProfile(previous => ({ ...previous, [key]: value }))

  const fetchWindows = useCallback(async () => {
    if (!send) return
    setFetchingWindows(true)
    try {
      const res = await send('vn.ocr.windows', {})
      const winList = (Array.isArray(res.windows) ? res.windows : []) as Array<{ id: number; windowId: number; title: string; app: string; appName: string; width: number; height: number }>
      setWindows(winList)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setFetchingWindows(false)
    }
  }, [send])

  useEffect(() => {
    if (section === 'connection' && profile.textSource === 'ocr') {
      void fetchWindows()
    }
  }, [section, profile.textSource, fetchWindows])

  const capturePreview = useCallback(async () => {
    if (!send) return
    const request = ++previewRequest.current
    setCapturingPreview(true)
    setError('')
    try {
      const res = await send('vn.ocr.preview', {
        windowId: profile.ocrWindowId,
        windowName: profile.ocrWindowName,
        appName: profile.ocrAppName,
      })
      if (request !== previewRequest.current) return
      if (res.error) {
        throw new Error(String(res.error))
      }
      if (res.imageBase64 && typeof res.imageBase64 === 'string') {
        setPreviewImage(res.imageBase64)
      } else {
        throw new Error(t('Window preview not found or empty.'))
      }
    } catch (err) {
      if (request === previewRequest.current) setError(err instanceof Error ? err.message : String(err))
    } finally {
      if (request === previewRequest.current) setCapturingPreview(false)
    }
  }, [send, profile.ocrWindowId, profile.ocrWindowName, profile.ocrAppName, t])

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
      const invalid = event.currentTarget.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>('input:invalid, select:invalid, textarea:invalid')
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
        const { id, name, textSource, gameExe, hookHelper, scriptPath, lunaWsUrl, launchGame, launchMethod, steamAppId, launchOverlay, stopWallpaper, closeGameOnStop, promptPack, voiceInput, visionMode, commentaryFrequency, terminology, ocrCropRect, ocrWindowName, ocrWindowId, ocrAppName, ocrLanguage, ocrIntervalMs } = profile
        await onSave({ ...(id ? { id } : {}), name, textSource, gameExe: textSource === 'ocr' ? '' : gameExe, hookHelper, scriptPath, lunaWsUrl, launchGame: textSource === 'ocr' ? false : launchGame, launchMethod, steamAppId, launchOverlay, stopWallpaper, closeGameOnStop, promptPack, voiceInput, visionMode, commentaryFrequency, terminology, ocrCropRect, ocrWindowName, ocrWindowId, ocrAppName, ocrLanguage, ocrIntervalMs }, agent, test)
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
            <details className="vn-editor-details"><summary>{t('Game terminology (optional)')}</summary>
              <label className="vn-field">{t('Terms and preferred names')}
                <textarea rows={4} maxLength={2000} value={profile.terminology} onChange={e => set('terminology', e.target.value)} />
                <small>{t('Optional naming references for this game. Terms are never generated automatically or treated as facts about story events.')}</small>
              </label>
            </details>
          </SettingsGroup></div>
          <div className="vn-editor-panel" role="tabpanel" id="vn-panel-connection" data-vn-panel="connection" aria-labelledby="vn-tab-connection" hidden={section !== 'connection'}>
          <SettingsGroup title="Text connection" detail="Use your existing extraction tool and a script that matches this game.">
            <CardShell vertical>
            <label className="vn-field">{t('Text source')}<select value={profile.textSource} onChange={e => {
              const src = e.target.value as 'agent' | 'luna' | 'ocr'
              setProfile(current => ({
                ...current,
                textSource: src,
                launchGame: src === 'ocr' ? false : (current.gameExe ? current.launchGame : src === 'agent'),
                gameExe: src === 'ocr' ? '' : current.gameExe,
              }))
            }}>
              <option value="agent">0xDC00 Agent</option>
              <option value="luna">{t('Luna original text (experimental)')}</option>
              <option value="ocr">{t('macOS Vision OCR (Window Capture)')}</option>
            </select></label></CardShell>
            {profile.textSource !== 'ocr' ? <>
              {fileField('Game executable', 'game', profile.gameExe, value => set('gameExe', value), profile.textSource === 'agent' || profile.launchGame)}
              <CardShell vertical><label className="vn-field">{t('Launch game with')}<select value={!profile.launchGame ? 'manual' : profile.launchMethod} onChange={e => setProfile(previous => ({ ...previous, launchGame: e.target.value !== 'manual', launchMethod: e.target.value === 'steam' ? 'steam' : 'exe' }))}>
                <option value="exe">{t('Game executable')}</option><option value="steam">Steam</option><option value="manual">{t('I will start the game')}</option>
              </select></label></CardShell>
              {profile.launchGame && profile.launchMethod === 'steam' && <label className="vn-field">{t('Steam app ID')}
                <input required inputMode="numeric" pattern="[1-9][0-9]{0,9}" value={profile.steamAppId} onChange={e => set('steamAppId', e.target.value)} placeholder="3345060" />
                <small>{t('The number in the game’s Steam store URL. Use the demo’s own ID when playing a demo.')}</small></label>}
              {!profile.launchGame && <p className="vn-help">{t('Start the game first. VN Player will connect to the selected text source.')}</p>}
            </> : (
              <p className="vn-help">{t('OCR mode directly captures an open game window. Game executable is not required.')}</p>
            )}
            {profile.textSource === 'ocr' ? <>
              <CardShell vertical>
                <div className="vn-field">
                  <label className="settings-field-label" htmlFor="vn-ocr-window">{t('Target Game Window')}</label>
                  <div className="vn-file-field">
                    <select
                      id="vn-ocr-window"
                      value={profile.ocrWindowId || ''}
                      onChange={e => {
                        const wid = Number(e.target.value) || undefined
                        const win = windows.find(w => w.id === wid)
                        ++previewRequest.current
                        setCapturingPreview(false)
                        setPreviewImage('')
                        setProfile(curr => ({
                          ...curr,
                          ocrWindowId: wid,
                          ocrWindowName: win?.title || '',
                          ocrAppName: win?.appName || win?.app || '',
                          name: curr.name || win?.title || win?.appName || '',
                        }))
                      }}
                    >
                      <option value="">{t('Choose an open game window…')}</option>
                      {windows.map(win => (
                        <option key={win.id} value={win.id}>
                          [{win.appName || win.app}] {win.title ? `${win.title} ` : ''}({win.width}×{win.height})
                        </option>
                      ))}
                    </select>
                    <button type="button" onClick={() => void fetchWindows()} disabled={fetchingWindows}>
                      {t(fetchingWindows ? 'Refreshing…' : 'Refresh')}
                    </button>
                  </div>
                </div>
              </CardShell>

              <CardShell vertical>
                <div className="vn-field">
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '8px' }}>
                    <label className="settings-field-label">{t('Dialogue Extraction Area (Drag to Adjust)')}</label>
                    <button
                      type="button"
                      onClick={() => void capturePreview()}
                      disabled={capturingPreview || (!profile.ocrWindowId && !profile.ocrWindowName && !profile.ocrAppName && !profile.gameExe)}
                      style={{ fontSize: '11px', padding: '4px 10px', minHeight: '28px' }}
                    >
                      {t(capturingPreview ? 'Capturing…' : previewImage ? 'Refresh Game Screenshot' : 'Capture Game Screenshot')}
                    </button>
                  </div>

                  {previewImage ? (
                    <VNCropOverlay
                      imageSrc={previewImage}
                      crop={profile.ocrCropRect || { x: 0, y: 0.65, w: 1, h: 0.35 }}
                      onChange={crop => set('ocrCropRect', crop)}
                    />
                  ) : (
                    <div
                      style={{
                        padding: '24px 16px',
                        border: '2px dashed var(--border)',
                        borderRadius: '8px',
                        textAlign: 'center',
                        backgroundColor: 'var(--surface-alt)',
                      }}
                    >
                      <p style={{ margin: '0 0 10px', fontSize: '12px', color: 'var(--muted)' }}>
                        {t('Select your game window above and click Capture to display your game screen, then drag the blue selection box to cover the dialogue area.')}
                      </p>
                      <button
                        type="button"
                        onClick={() => void capturePreview()}
                        disabled={capturingPreview || (!profile.ocrWindowId && !profile.ocrWindowName && !profile.ocrAppName && !profile.gameExe)}
                      >
                        {t(capturingPreview ? 'Capturing…' : 'Capture Game Screenshot')}
                      </button>
                    </div>
                  )}

                  <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', alignItems: 'center', marginTop: '6px' }}>
                    <span style={{ fontSize: '11px', color: 'var(--muted)', marginRight: '4px' }}>{t('Quick Presets')}:</span>
                    {[
                      { label: t('Bottom 35% (VN Default)'), crop: { x: 0, y: 0.65, w: 1, h: 0.35 } },
                      { label: t('Bottom 50%'), crop: { x: 0, y: 0.50, w: 1, h: 0.50 } },
                      { label: t('Bottom 25%'), crop: { x: 0, y: 0.75, w: 1, h: 0.25 } },
                      { label: t('Full Window'), crop: { x: 0, y: 0, w: 1, h: 1 } },
                    ].map(preset => (
                      <button
                        key={preset.label}
                        type="button"
                        onClick={() => set('ocrCropRect', preset.crop)}
                        style={{ fontSize: '11px', padding: '3px 8px', minHeight: '26px' }}
                      >
                        {preset.label}
                      </button>
                    ))}
                  </div>

                  <small style={{ color: 'var(--muted)', marginTop: '4px' }}>
                    {t('Selection area')}: X: {Math.round((profile.ocrCropRect?.x ?? 0) * 100)}%, Y: {Math.round((profile.ocrCropRect?.y ?? 0.65) * 100)}%, W: {Math.round((profile.ocrCropRect?.w ?? 1) * 100)}%, H: {Math.round((profile.ocrCropRect?.h ?? 0.35) * 100)}%
                  </small>
                </div>
              </CardShell>

              <CardShell vertical>
                <label className="vn-field">
                  {t('OCR Languages')}
                  <select
                    value={profile.ocrLanguage || 'ja,zh-Hans,zh-Hant,en-US'}
                    onChange={e => set('ocrLanguage', e.target.value)}
                  >
                    <option value="ja,zh-Hans,zh-Hant,en-US">{t('Auto (Japanese, Chinese, English)')}</option>
                    <option value="ja">{t('Japanese (ja)')}</option>
                    <option value="zh-Hans">{t('Simplified Chinese (zh-Hans)')}</option>
                    <option value="zh-Hant">{t('Traditional Chinese (zh-Hant)')}</option>
                    <option value="en-US">{t('English (en-US)')}</option>
                    <option value="ja,en-US">{t('Japanese + English')}</option>
                    <option value="zh-Hans,en-US">{t('Simplified Chinese + English')}</option>
                  </select>
                </label>
              </CardShell>

              <CardShell vertical>
                <label className="vn-field">
                  {t('OCR Scan Interval')}: {profile.ocrIntervalMs || 700} ms
                  <input
                    type="range"
                    min="300"
                    max="2000"
                    step="100"
                    value={profile.ocrIntervalMs || 700}
                    onChange={e => set('ocrIntervalMs', Number(e.target.value))}
                  />
                  <small>{t('Lower interval reacts faster; higher interval saves CPU/GPU resources.')}</small>
                </label>
              </CardShell>
            </> : profile.textSource === 'agent' ? <>
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
