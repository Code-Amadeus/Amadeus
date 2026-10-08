import { useCallback, useEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n'
import {
  inspectedVisualProfile, receiveVisualSnapshot, selectedVisualProfile,
  visualConfigurationChanged, visualStatusFromFrame,
  type VisualCapabilities, type VisualConfiguration, type VisualEditor,
  type VisualLayout, type VisualProfile, type VisualSnapshot,
  type VisualSurface, type VisualSurfaceStatus,
} from './characterVisuals'
import '../styles/characterVisuals.css'

interface Props {
  send: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
  subscribe: (method: string, fn: (p: Record<string, unknown>) => void) => () => void
  connected: boolean
}

interface Preview {
  url: string
  config: Record<string, unknown>
  fingerprint: string
  context: string
  status: VisualSurfaceStatus | null
}

const EMOTIONS = ['normal', 'neutral', 'smile', 'happy', 'thinking', 'angry', 'sad',
  'disappointed', 'work', 'working', 'serious_speaking', 'shy', 'blush', 'surprised']
const APPROXIMATE_EMOTIONS = new Set(['work', 'working', 'serious_speaking'])
const LOAD_LABELS = { loading: 'Loading…', ready: 'Ready', error: 'Error', unloaded: 'Unloaded' }

function snapshotFromResponse(response: Record<string, unknown>): VisualSnapshot {
  if (!response.config) throw new Error('Visual configuration is unavailable.')
  return response as unknown as VisualSnapshot
}

function previewFingerprint(profile: VisualProfile | undefined, corePath: string, surface: VisualSurface): string {
  return JSON.stringify({ profile, corePath, surface })
}

function previewContext(config: VisualConfiguration | null, profile: VisualProfile | undefined): string {
  return JSON.stringify([config?.backend, profile?.profile_id, profile?.model_path, config?.core_path])
}

export function VisualDiagnostic({ diagnostic }: { diagnostic?: VisualSurfaceStatus['diagnostic'] }) {
  const { t } = useI18n()
  if (!diagnostic) return null
  if (typeof diagnostic === 'string') return <p className="visuals-note">{diagnostic}</p>
  return <div>
    <p className="visuals-note">{t('Active expression')}: {diagnostic.expression || t('Default pose')}</p>
    {diagnostic.mouth_ids && <p className="visuals-note">{t('Mouth parameters')}: {diagnostic.mouth_ids.join(', ') || t('None declared')}</p>}
    {Boolean(diagnostic.warnings?.length) && <ul className="visuals-warning-list">
      {diagnostic.warnings!.map((warning, index) => <li key={index}>{warning}</li>)}
    </ul>}
    <details className="visuals-note">
      <summary>{t('Runtime details')}</summary>
      <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 10 }}>{JSON.stringify(diagnostic, null, 2)}</pre>
    </details>
  </div>
}

function NumberField({ label, value, min, max, step, onChange, disabled = false }: {
  label: string; value: number; min: number; max: number; step: number; disabled?: boolean; onChange: (value: number) => void
}) {
  return <div className="visuals-number-row">
    <label>{label}</label>
    <input type="number" aria-label={label} value={value} min={min} max={max} step={step} disabled={disabled}
      onChange={event => {
        if (!event.target.value) return
        const number = Number(event.target.value)
        if (Number.isFinite(number)) onChange(Math.min(max, Math.max(min, number)))
      }} />
  </div>
}

