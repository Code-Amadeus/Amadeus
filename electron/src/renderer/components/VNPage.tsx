import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useI18n } from '../i18n'
import VNProfileEditor, { type VNProfileSettings } from './VNProfileEditor'

type BackendSend = (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
type BackendSubscribe = (method: string, fn: (p: Record<string, unknown>) => void) => () => void

interface Props {
  send: BackendSend
  subscribe: BackendSubscribe
  connected: boolean
}

type VNProfile = Partial<VNProfileSettings> & {
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

type CapabilityState = { requested?: boolean; available?: boolean; enabled?: boolean; reason?: string }
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
  return JSON.stringify(payload).slice(0, 260)
}

function statusColor(status: string): string {
  const key = status.toLowerCase()
  if (key === 'active' || key === 'running') return '#107C10'
  if (key === 'starting' || key === 'stopping' || key === 'manual_required' || key === 'connecting' || key === 'waiting') return '#D83B01'
  if (key === 'error' || key === 'exited') return '#C42B1C'
  return 'var(--muted)'
}

function boolLabel(value?: boolean): string {
  return value ? 'ready' : 'missing'
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
  const [selectedProfile, setSelectedProfile] = useState('paranormasight')
  const [agentExe, setAgentExe] = useState('')
  const [editor, setEditor] = useState<{ initial?: VNProfile } | null>(null)
  const [launch, setLaunch] = useState<LaunchStatus>({ status: 'idle' })
  const [runtime, setRuntime] = useState<RuntimeState | null>(null)
  const [events, setEvents] = useState<VNEvent[]>([])
  const [lineText, setLineText] = useState('这里……是什么地方？')
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
      await send(method, {
        text,
        source: 'electron_vn_page',
        metadata: { source: 'vn_player_panel', mode: playerMode },
        ...((method === 'vn.player.ask' || method === 'vn.choice.ask') && visualAttachment ? { visual_context: visualAttachment } : {}),
      })
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
  const gameStatus = String(launch.game?.status || 'not_started')
  const hookStatus = String(launch.hook?.status || 'not_started')
  const overlayStatus = String(launch.overlay?.status || 'not_started')
  const bridgeStatus = String(launch.bridge?.status || 'not_started')
  const isActive = ['active', 'starting', 'stopping'].includes(launchStatus)
  const storyEvents = events.filter(item => ['vn.line', 'vn.reaction', 'vn.summary', 'vn.error'].includes(item.method))
  const capabilityNames: Record<string, string> = {
    immediate: 'Immediate commentary', interaction: 'Player interaction', summary: 'Story summaries',
    retrospective: 'Reflection', lookahead: 'Lookahead', reasoning: 'Detective reasoning',
  }
  const eventNames: Record<string, string> = {
    'vn.line': 'Game text', 'vn.reaction': 'Companion', 'vn.summary': 'Story summary', 'vn.error': 'Companion error',
  }
  const reasonNames: Record<string, string> = {
    disabled_by_profile: 'Turn this on in Companion settings.',
    ready: 'Available now',
    rules_only: 'Available through built-in story rules.',
    model_unavailable: 'The companion model is unavailable.',
    immediate_model_disabled: 'The commentary model is turned off.',
    retrospective_model_unavailable: 'The reflection model is unavailable.',
    lookahead_model_unavailable: 'The lookahead model is unavailable.',
    script_unavailable: 'Add a full script in Companion settings.',
    alignment_unavailable: 'Waiting for the script to align with the game.',
    semantic_type_unsupported: 'This companion type does not support this ability.',
    model_unsupported: 'The selected model does not support this ability.',
    model_unconfigured: 'Choose a model to use this ability.',
  }
  const reasonText = (reason: string | undefined, enabled = false) =>
    t(reason ? (reasonNames[reason] || (enabled ? 'Available now' : 'Unavailable now')) : (enabled ? 'Available now' : 'Unavailable now'))
  const controlButtonStyle = {
    height: 34,
    padding: '0 12px',
    borderRadius: 8,
    border: '1px solid var(--border)',
    color: 'var(--text)',
    backgroundColor: 'var(--surface)',
    fontSize: 11,
  }

  return (
    <div className="vn-player flex-1 flex flex-col min-h-0" style={{ padding: '16px 18px 18px', backgroundColor: 'var(--bg)' }}>
      <header className="flex items-start gap-4 shrink-0" style={{ marginBottom: 12 }}>
        <div className="min-w-0 flex-1">
          <h2 style={{ margin: 0, color: 'var(--text)', fontSize: 20, fontWeight: 700, lineHeight: '26px' }}>{t('VN Player — Experimental')}</h2>
          <p style={{ margin: '2px 0 0', color: 'var(--muted)', fontSize: 11, lineHeight: '16px' }}>
            {t('Live VN activity, player intervention, and runtime control.')}
          </p>
        </div>
        <button onClick={refresh} disabled={!connected || busy} style={controlButtonStyle}>{t('Refresh')}</button>
      </header>

      <div className="flex items-center flex-wrap gap-2 shrink-0" style={{ marginBottom: 9, padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)' }}>
        <span style={{ color: 'var(--muted)', fontSize: 10.5, fontWeight: 650 }}>{t('Profile')}</span>
        <select
          value={selectedProfile}
          onChange={event => setSelectedProfile(event.target.value)}
          disabled={busy || isActive}
          style={{ width: 190, height: 34, borderRadius: 8, border: '1px solid var(--border)', color: 'var(--text)', background: 'var(--bg)', padding: '0 10px', fontSize: 11 }}
        >
          {profiles.length === 0 && <option value="paranormasight">paranormasight</option>}
          {profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
        </select>
        <span style={{ color: statusColor(launchStatus), fontSize: 10.5, fontWeight: 650 }}>{launchStatus.replaceAll('_', ' ')}</span>
        <button onClick={() => setEditor({})} disabled={!connected || busy || isActive} style={controlButtonStyle}>{t('Add game')}</button>
        <button onClick={() => setEditor({ initial: activeProfile })} disabled={!connected || busy || isActive || !activeProfile} style={controlButtonStyle}>{t('Edit game profile')}</button>
        <span className="flex-1" />
        <button onClick={() => void startProfile(activeProfile.id, true)} disabled={!connected || busy || isActive || !activeProfile} style={controlButtonStyle}>{t('Test text capture')}</button>
        <button onClick={() => void startProfile(activeProfile.id)} disabled={!connected || busy || isActive || !activeProfile} style={{ ...controlButtonStyle, borderColor: 'var(--accent)', color: 'var(--accent)', fontWeight: 650 }}>{t('Start')}</button>
        <button onClick={stop} disabled={!connected || busy || launchStatus === 'idle'} style={controlButtonStyle}>{t('Stop')}</button>
      </div>

      <div className="vn-session-banner" role="status">
        <strong>{t(!connected ? 'Desktop connection unavailable' : launch.captureOnly ? 'Text capture test' : launchStatus === 'active' ? 'Companion is live' : launchStatus === 'starting' ? 'Connecting to game…' : launchStatus === 'error' ? 'Game connection failed' : 'Ready to connect')}</strong>
        <span>{!connected ? t('Reconnect to the desktop service to manage VN Player.') : launch.error || (launch.captureOnly
          ? t('Advance the game to check captured text. The companion is paused during this test.')
          : bridgeStatus === 'waiting' && launch.bridge?.error
            ? launch.bridge.error
            : launch.bridge?.lastTextPreview
              ? `${t('Latest game text')}: ${launch.bridge.lastTextPreview}`
              : t(launchStatus === 'active' ? 'Waiting for the next game line.' : 'Choose a game and press Start.'))}</span>
      </div>
      <div className="flex items-center flex-wrap gap-1.5 shrink-0" style={{ marginBottom: 9 }}>
        {!launch.captureOnly && <RuntimeChip label="Runtime" status={runtimeStatus} />}
        <RuntimeChip label="Game" status={gameStatus} detail={launch.game?.pid ? `pid ${launch.game.pid}` : undefined} />
        <RuntimeChip label="Text source" status={hookStatus} detail={launch.hook?.pid ? `pid ${launch.hook.pid}` : undefined} />
        {!launch.captureOnly && <RuntimeChip label="Overlay" status={overlayStatus} detail={launch.overlay?.pid ? `pid ${launch.overlay.pid}` : undefined} />}
        <RuntimeChip label="Bridge" status={bridgeStatus} detail={`${launch.bridge?.lineCount || 0} lines`} />
      </div>
      {!launch.captureOnly && runtime?.capabilities && <div className="vn-live-capabilities" aria-label={t('Companion abilities')}>
        {Object.entries(runtime.capabilities).map(([key, state]) => <span key={key}
          className={state.enabled ? 'vn-ability enabled' : 'vn-ability'}
          title={reasonText(state.reason, state.enabled)}>
          {t(capabilityNames[key] || key)} · {t(state.enabled ? 'On' : state.requested ? 'Waiting' : 'Off')}
        </span>)}
      </div>}

      <section aria-label={t(launch.captureOnly ? 'Captured text' : 'VN activity')} className="flex-1 flex flex-col min-h-0" style={{ border: '1px solid var(--border)', borderRadius: 11, overflow: 'hidden', background: 'var(--surface)' }}>
        <div className="flex items-center gap-2 shrink-0" style={{ minHeight: 41, padding: '6px 10px 6px 13px', borderBottom: '1px solid var(--border)' }}>
          <h3 style={{ margin: 0, color: 'var(--text)', fontSize: 13, fontWeight: 650 }}>{t(launch.captureOnly ? 'Captured text' : 'VN activity')}</h3>
          <span style={{ color: 'var(--faint)', fontSize: 10 }}>{launch.captureOnly ? (launch.capturedLines?.length || 0) : storyEvents.length} {t('recent')}</span>
          <span className="flex-1" />
          {!launch.captureOnly && <button onClick={() => setEvents([])} disabled={events.length === 0} style={{ ...controlButtonStyle, height: 28, color: 'var(--muted)', opacity: events.length ? 1 : 0.4 }}>{t('Clear')}</button>}
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto" style={{ padding: launch.captureOnly || storyEvents.length ? '4px 14px 12px' : 0 }}>
          {launch.captureOnly ? <>
            <p style={{ margin: '8px 0 12px', color: 'var(--muted)', fontSize: 12 }}>{t('Advance a few lines in the game and compare them here. Receiving text does not automatically confirm extraction quality.')}</p>
            {!launch.capturedLines?.length && <p style={{ color: 'var(--muted)', fontSize: 12 }}>{t('Waiting for game text…')}</p>}
            {launch.capturedLines?.map((line, index) => <div key={`${line.receivedAt}-${index}`} style={{ borderTop: '1px solid var(--border)', padding: '10px 0', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', color: 'var(--text)', fontSize: 13 }}>
              <span style={{ color: 'var(--muted)', marginRight: 8 }}>{index + 1}.</span>{line.speaker ? `${line.speaker}: ` : ''}{line.text}
            </div>)}
          </> : storyEvents.length === 0 ? (
            <div className="flex items-center justify-center h-full" style={{ color: 'var(--muted)', fontSize: 12 }}>{t('Your game text and companion responses will appear here.')}</div>
          ) : storyEvents.map(item => (
            <article key={item.id} className={`vn-story-item ${item.method === 'vn.line' ? 'game-line' : 'companion-line'}`}>
              <div className="flex items-center gap-2">
                <span style={{ color: 'var(--accent)', fontSize: 10.5, fontWeight: 700 }}>{t(eventNames[item.method] || item.method)}</span>
                {item.detail ? <span style={{ color: 'var(--muted)', fontSize: 10 }}>{item.detail}</span> : null}
                <span className="ml-auto" style={{ color: 'var(--faint)', fontSize: 10 }}>{item.time}</span>
              </div>
              <div style={{ marginTop: 3, color: 'var(--text)', fontSize: 12, lineHeight: 1.45, whiteSpace: 'pre-wrap' }}>{item.text}</div>
            </article>
          ))}
        </div>

        {error ? <div role="status" style={{ padding: '7px 12px', borderTop: '1px solid rgba(196,43,28,0.2)', color: '#C42B1C', background: 'rgba(196,43,28,0.04)', fontSize: 10.5 }}>{error}</div> : null}

        {!launch.captureOnly && <div className="shrink-0" style={{ padding: '10px 12px 11px', borderTop: '1px solid var(--border)', background: 'var(--surface-alt)' }}>
          <div className="flex items-center gap-2" style={{ marginBottom: 7 }}>
            <span style={{ color: 'var(--text)', fontSize: 11, fontWeight: 650 }}>{t('Player intervention')}</span>
            <button type="button" onClick={() => setVoiceWanted(previous => !previous)}
              disabled={!interactionEnabled}
              aria-pressed={voiceWanted} className="vn-mic-button">
              {t(voiceWanted ? 'Stop microphone' : 'Start microphone')}
            </button>
            <span style={{ color: playerListening ? '#107C10' : 'var(--muted)', fontSize: 10 }}>
              {t(playerListening ? 'Listening' : voiceWanted ? 'Starting microphone…' : 'Microphone off')}
            </span>
            <span className="flex-1" />
            <button type="button" onClick={() => void send('tts.interrupt', {}).catch(err => setError(err instanceof Error ? err.message : String(err)))}
              disabled={!interactionEnabled} className="vn-mic-button">{t('Stop speech')}</button>
          </div>
          <div className="flex items-stretch gap-2">
            <select
              value={playerMode}
              onChange={event => setPlayerMode(event.target.value as typeof playerMode)}
              disabled={busy || !interactionEnabled}
              style={{ width: 104, minHeight: 40, borderRadius: 8, border: '1px solid var(--border)', color: 'var(--text)', background: 'var(--surface)', padding: '0 8px', fontSize: 11 }}
            >
              <option value="ask">{t('Ask')}</option>
              <option value="note">{t('Note')}</option>
              <option value="choice">{t('Choice')}</option>
              <option value="pin">{t('Pin')}</option>
            </select>
            <textarea
              value={playerText}
              onChange={event => setPlayerText(event.target.value)}
              rows={2}
              placeholder={t('Ask about the current line, add a note, or inspect a choice...')}
              disabled={!interactionEnabled}
              style={{ minWidth: 0, flex: 1, resize: 'none', borderRadius: 8, border: '1px solid var(--border)', color: 'var(--text)', background: 'var(--surface)', padding: '8px 10px', fontSize: 12, lineHeight: 1.4 }}
            />
            <button onClick={sendPlayerIntervention} disabled={!connected || busy || !interactionEnabled || !playerText.trim()} style={{ ...controlButtonStyle, alignSelf: 'stretch', height: 'auto', color: 'var(--accent)', fontWeight: 650 }}>{t('Send')}</button>
          </div>
          <div className="vn-visual-controls">
            <button type="button" onClick={() => void attachGameView()}
              disabled={!interactionEnabled || !visualSupported || visualBusy || busy || !['ask', 'choice'].includes(playerMode)}
              className="vn-mic-button">{t(visualBusy ? 'Capturing game view…' : 'Attach game view')}</button>
            {!visualSupported && <span>{reasonText(runtime?.visual?.reason)}</span>}
            {!interactionEnabled && <span>{interaction?.reason ? reasonText(interaction.reason) : t('Start the companion and enable Player interaction to attach a game view.')}</span>}
            {visualAttachment && <div className="vn-visual-attachment">
              <img src={visualAttachment.frame.dataUrl} alt={t('Attached game view')} />
              <span>{t('Game view attached for next question')}</span>
              <button type="button" onClick={() => setVisualAttachment(null)}>{t('Remove')}</button>
            </div>}
          </div>
        </div>}
      </section>

      <details className="shrink-0" style={{ marginTop: 9, border: '1px solid var(--border)', borderRadius: 9, background: 'var(--surface)' }}>
        <summary className="cursor-pointer select-none" style={{ padding: '9px 12px', color: 'var(--muted)', fontSize: 11, fontWeight: 600 }}>
          {t('Diagnostics and manual line test')}
        </summary>
        <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 18, padding: '12px 14px 14px', borderTop: '1px solid var(--border)' }}>
          <div>
            {activeProfile ? (
              <div style={{ marginBottom: 10, color: 'var(--muted)', fontSize: 10.5, lineHeight: 1.5 }}>
                <div style={{ color: 'var(--text)', fontSize: 12, fontWeight: 650 }}>{activeProfile.description || activeProfile.name}</div>
                <span>Script {boolLabel(activeProfile.scriptExists)} · Game {boolLabel(activeProfile.gameExists)} · Hook {boolLabel(activeProfile.agentExists && activeProfile.hookExists)} · Overlay {boolLabel(activeProfile.overlayExists)}</span>
                <div className="truncate" title={activeProfile.scriptPath || ''}>{activeProfile.scriptPath || 'No script path'}</div>
              </div>
            ) : null}
            <p style={{ color: 'var(--muted)', fontSize: 12 }}>{t('Start uses this game’s saved settings. Edit the profile to change its script or launch behavior.')}</p>
          </div>

          <div>
            <div style={{ marginBottom: 9, color: 'var(--muted)', fontSize: 10.5 }}>
              {t('Session')} <span title={launch.sessionId || ''} style={{ color: 'var(--text)' }}>{launch.sessionId || '-'}</span>
            </div>
            <label style={{ display: 'block', marginBottom: 6, color: 'var(--text)', fontSize: 11, fontWeight: 650 }}>{t('Manual vn.line test')}</label>
            <textarea
              value={lineText}
              onChange={event => setLineText(event.target.value)}
              rows={3}
              style={{ width: '100%', resize: 'vertical', borderRadius: 8, border: '1px solid var(--border)', color: 'var(--text)', background: 'var(--bg)', padding: 9, fontSize: 12, lineHeight: 1.45 }}
            />
            <button onClick={sendLine} disabled={!connected || busy || runtimeStatus !== 'active'} style={{ ...controlButtonStyle, marginTop: 7 }}>{t('Send line')}</button>
          </div>
        </div>
        <div className="vn-raw-events">
          <strong>{t('Raw events')}</strong>
          {events.length === 0 ? <p>{t('No events received.')}</p> : events.map(item => <div key={item.id}>
            <time>{item.time}</time> <code>{item.method}</code> {item.detail && <small>{item.detail}</small>}
            <pre>{item.raw}</pre>
          </div>)}
        </div>
      </details>
      {editor && <VNProfileEditor initial={editor.initial} overlayAvailable={profiles.some(profile => profile.overlayExists)}
        agentExe={agentExe} onClose={() => setEditor(null)} onSave={saveProfile} />}
    </div>
  )
}

