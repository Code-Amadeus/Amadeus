import { useState, useCallback, useEffect } from 'react'
import { useBackend } from './hooks/useBackend'
import Sidebar from './components/Sidebar'
import ChatPage from './components/ChatPage'
import WorkPage from './components/WorkPage'
import ExpressionPage from './components/ExpressionPage'
import SettingsPage from './components/SettingsPage'
import BackendPage from './components/BackendPage'
import VNPage from './components/VNPage'
import WorkPreviewPage from './components/WorkPreviewPage'
import { ELECTRON_SLICE_START_PARAMS, syncElectronSliceHost } from './wallpaperSlice'

export type Page = 'chat' | 'vn' | 'backend' | 'expressions' | 'settings'

const WORK_FOCUS_RUN_KEY = 'amadeus.work.focusRunId'
const WORK_FOCUS_ACTION_KEY = 'amadeus.work.focusAction'
const WORK_FOCUS_PROVIDER_KEY = 'amadeus.work.focusProvider'
const WORK_FOCUS_CWD_KEY = 'amadeus.work.focusCwd'

function initialPage(): Page {
  const page = new URLSearchParams(window.location.search).get('page')
  if (page === 'vn' || page === 'backend' || page === 'expressions' || page === 'settings') {
    return page
  }
  return 'chat'
}

