/** One owner for renderer projection intent and native-window lifecycle. */
export type Projection = 'render' | 'wallpaper'
type Result = Record<string, unknown>
type Mode = Projection | null

export function createProjectionController(actions: {
  send: (method: string, params?: Result) => Promise<Result>
  stopWallpaper: () => Promise<boolean>
  openWallpaper: (payload: Result) => Promise<unknown>
  closeWallpaper: () => Promise<unknown>
  onError: (error: unknown) => void
}) {
  let active: Mode = null
  let wanted: Mode = null
  let url = ''
  let wallpaper: Result = {}
  let revision = 0
  let operation: 'start-wallpaper' | 'stop-wallpaper' | null = null
  let tail: Promise<unknown> = Promise.resolve()
  const listeners = new Set<() => void>()
  let snapshot = { renderActive: false, wallpaperActive: false, renderAssetUrl: '' }
  const publish = () => {
    snapshot = {
      // Toggle presentation and toggle() must use the same intent. The URL
      // separately gates the actual embedded surface while a start is pending.
      renderActive: wanted === 'render',
      wallpaperActive: wanted === 'wallpaper',
      renderAssetUrl: active === 'render' && wanted === 'render' ? url : '',
    }
    listeners.forEach(listener => listener())
  }
  const enqueue = <T,>(job: () => Promise<T>): Promise<T> => {
    const result = tail.then(job)
    // A failed explicit command must not poison later user commands.
    tail = result.then(() => {}, () => {})
    return result
  }
  const request = (next: Mode, stopMode?: Projection): Promise<Result> => {
    const owner = ++revision
    wanted = next
    publish()
    return enqueue(async () => {
      const superseded = () => owner !== revision
      if (superseded()) return { status: 'superseded' }
      let result: Result = { status: 'already_running', ...(next === 'render' ? { url } : {}) }
      try {
        // Explicit Backend Stop also reaches a Host that survived a renderer
        // reload. Local absence is not evidence that the Host has stopped.
        const stops = new Set<Projection>()
        if (active && active !== next) stops.add(active)
        if (stopMode) stops.add(stopMode)
        for (const mode of stops) {
          if (mode === 'wallpaper') {
            operation = 'stop-wallpaper'
            if (!await actions.stopWallpaper()) throw new Error('Wallpaper could not be stopped.')
          } else {
            // Removing the iframe ends this projection even when the transport
            // is unavailable. It does not change the shared expression route.
            if (stopMode === 'render') await actions.send('render.stop', {})
            else { try { await actions.send('render.stop', {}) } catch {} }
          }
          if (active === mode) { active = null; url = '' }
          operation = null
          publish()
        }
        if (superseded()) return { status: 'superseded' }
        if (next === 'wallpaper') {
          if (active !== 'wallpaper') {
            operation = 'start-wallpaper'
            result = await actions.send('wallpaper.start', { slice_host: 'electron' })
            if (result.status === 'error') throw new Error(String(result.error || 'Wallpaper could not start.'))
            active = 'wallpaper'
            wallpaper = result
          }
          // A ready event records the Host fact but never independently mounts
          // a window during our start. Cleanup and later starts use this queue.
          if (!superseded()) await actions.openWallpaper(wallpaper)
        } else if (next === 'render' && active !== 'render') {
          result = await actions.send('render.start', {})
          const nextUrl = typeof result.url === 'string' ? result.url : ''
          if (!nextUrl) throw new Error('Render did not return a page URL.')
          active = 'render'
          url = nextUrl
        } else if (next === null) {
          result = { status: 'stopped' }
        }
        publish()
        return superseded() ? { status: 'superseded' } : stopMode ? { status: 'stopped' } : result
      } catch (error) {
        if (!superseded()) wanted = active
        publish()
        throw error
      } finally { operation = null }
    })
  }
  return {
    getSnapshot: () => snapshot,
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } },
    start: (mode: Projection) => request(mode),
    startAutomatically: () => revision === 0 ? request('wallpaper') : Promise.resolve({ status: 'superseded' }),
    stop: (mode: Projection) => request(wanted === mode ? null : wanted, mode),
    toggle: (mode: Projection) => request(wanted === mode ? null : mode),
    ready(payload: Result) {
      wallpaper = payload
      if (operation === 'start-wallpaper') {
        active = 'wallpaper'
        publish()
        return
      }
      // A start requested outside this owner (e.g. a direct Host client) still
      // works, while its native mount joins the same serialized lifecycle.
      void request('wallpaper').catch(actions.onError)
    },
    exited() {
      // stopWallpaper already owns native cleanup and its success/failure.
      if (operation === 'stop-wallpaper') return
      if (wanted === 'wallpaper') { revision += 1; wanted = null }
      publish()
      void enqueue(async () => {
        await actions.closeWallpaper()
        if (active === 'wallpaper') active = null
        publish()
      }).catch(actions.onError)
    },
  }
}

export type ProjectionController = ReturnType<typeof createProjectionController>
