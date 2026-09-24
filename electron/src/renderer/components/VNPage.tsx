import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useI18n } from '../i18n'
import VNProfileEditor, { type VNProfileSettings } from './VNProfileEditor'
import VNAbilities, { capabilityReason, type CapabilityState, type VNCapabilities, type VNCapabilityPresets } from './VNAbilities'
import FluentIcon from './FluentIcon'
import { StatusPill } from './SettingsPrimitives'
import { activityFromEvent, mergeActivity, sessionBanner, type VNActivity } from './vnPresentation'

type BackendSend = (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
type BackendSubscribe = (method: string, fn: (p: Record<string, unknown>) => void) => () => void

interface Props {
  send: BackendSend
  subscribe: BackendSubscribe
  connected: boolean
}

type VNProfile = Partial<VNProfileSettings> & {
  capabilities?: VNCapabilities
  runtimeSupported?: boolean
  id: string
  name: string
  description?: string
  scriptPath?: string
  scriptExists?: boolean
  gameExe?: string
  gameExists?: boolean
  agentExe?: string
  agentExists?: boolean
  hookHelper?: string
  hookExists?: boolean
  overlayHelper?: string
  overlayExists?: boolean
  overlayUrl?: string
  overlayPort?: number
  overlayImagesDir?: string
  lineBridgeMode?: string
  agentWsUrl?: string
  processName?: string
  runnerPath?: string
}

type ProcessStatus = {
  status?: string
  pid?: number | null
  path?: string
  helper?: string
  url?: string
  owned?: boolean
  visible?: boolean
}

type BridgeStatus = {
  status?: string
  lineCount?: number
  source?: string
  error?: string
  lastTextPreview?: string
}

type LaunchStatus = {
  status?: string
  textSource?: string
  profileId?: string
  sessionId?: string
  startedAt?: number
  updatedAt?: number
  error?: string
  game?: ProcessStatus
  hook?: ProcessStatus
  overlay?: ProcessStatus
  bridge?: BridgeStatus
  captureOnly?: boolean
  capturedLines?: Array<{ text: string; speaker: string; script_id: string; receivedAt: number }>
  runtime?: Record<string, unknown> | null
  profiles?: VNProfile[]
}

type VNEvent = {
  id: string
  method: string
  text: string
  detail?: string
  raw: string
  time: string
}

type VNInputs = {
  session_id: string; kind: 'ask' | 'note' | 'choice' | 'pin'
  voice: { enabled: boolean; available: boolean; listening: boolean; starting: boolean; reason: string; error?: string }
  vision: { mode: 'off' | 'on_question'; enabled: boolean; available: boolean; reason: string }
}
type RuntimeState = Record<string, unknown> & {
  status?: string
  session_id?: string
  inputs?: VNInputs
  capabilities?: Record<string, CapabilityState>
  visual?: { supported?: boolean; reason?: string }
  preferences?: { commentary_frequency: 'quiet' | 'balanced' | 'frequent'; commentary_paused: boolean; speech_enabled: boolean }
}
type VisualAttachment = {
  frame: { dataUrl: string; mime: string; width: number; height: number }
  capturedAt: string
  actualScope: string
  game?: { pid?: number; title?: string }
  [key: string]: unknown
}

function statusColor(status: string): string {
  const key = status.toLowerCase()
  if (key === 'active' || key === 'running') return '#107C10'
  if (key === 'starting' || key === 'stopping' || key === 'manual_required' || key === 'connecting' || key === 'waiting') return '#D83B01'
  if (key === 'error' || key === 'exited') return '#C42B1C'
  return 'var(--muted)'
}

function RuntimeChip({ label, status, detail }: { label: string; status: string; detail?: string }) {
  const { t } = useI18n()
  return (
    <span
      className="inline-flex items-center gap-1.5 shrink-0"
      title={detail || status}
      style={{
        height: 28,
        padding: '0 9px',
        border: '1px solid var(--border)',
        borderRadius: 999,
        background: 'var(--surface)',
        color: 'var(--muted)',
        fontSize: 10.5,
      }}
    >
      <span className="rounded-full" style={{ width: 7, height: 7, background: statusColor(status) }} />
      <span style={{ color: 'var(--text)', fontWeight: 650 }}>{t(label)}</span>
      <span>{t(status.replaceAll('_', ' '))}</span>
      {detail ? <span style={{ color: 'var(--faint)' }}>{detail}</span> : null}
    </span>
  )
}

export default function VNPage({ send, subscribe, connected }: Props) {
  const { t } = useI18n()
  const [profiles, setProfiles] = useState<VNProfile[]>([])
  const [capabilityPresets, setCapabilityPresets] = useState<VNCapabilityPresets>({ base: {}, mystery: {} })
  const [selectedProfile, setSelectedProfile] = useState('paranormasight')
  const [agentExe, setAgentExe] = useState('')
  const [overlayAvailable, setOverlayAvailable] = useState(false)
  const [editor, setEditor] = useState<{ initial?: VNProfile } | null>(null)
  const [launch, setLaunch] = useState<LaunchStatus>({ status: 'idle' })
  const [runtime, setRuntime] = useState<RuntimeState | null>(null)
  const [events, setEvents] = useState<VNEvent[]>([])
  const [activity, setActivity] = useState<VNActivity[]>([])
  const [stopping, setStopping] = useState(false)
  const [sending, setSending] = useState(false)
  const [lineText, setLineText] = useState('')
  const [playerText, setPlayerText] = useState('')
  const [inputsBusy, setInputsBusy] = useState(false)
  const [visualAttachment, setVisualAttachment] = useState<VisualAttachment | null>(null)
  const [visualBusy, setVisualBusy] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const activeProfile = useMemo(
    () => profiles.find(profile => profile.id === selectedProfile) || profiles[0],
    [profiles, selectedProfile],
  )
  const runtimeStatus = launch.captureOnly ? 'not_started' : String((runtime?.status as string | undefined) || 'unknown')
  const runtimeSessionId = launch.sessionId || String(runtime?.session_id || '')
  const sessionRef = useRef('')
  const feedRef = useRef<HTMLDivElement>(null)
  const followFeedRef = useRef(true)
  const interaction = runtime?.capabilities?.interaction
  const interactionEnabled = runtimeStatus === 'active' && !launch.captureOnly && interaction?.enabled === true
  const inputs = runtime?.inputs
  const playerMode = inputs?.kind || 'ask'
  const voiceEnabled = inputs?.voice.enabled === true
  const visionEnabled = inputs?.vision.enabled === true
  const inputsRef = useRef(inputs)
  inputsRef.current = inputs

  const pushEvent = useCallback((method: string, payload: Record<string, unknown>) => {
    const visible = activityFromEvent(method, payload)
    if (visible) setActivity(previous => mergeActivity(previous, [visible], visible.sessionId))
    const detail = typeof payload.reason_label === 'string'
      ? payload.reason_label
      : typeof payload.status === 'string'
        ? payload.status
        : undefined
    const item: VNEvent = {
      id: crypto.randomUUID(),
      method,
      text: visible?.text || '',
      detail,
      raw: JSON.stringify(payload, (key, value) => key === 'dataUrl' ? '[image omitted]' : value),
      time: new Date().toLocaleTimeString(),
    }
    setEvents(prev => [item, ...prev].slice(0, 24))
  }, [])

  const refresh = useCallback(async () => {
    if (!connected) return
    try {
      const [profileRes, statusRes, runtimeRes] = await Promise.all([
        send('vn.launch.profiles', {}),
        send('vn.launch.status', {}),
        send('vn.status', { include_history: true }),
      ])
      const loadedProfiles = Array.isArray(profileRes.profiles) ? profileRes.profiles as VNProfile[] : []
      setProfiles(loadedProfiles)
      setLaunch(statusRes as LaunchStatus)
      setAgentExe(String(profileRes.agentExe || ''))
      setOverlayAvailable(profileRes.overlayAvailable === true)
      const history = (Array.isArray(runtimeRes.activity) ? runtimeRes.activity : []) as Array<{method: string; payload: Record<string, unknown>}>
      const restored = history.map(item => activityFromEvent(item.method, item.payload)).filter((item): item is VNActivity => !!item)
      const historySession = String((runtimeRes.profile as {session_id?: string} | undefined)?.session_id || '')
      if (historySession && (!sessionRef.current || sessionRef.current === historySession)) {
        setActivity(previous => mergeActivity(previous, restored, historySession))
      }
      if (profileRes.capabilityPresets) setCapabilityPresets(profileRes.capabilityPresets as VNCapabilityPresets)
      setSelectedProfile(previous => statusRes.profileId ? String(statusRes.profileId)
        : loadedProfiles.some(profile => profile.id === previous) ? previous : loadedProfiles[0]?.id || '')
      if (statusRes.runtime && typeof statusRes.runtime === 'object') {
        setRuntime(statusRes.runtime as RuntimeState)
      } else {
        setRuntime(null)
      }
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [connected, send])

  useEffect(() => {
    refresh().catch(() => {})
  }, [refresh])

  useEffect(() => {
    const unsubLaunch = subscribe('vn.launch.status', payload => {
      setLaunch(payload as LaunchStatus)
      if (payload.runtime && typeof payload.runtime === 'object') {
        setRuntime(payload.runtime as RuntimeState)
      }
    })
    const unsubStatus = subscribe('vn.status', payload => {
      setRuntime(payload as RuntimeState)
      pushEvent('vn.status', payload)
    })
    const unsubLine = subscribe('vn.line', payload => pushEvent('vn.line', payload))
    const unsubReaction = subscribe('vn.reaction', payload => pushEvent('vn.reaction', payload))
    const unsubSummary = subscribe('vn.summary', payload => pushEvent('vn.summary', payload))
    const unsubError = subscribe('vn.error', payload => {
      pushEvent('vn.error', payload)
      setError(String(payload.error || ''))
    })
    const unsubPlayer = subscribe('vn.player.event', payload => pushEvent('vn.player.event', payload))
    return () => {
      unsubLaunch()
      unsubStatus()
      unsubLine()
      unsubReaction()
      unsubSummary()
      unsubError()
      unsubPlayer()
    }
  }, [pushEvent, subscribe])

  useEffect(() => {
    sessionRef.current = runtimeSessionId
    setVisualAttachment(null)
  }, [runtimeSessionId, inputs?.vision.mode])

  const setInputs = async (patch: { voice?: boolean; vision_mode?: 'off' | 'on_question'; kind?: VNInputs['kind'] }) => {
    const session = runtimeSessionId
    setInputsBusy(true); setError('')
    try {
      const result = await send('vn.input.set', { session_id: session, ...patch })
      if (session === sessionRef.current) setRuntime(result as RuntimeState)
    } catch (err) { setError(t(err instanceof Error ? err.message : String(err))) }
    finally { setInputsBusy(false) }
  }

  const startProfile = async (profileId: string, captureOnly = false) => {
    setBusy(true)
    setError('')
    setEvents([])
    setActivity([])
    setSending(false)
    setPlayerText('')
    followFeedRef.current = true
    try {
      const res = await send('vn.launch.start', { profileId, captureOnly })
      setLaunch(res as LaunchStatus)
      setRuntime(res.runtime && typeof res.runtime === 'object' ? res.runtime as RuntimeState : null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const saveProfile = async (profile: VNProfileSettings, sharedAgent: string, test: boolean) => {
    const res = await send('vn.launch.profile.save', { profile, agentExe: sharedAgent })
    setProfiles(res.profiles as VNProfile[])
    setAgentExe(String(res.agentExe || ''))
    const id = String(res.profileId)
    setSelectedProfile(id)
    setEditor(null)
    if (test) await startProfile(id, true)
  }

  const stop = async () => {
    setStopping(true)
    setError('')
    try {
      setVisualAttachment(null)
      const res = await send('vn.launch.stop', {
        reason: 'electron_vn_page',
      })
      setLaunch(res as LaunchStatus)
      if (res.runtime && typeof res.runtime === 'object') {
        setRuntime(res.runtime as RuntimeState)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setStopping(false)
    }
  }

  const routePlayerMethod = (mode: typeof playerMode): string => {
    if (mode === 'note') return 'vn.player.note'
    if (mode === 'pin') return 'vn.player.pin'
    if (mode === 'choice') return 'vn.choice.ask'
    return 'vn.player.ask'
  }

  const sendPlayerIntervention = async () => {
    const text = playerText.trim()
    if (!text) return
    const session = runtimeSessionId
    setSending(true)
    setError('')
    try {
      const method = routePlayerMethod(playerMode)
      const response = await send(method, {
        text,
        source: 'electron_vn_page', session_id: session,
        metadata: { source: 'vn_player_panel', mode: playerMode },
      })
      if (session !== sessionRef.current) return
      if (response.status === 'unavailable' || response.error) throw new Error(String(response.error || t(capabilityReason(String(response.reason || '')))))
      setPlayerText('')
      setVisualAttachment(null)
    } catch (err) {
      if (session === sessionRef.current) setError(err instanceof Error ? err.message : String(err))
    } finally {
      if (session === sessionRef.current) setSending(false)
    }
  }

  const attachGameView = async () => {
    const captureSessionId = runtimeSessionId
    setVisualBusy(true)
    setError('')
    try {
      const res = await send('vn.launch.capture', {})
      if (res.status !== 'ok' || !res.visual_context || typeof res.visual_context !== 'object') {
        throw new Error(String(res.error || res.reason || t('Game view is unavailable.')))
      }
      const context = res.visual_context as VisualAttachment
      if (!context.frame?.dataUrl) throw new Error(t('Game view is unavailable.'))
      if (captureSessionId === sessionRef.current && inputsRef.current?.vision.enabled) setVisualAttachment(context)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setVisualBusy(false)
    }
  }

  const sendLine = async () => {
    const text = lineText.trim()
    if (!text) return
    setBusy(true)
    setError('')
    try {
      await send('vn.line', { text, speaker: 'demo', metadata: { source: 'electron_vn_page' } })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const launchStatus = String(launch.status || 'idle')
  const isActive = ['active', 'starting', 'stopping'].includes(launchStatus)
  const isTesting = isActive && !!launch.captureOnly
  const isPlaying = launchStatus === 'active' && !launch.captureOnly
  const preset = capabilityPresets[activeProfile?.promptPack || 'base']
  const storyEvents = activity
  const eventNames: Record<string, string> = {
    'vn.line': 'Game text', 'vn.reaction': 'Companion', 'vn.summary': 'Story summary',
    'vn.player.event': 'You', 'vn.error': 'Companion error',
  }
  const sourceReady = !!activeProfile && (!activeProfile.launchGame || !!activeProfile.gameExists)
    && (activeProfile.textSource === 'luna' ? !!activeProfile.lunaWsUrl : !!(activeProfile.gameExists && activeProfile.agentExists && activeProfile.hookExists))
  const lineCount = launch.bridge?.lineCount || 0
  const reasonText = (reason?: string, enabled = false) => t(capabilityReason(reason, enabled))
  const banner = sessionBanner(launch, runtime, connected, sourceReady)
  const preferences = runtime?.preferences
  const reconnect = async () => {
    const captureOnly = !!launch.captureOnly
    setStopping(true); setError('')
    try {
      await send('vn.launch.stop', { closeGame: false, reason: 'reconnect' })
      await startProfile(activeProfile.id, captureOnly)
    } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
    finally { setStopping(false) }
  }

  const setPreferences = async (patch: Partial<NonNullable<RuntimeState['preferences']>>) => {
    setInputsBusy(true); setError('')
    const session = runtimeSessionId
    try {
      const result = await send('vn.mode.set', { session_id: session, ...patch })
      if (session === sessionRef.current) setRuntime(result as RuntimeState)
    } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
    finally { setInputsBusy(false) }
  }

  const toggleOverlay = async () => {
    setInputsBusy(true); setError('')
    try {
      const showing = ['running', 'external_running'].includes(String(launch.overlay?.status)) && launch.overlay?.visible !== false
      const result = await send('vn.launch.overlay', { session_id: runtimeSessionId, enabled: !showing })
      setLaunch(result as LaunchStatus)
    } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
    finally { setInputsBusy(false) }
  }

  useEffect(() => {
    if (followFeedRef.current && feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight
  }, [activity, launch.capturedLines])

  const beginAfterTest = async () => {
    const profileId = activeProfile.id
    setBusy(true); setError('')
    try {
      // Keep the game open while changing from preview to companion mode.
      await send('vn.launch.stop', { closeGame: false, reason: 'capture_to_companion' })
      await startProfile(profileId)
    } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
    finally { setBusy(false) }
  }

  return <div className="vn-player">
    <header className="vn-page-heading">
      <div><h2 className="settings-page-title">{t('VN Player')}</h2><p className="settings-page-context">{t('Stay in the story. Your companion follows along.')}</p></div>
      <StatusPill ok={false} tone="neutral">{t('Experimental')}</StatusPill>
    </header>
    <section className="vn-game-bar" aria-label={t('Selected game')}>
      <div className="vn-game-select"><label htmlFor="vn-game-select">{t('Your game')}</label>
        <select id="vn-game-select" value={selectedProfile} onChange={e => setSelectedProfile(e.target.value)} disabled={busy || isActive}>
          {!profiles.length && <option value="">{t('Add your first game')}</option>}
          {profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
        </select>
        {activeProfile && <span>{t(activeProfile.promptPack === 'mystery' ? 'Mystery VN' : 'General VN')} · {activeProfile.textSource === 'luna' ? 'Luna' : 'Agent'}</span>}
      </div>
      <div className="vn-game-actions">
        <button onClick={() => setEditor({})} disabled={!connected || busy || isActive}>{t('Add game')}</button>
        <button onClick={() => setEditor({ initial: activeProfile })} disabled={!connected || busy || isActive || !activeProfile}><FluentIcon name="Setting" size={14} aria-hidden="true" />{t('Edit game profile')}</button>
        {!isActive && <button className="vn-primary" onClick={() => void startProfile(activeProfile.id)} disabled={!connected || busy || !sourceReady}><FluentIcon name="Play" size={14} aria-hidden="true" />{t('Start')}</button>}
        {isActive && <button onClick={stop} disabled={!connected || stopping}>{t(launchStatus === 'starting' ? 'Cancel connection' : 'End session')}</button>}
      </div>
    </section>
    <div className={`vn-session-banner ${banner.tone === 'error' ? 'has-error' : ''}`} role="status">
      <span className={`vn-status-dot ${banner.tone}`} aria-hidden="true" />
      <div><strong>{t(banner.title)}</strong><p>{t(banner.detail)}</p>{launch.error && <p className="vn-error">{launch.error}</p>}</div>
      {launchStatus === 'active' && (banner.tone === 'error' || launch.bridge?.status !== 'running') && <button onClick={() => void reconnect()} disabled={stopping}>{t('Reconnect')}</button>}
      {isActive && <span className="vn-line-count">{lineCount} {t('lines received')}</span>}
    </div>
    {error && <div role="alert" className="vn-error vn-page-error">{error}</div>}
    {isPlaying && <section className="vn-session-inputs" aria-label={t('Session input controls')}>
      <header><strong>{t('This play session')}</strong><span>{t('Changes here do not alter the saved game profile.')}</span>
        <button onClick={() => void toggleOverlay()} disabled={inputsBusy || !overlayAvailable}>{t(launch.overlay?.visible !== false && ['running', 'external_running'].includes(String(launch.overlay?.status)) ? 'Hide portrait' : 'Show portrait')}</button>
      </header>
      <div className="vn-input-options">
        <div className="vn-input-option"><div><label htmlFor="vn-commentary-frequency">{t('Commentary frequency')}</label><p>{t('Controls spontaneous comments, not answers to your questions.')}</p></div>
          <select id="vn-commentary-frequency" value={preferences?.commentary_frequency || 'balanced'} disabled={inputsBusy} onChange={e => void setPreferences({ commentary_frequency: e.target.value as 'quiet' | 'balanced' | 'frequent' })}>
            <option value="quiet">{t('Occasional')}</option><option value="balanced">{t('Balanced')}</option><option value="frequent">{t('Frequent')}</option>
          </select>
        </div>
        <div className="vn-input-option"><div><strong>{t('Proactive commentary')}</strong><p>{t('Pause comments while continuing to follow the story.')}</p></div>
          <button disabled={inputsBusy} aria-pressed={preferences?.commentary_paused === true} onClick={() => void setPreferences({ commentary_paused: !preferences?.commentary_paused })}>{t(preferences?.commentary_paused ? 'Resume comments' : 'Pause comments')}</button>
        </div>
        <div className="vn-input-option">
          <FluentIcon name="Microphone" size={18} aria-hidden="true" />
          <div><strong>{t('Voice input (ASR)')}</strong><p>{t(inputs?.voice.starting ? 'Starting microphone…' : voiceEnabled ? 'Speak to ask your companion.' : 'Microphone off')}
            {!inputs?.voice.available && ` · ${reasonText(inputs?.voice.reason)}`}</p></div>
          <button type="button" role="switch" aria-label={t('Voice input (ASR)')} aria-checked={voiceEnabled} className="vn-input-switch"
            disabled={inputsBusy || (!inputs?.voice.available && !voiceEnabled)} onClick={() => void setInputs({ voice: !voiceEnabled })}><span aria-hidden="true" /></button>
          <span className="vn-input-value">{t(voiceEnabled ? 'On' : 'Off')}</span>
        </div>
        <div className="vn-input-option">
          <FluentIcon name="Camera" size={18} aria-hidden="true" />
          <div><label htmlFor="vn-vision-mode">{t('VN vision')}</label><p>{t('Only the bound game window. Separate from General vision settings.')}</p></div>
          <select id="vn-vision-mode" value={inputs?.vision.mode || 'off'} disabled={inputsBusy || (!inputs?.vision.available && inputs?.vision.mode !== 'on_question')}
            onChange={e => void setInputs({ vision_mode: e.target.value as 'off' | 'on_question' })}>
            <option value="off">{t('Off')}</option><option value="on_question" disabled={!inputs?.vision.available}>{t('When I ask')}</option>
          </select>
        </div>
      </div>
      <label className="vn-session-speech"><input type="checkbox" aria-label={t('Read upcoming replies aloud')} checked={preferences?.speech_enabled !== false} disabled={inputsBusy} onChange={e => void setPreferences({ speech_enabled: e.target.checked })} />{t('Read upcoming replies aloud')}<span>{t('Text replies remain visible. Use Stop speech to interrupt current audio.')}</span></label>
      {inputs?.voice.error && <p className="vn-error">{t(inputs.voice.error)}</p>}
      {inputs?.vision.mode === 'on_question' && !visionEnabled && <p className="vn-help">{reasonText(inputs.vision.reason)}</p>}
    </section>}
    <div className="vn-workspace">
      <section className="vn-conversation" aria-label={t(isTesting ? 'Captured text' : 'VN activity')}>
        <header className="vn-section-heading"><div><h3>{t(isTesting ? 'Captured text' : 'Your play session')}</h3>
          <p>{t(isTesting ? 'Text capture only · companion responses are paused' : 'Game dialogue and companion responses')}</p></div>
          {!isActive && <button onClick={() => void startProfile(activeProfile.id, true)} disabled={!connected || busy || !sourceReady}>{t('Test text capture')}</button>}
          {isTesting && lineCount > 0 && <button className="vn-primary" onClick={() => void beginAfterTest()} disabled={busy}>{t('Text looks right — start companion')}</button>}
        </header>
        <div className="vn-feed" ref={feedRef} onScroll={e => {
          const el = e.currentTarget; followFeedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
        }}>
          {isTesting ? <>
            {!launch.capturedLines?.length && <div className="vn-empty"><h4>{t('Advance one line in the game')}</h4><p>{t('Dialogue will appear here when the connection is working.')}</p></div>}
            {launch.capturedLines?.map((line, index) => <article key={`${line.receivedAt}-${index}`} className="vn-story-item game-line">
              <header><span>{t('Game text')} {index + 1}</span></header><p>{line.speaker ? `${line.speaker}: ` : ''}{line.text}</p>
            </article>)}
          </> : !storyEvents.length ? <div className="vn-empty">
            <div className="vn-empty-symbol" aria-hidden="true"><FluentIcon name="Movie" size={24} /></div>
            <h4>{t(isPlaying ? 'Waiting for the story to begin' : 'A companion for your next story')}</h4>
            <p>{t(isPlaying ? 'Advance the game. Dialogue and responses will appear here.' : 'Set up your game, check its text, then settle in and play.')}</p>
            {!isActive && <ol className="vn-journey">
              <li><span>1</span><div><strong>{t('Set up your game')}</strong><small>{t('Choose its type and connect its text source.')}</small></div></li>
              <li><span>2</span><div><strong>{t('Check a few lines')}</strong><small>{t('Compare extracted text with the game before playing.')}</small></div></li>
              <li><span>3</span><div><strong>{t('Start your companion')}</strong><small>{t('Abilities are selected automatically for this game type.')}</small></div></li>
            </ol>}
          </div> : storyEvents.map(item => <article key={item.id} className={`vn-story-item ${item.method === 'vn.line' ? 'game-line' : item.method.startsWith('vn.player') ? 'player-line' : 'companion-line'}`}>
            <header><span>{t(eventNames[item.method])}</span><time>{new Date(item.time).toLocaleTimeString()}</time></header><p>{item.text}</p>
          </article>)}
        </div>
        {isPlaying && <div className="vn-composer">
          <div className="vn-composer-tools">
            <details className="vn-message-actions"><summary>{t('Message options')}{playerMode !== 'ask' ? ` · ${t(playerMode === 'choice' ? 'Choice' : playerMode === 'note' ? 'Note' : 'Pin')}` : ''}</summary>
            <label className="sr-only" htmlFor="vn-player-mode">{t('Message type')}</label>
            <select id="vn-player-mode" value={playerMode} onChange={e => void setInputs({ kind: e.target.value as typeof playerMode })} disabled={busy || inputsBusy || !interactionEnabled}>
              <option value="ask">{t('Ask')}</option><option value="note">{t('Note')}</option><option value="choice">{t('Choice')}</option><option value="pin">{t('Pin')}</option>
            </select>
            </details>
            <button onClick={() => void attachGameView()} disabled={!visionEnabled || visualBusy || busy || inputsBusy}>{t(visualBusy ? 'Capturing game view…' : 'Preview game view')}</button>
            <button onClick={() => void send('tts.interrupt', {}).catch(err => setError(String(err)))} disabled={!interactionEnabled}>{t('Stop speech')}</button>
          </div>
          {visualAttachment && <div className="vn-visual-attachment"><img src={visualAttachment.frame.dataUrl} alt={t('Attached game view')} /><span>{t('Preview only. Each question captures a fresh game frame.')}</span><button onClick={() => setVisualAttachment(null)}>{t('Remove')}</button></div>}
          <div className="vn-compose-row"><textarea aria-label={t('Message to companion')} value={playerText} onChange={e => setPlayerText(e.target.value)} rows={2}
            placeholder={t('Ask about the current story…')} disabled={!interactionEnabled}
            onKeyDown={e => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && !e.nativeEvent.isComposing && !sending && !inputsBusy && playerText.trim()) void sendPlayerIntervention() }} />
            <button className="vn-primary" onClick={sendPlayerIntervention} disabled={!connected || sending || inputsBusy || !interactionEnabled || !playerText.trim()}>{t(sending ? 'Waiting for reply…' : 'Send')}</button></div>
          {!interactionEnabled && <p className="vn-help">{reasonText(interaction?.reason)}</p>}
          {visionEnabled && <p className="vn-help">{t('Typed and spoken questions include a fresh game image. Notes and pins remain text-only.')}</p>}
        </div>}
      </section>
    </div>
    {activeProfile && <details className="vn-capabilities"><summary>{t(activeProfile.promptPack === 'mystery' ? 'Mystery VN' : 'General VN')} · {t('Game type and abilities')}</summary>
      <VNAbilities preset={preset} states={isPlaying ? runtime?.capabilities : undefined} />
      <p className="vn-help">{t('Availability depends on the model, story script and current alignment.')}</p>
    </details>}
    <details className="vn-diagnostics">
      <summary>{t('Connection details and diagnostics')}</summary>
      <div className="vn-diagnostic-body"><div className="vn-diagnostic-status">
        <RuntimeChip label="Runtime" status={runtimeStatus} /><RuntimeChip label="Game" status={String(launch.game?.status || 'not_started')} detail={launch.game?.pid ? `PID ${launch.game.pid}` : undefined} />
        <RuntimeChip label="Text source" status={String(launch.hook?.status || 'not_started')} /><RuntimeChip label="Bridge" status={String(launch.bridge?.status || 'not_started')} />
        <button onClick={refresh} disabled={!connected || busy}>{t('Refresh')}</button>
      </div>
      <p>{t('Session')}: {launch.sessionId || '—'}</p>
      {launch.bridge?.error && <p className="vn-error">{launch.bridge.error}</p>}
      <details><summary>{t('Manual vn.line test')}</summary><textarea aria-label={t('Manual vn.line test')} value={lineText} onChange={e => setLineText(e.target.value)} rows={2} /><button onClick={sendLine} disabled={!connected || busy || runtimeStatus !== 'active' || !lineText.trim()}>{t('Send line')}</button></details>
      <details className="vn-raw-events"><summary>{t('Raw events')}</summary>{events.map(item => <div key={item.id}><time>{item.time}</time> <code>{item.method}</code><pre>{item.raw}</pre></div>)}</details>
      </div>
    </details>
    {editor && <VNProfileEditor initial={editor.initial} capabilityPresets={capabilityPresets} overlayAvailable={overlayAvailable} agentExe={agentExe} inspectGame={gameExe => send('vn.launch.inspect', { gameExe })} onClose={() => setEditor(null)} onSave={saveProfile} />}
  </div>
}