export default function CharacterVisualsPage({ send, subscribe, connected }: Props) {
  const { t } = useI18n()
  const [editor, setEditor] = useState<VisualEditor>({ applied: null, draft: null })
  const [capabilities, setCapabilities] = useState<Record<string, VisualCapabilities>>({})
  const [capabilityError, setCapabilityError] = useState({ path: '', message: '' })
  const [modelPath, setModelPath] = useState('')
  const [mouthIds, setMouthIds] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [previewSurface, setPreviewSurface] = useState<VisualSurface>('render')
  const [mouthValue, setMouthValue] = useState(0)
  const previewFrameRef = useRef<HTMLIFrameElement>(null)
  const previewRef = useRef<Preview | null>(null)
  const previewRequestRef = useRef(0)
  const draft = editor.draft
  const profile = selectedVisualProfile(draft)
  const capability = profile ? capabilities[profile.model_path] : undefined
  const dirty = visualConfigurationChanged(editor)
  const previewStale = Boolean(preview && preview.fingerprint !== previewFingerprint(profile, draft?.core_path || '', previewSurface))
  const previewReady = Boolean(preview && preview.status?.state === 'ready' && !previewStale)
  const unavailable = Boolean(busy) || !connected

  const postPreview = useCallback((action: string, fields: Record<string, unknown> = {}) => {
    previewFrameRef.current?.contentWindow?.postMessage({
      type: 'amadeus.visual.preview', action, ...fields,
    }, '*')
  }, [])

  const closePreview = useCallback(() => {
    previewRequestRef.current += 1
    postPreview('destroy')
    previewRef.current = null
    setPreview(null)
    setMouthValue(0)
  }, [postPreview])

  const run = useCallback(async (action: string, operation: () => Promise<void>) => {
    setBusy(action)
    setError('')
    setNotice('')
    try { await operation() }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
    finally { setBusy('') }
  }, [])

  const acceptSnapshot = useCallback((snapshot: VisualSnapshot, replaceDraft = false) => {
    setEditor(current => receiveVisualSnapshot(current, snapshot, replaceDraft))
    const appliedProfile = selectedVisualProfile(snapshot.config)
    if (appliedProfile) setCapabilities(current => ({
      ...current, [appliedProfile.model_path]: snapshot.capabilities,
    }))
  }, [])

  useEffect(() => {
    const unsubscribe = subscribe('visual.updated', response => acceptSnapshot(snapshotFromResponse(response)))
    if (connected) void run('loading', async () => {
      acceptSnapshot(snapshotFromResponse(await send('visual.get', {})))
    })
    return unsubscribe
  }, [acceptSnapshot, connected, run, send, subscribe])

  useEffect(() => {
    setModelPath(profile?.model_path || '')
    setMouthIds(profile?.mouth.parameter_ids.join(', ') || '')
  }, [profile?.profile_id, profile?.model_path])

  useEffect(() => {
    if (!connected || !profile || capability) return
    let cancelled = false
    const path = profile.model_path
    setCapabilityError({ path, message: '' })
    send('visual.inspect', { model_path: path }).then(response => {
      if (!cancelled) setCapabilities(current => ({
        ...current, [path]: response.capabilities as unknown as VisualCapabilities,
      }))
    }).catch(reason => {
      if (!cancelled) setCapabilityError({ path, message: reason instanceof Error ? reason.message : String(reason) })
    })
    return () => { cancelled = true }
  }, [capability, connected, profile?.model_path, send])

  useEffect(() => {
    if (preview && preview.context !== previewContext(draft, profile)) closePreview()
  }, [closePreview, draft, preview, profile])

  const handlePreviewMessage = useCallback((event: MessageEvent) => {
    const ownedWindow = previewFrameRef.current?.contentWindow
    const current = previewRef.current
    if (!current || !ownedWindow || event.source !== ownedWindow) return
    if (event.data?.type === 'amadeus.visual.preview.ready') {
      postPreview('configure', { config: current.config })
      return
    }
    const status = visualStatusFromFrame(event.source, ownedWindow, event.data, 'amadeus.visual.preview.status')
    if (!status || status.runtime_id !== current.config.runtime_id
      || status.profile_id !== current.config.profile_id || status.revision !== current.config.revision) return
    const updated = { ...current, status }
    previewRef.current = updated
    setPreview(updated)
  }, [postPreview])

  useEffect(() => {
    window.addEventListener('message', handlePreviewMessage)
    return () => {
      window.removeEventListener('message', handlePreviewMessage)
      previewRequestRef.current += 1
      postPreview('destroy')
      previewRef.current = null
    }
  }, [handlePreviewMessage, postPreview])

  const updateDraft = useCallback((update: (config: VisualConfiguration) => VisualConfiguration) => {
    setEditor(current => current.draft ? { ...current, draft: update(current.draft) } : current)
    setNotice('')
  }, [])

  const updateProfile = useCallback((update: (value: VisualProfile) => VisualProfile) => {
    updateDraft(config => ({
      ...config, profiles: config.profiles.map(value => value.profile_id === config.selected_profile_id ? update(value) : value),
    }))
  }, [updateDraft])

  const inspectPath = useCallback(async (path: string, add: boolean) => {
    const response = await send('visual.inspect', { model_path: path.trim() })
    const inspected = response.profile as unknown as VisualProfile
    const replacement = inspectedVisualProfile(inspected, add ? undefined : profile)
    closePreview()
    setCapabilities(current => ({
      ...current, [inspected.model_path]: response.capabilities as unknown as VisualCapabilities,
    }))
    setCapabilityError({ path: '', message: '' })
    updateDraft(config => ({
      ...config, selected_profile_id: replacement.profile_id,
      profiles: add || !profile ? [...config.profiles, replacement]
        : config.profiles.map(value => value.profile_id === replacement.profile_id ? replacement : value),
    }))
    setModelPath(replacement.model_path)
    setNotice(t('Model inspected. Save and apply when ready.'))
  }, [closePreview, profile, send, t, updateDraft])

  const chooseModel = useCallback((add: boolean) => {
    void run('inspecting', async () => {
      if (!window.amadeus) throw new Error(t('Enter a local model path to inspect it in this browser.'))
      const result = await window.amadeus.selectVisualFile('model', add ? '' : modelPath)
      if (result.cancelled) return
      if (!result.ok) throw new Error(result.detail)
      await inspectPath(result.path, add)
    })
  }, [inspectPath, modelPath, run, t])

  const chooseCore = useCallback(() => {
    void run('selecting', async () => {
      if (!window.amadeus) return
      const result = await window.amadeus.selectVisualFile('core', draft?.core_path)
      if (result.cancelled) return
      if (!result.ok) throw new Error(result.detail)
      closePreview()
      updateDraft(config => ({ ...config, core_path: result.path }))
    })
  }, [closePreview, draft?.core_path, run, updateDraft])

  const save = useCallback(() => {
    if (!draft) return
    void run('saving', async () => {
      acceptSnapshot(snapshotFromResponse(await send('visual.save', { config: draft })), true)
      setNotice(t('Visual configuration saved and applied.'))
    })
  }, [acceptSnapshot, draft, run, send, t])

  const reload = useCallback(() => {
    void run('reloading', async () => {
      acceptSnapshot(snapshotFromResponse(await send('visual.reload', {})))
      setNotice(t('Applied configuration reloaded.'))
    })
  }, [acceptSnapshot, run, send, t])

  const openPreview = useCallback(() => {
    if (!profile || !draft) return
    closePreview()
    const request = ++previewRequestRef.current
    void run('preview', async () => {
      const response = await send('visual.preview', { profile, core_path: draft.core_path, surface: previewSurface })
      if (request !== previewRequestRef.current) return
      const config = response.config as Record<string, unknown>
      const next: Preview = {
        url: String(response.url), config, status: null,
        fingerprint: previewFingerprint(profile, draft.core_path, previewSurface),
        context: previewContext(draft, profile),
      }
      previewRef.current = next
      setPreview(next)
      setCapabilities(current => ({
        ...current, [profile.model_path]: response.capabilities as unknown as VisualCapabilities,
      }))
    })
  }, [closePreview, draft, previewSurface, profile, run, send])

  const resetPreview = useCallback(() => {
    postPreview('reset')
    setMouthValue(0)
  }, [postPreview])

  const removeProfile = useCallback(() => {
    if (!profile) return
    closePreview()
    updateDraft(config => {
      const remaining = config.profiles.filter(value => value.profile_id !== profile.profile_id)
      return { ...config, profiles: remaining, selected_profile_id: remaining[0]?.profile_id || '' }
    })
  }, [closePreview, profile, updateDraft])

  const updateLayout = (surface: VisualSurface, key: keyof VisualLayout, value: number) => {
    updateProfile(current => ({ ...current, layouts: {
      ...current.layouts, [surface]: { ...current.layouts[surface], [key]: value },
    } }))
  }

  const labels = profile ? [...new Set([...EMOTIONS, ...Object.keys(profile.emotion_map)])] : []
  const appliedProfile = selectedVisualProfile(editor.applied?.config || null)

  return <div className="visuals-page">
    <header className="visuals-header">
      <div>
        <h3>{t('Character visuals')}</h3>
        <p>{t('Choose artwork for rendering and wallpaper. Visual profiles are independent from conversation characters.')}</p>
      </div>
      <div className="visuals-actions">
        <span className="visuals-state">{t(dirty ? 'Unsaved changes' : 'Saved configuration')}</span>
        <button className="visuals-button" disabled={unavailable || !dirty}
          onClick={() => {
            closePreview()
            if (editor.applied) setEditor(current => receiveVisualSnapshot(current, editor.applied!, true))
            setNotice('')
            setError('')
          }}>{t('Discard changes')}</button>
        <button className="visuals-button primary" disabled={unavailable || !dirty || (draft?.backend === 'live2d' && !profile)}
          onClick={save}>{t(busy === 'saving' ? 'Saving…' : 'Save and apply')}</button>
      </div>
    </header>
    <div className="visuals-scroll">
      {!connected && <p className="visuals-banner">{t('Connect the backend to edit visual profiles.')}</p>}
      {error && <p className="visuals-banner error" role="alert">{error}</p>}
      {notice && <p className="visuals-banner" role="status">{notice}</p>}
      {editor.applied?.diagnostic && <p className="visuals-banner error" role="alert">{editor.applied.diagnostic}</p>}
      {!draft ? <p className="visuals-note">{t(connected ? 'Loading visual configuration…' : 'Backend not connected')}</p> :
        <div className="visuals-grid">
          <div className="visuals-column">
            <section className="visuals-card">
              <h4>{t('Artwork selection')}</h4>
              <label className="visuals-label" htmlFor="visual-backend">{t('Visual renderer')}</label>
              <select id="visual-backend" disabled={unavailable} value={draft.backend}
                onChange={event => {
                  closePreview()
                  updateDraft(config => ({ ...config, backend: event.target.value as VisualConfiguration['backend'] }))
                }}>
                <option value="sprite">{t('Sprite (default)')}</option>
                <option value="live2d">{t('Live2D (experimental)')}</option>
              </select>
              <label className="visuals-label" htmlFor="visual-profile">{t('Visual profile')}</label>
              <select id="visual-profile" disabled={unavailable} value={draft.selected_profile_id}
                onChange={event => {
                  closePreview()
                  updateDraft(config => ({ ...config, selected_profile_id: event.target.value }))
                }}>
                {!profile && <option value="">{t('Select a Live2D profile')}</option>}
                {draft.profiles.map(value => <option key={value.profile_id} value={value.profile_id}>{value.name}</option>)}
              </select>
              <div className="visuals-actions" style={{ marginTop: 12 }}>
                <button className="visuals-button" disabled={unavailable} onClick={() => chooseModel(true)}>{t('Add Live2D model…')}</button>
                <button className="visuals-button danger" disabled={unavailable || !profile} onClick={removeProfile}>{t('Remove profile')}</button>
              </div>
              <p className="visuals-note">{t('You can create several visual profiles from the same model. Changes take effect after saving.')}</p>
              {draft.backend === 'live2d' && !profile && <p className="visuals-note">{t('Add or select a model before saving Live2D.')}</p>}
              {profile && <>
                <label className="visuals-label" htmlFor="visual-profile-name">{t('Profile name')}</label>
                <input id="visual-profile-name" type="text" value={profile.name} maxLength={120} disabled={unavailable}
                  onChange={event => updateProfile(value => ({ ...value, name: event.target.value }))} />
              </>}
              <label className="visuals-label" htmlFor="visual-model-path">{t('Model file')}</label>
              <input id="visual-model-path" type="text" value={modelPath} disabled={unavailable}
                placeholder={t('Absolute path to a .model3.json file')} onChange={event => setModelPath(event.target.value)} />
              <div className="visuals-actions" style={{ marginTop: 8 }}>
                <button className="visuals-button" disabled={unavailable || !window.amadeus} onClick={() => chooseModel(false)}>{t('Choose model file…')}</button>
                <button className="visuals-button" disabled={unavailable || !modelPath.trim()}
                  onClick={() => void run('inspecting', () => inspectPath(modelPath, false))}>{t(profile ? 'Inspect and update' : 'Inspect and add')}</button>
                {profile && <button className="visuals-button" disabled={unavailable || !modelPath.trim()}
                  onClick={() => void run('inspecting', () => inspectPath(modelPath, true))}>{t('Add as new profile')}</button>}
              </div>
              <p className="visuals-note">{t('Replacing the model refreshes expression mappings and mouth parameters. Its profile identity and layouts stay the same.')}</p>
              <label className="visuals-label" htmlFor="visual-core-path">{t('Local Cubism Core')}</label>
              <div className="visuals-row">
                <input id="visual-core-path" type="text" value={draft.core_path} disabled={unavailable}
                  placeholder={t('Local live2dcubismcore.min.js path')}
                  onChange={event => {
                    closePreview()
                    updateDraft(config => ({ ...config, core_path: event.target.value }))
                  }} />
                <button className="visuals-button" disabled={unavailable || !window.amadeus} onClick={chooseCore}>{t('Choose Core…')}</button>
              </div>
              <p className="visuals-note">{t('Uses your local SDK file. Leaving this blank uses an existing local default. Model assets and Core are not copied.')}</p>
            </section>

            {profile && <section className="visuals-card">
              <h4>{t('Emotion mappings')}</h4>
              <p className="visuals-note">{t('Map existing emotion labels to expressions registered in this model. Default pose explicitly clears the expression.')}</p>
              {capabilityError.path === profile.model_path && capabilityError.message &&
                <p className="visuals-banner error" role="alert">{capabilityError.message}</p>}
              {!capability && !capabilityError.message && <p className="visuals-note">{t('Inspecting model capabilities…')}</p>}
              <table className="visuals-mapping">
                <thead><tr><th>{t('Emotion label')}</th><th>{t('Model expression')}</th><th>{t('Test')}</th></tr></thead>
                <tbody>{labels.map(label => {
                  const expression = profile.emotion_map[label] ?? null
                  const missing = Boolean(expression && capability && !capability.expressions.includes(expression))
                  return <tr key={label}>
                    <td><code>{label}</code>
                      {APPROXIMATE_EMOTIONS.has(label) && expression && <p className="visuals-note">{t('Approximate mapping')}</p>}
                    </td>
                    <td><select aria-label={t('Expression for {label}', { label })} disabled={unavailable || !capability}
                      value={expression || ''} onChange={event => updateProfile(value => ({
                        ...value, emotion_map: { ...value.emotion_map, [label]: event.target.value || null },
                      }))}>
                      <option value="">{t('Default pose')}</option>
                      {missing && <option value={expression!}>{expression} ({t('Unavailable')})</option>}
                      {capability?.expressions.map(name => <option value={name} key={name}>{name}</option>)}
                    </select>
                      {!expression && label !== 'normal' && label !== 'neutral' && <p className="visuals-note">{t('No separate expression mapped')}</p>}
                      {missing && <p className="visuals-note">{t('This expression is missing from the model.')}</p>}
                    </td>
                    <td><button className="visuals-button" disabled={!previewReady || missing}
                      aria-label={t('Preview emotion {label}', { label })} onClick={() => postPreview('intent', { label })}>▶</button></td>
                  </tr>
                })}</tbody>
              </table>
              {Boolean(capability?.warnings.length) && <ul className="visuals-warning-list">
                {capability!.warnings.map((warning, index) => <li key={index}>{warning}</li>)}
              </ul>}
            </section>}
          </div>

          <div className="visuals-column">
            <section className="visuals-card">
              <h4>{t('Isolated preview')}</h4>
              <p className="visuals-note">{t('Tests this draft in a separate instance. Chat audio and the applied visuals continue independently.')}</p>
              <label className="visuals-label" htmlFor="visual-preview-surface">{t('Preview layout')}</label>
              <select id="visual-preview-surface" disabled={unavailable} value={previewSurface}
                onChange={event => {
                  closePreview()
                  setPreviewSurface(event.target.value as VisualSurface)
                }}>
                <option value="render">{t('Render')}</option>
                <option value="wallpaper">{t('Wallpaper')}</option>
              </select>
              <div className="visuals-actions" style={{ marginTop: 12 }}>
                <button className="visuals-button primary" disabled={unavailable || !profile} onClick={openPreview}>{t(preview ? 'Refresh preview' : 'Open preview')}</button>
                <button className="visuals-button" disabled={!preview} onClick={closePreview}>{t('Close preview')}</button>
                {preview && <span className={'visuals-state ' + (preview.status?.state || '')}>{t(LOAD_LABELS[preview.status?.state || 'loading'])}</span>}
              </div>
              <div className="visuals-preview">
                {preview ? <iframe ref={previewFrameRef} src={preview.url} title={t('Visual draft preview')} /> :
                  <div className="visuals-preview-empty">{t('Select a model and open the preview to test its expressions and mouth movement.')}</div>}
              </div>
              {previewStale && <p className="visuals-banner">{t('This draft has changed. Refresh preview to test the latest edits.')}</p>}
              {preview?.status?.error && <p className="visuals-banner error" role="alert">{t(preview.status.error)}</p>}
              <VisualDiagnostic diagnostic={preview?.status?.diagnostic} />
              {preview && <p className="visuals-note">{t('The preview uses the selected layout in this viewport; wallpaper cropping is verified in the wallpaper surface.')}</p>}
              <h5 style={{ marginTop: 16 }}>{t('Registered expressions')}</h5>
              <div className="visuals-expression-list">
                {capability?.expressions.map(name => <button className="visuals-button" key={name}
                  disabled={!previewReady} onClick={() => postPreview('expression', { name })}>{name}</button>)}
                {!capability?.expressions.length && <span className="visuals-note">{t('No registered expressions')}</span>}
              </div>
              <label className="visuals-label" htmlFor="visual-mouth-test">{t('Mouth test')} · {mouthValue.toFixed(2)}</label>
              <input id="visual-mouth-test" type="range" style={{ width: '100%' }} min={0} max={1} step={0.01}
                value={mouthValue} disabled={!previewReady} onChange={event => {
                  const value = Number(event.target.value)
                  setMouthValue(value)
                  postPreview('mouth', { value })
                }} />
              <button className="visuals-button" style={{ marginTop: 8 }} disabled={!previewReady} onClick={resetPreview}>{t('Reset preview')}</button>
            </section>

            {profile && <section className="visuals-card">
              <h4>{t('Speech mouth movement')}</h4>
              <p className="visuals-note">{t('Uses the existing speech amplitude. These controls only change model mouth movement.')}</p>
              <NumberField disabled={unavailable} label={t('Mouth gain')} value={profile.mouth.gain} min={0} max={5} step={0.1}
                onChange={gain => updateProfile(value => ({ ...value, mouth: { ...value.mouth, gain } }))} />
              <NumberField disabled={unavailable} label={t('Smoothing (ms)')} value={profile.mouth.smoothing_ms} min={0} max={500} step={10}
                onChange={smoothing_ms => updateProfile(value => ({ ...value, mouth: { ...value.mouth, smoothing_ms } }))} />
              <p className="visuals-note">{t('Declared LipSync parameters')}: {capability?.lip_sync_ids.join(', ') || t('None declared')}</p>
              {!capability?.lip_sync_ids.length && <>
                <label className="visuals-label" htmlFor="visual-mouth-ids">{t('Mouth parameter IDs')}</label>
                <input id="visual-mouth-ids" type="text" value={mouthIds} disabled={unavailable || !capability}
                  onChange={event => {
                    setMouthIds(event.target.value)
                    const parameter_ids = event.target.value.split(/[,\s]+/).filter(Boolean)
                    updateProfile(value => ({ ...value, mouth: { ...value.mouth, parameter_ids } }))
                  }} />
                <p className="visuals-note">{t('Only when the model has no LipSync declaration. Enter model parameter IDs separated by commas; the runtime verifies they exist.')}</p>
              </>}
            </section>}

            {profile && <section className="visuals-card">
              <h4>{t('Surface layouts')}</h4>
              <p className="visuals-note">{t('Scale is relative to the fitted model. Position is relative to each viewport: −1 to 1.')}</p>
              <div className="visuals-layouts" style={{ marginTop: 12 }}>
                {(['render', 'wallpaper'] as const).map(surface => <div key={surface}>
                  <h5>{t(surface === 'render' ? 'Render' : 'Wallpaper')}</h5>
                  <NumberField disabled={unavailable} label={t('Scale')} value={profile.layouts[surface].scale} min={0.1} max={5} step={0.1}
                    onChange={value => updateLayout(surface, 'scale', value)} />
                  <NumberField disabled={unavailable} label={t('Horizontal position')} value={profile.layouts[surface].x} min={-1} max={1} step={0.05}
                    onChange={value => updateLayout(surface, 'x', value)} />
                  <NumberField disabled={unavailable} label={t('Vertical position')} value={profile.layouts[surface].y} min={-1} max={1} step={0.05}
                    onChange={value => updateLayout(surface, 'y', value)} />
                </div>)}
              </div>
            </section>}

            <section className="visuals-card">
              <h4>{t('Applied visual status')}</h4>
              <p className="visuals-note">{t('Saved selection')}: {editor.applied?.config.backend === 'live2d' ? 'Live2D' : 'Sprite'}
                {editor.applied?.config.backend === 'live2d' && appliedProfile ? ' · ' + appliedProfile.name : ''}</p>
              {(['render', 'wallpaper'] as const).map(surface => {
                const status = editor.applied?.surfaces[surface]
                return <div key={surface}>
                  <div className="visuals-status-row">
                    <span>{t(surface === 'render' ? 'Render' : 'Wallpaper')}</span>
                    <span className={'visuals-state ' + (status?.state || '')}>{t(status ? LOAD_LABELS[status.state] : 'No surface report')}</span>
                  </div>
                  {status?.error && <p className="visuals-banner error" role="alert">{t(status.error)}</p>}
                  <VisualDiagnostic diagnostic={status?.diagnostic} />
                </div>
              })}
              <button className="visuals-button" disabled={unavailable || !editor.applied} onClick={reload}>{t(busy === 'reloading' ? 'Reloading model…' : 'Reload model')}</button>
              <p className="visuals-note">{t('Reload uses the saved model configuration and preserves unsaved edits. Close and reopen Render or Wallpaper to apply Core changes.')}</p>
            </section>
          </div>
        </div>}
    </div>
  </div>
}
