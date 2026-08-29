import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useBackend } from '../hooks/useBackend'
import '../styles/workPreview.css'

type PreviewLoadState = {
  status: 'idle' | 'loading' | 'loaded' | 'failed'
  detail: string
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? value as Record<string, unknown> : {}
}

function eventDescriptor(payload: Record<string, unknown>): Record<string, unknown> {
  const nested = recordValue(payload.descriptor || payload.preview)
  return { ...payload, ...nested }
}

function eventPreviewId(payload: Record<string, unknown>): string {
  const descriptor = eventDescriptor(payload)
  return String(descriptor.previewId || descriptor.preview_id || '')
}

function compactWorkItemId(workItemId: string): string {
  if (workItemId.length <= 34) return workItemId
  return `${workItemId.slice(0, 17)}…${workItemId.slice(-12)}`
}

function statusTone(status: string): string {
  const normalized = status.toLowerCase()
  if (['failed', 'error', 'blocked', 'ambiguous'].includes(normalized)) return 'failed'
  if (
    ['ready', 'complete', 'completed', 'terminal', 'final', 'holding', 'attached', 'frozen']
      .includes(normalized)
  ) return 'ready'
  return 'live'
}

export default function WorkPreviewPage() {
  const { send, subscribe, connected } = useBackend()
  const previewId = useMemo(
    () => new URLSearchParams(window.location.search).get('previewId') || '',
    [],
  )
  const viewportRef = useRef<HTMLDivElement | null>(null)
  const closeInFlightRef = useRef(false)
  const finishCloseStartedRef = useRef(false)
  const closeTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [descriptor, setDescriptor] = useState<WorkPreviewDescriptor | null>(null)
  const [loadState, setLoadState] = useState<PreviewLoadState>({ status: 'idle', detail: '' })
  const [shellError, setShellError] = useState('')

  const acceptResult = useCallback((result: WorkPreviewIpcResult | undefined) => {
    if (!result) {
      setShellError('Desktop preview bridge unavailable.')
      return
    }
    if (!result.ok) {
      setShellError(result.detail || 'Preview update rejected.')
      return
    }
    if (result.descriptor) setDescriptor(result.descriptor)
    setShellError('')
  }, [])

  const applyBackendDescriptor = useCallback(async (payload: Record<string, unknown>) => {
    if (!previewId || eventPreviewId(payload) !== previewId) return
    try {
      acceptResult(await window.amadeus?.updateWorkPreview(eventDescriptor(payload)))
    } catch (error) {
      setShellError(error instanceof Error ? error.message : String(error))
    }
  }, [acceptResult, previewId])

  const finishPreviewClose = useCallback(async (current: WorkPreviewDescriptor) => {
    await send('work.preview.close', { work_item_id: current.workItemId })
    const closed = await window.amadeus?.closeWorkPreview(previewId)
    if (!closed?.ok) throw new Error(closed?.detail || 'Desktop preview bridge unavailable.')
  }, [previewId, send])

  const close = useCallback(async () => {
    if (!previewId || closeInFlightRef.current) return
    try {
      let current = descriptor
      if (!current) {
        const result = await window.amadeus?.getWorkPreview(previewId)
        if (!result?.ok || !result.descriptor) {
          throw new Error(result?.detail || 'Preview identity is unavailable.')
        }
        current = result.descriptor
      }
      const phase = current.presentationPhase || 'preview'
      const ownsAuip = current.lifecycle === 'attached'
        || ['auip-preloading', 'auip-attached', 'auip-closing', 'auip-conflict'].includes(phase)
      closeInFlightRef.current = true
      if (!ownsAuip) {
        await finishPreviewClose(current)
        return
      }
      if (phase === 'auip-preloading' && !current.appSessionId) {
        // No AppSession owns the surface yet. Closing the Host Preview cancels
        // the detached renderer; the one-shot ticket then fails or expires.
        await finishPreviewClose(current)
        return
      }
      const appSessionId = current.appSessionId || current.presentedAppSessionId || ''
      const hostSurfaceId = current.hostSurfaceId || current.presentedHostSurfaceId || ''
      if (!appSessionId || !hostSurfaceId) {
        throw new Error('The attached AppSession does not have an exact Host surface identity.')
      }
      const left = await send('auip.leave', {
        app_session_id: appSessionId,
        reason: 'app_surface_window_closed',
      })
      if (
        String(left.app_session_id || '') !== appSessionId
        || String(left.host_surface_id || '') !== hostSurfaceId
      ) {
        throw new Error('Host acknowledged leave for a different AppSession or surface.')
      }
      setShellError('Closing the verified AppSession surface…')
      closeTimeoutRef.current = setTimeout(() => {
        closeTimeoutRef.current = null
        closeInFlightRef.current = false
        setShellError('The AppSession did not reach a verified frozen surface. You can retry closing.')
      }, 15_000)
    } catch (error) {
      closeInFlightRef.current = false
      setShellError(error instanceof Error ? error.message : String(error))
    }
  }, [descriptor, finishPreviewClose, previewId, send])

  useEffect(() => {
    if (!closeInFlightRef.current || finishCloseStartedRef.current || !descriptor) return
    const verifiedSameAttemptClose = descriptor.lifecycle === 'frozen'
      && descriptor.presentationPhase === 'auip-ended'
    const verifiedConflictClose = descriptor.presentationPhase === 'preview'
      && !descriptor.presentedAppSessionId
      && !descriptor.presentedHostSurfaceId
      && !['attached', 'handoff'].includes(descriptor.lifecycle)
    if (!verifiedSameAttemptClose && !verifiedConflictClose) return
    if (closeTimeoutRef.current) {
      clearTimeout(closeTimeoutRef.current)
      closeTimeoutRef.current = null
    }
    finishCloseStartedRef.current = true
    void finishPreviewClose(descriptor).catch(error => {
      finishCloseStartedRef.current = false
      closeInFlightRef.current = false
      setShellError(error instanceof Error ? error.message : String(error))
    })
  }, [descriptor, finishPreviewClose])

  useEffect(() => () => {
    if (closeTimeoutRef.current) clearTimeout(closeTimeoutRef.current)
  }, [])

  useEffect(() => {
    if (!previewId) {
      setShellError('Missing preview identity.')
      return
    }
    void window.amadeus?.getWorkPreview(previewId)
      .then(acceptResult)
      .catch(error => setShellError(error instanceof Error ? error.message : String(error)))
    const removeDescriptor = window.amadeus?.onWorkPreviewDescriptor(next => {
      if (next.previewId === previewId) setDescriptor(next)
    })
    const removeLoadState = window.amadeus?.onWorkPreviewLoadState(next => {
      if (String(next.previewId || '') !== previewId) return
      const status = String(next.status || 'idle') as PreviewLoadState['status']
      setLoadState({ status, detail: String(next.detail || '') })
    })
    const removeCloseRequested = window.amadeus?.onWorkPreviewCloseRequested(next => {
      if (String(next.previewId || '') === previewId) void close()
    })
    return () => {
      removeDescriptor?.()
      removeLoadState?.()
      removeCloseRequested?.()
    }
  }, [acceptResult, close, previewId])

  useEffect(() => {
    const removeUpdated = subscribe('work.preview.updated', payload => {
      void applyBackendDescriptor(payload)
    })
    const removeOpened = subscribe('work.preview.open.requested', payload => {
      void applyBackendDescriptor(payload)
    })
    return () => {
      removeUpdated()
      removeOpened()
    }
  }, [applyBackendDescriptor, subscribe])

  useEffect(() => {
    const viewport = viewportRef.current
    if (!viewport || !previewId) return
    const publishBounds = () => {
      const rect = viewport.getBoundingClientRect()
      void window.amadeus?.setWorkPreviewBounds(previewId, {
        x: rect.left,
        y: rect.top,
        width: rect.width,
        height: rect.height,
      })
    }
    const observer = new ResizeObserver(publishBounds)
    observer.observe(viewport)
    window.addEventListener('resize', publishBounds)
    publishBounds()
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', publishBounds)
    }
  }, [previewId])

  const reload = useCallback(() => {
    if (
      !previewId
      || ['assembling', 'handoff', 'attached', 'holding', 'frozen']
        .includes(descriptor?.lifecycle || '')
    ) return
    setShellError('')
    void window.amadeus?.reloadWorkPreview(previewId)
      .then(acceptResult)
      .catch(error => setShellError(error instanceof Error ? error.message : String(error)))
  }, [acceptResult, descriptor?.lifecycle, previewId])

  const status = descriptor?.status || 'preparing'
  const lifecycle = descriptor?.lifecycle || 'preview'
  const presentationPhase = descriptor?.presentationPhase || 'preview'
  const reloadLocked = ['assembling', 'handoff', 'attached', 'holding', 'frozen'].includes(lifecycle)
    || presentationPhase !== 'preview'
  const auipTransition = ['assembling', 'handoff'].includes(lifecycle)
    || presentationPhase === 'auip-preloading'
  const ended = lifecycle === 'frozen' && presentationPhase === 'auip-ended'
  const surfaceLabel = ['attached', 'frozen'].includes(lifecycle) || presentationPhase !== 'preview'
    ? 'APP SURFACE'
    : 'WORK PREVIEW'
  const tone = statusTone(lifecycle === 'preview' ? status : lifecycle)
  const loadLabel = presentationPhase === 'auip-conflict'
    ? 'IDENTITY CONFLICT'
    : presentationPhase === 'auip-closing'
      ? 'CLOSING'
      : lifecycle === 'attached'
    ? 'AUIP ACTIVE'
    : lifecycle === 'assembling'
      ? 'ASSEMBLING'
      : lifecycle === 'handoff'
        ? 'ATTACHING'
        : lifecycle === 'holding'
          ? 'FINAL FRAME'
        : ended
          ? 'FROZEN'
          : loadState.status === 'failed'
    ? 'LOAD FAILED'
    : loadState.status === 'loading'
      ? 'REFRESHING'
      : loadState.status === 'loaded'
        ? 'LIVE'
        : 'WAITING'
  const descriptorError = status.toLowerCase() === 'waiting'
    ? ''
    : String(descriptor?.error || '')

  return (
    <main className="work-preview-shell">
      <div className="work-preview-grid" aria-hidden="true" />
      <header className="work-preview-titlebar">
        <div className="work-preview-title-copy">
          <span className={`work-preview-status-dot ${tone}`} />
          <div>
            <small>{surfaceLabel} / {lifecycle.toUpperCase()} / {status.toUpperCase()}</small>
            <strong>{descriptor?.title || 'Preparing preview…'}</strong>
          </div>
        </div>
        <div className="work-preview-window-actions">
          <span className={`work-preview-load-label ${loadState.status}`}>{loadLabel}</span>
          {auipTransition && (
            <span className="work-preview-auip-badge"><i>A</i> AUIP</span>
          )}
          <button
            type="button"
            onClick={reload}
            disabled={reloadLocked}
            aria-label="Reload preview"
            title={reloadLocked ? `Reload unavailable while ${lifecycle}` : 'Reload preview'}
          >↻</button>
          <button type="button" className="close" onClick={close} aria-label="Close preview" title="Close preview">×</button>
        </div>
      </header>

      <section className="work-preview-frame" aria-label="Sandboxed web preview">
        <div className="work-preview-corner top-left" aria-hidden="true" />
        <div className="work-preview-corner top-right" aria-hidden="true" />
        <div className="work-preview-corner bottom-left" aria-hidden="true" />
        <div className="work-preview-corner bottom-right" aria-hidden="true" />
        <div ref={viewportRef} className="work-preview-viewport">
          <div className="work-preview-placeholder">
            <span>AWAITING LOCAL RENDER SURFACE</span>
          </div>
          {auipTransition && (
            <div className="work-preview-auip-stage" aria-live="polite">
              <div className="work-preview-auip-mark" aria-hidden="true">
                <span>A</span>
              </div>
              <strong>{lifecycle === 'handoff' ? 'AUIP ATTACHING' : 'AUIP ASSEMBLING'}</strong>
              <small>
                {lifecycle === 'handoff'
                  ? 'Validating the application in an isolated surface'
                  : 'Preparing the final interactive application'}
              </small>
            </div>
          )}
          {ended && (
            <div className="work-preview-auip-stage ended" aria-live="polite">
              <div className="work-preview-auip-mark" aria-hidden="true"><span>A</span></div>
              <strong>APP SESSION ENDED</strong>
              <small>The verified surface has been released.</small>
            </div>
          )}
        </div>
      </section>

      <footer className="work-preview-footer">
        <span className={connected ? 'connected' : 'disconnected'}>
          {connected ? 'HOST LINK' : 'HOST RECONNECTING'}
        </span>
        <span title={descriptor?.workItemId || ''}>
          {descriptor ? `WORK ${compactWorkItemId(descriptor.workItemId)}` : 'WORK UNBOUND'}
        </span>
        <span>REV {descriptor?.revision ?? '—'} / CONTENT {descriptor?.contentRevision ?? '—'}</span>
      </footer>
      {(shellError || loadState.detail || descriptorError) && (
        <div className="work-preview-notice" role="status" aria-live="polite">
          {shellError || loadState.detail || descriptorError}
        </div>
      )}
    </main>
  )
}
