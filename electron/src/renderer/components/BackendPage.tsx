import { useState, useEffect, useCallback, useRef } from 'react'
import FluentIcon from './FluentIcon'
import { SettingsButton } from './SettingsPrimitives'
import type { ProjectionController, Projection } from '../projectionController'
import { useI18n } from '../i18n'

interface Props {
  projections: ProjectionController
  send: (method: string, params?: Record<string, unknown>) => Promise<Record<string, unknown>>
  subscribe: (method: string, fn: (p: Record<string, unknown>) => void) => () => void
  connected: boolean
  renderActive: boolean
  wallpaperActive: boolean
}

export default function BackendPage({ projections, send, subscribe, connected, renderActive, wallpaperActive }: Props) {
  const { t } = useI18n()
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
    mode: Projection,
    start: boolean,
    setter: (v: string) => void,
  ) => {
    setActionsDisabled(true)
    setter('...')
    try {
      const res = await (start ? projections.start(mode) : projections.stop(mode))
      setter(JSON.stringify(res))
    } catch (e) {
      setter(`ERROR: ${e}`)
    }
    setActionsDisabled(false)
    fetchLog()
  }, [projections, fetchLog])

  const dot = (ok: unknown) => <span className="status-dot" data-state={ok ? 'ok' : ok === false ? 'error' : undefined} />

  const statusChip = (label: string, ok: unknown, detail?: string) => (
    <span className="status-chip">
      {dot(ok)}
      <span className="status-chip-label">{t(label)}</span>
      {detail ? <span className="status-chip-detail">{detail}</span> : null}
    </span>
  )

  return (
    <div className="backend-page">
      <header className="backend-header">
        <div className="min-w-0 flex-1">
          <h2 className="settings-page-title">{t('Backend')}</h2>
          <p className="settings-page-context">
            {t('Live runtime status and server diagnostics.')}
          </p>
        </div>
        <span className="backend-connection" data-state={connected ? 'ok' : 'error'}>
          {dot(connected)} {t(connected ? 'Connected' : 'Offline')}
        </span>
      </header>

      <div className="backend-chips">
        {statusChip('VTS', status.vts_connected)}
        {statusChip('TTS', status.tts_ready)}
        {statusChip('ASR', status.asr_ready)}
        {statusChip('Render', renderActive, renderActive ? 'PixiJS' : 'VTS')}
        {statusChip('Wallpaper', wallpaperActive)}
      </div>

      <section className="backend-log">
        <div className="backend-log-header">
          <h3 className="backend-log-title">{t('Server log')}</h3>
          <span className="backend-log-meta">{logTotal.toLocaleString()} {t('lines')}</span>
          <span className="flex-1" />
          <span className="backend-log-meta">{t('Updates every 3s')}</span>
          <SettingsButton size="small" onClick={fetchLog}>
            <FluentIcon name="Sync" size={14} /> {t('Refresh')}
          </SettingsButton>
        </div>
        <div className="backend-log-view">
          {logLines.length === 0 ? (
            <span className="backend-log-empty">{t('No log output yet...')}</span>
          ) : (
            logLines.map((line, i) => (
              <div key={i} className="backend-log-line" data-level={logLevel(line)}>
                {line}
              </div>
            ))
          )}
          <div ref={logEndRef} />
        </div>
      </section>

      <details className="backend-controls">
        <summary>
          {t('Runtime controls and connection details')}
        </summary>
        <div className="backend-controls-body">
          <div className="backend-controls-row">
            <span className="backend-endpoint">ws://127.0.0.1:17777/ws</span>
            <SettingsButton size="small" disabled={!connected || actionsDisabled} onClick={() => doAction('render', true, setRenderStatus)}>
              <FluentIcon name="Video" size={14} /> {t('Start Render')}
            </SettingsButton>
            <SettingsButton size="small" disabled={!connected || actionsDisabled} onClick={() => doAction('render', false, setRenderStatus)}>
              <FluentIcon name="Video" size={14} /> {t('Stop Render')}
            </SettingsButton>
            <SettingsButton size="small" disabled={!connected || actionsDisabled} onClick={() => doAction('wallpaper', true, setWallpaperStatus)}>
              <FluentIcon name="Tiles" size={14} /> {t('Start Wallpaper')}
            </SettingsButton>
            <SettingsButton size="small" disabled={!connected || actionsDisabled} onClick={() => doAction('wallpaper', false, setWallpaperStatus)}>
              <FluentIcon name="Tiles" size={14} /> {t('Stop Wallpaper')}
            </SettingsButton>
          </div>
          {(renderStatus || wallpaperStatus) && (
            <div className="backend-results">
              {renderStatus ? <pre className="backend-result">{t('Render')}: {renderStatus}</pre> : null}
              {wallpaperStatus ? <pre className="backend-result">{t('Wallpaper')}: {wallpaperStatus}</pre> : null}
            </div>
          )}
        </div>
      </details>
    </div>
  )
}

/** Log lines are colored by the first level marker they contain. */
function logLevel(line: string): string | undefined {
  if (line.includes('ERROR')) return 'error'
  if (line.includes('WARNING')) return 'warning'
  if (line.includes('INFO')) return 'info'
  if (line.includes('DEBUG')) return 'debug'
  return undefined
}
