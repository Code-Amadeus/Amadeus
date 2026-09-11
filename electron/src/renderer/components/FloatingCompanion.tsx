import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { useBackend } from '../hooks/useBackend'
import { postRenderEvent, RENDER_EVENT_METHODS } from '../renderBridge'
import type { ProviderEvent, ProviderRun } from './work/types'
import { normalizeRun } from './work/workState'
import { isVisibleProviderEvent } from './work/providerEventVisibility'
import { useCompanionSpeech } from './useCompanionSpeech'
import { TASK_INACTIVITY_MS } from './companionNotificationQueue'
import SpokenCaption from './SpokenCaption'
import CompanionTaskConstellation from './CompanionTaskConstellation'
import CompanionLayoutControls from './CompanionLayoutControls'
import { useCompanionLayout } from './useCompanionLayout'
import { useCompanionDrag } from './useCompanionDrag'
import type { SceneTransform } from './companionLayoutPreferences'
import { boundsOf, characterHeightFor, displayForPoint, fitCarriedScene, localWorkArea, nodeKey, nodesOnDisplay, placeSelection, readDesktopNodes, transformSceneNode } from './companionDesktopLayout'
import {
  applyCompanionProviderEvent,
  deriveFloatingCompanionPresence,
  upsertCompanionRun,
  withCompanionContexts,
  type CompanionTaskContext,
  type FloatingCompanionTask,
} from './floatingCompanionState'

