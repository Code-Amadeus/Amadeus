import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

type BackendSend = (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
type BackendSubscribe = (method: string, fn: (p: Record<string, unknown>) => void) => () => void

interface Props {
  send: BackendSend
  subscribe: BackendSubscribe
  connected: boolean
}

type VNProfile = {
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
}

type LaunchStatus = {
  status?: string
  profileId?: string
  sessionId?: string
  startedAt?: number
  updatedAt?: number
  error?: string
  game?: ProcessStatus
  hook?: ProcessStatus
  overlay?: ProcessStatus
  bridge?: BridgeStatus
  runtime?: Record<string, unknown> | null
  profiles?: VNProfile[]
}

type VNEvent = {
  id: string
  method: string
  text: string
  detail?: string
  time: string
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
  if (key === 'starting' || key === 'stopping' || key === 'manual_required') return '#D83B01'
  if (key === 'error' || key === 'exited') return '#C42B1C'
  return 'var(--muted)'
}

function boolLabel(value?: boolean): string {
  return value ? 'ready' : 'missing'
}

function Toggle({
  checked,
  disabled,
  label,
  detail,
  onChange,
}: {
  checked: boolean
  disabled?: boolean
  label: string
  detail?: string
  onChange: (checked: boolean) => void
}) {
  return (
    <label
      className="flex items-start gap-2"
      style={{
        color: disabled ? 'var(--muted)' : 'var(--text)',
        fontSize: 12,
        lineHeight: 1.35,
        cursor: disabled ? 'not-allowed' : 'pointer',
      }}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={event => onChange(event.target.checked)}
        style={{ marginTop: 2 }}
      />
      <span>
        <span style={{ fontWeight: 650 }}>{label}</span>
        {detail && <span style={{ color: 'var(--muted)' }}> · {detail}</span>}
      </span>
    </label>
  )
}

function RuntimeChip({ label, status, detail }: { label: string; status: string; detail?: string }) {
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
      <span style={{ color: 'var(--text)', fontWeight: 650 }}>{label}</span>
      <span>{status.replaceAll('_', ' ')}</span>
      {detail ? <span style={{ color: 'var(--faint)' }}>{detail}</span> : null}
    </span>
  )
}