function AmadeusApp() {
  const { send, subscribe, connected } = useBackend()
  const searchParams = new URLSearchParams(window.location.search)
  const desktopProjection = searchParams.get('desktopProjection') === '1'
  const panelWindow = searchParams.get('panelWindow') === '1'
  const glowWindow = searchParams.get('glowWindow') === '1'
  const [page, setPage] = useState<Page>(() => initialPage())
  const [renderActive, setRenderActive] = useState(false)  // false=VTS, true=PixiJS
  const [wallpaperActive, setWallpaperActive] = useState(false)
  const [renderAssetUrl, setRenderAssetUrl] = useState('')

  useEffect(() => {
    if (desktopProjection) return
    const removeOpen = subscribe('work.preview.open.requested', payload => {
      void window.amadeus?.openWorkPreview(payload).catch(error => {
        console.error('[work-preview] open request failed', error)
      })
    })
    const removeUpdate = subscribe('work.preview.updated', payload => {
      void window.amadeus?.updateWorkPreview(payload).catch(error => {
        console.error('[work-preview] update failed', error)
      })
    })
    return () => {
      removeOpen()
      removeUpdate()
    }
  }, [desktopProjection, subscribe])

  const handleNavigate = useCallback((p: Page) => setPage(p), [])

  useEffect(() => {
    document.documentElement.classList.toggle('desktop-work-overlay', desktopProjection)
    document.body.classList.toggle('desktop-work-overlay', desktopProjection)
    document.documentElement.classList.toggle('desktop-work-glow-window', glowWindow)
    document.body.classList.toggle('desktop-work-glow-window', glowWindow)
    document.documentElement.classList.toggle('desktop-work-slice-window', panelWindow)
    document.body.classList.toggle('desktop-work-slice-window', panelWindow)
    return () => {
      document.documentElement.classList.remove('desktop-work-overlay')
      document.body.classList.remove('desktop-work-overlay')
      document.documentElement.classList.remove('desktop-work-glow-window')
      document.body.classList.remove('desktop-work-glow-window')
      document.documentElement.classList.remove('desktop-work-slice-window')
      document.body.classList.remove('desktop-work-slice-window')
    }
  }, [desktopProjection, glowWindow, panelWindow])

  // Toggle the supported VTS/PixiJS render projection.
  const handleToggleRender = useCallback(async () => {
    const next = !renderActive
    setRenderActive(next)
    setPage('chat')

    if (next) {
      if (wallpaperActive) {
        try { await send('wallpaper.stop', {}) } catch {}
        setWallpaperActive(false)
      }
      // Start PixiJS render mode
      const backend = 'graph'
      send('expression.set_backend', { backend }).catch(() => {})
      // Start AssetServer and get the render page URL
      try {
        const res = await send('render.start', {})
        if (res?.url) setRenderAssetUrl(String(res.url))
      } catch { /* AssetServer might already be running */ }
    } else {
      // Switch back to VTS
      send('expression.set_backend', { backend: 'vts' }).catch(() => {})
      send('render.stop', {}).catch(() => {})
      setRenderAssetUrl('')
    }
  }, [send, renderActive, wallpaperActive])

  // Toggle the Electron Slice wallpaper projection.
  const handleToggleWallpaper = useCallback(async () => {
    const next = !wallpaperActive
    setPage('chat')
    setWallpaperActive(next)

    if (next) {
      if (renderActive) {
        send('expression.set_backend', { backend: 'vts' }).catch(() => {})
        try { await send('render.stop', {}) } catch {}
        setRenderActive(false)
        setRenderAssetUrl('')
      }
     try {
      const res = await send('wallpaper.start', ELECTRON_SLICE_START_PARAMS)
      if (res?.status === 'error') setWallpaperActive(false)   // 失败回退
      else await syncElectronSliceHost(res)
    } catch {
      setWallpaperActive(false)     // 失败回退
    }
  } else {
      try { await send('wallpaper.stop', {}) } catch {}
      await window.amadeus?.closeElectronSlice()
      setWallpaperActive(false)
    }
  }, [wallpaperActive, renderActive, send])

  // listen for render mode back-to-vts from model bar
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail
      if (detail === 'expressions') setPage('expressions')
      if (detail === 'toggle-render') handleToggleRender()
    }
    window.addEventListener('navigate', handler)
    return () => window.removeEventListener('navigate', handler)
  }, [handleToggleRender])

  useEffect(() => {
    if (desktopProjection) return
    const unsubReady = subscribe('wallpaper.ready', (payload) => {
      setWallpaperActive(true)
      void syncElectronSliceHost(payload)
    })
    const unsubExited = subscribe('wallpaper.exited', () => {
      setWallpaperActive(false)
      void window.amadeus?.closeElectronSlice()
    })
    return () => { unsubReady(); unsubExited() }
  }, [desktopProjection, subscribe])

  useEffect(() => {
    if (desktopProjection) return
    const unsubProviderAction = subscribe('provider.event', (p) => {
      if (p.type !== 'canvas.action') return
      const payload = (p.payload && typeof p.payload === 'object' ? p.payload : {}) as Record<string, unknown>
      const runId = String(payload.run_id || p.run_id || '')
      const action = String(payload.action || '')
      const actionProvider = String(payload.provider || p.provider || '').toLowerCase()
      const cwd = String(payload.cwd || '')
      if (!runId) return
      if (action !== 'open_details') return
      localStorage.setItem(WORK_FOCUS_RUN_KEY, runId)
      localStorage.setItem(WORK_FOCUS_ACTION_KEY, action)
      localStorage.setItem(WORK_FOCUS_PROVIDER_KEY, actionProvider)
      if (cwd) localStorage.setItem(WORK_FOCUS_CWD_KEY, cwd)
      else localStorage.removeItem(WORK_FOCUS_CWD_KEY)
      void (window as any).amadeus?.openWorkOverlay?.()
    })
    return () => unsubProviderAction()
  }, [desktopProjection, subscribe])

  useEffect(() => {
    if (desktopProjection) return
    const unsubscribe = subscribe('session.changed', (payload) => {
      if (payload.source !== 'slice') return
      setPage('chat')
      window.amadeus?.focusMainWindow().catch(() => {})
    })
    return () => unsubscribe()
  }, [desktopProjection, subscribe])

  useEffect(() => {
    // Exactly one trusted surface owns automatic AUIP launch.  Slice/glow
    // windows also receive backend events, so letting WorkPage handle this
    // would open the same application once per Electron window.
    if (desktopProjection) return
    const unsubscribe = subscribe('auip.launch.requested', payload => {
      const requestId = String(payload.request_id || '')
      const artifactId = String(payload.artifact_id || '')
      const mode = String(payload.mode || 'observe')
      if (!requestId || !artifactId) return
      void (async () => {
        let status = 'failed'
        let detail = ''
        try {
          const prepared = await send('auip.attach.prepare', {
            request_id: requestId,
            artifact_id: artifactId,
            mode,
          })
          const launchUrl = String(prepared.launch_url || '')
          const hostSurfaceId = String(prepared.host_surface_id || '')
          const workItemId = String(prepared.work_item_id || '')
          if (!launchUrl) throw new Error('The host did not return an AUIP launch descriptor.')
          if (!hostSurfaceId) throw new Error('The host did not bind an AUIP surface identity.')
          const opened = await window.amadeus?.openAuipApp(launchUrl, hostSurfaceId, workItemId)
          if (!opened?.ok) {
            throw new Error(opened?.detail || 'The desktop host refused the AUIP launch URL.')
          }
          status = 'opened'
        } catch (error) {
          detail = error instanceof Error ? error.message : String(error)
        }
        await send('auip.launch.result', {
          request_id: requestId,
          status,
          detail,
        }).catch(() => {})
      })()
    })
    return () => unsubscribe()
  }, [desktopProjection, send, subscribe])

  useEffect(() => {
    if (desktopProjection) return
    const unsubscribe = subscribe('auip.surface.close.requested', payload => {
      const appSessionId = String(payload.app_session_id || '')
      const hostSurfaceId = String(payload.host_surface_id || '')
      if (!appSessionId || !hostSurfaceId) return
      void (async () => {
        let status = 'failed'
        let detail = ''
        try {
          const closed = await window.amadeus?.closeAuipApp(hostSurfaceId, appSessionId)
          status = String(closed?.status || (closed?.ok ? 'closed' : 'failed'))
          detail = String(closed?.detail || '')
        } catch (error) {
          detail = error instanceof Error ? error.message : String(error)
        }
        await send('auip.surface.close.result', {
          app_session_id: appSessionId,
          host_surface_id: hostSurfaceId,
          status,
          detail,
        }).catch(() => {})
      })()
    })
    return () => unsubscribe()
  }, [desktopProjection, send, subscribe])

  if (glowWindow) {
    return <div className="work-glow-window" />
  }

  if (desktopProjection) {
    return <WorkPage send={send} subscribe={subscribe} connected={connected} />
  }

  return (
    <div className="flex h-full">
      <Sidebar
        page={page} onNavigate={handleNavigate}
        renderActive={renderActive} wallpaperActive={wallpaperActive}
        onToggleRender={handleToggleRender} onToggleWallpaper={handleToggleWallpaper}
      />
      <div className="flex-1 flex flex-col min-w-0" style={{ backgroundColor: 'var(--bg)' }}>
        {page === 'chat' && <ChatPage send={send} subscribe={subscribe} connected={connected} renderActive={renderActive} renderAssetUrl={renderAssetUrl} />}
        {page === 'vn' && <VNPage send={send} subscribe={subscribe} connected={connected} />}
        {page === 'expressions' && <ExpressionPage send={send} subscribe={subscribe} />}
        {page === 'backend' && <BackendPage send={send} subscribe={subscribe} connected={connected} renderActive={renderActive} wallpaperActive={wallpaperActive} />}
        {page === 'settings' && <SettingsPage send={send} subscribe={subscribe} />}
      </div>
    </div>
  )
}

export default function App() {
  const previewWindow = new URLSearchParams(window.location.search).get('previewWindow') === '1'
  return previewWindow ? <WorkPreviewPage /> : <AmadeusApp />
}