export default function FloatingCompanion() {
  const { send, subscribe, connected } = useBackend()
  const rootRef = useRef<HTMLDivElement>(null)
  const frameRef = useRef<HTMLDivElement>(null)
  const renderFrameRef = useRef<HTMLIFrameElement>(null)
  const [renderAssetUrl, setRenderAssetUrl] = useState('')
  const [runs, setRuns] = useState<ProviderRun[]>([])
  const [skin, setSkin] = useState(() => localStorage.getItem('amadeus.companion.skin') === 'codex' ? 'codex' : 'amadeus')
  const [controlsOpen, setControlsOpen] = useState(false)
  const layout = useCompanionLayout()
  const sceneDrag = useCompanionDrag(layout.edit === 'scene')
  const [codexTasks, setCodexTasks] = useState<FloatingCompanionTask[]>([])
  const [contexts, setContexts] = useState<CompanionTaskContext[]>([])
  const [sourceNote, setSourceNote] = useState('')
  const presence = useMemo(() => deriveFloatingCompanionPresence(runs), [runs])
  const allTasks = useMemo(() => [...codexTasks, ...presence.tasks], [codexTasks, presence.tasks])
  const { visible: tasks, holdCard, dismissCard, acknowledge, acknowledged, audioNote, fading, voiceEnabled, toggleVoice, faults, speakingKey } = useCompanionSpeech(allTasks, send, subscribe, connected)
  const graphTasks = useMemo(() => withCompanionContexts(tasks, [...allTasks, ...contexts]), [tasks, allTasks, contexts])
  const openTask = (task: FloatingCompanionTask) => task.codexThreadId
    ? window.amadeus?.openCodexThread?.(task.codexThreadId)
    : window.amadeus?.focusMainWindow()

  useEffect(() => {
    const update = (payload: Record<string, unknown>) => {
      if (Array.isArray(payload.tasks)) setCodexTasks(payload.tasks as FloatingCompanionTask[])
      if (Array.isArray(payload.contexts)) setContexts(payload.contexts as CompanionTaskContext[])
      if (typeof payload.note === 'string') setSourceNote(payload.note)
    }
    const unsubscribe = subscribe('companion.tasks', update)
    if (connected) void send('companion.tasks', {}).then(update).catch(() => {})
    return unsubscribe
  }, [connected, send, subscribe])

  const syncHitRegions = useCallback(() => {
    const root = rootRef.current
    if (!root) return
    const rootBounds = root.getBoundingClientRect()
    const regions = [...root.querySelectorAll<HTMLElement>('[data-companion-hit]')]
      .filter(element => element.offsetParent !== null && getComputedStyle(element).visibility === 'visible')
      .map(element => {
        const bounds = element.getBoundingClientRect()
        // Reading cards scroll independently of the graph. Their offscreen
        // rectangles must not intercept clicks intended for the desktop.
        const clips = [rootBounds, ...['.companion-display-reading', '.companion-focus-panel']
          .map(selector => element.closest(selector)?.getBoundingClientRect()).filter((box): box is DOMRect => Boolean(box))]
        const left = Math.max(bounds.left, ...clips.map(box => box.left))
        const top = Math.max(bounds.top, ...clips.map(box => box.top))
        const right = Math.min(bounds.right, ...clips.map(box => box.right))
        const bottom = Math.min(bounds.bottom, ...clips.map(box => box.bottom))
        return {
          x: left - rootBounds.left,
          y: top - rootBounds.top,
          width: right - left,
          height: bottom - top,
        }
      })
      .filter(region => region.width > 0 && region.height > 0)
    void window.amadeus?.setFloatingCompanionHitRegions?.(regions)
  }, [])

  useEffect(() => {
    document.documentElement.classList.add('floating-companion-window')
    document.body.classList.add('floating-companion-window')
    return () => {
      document.documentElement.classList.remove('floating-companion-window')
      document.body.classList.remove('floating-companion-window')
    }
  }, [])

  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    const observer = new ResizeObserver(syncHitRegions)
    observer.observe(root)
    const mutationObserver = new MutationObserver(syncHitRegions)
    mutationObserver.observe(root, { childList: true, subtree: true, attributes: true })
    const timer = window.setTimeout(syncHitRegions, 0)
    return () => {
      window.clearTimeout(timer)
      observer.disconnect()
      mutationObserver.disconnect()
    }
  }, [syncHitRegions])

  useEffect(() => {
    syncHitRegions()
  }, [presence.phase, syncHitRegions])

  useEffect(() => {
    if (!connected) return
    void send('render.start', {}).then(result => {
      const url = String(result.url || '')
      if (url) setRenderAssetUrl(url)
    }).catch(error => console.error('[floating-companion] render start failed', error))
    void send('provider.list', {}).then(result => {
      if (!Array.isArray(result.runs)) return
      // Recent work remains visible after reopening; these Amadeus-owned runs
      // have announce:false and do not replay WorkObserver's narration.
      setRuns(result.runs.map(normalizeRun).filter((run): run is ProviderRun => (
        run !== null && (['running', 'queued'].includes(run.status)
          || (run.events || []).some(event => (event.time_ms ?? 0) > Date.now() - TASK_INACTIVITY_MS))
      )))
    }).catch(() => {})
  }, [connected, send])

  useEffect(() => {
    // This surface reads the original text in floating cards. The renderer's
    // bottom subtitle is for the chat surface and would be cropped here.
    const unsubs = RENDER_EVENT_METHODS.filter(method => method !== 'render.subtitle').map(method => (
      subscribe(method, params => postRenderEvent(renderFrameRef.current, method, params))
    ))
    unsubs.push(subscribe('provider.event', payload => {
      const event = payload as unknown as ProviderEvent
      if (!isVisibleProviderEvent(event)) return
      setRuns(current => applyCompanionProviderEvent(current, event))
    }))
    unsubs.push(subscribe('provider.result', payload => {
      const run = normalizeRun(payload)
      if (run) setRuns(current => upsertCompanionRun(current, run))
    }))
    return () => unsubs.forEach(unsubscribe => unsubscribe())
  }, [subscribe])

  const handleRenderReady = useCallback(() => {
    if (!renderAssetUrl) return
    void send('render.ready', {}).catch(error => {
      console.error('[floating-companion] render replay failed', error)
    })
  }, [renderAssetUrl, send])

  const desktop = layout.desktop
  const home = desktop?.home || { x: 0, y: 0, width: 1080, height: 1872 }
  const characterHeight = characterHeightFor(home)
  const characterCentre = { x: home.width / 2 + layout.profile.scene.x,
    y: home.height + layout.profile.scene.y - characterHeight * layout.profile.scene.scale / 2 }
  const currentDisplay = desktop && displayForPoint(characterCentre, desktop)
  const controlsArea = desktop && currentDisplay ? { ...currentDisplay.workArea,
    x: currentDisplay.workArea.x - desktop.bounds.x, y: currentDisplay.workArea.y - desktop.bounds.y } : home
  const snapshotScene = () => {
    const nodes = readDesktopNodes(rootRef.current!, frameRef.current!)
    const origin = frameRef.current!.getBoundingClientRect(), box = renderFrameRef.current!.getBoundingClientRect()
    const character = { id: 'character', kind: 'tasks' as const, x: box.x - origin.x, y: box.y - origin.y, width: box.width, height: box.height, scale: 1 }
    return { nodes, selected: nodesOnDisplay(nodes, currentDisplay!.id, desktop!), character, scene: layout.profile.scene, placements: layout.profile.placements }
  }
  const applyScene = (scene: SceneTransform, snapshot = snapshotScene()) => {
    const placements = placeSelection(snapshot.placements, snapshot.nodes, snapshot.selected,
      node => transformSceneNode(node, snapshot.scene, scene, home.width, home.height))
    layout.updateProfile(before => ({ ...before, scene, placements }))
  }
  const fitComposition = () => {
    const snapshot = snapshotScene()
    const box = boundsOf([...snapshot.nodes.filter(node => snapshot.selected.has(nodeKey(node))), snapshot.character])
    applyScene(fitCarriedScene(snapshot.scene, box, localWorkArea(currentDisplay!, desktop!), home.width, home.height), snapshot)
  }
  const sceneStyle = { transform: `translate(${layout.profile.scene.x}px, ${layout.profile.scene.y}px) scale(${layout.profile.scene.scale})` }

  return (
    <div ref={rootRef} className={`floating-companion is-desktop-spanning ${desktop && desktop.displays.length > 1 ? 'is-multi-display' : ''} skin-${skin} ${layout.edit !== 'locked' ? 'is-layout-editing' : ''}`}
      style={{ visibility: desktop ? 'visible' : 'hidden', '--character-height': `${characterHeight}px` } as CSSProperties}>
      <div ref={frameRef} className="companion-layout-frame" style={{ left: home.x - (desktop?.bounds.x || 0), top: home.y - (desktop?.bounds.y || 0), width: home.width, height: home.height }}>
      {/* The current cutout carries a faint rectangular matte below 1/8 alpha.
          Remove that residue at presentation time, keeping a continuous alpha
          ramp for the character edges and leaving the source textures intact. */}
      <svg width="0" height="0" aria-hidden="true" className="companion-render-filters">
        <defs>
          <filter id="companion-cutout-alpha" colorInterpolationFilters="sRGB">
            <feComponentTransfer>
              <feFuncA type="linear" slope={8 / 7} intercept={-1 / 7} />
            </feComponentTransfer>
          </filter>
        </defs>
      </svg>
      <div className="companion-character-scene" style={sceneStyle}>
      <iframe
        ref={renderFrameRef}
        className="floating-companion-render"
        src={renderAssetUrl || 'about:blank'}
        title="Amadeus character"
        onLoad={handleRenderReady}
      />
      <button
        type="button"
        className={`floating-companion-character-hit ${layout.edit === 'scene' ? 'is-draggable' : ''}`}
        data-companion-hit
        aria-label={layout.edit === 'scene' ? '拖动人物与整组卡片' : '显示或隐藏人物控制'}
        onPointerDown={layout.edit === 'scene' ? event => {
          const snapshot = snapshotScene()
          sceneDrag.begin(event, delta => {
            applyScene({ ...snapshot.scene, x: snapshot.scene.x + delta.x, y: snapshot.scene.y + delta.y }, snapshot)
          }, () => layout.updateProfile(before => ({ ...before, scene: snapshot.scene, placements: snapshot.placements })), (delta, point) => {
            const scene = { ...snapshot.scene, x: snapshot.scene.x + delta.x, y: snapshot.scene.y + delta.y }
            const target = displayForPoint({ x: point.x - home.x + desktop!.bounds.x, y: point.y - home.y + desktop!.bounds.y }, desktop!)
            const carried = [...snapshot.nodes.filter(node => snapshot.selected.has(nodeKey(node))), snapshot.character]
              .map(node => ({ ...node, ...transformSceneNode(node, snapshot.scene, scene, home.width, home.height) }))
            applyScene(fitCarriedScene(scene, boundsOf(carried), localWorkArea(target, desktop!), home.width, home.height), snapshot)
          })
        } : undefined}
        onClick={() => {
          if (sceneDrag.consumeClick()) return
          if (layout.edit !== 'locked') return
          setControlsOpen(value => !value)
          layout.setPanelOpen(false)
        }}
      />
      </div>

      <div className={`companion-caption-scene ${controlsOpen ? 'has-controls' : ''}`} style={sceneStyle}>
        <SpokenCaption subscribe={subscribe} />
      </div>
      <CompanionTaskConstellation layout={layout} tasks={graphTasks} fading={fading} faults={faults} acknowledged={acknowledged} speakingKey={speakingKey}
        openTask={openTask} dismissCard={dismissCard} acknowledge={acknowledge} holdCard={holdCard} syncHitRegions={syncHitRegions} />
      </div>
      {sceneDrag.dragging && <div className="companion-drag-capture" data-companion-hit />}

      <div className="companion-controls-surface" style={{ left: controlsArea.x, top: controlsArea.y, width: controlsArea.width, height: controlsArea.height }}>

      {controlsOpen && <div className="floating-companion-toolbar" data-companion-hit>
        <span className="floating-companion-grip">AMADEUS</span>
        <span className={`floating-companion-connection ${connected ? 'connected' : ''}`}>
          {connected ? 'ONLINE' : 'OFFLINE'}
        </span>
        <button type="button" title="Open full interface" onClick={() => void window.amadeus?.focusMainWindow()}>↗</button>
        <button type="button" aria-pressed={voiceEnabled} onClick={toggleVoice}>{voiceEnabled ? '提醒语音：开' : '提醒语音：关'}</button>
        <button type="button" onClick={() => setSkin(current => {
          const next = current === 'amadeus' ? 'codex' : 'amadeus'
          localStorage.setItem('amadeus.companion.skin', next)
          return next
        })}>皮肤：{skin === 'amadeus' ? 'Amadeus' : 'Codex'}</button>
        <button type="button" aria-expanded={layout.panelOpen} onClick={() => {
          layout.setPanelOpen(value => !value)
          if (layout.panelOpen) layout.setEdit('locked')
        }}>布局</button>
        <button type="button" title="Close companion" onClick={() => void window.amadeus?.closeFloatingCompanion()}>×</button>
      </div>}

      {(controlsOpen || layout.edit !== 'locked') && layout.panelOpen && <CompanionLayoutControls layout={layout} fit={fitComposition} changeScene={scene => applyScene(scene)} />}
      {controlsOpen && <p className="companion-source-note">{sourceNote || '当前显示 Amadeus 工作'}</p>}
      {audioNote && <p className="companion-audio-note">{audioNote}</p>}
      </div>
    </div>
  )
}
