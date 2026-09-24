import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useI18n } from '../i18n'
import VNProfileEditor, { type VNProfileSettings } from './VNProfileEditor'
import VNAbilities, { capabilityReason, type CapabilityState, type VNCapabilities, type VNCapabilityPresets } from './VNAbilities'
import FluentIcon from './FluentIcon'
import { StatusPill } from './SettingsPrimitives'

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

type RuntimeState = Record<string, unknown> & {
  status?: string
  session_id?: string
  capabilities?: Record<string, CapabilityState>
  visual?: { supported?: boolean; reason?: string }
}
type VisualAttachment = {
  frame: { dataUrl: string; mime: string; width: number; height: number }
  capturedAt: string
  actualScope: string
  game?: { pid?: number; title?: string }
  [key: string]: unknown
}

function textFromPayload(payload: Record<string, unknown>): string {
  const direct = payload.text ?? payload.summary ?? payload.message ?? payload.error
  if (typeof direct === 'string' && direct.trim()) return direct
  const line = payload.line
  if (line && typeof line === 'object' && 'text' in line) {
    const value = (line as { text?: unknown }).text
    if (typeof value === 'string') return value
  }
  const speak = payload.speak
  if (speak && typeof speak === 'object' && 'text' in speak) {
    const value = (speak as { text?: unknown }).text
    if (typeof value === 'string') return value
  }
  return ''
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
  const [editor, setEditor] = useState<{ initial?: VNProfile } | null>(null)
  const [launch, setLaunch] = useState<LaunchStatus>({ status: 'idle' })
  const [runtime, setRuntime] = useState<RuntimeState | null>(null)
  const [events, setEvents] = useState<VNEvent[]>([])
  const [lineText, setLineText] = useState('')
  const [playerText, setPlayerText] = useState('')
  const [playerMode, setPlayerMode] = useState<'ask' | 'note' | 'choice' | 'pin'>('ask')
  const [playerListening, setPlayerListening] = useState(false)
  const [voiceWanted, setVoiceWanted] = useState(false)
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
  const playerAsrRequestKeyRef = useRef<string | null>(null)
  const playerAsrOwnedRef = useRef(false)
  const playerAsrQueueRef = useRef<Promise<void>>(Promise.resolve())
  const sessionRef = useRef('')
  const feedRef = useRef<HTMLDivElement>(null)
  const followFeedRef = useRef(true)
  const interaction = runtime?.capabilities?.interaction
  const interactionEnabled = runtimeStatus === 'active' && !launch.captureOnly && interaction?.enabled === true
  const visualSupported = runtime?.visual?.supported === true

  const pushEvent = useCallback((method: string, payload: Record<string, unknown>) => {
    const detail = typeof payload.reason_label === 'string'
      ? payload.reason_label
      : typeof payload.status === 'string'
        ? payload.status
        : undefined
    const item: VNEvent = {
      id: crypto.randomUUID(),
      method,
      text: textFromPayload(payload),
      detail,
      raw: JSON.stringify(payload, (key, value) => key === 'dataUrl' ? '[image omitted]' : value),
      time: new Date().toLocaleTimeString(),
    }
    setEvents(prev => [item, ...prev].slice(0, 24))
  }, [])

  const refresh = useCallback(async () => {
    if (!connected) return
    try {
      const [profileRes, statusRes] = await Promise.all([
        send('vn.launch.profiles', {}),
        send('vn.launch.status', {}),
      ])
      const loadedProfiles = Array.isArray(profileRes.profiles) ? profileRes.profiles as VNProfile[] : []
      setProfiles(loadedProfiles)
      setLaunch(statusRes as LaunchStatus)
      setAgentExe(String(profileRes.agentExe || ''))
      if (profileRes.capabilityPresets) setCapabilityPresets(profileRes.capabilityPresets as VNCapabilityPresets)
      if (statusRes.profileId) setSelectedProfile(String(statusRes.profileId))
      if (statusRes.runtime && typeof statusRes.runtime === 'object') {
        setRuntime(statusRes.runtime as RuntimeState)
      } else {
        setRuntime(null)
      }
      if (loadedProfiles.length && !loadedProfiles.some(profile => profile.id === selectedProfile)) {
        setSelectedProfile(loadedProfiles[0].id)
      }
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [connected, send, selectedProfile])

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
      setError(textFromPayload(payload))
    })
    const unsubAsrRecognized = subscribe('asr.recognized', payload => {
      if (payload.source !== 'vn_player') return
      const sourcePayload = payload.source_payload as Record<string, unknown> | undefined
      if (sourcePayload?.session_id && sourcePayload.session_id !== sessionRef.current) return
      pushEvent('vn.player.asr', payload)
    })
    const unsubAsrStatus = subscribe('asr.status', payload => {
      if (payload.source !== 'vn_player') return
      const sourcePayload = payload.source_payload as Record<string, unknown> | undefined
      if (sourcePayload?.session_id && sourcePayload.session_id !== sessionRef.current) return
      const status = String(payload.status || '')
      if (['listening', 'loading', 'paused_tts', 'routed'].includes(status)) {
        setPlayerListening(true)
      }
      if (['idle', 'unloaded', 'error'].includes(status)) {
        setPlayerListening(false)
      }
    })
    return () => {
      unsubLaunch()
      unsubStatus()
      unsubLine()
      unsubReaction()
      unsubSummary()
      unsubError()
      unsubAsrRecognized()
      unsubAsrStatus()
    }
  }, [pushEvent, subscribe])

  useEffect(() => {
    const shouldListen = connected && interactionEnabled && voiceWanted && !!runtimeSessionId
    const requestKey = shouldListen ? `${runtimeSessionId}:${playerMode}` : null
    if (playerAsrRequestKeyRef.current === requestKey) return
    playerAsrRequestKeyRef.current = requestKey
    if (!requestKey) setPlayerListening(false)
    playerAsrQueueRef.current = playerAsrQueueRef.current.then(async () => {
      if (playerAsrOwnedRef.current) {
        await send('asr.stop', { source: 'vn_player' })
        playerAsrOwnedRef.current = false
      }
      if (!requestKey || playerAsrRequestKeyRef.current !== requestKey) return
      const result = await send('asr.start', {
        source: 'vn_player',
        one_shot: false,
        finish_after_turn_complete: false,
        source_payload: { kind: playerMode, session_id: runtimeSessionId },
      })
      const status = String(result.status || '')
      if (!['listening', 'awake', 'starting'].includes(status) || (result.source && result.source !== 'vn_player')) {
        throw new Error(status === 'already_listening'
          ? t('Microphone is busy in another session.')
          : String(result.error || result.reason || t('Microphone could not start.')))
      }
      if (playerAsrRequestKeyRef.current === requestKey) {
        playerAsrOwnedRef.current = true
        setPlayerListening(true)
      } else {
        await send('asr.stop', { source: 'vn_player' })
      }
    }).catch(err => {
      if (playerAsrRequestKeyRef.current === requestKey) {
        playerAsrRequestKeyRef.current = null
        setPlayerListening(false)
        setVoiceWanted(false)
        setError(err instanceof Error ? err.message : String(err))
      }
    })
  }, [connected, interactionEnabled, voiceWanted, playerMode, runtimeSessionId, send, t])

  useEffect(() => {
    if (runtimeSessionId === sessionRef.current) return
    sessionRef.current = runtimeSessionId
    setVisualAttachment(null)
    setVoiceWanted(runtimeSessionId && !launch.captureOnly && activeProfile?.voiceInput === true ? true : false)
  }, [runtimeSessionId, launch.captureOnly, activeProfile?.voiceInput])

  useEffect(() => () => {
    playerAsrRequestKeyRef.current = null
    playerAsrQueueRef.current.then(() => {
      if (playerAsrOwnedRef.current) return send('asr.stop', { source: 'vn_player' })
    }).catch(() => {})
  }, [send])

  const startProfile = async (profileId: string, captureOnly = false) => {
    setBusy(true)
    setError('')
    setEvents([])
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
    setBusy(true)
    setError('')
    try {
      if (playerAsrOwnedRef.current) {
        await send('asr.stop', { source: 'vn_player' })
        playerAsrOwnedRef.current = false
        playerAsrRequestKeyRef.current = null
        setPlayerListening(false)
      }
      setVoiceWanted(false)
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
      setBusy(false)
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
    setBusy(true)
    setError('')
    try {
      const method = routePlayerMethod(playerMode)
      const response = await send(method, {
        text,
        source: 'electron_vn_page',
        metadata: { source: 'vn_player_panel', mode: playerMode },
        ...((method === 'vn.player.ask' || method === 'vn.choice.ask') && visualAttachment ? { visual_context: visualAttachment } : {}),
      })
      if (response.status === 'unavailable' || response.error) throw new Error(String(response.error || t(capabilityReason(String(response.reason || '')))))
      pushEvent('vn.player', { text })
      setPlayerText('')
      setVisualAttachment(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
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
      if (captureSessionId === sessionRef.current) setVisualAttachment(context)
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
  const storyEvents = events.filter(item => item.text && ['vn.line', 'vn.reaction', 'vn.summary', 'vn.player', 'vn.player.asr', 'vn.error'].includes(item.method)).reverse()
  const eventNames: Record<string, string> = {
    'vn.line': 'Game text', 'vn.reaction': 'Companion', 'vn.summary': 'Story summary',
    'vn.player': 'You', 'vn.player.asr': 'You', 'vn.error': 'Companion error',
  }
  const sourceReady = activeProfile?.textSource === 'luna' ? !!activeProfile.lunaWsUrl : !!(activeProfile?.gameExists && activeProfile.agentExists && activeProfile.hookExists)
  const lineCount = launch.bridge?.lineCount || 0
  const reasonText = (reason?: string, enabled = false) => t(capabilityReason(reason, enabled))
  const bannerTitle = !connected ? 'Desktop connection unavailable' : launchStatus === 'error' ? 'Game connection failed'
    : launchStatus === 'starting' ? launch.game?.status === 'waiting_for_steam' ? 'Waiting for Steam to start the game…' : 'Connecting to game…'
    : launchStatus === 'stopping' ? 'Ending session…' : isTesting ? lineCount ? 'Text received — compare it with the game' : 'Waiting for game text…'
    : isPlaying ? 'Companion is live' : !sourceReady ? 'Finish game setup' : 'Ready to play'
  const bannerDetail = !connected ? 'Reconnect to the desktop service to manage VN Player.'
    : isTesting ? 'Advance a few lines in the game. Check that the text below matches what you see.'
    : isPlaying ? 'Keep playing. Your companion follows the story as text arrives.'
    : !sourceReady ? 'Choose the game executable, Agent and its game script in settings.'
    : activeProfile?.launchGame === false ? 'Start the game yourself. Agent will attach automatically when you click Start.'
    : activeProfile?.launchMethod === 'steam' ? 'Steam will open the game. Agent connects automatically.' : 'Your saved settings will launch the game and connect Agent.'

  useEffect(() => {
    if (followFeedRef.current && feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight
  }, [events, launch.capturedLines])

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
        <span>{t(activeProfile?.promptPack === 'mystery' ? 'Mystery VN' : 'General VN')} · {activeProfile?.textSource === 'luna' ? 'Luna' : 'Agent'}</span>
      </div>
      <div className="vn-game-actions">
        <button onClick={() => setEditor({})} disabled={!connected || busy || isActive}>{t('Add game')}</button>
        <button onClick={() => setEditor({ initial: activeProfile })} disabled={!connected || busy || isActive || !activeProfile}><FluentIcon name="Setting" size={14} aria-hidden="true" />{t('Edit game profile')}</button>
        {!isActive && <button className="vn-primary" onClick={() => void startProfile(activeProfile.id)} disabled={!connected || busy || !sourceReady}><FluentIcon name="Play" size={14} aria-hidden="true" />{t('Start')}</button>}
        {isActive && <button onClick={stop} disabled={!connected || busy}>{t('Stop')}</button>}
      </div>
    </section>
    <div className={`vn-session-banner ${launchStatus === 'error' ? 'has-error' : ''}`} role="status">
      <span className={`vn-status-dot ${isPlaying || (isTesting && lineCount) ? 'live' : ''}`} aria-hidden="true" />
      <div><strong>{t(bannerTitle)}</strong><p>{launch.error || t(bannerDetail)}</p></div>
      {isActive && <span className="vn-line-count">{lineCount} {t('lines received')}</span>}
    </div>
    {error && <div role="alert" className="vn-error vn-page-error">{error}</div>}
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
            <header><span>{t(eventNames[item.method])}</span><time>{item.time}</time></header><p>{item.text}</p>
          </article>)}
        </div>
        {isPlaying && <div className="vn-composer">
          <div className="vn-composer-tools">
            <label className="sr-only" htmlFor="vn-player-mode">{t('Message type')}</label>
            <select id="vn-player-mode" value={playerMode} onChange={e => setPlayerMode(e.target.value as typeof playerMode)} disabled={busy || !interactionEnabled}>
              <option value="ask">{t('Ask')}</option><option value="note">{t('Note')}</option><option value="choice">{t('Choice')}</option><option value="pin">{t('Pin')}</option>
            </select>
            <button onClick={() => setVoiceWanted(previous => !previous)} disabled={!interactionEnabled} aria-pressed={voiceWanted}>{t(voiceWanted ? 'Stop microphone' : 'Start microphone')}</button>
            {voiceWanted && <span>{t(playerListening ? 'Listening' : 'Starting microphone…')}</span>}
            <button onClick={() => void attachGameView()} disabled={!interactionEnabled || !visualSupported || visualBusy || busy || !['ask', 'choice'].includes(playerMode)}>{t(visualBusy ? 'Capturing game view…' : 'Attach game view')}</button>
            <button onClick={() => void send('tts.interrupt', {}).catch(err => setError(String(err)))} disabled={!interactionEnabled}>{t('Stop speech')}</button>
          </div>
          {visualAttachment && <div className="vn-visual-attachment"><img src={visualAttachment.frame.dataUrl} alt={t('Attached game view')} /><span>{t('Game view attached for next question')}</span><button onClick={() => setVisualAttachment(null)}>{t('Remove')}</button></div>}
          <div className="vn-compose-row"><textarea aria-label={t('Message to companion')} value={playerText} onChange={e => setPlayerText(e.target.value)} rows={2}
            placeholder={t('Ask about the current line, add a note, or inspect a choice...')} disabled={!interactionEnabled}
            onKeyDown={e => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && !e.nativeEvent.isComposing && !busy && playerText.trim()) void sendPlayerIntervention() }} />
            <button className="vn-primary" onClick={sendPlayerIntervention} disabled={!connected || busy || !interactionEnabled || !playerText.trim()}>{t('Send')}</button></div>
          {!interactionEnabled && <p className="vn-help">{reasonText(interaction?.reason)}</p>}
          {!visualSupported && <p className="vn-help">{t('Game view')}: {reasonText(runtime?.visual?.reason)}</p>}
        </div>}
      </section>
      <aside className="vn-companion-panel" aria-label={t('Game type and abilities')}>
        <div className="vn-section-heading"><div><h3>{t(activeProfile?.promptPack === 'mystery' ? 'Mystery VN' : 'General VN')}</h3><p>{t('Abilities follow your game type.')}</p></div></div>
        <VNAbilities preset={preset} states={isPlaying ? runtime?.capabilities : undefined} />
        <p className="vn-sidebar-note">{t(isTesting ? 'Abilities start after the text test.' : 'Availability depends on the model, story script and current alignment.')}</p>
      </aside>
    </div>
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
    {editor && <VNProfileEditor initial={editor.initial} capabilityPresets={capabilityPresets} overlayAvailable={profiles.some(profile => profile.overlayExists)} agentExe={agentExe} onClose={() => setEditor(null)} onSave={saveProfile} />}
  </div>
}
