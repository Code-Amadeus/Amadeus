import { useState, useEffect, useCallback, useRef } from 'react'
import FluentIcon from './FluentIcon'
import { ELECTRON_SLICE_START_PARAMS } from '../wallpaperSlice'

interface Props {
  send: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
  subscribe: (method: string, fn: (p: Record<string, unknown>) => void) => () => void
  connected: boolean
  renderActive: boolean
  wallpaperActive: boolean
}

export default function BackendPage({ send, subscribe, connected, renderActive, wallpaperActive }: Props) {
  const [status, setStatus] = useState<Record<string, unknown>>({})
  const [logLines, setLogLines] = useState<string[]>([])
  const [logTotal, setLogTotal] = useState(0)
  const [renderStatus, setRenderStatus] = useState('')
  const [wallpaperStatus, setWallpaperStatus] = useState('')
  const [actionsDisabled, setActionsDisabled] = useState(false)
  const logEndRef = useRef<HTMLDivElement>(null)

  // Subscribe to system status
  useEffect(() => {
    const unsub = subscribe('system.status', (p) => setStatus(p))
    return unsub
  }, [subscribe])

  // Poll log
  const fetchLog = useCallback(async () => {
    try {
      const res = await send('system.get_log', { lines: 60 })
      if (Array.isArray(res?.lines)) {
        setLogLines(res.lines as string[])
        setLogTotal((res.total as number) ?? 0)
      }
    } catch { /* ignore */ }
  }, [send])

  useEffect(() => {
    fetchLog()
    const t = setInterval(fetchLog, 3000)
    return () => clearInterval(t)
  }, [fetchLog])

  // Auto-scroll log
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [logLines])

  const doAction = useCallback(async (
    method: string,
    setter: (v: string) => void,
    params: Record<string, unknown> = {},
  ) => {
    setActionsDisabled(true)
    setter('...')
    try {
      const res = await send(method, params)
      setter(JSON.stringify(res))
    } catch (e) {
      setter(`ERROR: ${e}`)
    }
    setActionsDisabled(false)
    fetchLog()
  }, [send, fetchLog])

  const statusColor = connected ? '#107C10' : '#C42B1C'
  const dot = (ok: unknown) => (
    <span className="inline-block rounded-full shrink-0" style={{
      width: 8, height: 8,
      backgroundColor: ok ? '#107C10' : ok === false ? '#C42B1C' : '#98A2B3',
    }} />
  )

  const statusChip = (label: string, ok: unknown, detail?: string) => (
    <span
      className="inline-flex items-center gap-1.5 shrink-0"
      style={{
        height: 27,
        padding: '0 9px',
        border: '1px solid var(--border)',
        borderRadius: 999,
        background: 'var(--surface)',
        color: 'var(--muted)',
        fontSize: 10.5,
      }}
    >
      {dot(ok)}
      <span style={{ color: 'var(--text)', fontWeight: 600 }}>{label}</span>
      {detail ? <span style={{ color: 'var(--faint)' }}>{detail}</span> : null}
    </span>
  )

  const actionButtonStyle = {
    height: 31,
    padding: '0 10px',
    borderRadius: 7,
    border: '1px solid var(--border)',
    background: 'var(--surface)',
    color: 'var(--text)',
    fontSize: 10.5,
  }

  return (
    <div className="flex-1 flex flex-col min-h-0" style={{ padding: '16px 18px 18px', backgroundColor: 'var(--bg)' }}>
      <header className="flex items-start gap-4 shrink-0" style={{ marginBottom: 12 }}>
        <div className="min-w-0 flex-1">
          <h2 style={{ margin: 0, color: 'var(--text)', fontSize: 20, fontWeight: 700, lineHeight: '26px' }}>Backend</h2>
          <p style={{ margin: '2px 0 0', color: 'var(--muted)', fontSize: 11, lineHeight: '16px' }}>
            Live runtime status and server diagnostics.
          </p>
        </div>
        <span className="inline-flex items-center gap-2 shrink-0" style={{ height: 29, color: statusColor, fontSize: 11, fontWeight: 650 }}>
          {dot(connected)} {connected ? 'Connected' : 'Offline'}
        </span>
      </header>

      <div className="flex items-center flex-wrap gap-1.5 shrink-0" style={{ marginBottom: 10 }}>
        {statusChip('VTS', status.vts_connected)}
        {statusChip('TTS', status.tts_ready)}
        {statusChip('ASR', status.asr_ready)}
        {statusChip('Render', renderActive, renderActive ? 'PixiJS' : 'VTS')}
        {statusChip('Wallpaper', wallpaperActive)}
      </div>

      <section className="flex-1 flex flex-col min-h-0" style={{ border: '1px solid var(--border)', borderRadius: 11, overflow: 'hidden', background: 'var(--surface)' }}>
        <div className="flex items-center gap-2 shrink-0" style={{ minHeight: 43, padding: '6px 10px 6px 13px', borderBottom: '1px solid var(--border)' }}>
          <h3 style={{ margin: 0, color: 'var(--text)', fontSize: 13, fontWeight: 650 }}>Server log</h3>
          <span style={{ color: 'var(--faint)', fontSize: 10 }}>{logTotal.toLocaleString()} lines</span>
          <span className="flex-1" />
          <span style={{ color: 'var(--faint)', fontSize: 9.5 }}>Updates every 3s</span>
          <button
            onClick={fetchLog}
            className="inline-flex items-center gap-1.5 border border-[var(--border)] bg-[var(--surface)] hover:bg-[var(--hover)] cursor-pointer transition-colors"
            style={{ ...actionButtonStyle, height: 29 }}
          >
            <FluentIcon name="Sync" size={13} /> Refresh
          </button>
        </div>
        <div
          className="flex-1 overflow-y-auto min-h-0"
          style={{
            backgroundColor: '#1E1E2E',
            fontFamily: "'Cascadia Code', 'Fira Code', 'Consolas', monospace",
            fontSize: 11.5,
            lineHeight: 1.58,
            padding: '13px 15px',
          }}
        >
          {logLines.length === 0 ? (
            <span style={{ color: '#6C7086' }}>No log output yet...</span>
          ) : (
            logLines.map((line, i) => (
              <div
                key={i}
                style={{
                  color: line.includes('ERROR') ? '#F38BA8'
                       : line.includes('WARNING') ? '#F9E2AF'
                       : line.includes('INFO') ? '#A6E3A1'
                       : line.includes('DEBUG') ? '#89B4FA'
                       : '#CDD6F4',
                  whiteSpace: 'pre-wrap',
                  overflowWrap: 'anywhere',
                }}
              >
                {line}
              </div>
            ))
          )}
          <div ref={logEndRef} />
        </div>
      </section>

      <details className="shrink-0" style={{ marginTop: 10, border: '1px solid var(--border)', borderRadius: 9, background: 'var(--surface)' }}>
        <summary className="cursor-pointer select-none" style={{ padding: '9px 12px', color: 'var(--muted)', fontSize: 11, fontWeight: 600 }}>
          Runtime controls and connection details
        </summary>
        <div style={{ padding: '0 12px 12px', borderTop: '1px solid var(--border)' }}>
          <div className="flex items-center flex-wrap gap-2" style={{ padding: '10px 0' }}>
            <span style={{ marginRight: 6, color: 'var(--faint)', fontSize: 10 }}>ws://127.0.0.1:17777/ws</span>
            <button disabled={!connected || actionsDisabled} onClick={() => doAction('render.start', setRenderStatus)} className="inline-flex items-center gap-1.5 disabled:opacity-40" style={actionButtonStyle}>
              <FluentIcon name="Video" size={13} /> Start Render
            </button>
            <button disabled={!connected || actionsDisabled} onClick={() => doAction('render.stop', setRenderStatus)} className="inline-flex items-center gap-1.5 disabled:opacity-40" style={actionButtonStyle}>
              <FluentIcon name="Video" size={13} /> Stop Render
            </button>
            <button disabled={!connected || actionsDisabled} onClick={() => doAction('wallpaper.start', setWallpaperStatus, ELECTRON_SLICE_START_PARAMS)} className="inline-flex items-center gap-1.5 disabled:opacity-40" style={actionButtonStyle}>
              <FluentIcon name="Tiles" size={13} /> Start Wallpaper
            </button>
            <button disabled={!connected || actionsDisabled} onClick={() => doAction('wallpaper.stop', setWallpaperStatus)} className="inline-flex items-center gap-1.5 disabled:opacity-40" style={actionButtonStyle}>
              <FluentIcon name="Tiles" size={13} /> Stop Wallpaper
            </button>
          </div>
          {(renderStatus || wallpaperStatus) && (
            <div className="grid gap-1" style={{ padding: '9px 10px', borderRadius: 7, background: 'var(--surface-alt)' }}>
              {renderStatus ? <pre className="whitespace-pre-wrap break-words" style={{ margin: 0, color: 'var(--text)', fontSize: 9.5 }}>Render: {renderStatus}</pre> : null}
              {wallpaperStatus ? <pre className="whitespace-pre-wrap break-words" style={{ margin: 0, color: 'var(--text)', fontSize: 9.5 }}>Wallpaper: {wallpaperStatus}</pre> : null}
            </div>
          )}
        </div>
      </details>
    </div>
  )
}