export default function VNPage({ send, subscribe, connected }: Props) {
  const [profiles, setProfiles] = useState<VNProfile[]>([])
  const [selectedProfile, setSelectedProfile] = useState('paranormasight')
  const [launch, setLaunch] = useState<LaunchStatus>({ status: 'idle' })
  const [runtime, setRuntime] = useState<Record<string, unknown> | null>(null)
  const [events, setEvents] = useState<VNEvent[]>([])
  const [lineText, setLineText] = useState('这里……是什么地方？')
  const [playerText, setPlayerText] = useState('')
  const [playerMode, setPlayerMode] = useState<'ask' | 'note' | 'choice' | 'pin'>('ask')
  const [playerListening, setPlayerListening] = useState(false)
  const [launchGame, setLaunchGame] = useState(false)
  const [attachHook, setAttachHook] = useState(false)
  const [launchOverlay, setLaunchOverlay] = useState(true)
  const [bridgeClipboard, setBridgeClipboard] = useState(true)
  const [stopWallpaper, setStopWallpaper] = useState(true)
  const [closeGameOnStop, setCloseGameOnStop] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const activeProfile = useMemo(
    () => profiles.find(profile => profile.id === selectedProfile) || profiles[0],
    [profiles, selectedProfile],
  )
  const runtimeStatus = String((runtime?.status as string | undefined) || 'unknown')
  const runtimeSessionId = launch.sessionId || String(runtime?.session_id || '')
  const playerAsrRequestKeyRef = useRef<string | null>(null)

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
      if (statusRes.runtime && typeof statusRes.runtime === 'object') {
        setRuntime(statusRes.runtime as Record<string, unknown>)
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
        setRuntime(payload.runtime as Record<string, unknown>)
      }
    })
    const unsubStatus = subscribe('vn.status', payload => {
      setRuntime(payload)
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
      pushEvent('vn.player.asr', payload)
    })
    const unsubAsrStatus = subscribe('asr.status', payload => {
      if (payload.source !== 'vn_player') return
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
    const shouldListen = connected && runtimeStatus === 'active'
    const requestKey = `${runtimeSessionId || 'vn'}:${playerMode}`

    if (!shouldListen) {
      if (playerListening || playerAsrRequestKeyRef.current) {
        send('asr.stop', { source: 'vn_player' }).catch(() => {})
      }
      playerAsrRequestKeyRef.current = null
      return
    }

    if (playerAsrRequestKeyRef.current === requestKey) return
    playerAsrRequestKeyRef.current = requestKey
    send('asr.start', {
      source: 'vn_player',
      one_shot: false,
      finish_after_turn_complete: false,
      source_payload: {
        kind: playerMode,
        session_id: runtimeSessionId,
      },
    }).catch(err => {
      playerAsrRequestKeyRef.current = null
      setError(err instanceof Error ? err.message : String(err))
    })
  }, [connected, playerListening, playerMode, runtimeSessionId, runtimeStatus, send])

  const startWithOptions = async (options?: Partial<{
    launchGame: boolean
    attachHook: boolean
    launchOverlay: boolean
    bridgeClipboard: boolean
    stopWallpaper: boolean
  }>) => {
    const nextLaunchGame = options?.launchGame ?? launchGame
    const payload = {
      profileId: selectedProfile,
      launchGame: nextLaunchGame,
      attachHook: options?.attachHook ?? attachHook,
      launchOverlay: options?.launchOverlay ?? launchOverlay,
      bridgeClipboard: options?.bridgeClipboard ?? bridgeClipboard,
      stopWallpaper: options?.stopWallpaper ?? (nextLaunchGame ? stopWallpaper : false),
    }
    setBusy(true)
    setError('')
    try {
      const res = await send('vn.launch.start', payload)
      setLaunch(res as LaunchStatus)
      if (res.runtime && typeof res.runtime === 'object') {
        setRuntime(res.runtime as Record<string, unknown>)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const stop = async () => {
    setBusy(true)
    setError('')
    try {
      if (playerListening) {
        await send('asr.stop', { source: 'vn_player' })
        setPlayerListening(false)
      }
      const res = await send('vn.launch.stop', {
        reason: 'electron_vn_page',
        closeGame: closeGameOnStop,
      })
      setLaunch(res as LaunchStatus)
      if (res.runtime && typeof res.runtime === 'object') {
        setRuntime(res.runtime as Record<string, unknown>)
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
      await send(routePlayerMethod(playerMode), {
        text,
        source: 'electron_vn_page',
        metadata: { source: 'vn_player_panel', mode: playerMode },
      })
      setPlayerText('')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
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
  const isActive = launchStatus === 'active' || launchStatus === 'starting'
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
    <div className="flex-1 flex flex-col min-h-0" style={{ padding: '16px 18px 18px', backgroundColor: 'var(--bg)' }}>
      <header className="flex items-start gap-4 shrink-0" style={{ marginBottom: 12 }}>
        <div className="min-w-0 flex-1">
          <h2 style={{ margin: 0, color: 'var(--text)', fontSize: 20, fontWeight: 700, lineHeight: '26px' }}>VN Player — Experimental</h2>
          <p style={{ margin: '2px 0 0', color: 'var(--muted)', fontSize: 11, lineHeight: '16px' }}>
            Live VN activity, player intervention, and runtime control.
          </p>
        </div>
        <button onClick={refresh} disabled={!connected || busy} style={controlButtonStyle}>Refresh</button>
      </header>

      <div className="flex items-center flex-wrap gap-2 shrink-0" style={{ marginBottom: 9, padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)' }}>
        <span style={{ color: 'var(--muted)', fontSize: 10.5, fontWeight: 650 }}>Profile</span>
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
        <span className="flex-1" />
        <button onClick={() => startWithOptions()} disabled={!connected || busy || isActive} style={{ ...controlButtonStyle, borderColor: 'var(--accent)', color: 'var(--accent)', fontWeight: 650 }}>Start</button>
        <button onClick={stop} disabled={!connected || busy || launchStatus === 'idle'} style={controlButtonStyle}>Stop</button>
      </div>

      <div className="flex items-center flex-wrap gap-1.5 shrink-0" style={{ marginBottom: 9 }}>
        <RuntimeChip label="Runtime" status={runtimeStatus} />
        <RuntimeChip label="Game" status={gameStatus} detail={launch.game?.pid ? `pid ${launch.game.pid}` : undefined} />
        <RuntimeChip label="Hook" status={hookStatus} detail={launch.hook?.pid ? `pid ${launch.hook.pid}` : undefined} />
        <RuntimeChip label="Overlay" status={overlayStatus} detail={launch.overlay?.pid ? `pid ${launch.overlay.pid}` : undefined} />
        <RuntimeChip label="Bridge" status={bridgeStatus} detail={`${launch.bridge?.lineCount || 0} lines`} />
      </div>

      <section className="flex-1 flex flex-col min-h-0" style={{ border: '1px solid var(--border)', borderRadius: 11, overflow: 'hidden', background: 'var(--surface)' }}>
        <div className="flex items-center gap-2 shrink-0" style={{ minHeight: 41, padding: '6px 10px 6px 13px', borderBottom: '1px solid var(--border)' }}>
          <h3 style={{ margin: 0, color: 'var(--text)', fontSize: 13, fontWeight: 650 }}>VN activity</h3>
          <span style={{ color: 'var(--faint)', fontSize: 10 }}>{events.length} recent</span>
          <span className="flex-1" />
          <button onClick={() => setEvents([])} disabled={events.length === 0} style={{ ...controlButtonStyle, height: 28, color: 'var(--muted)', opacity: events.length ? 1 : 0.4 }}>Clear</button>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto" style={{ padding: events.length ? '4px 14px 12px' : 0 }}>
          {events.length === 0 ? (
            <div className="flex items-center justify-center h-full" style={{ color: 'var(--muted)', fontSize: 12 }}>No VN activity yet.</div>
          ) : events.map(item => (
            <article key={item.id} style={{ padding: '10px 0', borderBottom: '1px solid var(--border)' }}>
              <div className="flex items-center gap-2">
                <span style={{ color: 'var(--accent)', fontSize: 10.5, fontWeight: 700 }}>{item.method}</span>
                {item.detail ? <span style={{ color: 'var(--muted)', fontSize: 10 }}>{item.detail}</span> : null}
                <span className="ml-auto" style={{ color: 'var(--faint)', fontSize: 10 }}>{item.time}</span>
              </div>
              <div style={{ marginTop: 3, color: 'var(--text)', fontSize: 12, lineHeight: 1.45, whiteSpace: 'pre-wrap' }}>{item.text}</div>
            </article>
          ))}
        </div>

        {error ? <div role="status" style={{ padding: '7px 12px', borderTop: '1px solid rgba(196,43,28,0.2)', color: '#C42B1C', background: 'rgba(196,43,28,0.04)', fontSize: 10.5 }}>{error}</div> : null}

        <div className="shrink-0" style={{ padding: '10px 12px 11px', borderTop: '1px solid var(--border)', background: 'var(--surface-alt)' }}>
          <div className="flex items-center gap-2" style={{ marginBottom: 7 }}>
            <span style={{ color: 'var(--text)', fontSize: 11, fontWeight: 650 }}>Player intervention</span>
            <span style={{ color: playerListening ? '#107C10' : 'var(--muted)', fontSize: 10 }}>
              {runtimeStatus === 'active' ? (playerListening ? 'voice lane active' : 'arming voice lane') : 'starts with runtime'}
            </span>
          </div>
          <div className="flex items-stretch gap-2">
            <select
              value={playerMode}
              onChange={event => setPlayerMode(event.target.value as typeof playerMode)}
              disabled={busy}
              style={{ width: 104, minHeight: 40, borderRadius: 8, border: '1px solid var(--border)', color: 'var(--text)', background: 'var(--surface)', padding: '0 8px', fontSize: 11 }}
            >
              <option value="ask">Ask</option>
              <option value="note">Note</option>
              <option value="choice">Choice</option>
              <option value="pin">Pin</option>
            </select>
            <textarea
              value={playerText}
              onChange={event => setPlayerText(event.target.value)}
              rows={2}
              placeholder="Ask about the current line, add a note, or inspect a choice..."
              style={{ minWidth: 0, flex: 1, resize: 'none', borderRadius: 8, border: '1px solid var(--border)', color: 'var(--text)', background: 'var(--surface)', padding: '8px 10px', fontSize: 12, lineHeight: 1.4 }}
            />
            <button onClick={sendPlayerIntervention} disabled={!connected || busy || runtimeStatus !== 'active' || !playerText.trim()} style={{ ...controlButtonStyle, alignSelf: 'stretch', height: 'auto', color: 'var(--accent)', fontWeight: 650 }}>Send</button>
          </div>
          <div style={{ marginTop: 5, color: playerListening ? '#107C10' : 'var(--faint)', fontSize: 9.5 }}>
            {playerListening ? 'Speech is routed to VN runtime only.' : 'Player speech never enters main chat.'}
          </div>
        </div>
      </section>

      <details className="shrink-0" style={{ marginTop: 9, border: '1px solid var(--border)', borderRadius: 9, background: 'var(--surface)' }}>
        <summary className="cursor-pointer select-none" style={{ padding: '9px 12px', color: 'var(--muted)', fontSize: 11, fontWeight: 600 }}>
          Advanced launch options and line test
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
            <div className="grid gap-2">
              <Toggle checked={launchGame} disabled={busy || isActive} label="Launch game process" detail="PARANORMASIGHT.exe" onChange={setLaunchGame} />
              <Toggle checked={attachHook} disabled={busy || isActive} label="Attach hook agent" detail="0xDC00 Agent + script" onChange={setAttachHook} />
              <Toggle checked={launchOverlay} disabled={busy || isActive} label="Portrait overlay" detail="avatar + subtitle box" onChange={setLaunchOverlay} />
              <Toggle checked={bridgeClipboard} disabled={busy || isActive} label="Enable line bridge" detail="agent websocket -> vn.line" onChange={setBridgeClipboard} />
              <Toggle checked={stopWallpaper} disabled={busy || isActive || !launchGame} label="Exit wallpaper before game" detail="keeps game focus clean" onChange={setStopWallpaper} />
              <Toggle checked={closeGameOnStop} disabled={busy} label="Close game on stop" detail="off keeps the game open" onChange={setCloseGameOnStop} />
            </div>
            <button onClick={() => startWithOptions({ launchGame: true, attachHook: true, launchOverlay: true, bridgeClipboard: true, stopWallpaper: true })} disabled={!connected || busy || isActive} style={{ ...controlButtonStyle, marginTop: 10 }}>Start full stack</button>
          </div>

          <div>
            <div style={{ marginBottom: 9, color: 'var(--muted)', fontSize: 10.5 }}>
              Session <span title={launch.sessionId || ''} style={{ color: 'var(--text)' }}>{launch.sessionId || '-'}</span>
            </div>
            <label style={{ display: 'block', marginBottom: 6, color: 'var(--text)', fontSize: 11, fontWeight: 650 }}>Manual vn.line test</label>
            <textarea
              value={lineText}
              onChange={event => setLineText(event.target.value)}
              rows={3}
              style={{ width: '100%', resize: 'vertical', borderRadius: 8, border: '1px solid var(--border)', color: 'var(--text)', background: 'var(--bg)', padding: 9, fontSize: 12, lineHeight: 1.45 }}
            />
            <button onClick={sendLine} disabled={!connected || busy || runtimeStatus !== 'active'} style={{ ...controlButtonStyle, marginTop: 7 }}>Send line</button>
          </div>
        </div>
      </details>
    </div>
  )
}

