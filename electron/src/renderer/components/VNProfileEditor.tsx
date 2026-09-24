import { useEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n'

export type VNProfileSettings = {
  id?: string
  name: string
  textSource: 'agent' | 'luna'
  gameExe: string
  hookHelper: string
  scriptPath: string
  lunaWsUrl: string
  launchGame: boolean
  launchOverlay: boolean
  stopWallpaper: boolean
  closeGameOnStop: boolean
}

type Props = {
  initial?: Partial<VNProfileSettings>
  agentExe: string
  onClose: () => void
  onSave: (profile: VNProfileSettings, agentExe: string, test: boolean) => Promise<void>
}

const fieldStyle = { width: '100%', minWidth: 0, padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 7, background: 'var(--bg)', color: 'var(--text)' }
const buttonStyle = { padding: '8px 12px', border: '1px solid var(--border)', borderRadius: 7, background: 'var(--surface)', color: 'var(--text)' }

export default function VNProfileEditor({ initial, agentExe, onClose, onSave }: Props) {
  const { t } = useI18n()
  const [profile, setProfile] = useState<VNProfileSettings>({
    name: '', textSource: 'agent', gameExe: '', hookHelper: '', scriptPath: '', lunaWsUrl: '',
    launchGame: true, launchOverlay: false, stopWallpaper: true, closeGameOnStop: false, ...initial,
  })
  const [agent, setAgent] = useState(agentExe)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const dialog = useRef<HTMLDialogElement>(null)
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

  const fileField = (label: string, kind: 'game' | 'agent' | 'hook' | 'script', value: string, update: (value: string) => void, required = true) => (
    <label style={{ display: 'grid', gap: 5 }}>
      <span>{t(label)}</span>
      <span style={{ display: 'flex', gap: 6 }}>
        <input aria-label={t(label)} required={required} value={value} onChange={e => update(e.target.value)} style={fieldStyle} />
        <button type="button" onClick={() => void browse(kind, update)} style={buttonStyle}>{t('Browse')}</button>
      </span>
    </label>
  )

  return (
    <dialog ref={dialog} aria-labelledby="vn-profile-title" onCancel={event => { event.preventDefault(); if (!busy) onClose() }}
      style={{ margin: 'auto', width: 'min(620px, calc(100vw - 40px))', maxHeight: '85vh', padding: 24, border: '1px solid var(--border)', borderRadius: 12, color: 'var(--text)', background: 'var(--surface)' }}>
      <form onSubmit={async event => {
        event.preventDefault()
        const test = (event.nativeEvent as SubmitEvent).submitter?.getAttribute('value') === 'test'
        setBusy(true); setError('')
        try {
          // Only persist editable settings, never runtime presets or live process IDs.
          const { id, name, textSource, gameExe, hookHelper, scriptPath, lunaWsUrl, launchGame, launchOverlay, stopWallpaper, closeGameOnStop } = profile
          await onSave({ ...(id ? { id } : {}), name, textSource, gameExe, hookHelper, scriptPath, lunaWsUrl, launchGame, launchOverlay, stopWallpaper, closeGameOnStop }, agent, test)
        } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
        finally { setBusy(false) }
      }}>
        <h3 id="vn-profile-title" style={{ margin: '0 0 16px' }}>{t(initial?.id ? 'Edit game profile' : 'Add game')}</h3>
        <fieldset disabled={busy} style={{ border: 0, padding: 0, margin: 0, display: 'grid', gap: 14, fontSize: 13 }}>
          <label style={{ display: 'grid', gap: 5 }}>{t('Game name')}
            <input autoFocus required maxLength={120} value={profile.name} onChange={e => set('name', e.target.value)} style={fieldStyle} />
          </label>
          <label style={{ display: 'grid', gap: 5 }}>{t('Text source')}
            <select value={profile.textSource} onChange={e => set('textSource', e.target.value as 'agent' | 'luna')} style={fieldStyle}>
              <option value="agent">0xDC00 Agent</option>
              <option value="luna">{t('Luna original text (experimental)')}</option>
            </select>
          </label>
          {profile.textSource === 'agent' ? <>
            {fileField('Game executable', 'game', profile.gameExe, value => set('gameExe', value))}
            {fileField('Agent installation (shared by all games)', 'agent', agent, setAgent)}
            {fileField('Game hook script (.js)', 'hook', profile.hookHelper, value => set('hookHelper', value))}
            <p style={{ margin: 0, color: 'var(--muted)', lineHeight: 1.5 }}>{t('Choose an existing script compatible with this game. Save and test to preview its extracted text.')}</p>
            <label><input type="checkbox" checked={profile.launchGame} onChange={e => set('launchGame', e.target.checked)} /> {t('Launch game if it is not running')}</label>
            {!profile.launchGame && <p style={{ margin: 0, color: 'var(--muted)' }}>{t('Start the game yourself. Agent will attach automatically when you click Start.')}</p>}
            <label><input type="checkbox" checked={profile.stopWallpaper} onChange={e => set('stopWallpaper', e.target.checked)} /> {t('Exit wallpaper before game')}</label>
            <label><input type="checkbox" checked={profile.closeGameOnStop} onChange={e => set('closeGameOnStop', e.target.checked)} /> {t('Close games launched by VN Player on stop')}</label>
          </> : <>
            <label style={{ display: 'grid', gap: 5 }}>{t('Luna WebSocket URL')}
              <input required value={profile.lunaWsUrl} onChange={e => set('lunaWsUrl', e.target.value)} placeholder="ws://127.0.0.1:<port>/api/ws/text/origin" style={fieldStyle} />
            </label>
            <p style={{ margin: 0, color: 'var(--muted)' }}>{t('Start the game and configure extraction in Luna first. VN Player connects to its original-text stream.')}</p>
          </>}
          {initial?.id === 'paranormasight' && <details>
            <summary>{t('Existing companion settings')}</summary>
            <div style={{ display: 'grid', gap: 12, marginTop: 10 }}>
              {fileField('Full script for alignment', 'script', profile.scriptPath, value => set('scriptPath', value), false)}
              {profile.textSource === 'agent' && <label><input type="checkbox" checked={profile.launchOverlay} onChange={e => set('launchOverlay', e.target.checked)} /> {t('Portrait overlay')}</label>}
            </div>
          </details>}
          <p style={{ margin: 0, color: 'var(--muted)', lineHeight: 1.5 }}>{t('Text testing needs no full script and makes no companion responses. New games currently start in text capture mode.')}</p>
          {error && <p role="alert" style={{ color: '#C42B1C', margin: 0, whiteSpace: 'pre-wrap' }}>{error}</p>}
          <div style={{ display: 'flex', justifyContent: 'flex-end', flexWrap: 'wrap', gap: 8 }}>
            <button type="button" onClick={onClose} style={buttonStyle}>{t('Cancel')}</button>
            <button type="submit" value="save" style={buttonStyle}>{t('Save')}</button>
            <button type="submit" value="test" style={{ ...buttonStyle, color: 'var(--accent)' }}>{t('Save and test text')}</button>
          </div>
        </fieldset>
      </form>
    </dialog>
  )
}
