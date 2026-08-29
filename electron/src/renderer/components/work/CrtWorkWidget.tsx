import { useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import FluentIcon from '../FluentIcon'
import type { FluentIconName } from '../FluentIcon'
import { getMockWorkboardFrame } from './mockWorkboardPlayback'
import { mockWorkTurnFixture } from './mockWorkTurn.fixture'

type WidgetPhase = 'idle' | 'cue' | 'active' | 'review' | 'blocked' | 'done'
type CanvasMode = 'workflow' | 'markdown' | 'html' | 'image' | 'table' | 'code'
type CanvasOffset = { x: number; y: number }
type CanvasSize = { width: number; height: number }
type CanvasPreset = 'compact' | 'wide' | 'custom'

const CANVAS_OFFSET_KEY = 'amadeus.crtCanvas.offset'
const CANVAS_SIZE_KEY = 'amadeus.crtCanvas.size.v3'
const CANVAS_PRESET_KEY = 'amadeus.crtCanvas.preset.v3'

function presetSize(preset: CanvasPreset): CanvasSize {
  return preset === 'wide' ? { width: 560, height: 455 } : { width: 340, height: 430 }
}

function initialCanvasOffset(): CanvasOffset {
  try {
    const saved = JSON.parse(localStorage.getItem(CANVAS_OFFSET_KEY) || 'null') as Partial<CanvasOffset> | null
    if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
      return { x: Number(saved.x), y: Number(saved.y) }
    }
  } catch {
    // Ignore malformed persisted layout state.
  }
  return { x: 0, y: 56 }
}

function initialCanvasPreset(): CanvasPreset {
  try {
    const saved = localStorage.getItem(CANVAS_PRESET_KEY)
    if (saved === 'compact' || saved === 'wide' || saved === 'custom') return saved
  } catch {
    // Ignore malformed persisted layout state.
  }
  return 'compact'
}

function initialCanvasSize(): CanvasSize {
  try {
    const saved = JSON.parse(localStorage.getItem(CANVAS_SIZE_KEY) || 'null') as Partial<CanvasSize> | null
    if (saved && Number.isFinite(saved.width) && Number.isFinite(saved.height)) {
      return { width: Number(saved.width), height: Number(saved.height) }
    }
  } catch {
    // Ignore malformed persisted layout state.
  }
  return presetSize(initialCanvasPreset())
}

const MARKDOWN_SAMPLE = `### AUIP Runtime Note

The current turn should expose **state**, not raw logs.

\`WorkSignal = raw provider events -> compact user-facing evidence\`

Formula draft:

\`trust = validation * reversibility / risk\`

Next canvas targets:
- render provider summaries as markdown
- preview generated HTML artifacts
- show image/table/chart outputs without opening an IDE pane`

const HTML_SAMPLE = `<!doctype html>
<html>
  <head>
    <style>
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        font-family: Inter, system-ui, sans-serif;
        color: #d8fff2;
        background:
          radial-gradient(circle at 18% 18%, rgba(130, 255, 224, 0.20), transparent 34%),
          linear-gradient(135deg, #05191d, #10172a 62%, #1e1230);
      }
      main { padding: 20px; }
      h1 { margin: 0 0 8px; font-size: 24px; letter-spacing: 0; }
      p { margin: 0 0 18px; color: rgba(216, 255, 242, 0.72); line-height: 1.45; }
      .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
      .cell {
        border: 1px solid rgba(147, 255, 229, 0.28);
        border-radius: 14px;
        padding: 12px;
        background: rgba(4, 28, 34, 0.52);
      }
      strong { display: block; font-size: 20px; }
      span { color: rgba(216, 255, 242, 0.62); font-size: 11px; text-transform: uppercase; }
    </style>
  </head>
  <body>
    <main>
      <h1>Provider Runtime Snapshot</h1>
      <p>A generated HTML artifact can live inside the CRT canvas without taking over the main chatbox.</p>
      <section class="grid">
        <div class="cell"><strong>04</strong><span>signals</span></div>
        <div class="cell"><strong>2</strong><span>files</span></div>
        <div class="cell"><strong>1</strong><span>permission</span></div>
      </section>
    </main>
  </body>
</html>`

const TABLE_ROWS = [
  { label: 'Context packet', value: 'sent', detail: '3 refs' },
  { label: 'Workspace write lease', value: 'pending', detail: 'level 2' },
  { label: 'HTML artifact preview', value: 'ready', detail: 'wide card' },
  { label: 'Audit trace', value: 'folded', detail: 'raw events' },
]

const CODE_LINES = [
  { kind: 'context', line: 128, prefix: ' ', text: 'const run = await provider.open(task)' },
  { kind: 'remove', line: 129, prefix: '-', text: 'renderRawToolEvents(events)' },
  { kind: 'add', line: 129, prefix: '+', text: 'renderWorkSignals(compact(events))' },
  { kind: 'add', line: 130, prefix: '+', text: 'await permissionBroker.request(scope)' },
  { kind: 'context', line: 131, prefix: ' ', text: 'return createArtifactPreview(run)' },
]

function phaseLabel(phase: WidgetPhase) {
  if (phase === 'cue') return 'Cue'
  if (phase === 'active') return 'Working'
  if (phase === 'review') return 'Review'
  if (phase === 'blocked') return 'Permission'
  if (phase === 'done') return 'Done'
  return 'Ready'
}

function phaseFromMode(mode: string): WidgetPhase {
  if (mode === 'cue') return 'cue'
  if (mode === 'active') return 'active'
  if (mode === 'review') return 'review'
  return 'idle'
}

function modeLabel(mode: CanvasMode) {
  if (mode === 'workflow') return 'Work'
  if (mode === 'markdown') return 'Doc'
  if (mode === 'html') return 'HTML'
  if (mode === 'table') return 'Table'
  if (mode === 'code') return 'Code'
  return 'Image'
}

function modeIcon(mode: CanvasMode): FluentIconName {
  if (mode === 'workflow') return 'Work'
  if (mode === 'markdown') return 'Edit'
  if (mode === 'html') return 'Tiles'
  if (mode === 'table') return 'Tiles'
  if (mode === 'code') return 'CommandPrompt'
  return 'Photo'
}

export default function CrtWorkWidget() {
  const [expanded, setExpanded] = useState(() => localStorage.getItem('amadeus.crtWorkWidget.expanded') === '1')
  const [mode, setMode] = useState<CanvasMode>('workflow')
  const [permissionVisible, setPermissionVisible] = useState(true)
  const [elapsedMs, setElapsedMs] = useState(0)
  const [canvasOffset, setCanvasOffset] = useState<CanvasOffset>(initialCanvasOffset)
  const [canvasPreset, setCanvasPreset] = useState<CanvasPreset>(initialCanvasPreset)
  const [canvasSize, setCanvasSize] = useState<CanvasSize>(initialCanvasSize)
  const [dragging, setDragging] = useState(false)
  const layerRef = useRef<HTMLDivElement>(null)
  const cardRef = useRef<HTMLElement>(null)
  const dragRef = useRef<{
    pointerId: number
    startX: number
    startY: number
    startOffset: CanvasOffset
  } | null>(null)

  useEffect(() => {
    localStorage.setItem('amadeus.crtWorkWidget.expanded', expanded ? '1' : '0')
  }, [expanded])

  useEffect(() => {
    localStorage.setItem(CANVAS_OFFSET_KEY, JSON.stringify(canvasOffset))
  }, [canvasOffset])

  useEffect(() => {
    localStorage.setItem(CANVAS_SIZE_KEY, JSON.stringify(canvasSize))
  }, [canvasSize])

  useEffect(() => {
    localStorage.setItem(CANVAS_PRESET_KEY, canvasPreset)
  }, [canvasPreset])

  useEffect(() => {
    const startedAt = Date.now()
    const timer = window.setInterval(() => setElapsedMs(Date.now() - startedAt), 320)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!expanded || !cardRef.current || typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(entries => {
      const rect = entries[0]?.contentRect
      if (!rect) return
      setCanvasSize(current => {
        if (Math.abs(rect.width - current.width) < 2 && Math.abs(rect.height - current.height) < 2) return current
        return clampSize({ width: rect.width, height: rect.height })
      })
      setCanvasPreset(current => current === 'custom' ? current : 'custom')
    })
    observer.observe(cardRef.current)
    return () => observer.disconnect()
  }, [expanded])

  const frame = useMemo(() => getMockWorkboardFrame(elapsedMs), [elapsedMs])
  const phase = phaseFromMode(frame.mode)
  const elapsedMinutes = Math.max(0, Math.floor(elapsedMs / 60000))
  const statusText = `${phaseLabel(phase).toUpperCase()} - ${elapsedMinutes}m`
  const semanticTitle = mode === 'workflow'
    ? frame.title
    : mode === 'markdown'
      ? 'AUIP Runtime Note'
      : mode === 'html'
        ? 'Provider Runtime Snapshot'
        : mode === 'table'
          ? 'Provider Run Metrics'
          : mode === 'code'
            ? 'Diff and Terminal Evidence'
            : 'AUIP Card Map'
  const semanticKicker = `${phaseLabel(phase).toUpperCase()} / ${modeLabel(mode).toUpperCase()}`
  const progress = frame.mode === 'cue' ? 18 : frame.mode === 'active' ? 62 : 92
  const canvasModes: CanvasMode[] = ['workflow', 'markdown', 'html', 'image', 'table', 'code']
  const cycleMode = () => {
    setMode(current => canvasModes[(canvasModes.indexOf(current) + 1) % canvasModes.length])
  }
  const toggleCanvasPreset = () => {
    const nextPreset: CanvasPreset = canvasPreset === 'wide' ? 'compact' : 'wide'
    setCanvasPreset(nextPreset)
    setCanvasSize(clampSize(presetSize(nextPreset)))
  }
  const canvasStyle = {
    '--crt-canvas-x': `${canvasOffset.x}px`,
    '--crt-canvas-y': `${canvasOffset.y}px`,
    '--crt-canvas-w': `${canvasSize.width}px`,
    '--crt-canvas-h': `${canvasSize.height}px`,
  } as CSSProperties

  const clampOffset = (next: CanvasOffset): CanvasOffset => {
    const layerRect = layerRef.current?.getBoundingClientRect()
    if (!layerRect) return next
    const cardRect = cardRef.current?.getBoundingClientRect()
    const cardWidth = cardRect?.width || 430
    const cardHeight = cardRect?.height || 380
    const minX = -18
    const maxX = Math.max(-18, layerRect.width - cardWidth - 28)
    const minY = -34
    const maxY = Math.max(24, layerRect.height - cardHeight - 34)
    return {
      x: Math.min(Math.max(next.x, minX), maxX),
      y: Math.min(Math.max(next.y, minY), maxY),
    }
  }

  const clampSize = (next: CanvasSize): CanvasSize => {
    const layerRect = layerRef.current?.getBoundingClientRect()
    const layerWidth = layerRect?.width || 760
    const layerHeight = layerRect?.height || 620
    const leftOffset = Math.min(Math.max(18, layerWidth * 0.03), 42)
    const topOffset = Math.min(Math.max(56, layerHeight * 0.09), 86)
    const maxWidth = Math.min(620, Math.max(320, layerWidth - leftOffset - 76))
    const maxHeight = Math.min(520, Math.max(300, layerHeight - topOffset - 40))
    return {
      width: Math.min(Math.max(next.width, 300), maxWidth),
      height: Math.min(Math.max(next.height, 300), maxHeight),
    }
  }

  const startDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if ((event.target as HTMLElement).closest('button')) return
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startOffset: canvasOffset,
    }
    setDragging(true)
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  const dragCanvas = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    setCanvasOffset(clampOffset({
      x: drag.startOffset.x + event.clientX - drag.startX,
      y: drag.startOffset.y + event.clientY - drag.startY,
    }))
  }

  const endDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if (dragRef.current?.pointerId === event.pointerId) {
      dragRef.current = null
      setDragging(false)
    }
  }

  const latestSignals = frame.summaryLines.length > 0
    ? frame.summaryLines.slice(-3)
    : mockWorkTurnFixture.turn.signals.slice(0, 3).map(signal => ({
      id: signal.id,
      label: signal.phase,
      text: signal.summary,
      files: signal.evidence?.map(item => item.label) || [],
      added: 0,
      removed: 0,
    }))
  const showPermission = permissionVisible && frame.mode === 'active' && elapsedMs % mockWorkTurnFixture.loopMs > 26000

  return (
    <div ref={layerRef} className="crt-widget-layer" style={canvasStyle} aria-label="CRT canvas widget">
      <button
        className={`crt-widget-dot ${phase} ${expanded ? 'expanded' : ''}`}
        onClick={() => setExpanded(value => !value)}
        aria-label={expanded ? 'Fold canvas' : 'Expand canvas'}
      >
        <span />
      </button>
      <button
        type="button"
        className={`crt-widget-status-chip ${expanded ? 'expanded' : ''}`}
        onClick={() => setExpanded(value => !value)}
        aria-label={statusText}
      >
        {statusText}
      </button>

      {expanded && (
        <section ref={cardRef} className={`crt-widget-card crt-canvas-card ${phase} ${dragging ? 'dragging' : ''}`}>
          <div
            className="crt-canvas-drag-zone"
            onPointerDown={startDrag}
            onPointerMove={dragCanvas}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
          />

          <div className="crt-canvas-semantic-header">
            <span>{semanticKicker}</span>
            <strong>{semanticTitle}</strong>
          </div>

          <div className="crt-canvas-hover-actions">
            <button type="button" onClick={cycleMode} aria-label={`Switch canvas mode: ${modeLabel(mode)}`}>
              <FluentIcon name={modeIcon(mode)} size={12} />
            </button>
            <button type="button" onClick={toggleCanvasPreset} aria-label="Toggle canvas size">
              []
            </button>
            <button type="button" onClick={() => window.dispatchEvent(new CustomEvent('navigate', { detail: 'work' }))} aria-label="Open work page">
              <FluentIcon name="Work" size={12} />
            </button>
            <button type="button" onClick={() => setExpanded(false)} aria-label="Fold canvas">
              <FluentIcon name="RightArrow" size={12} />
            </button>
          </div>

          {mode === 'workflow' && (
            <div className="crt-canvas-pane workflow">
              <p className="crt-widget-lead">{frame.lead}</p>

              <div className="crt-widget-progress" aria-label={`Progress ${progress}%`}>
                <i style={{ width: `${progress}%` }} />
              </div>

              <div className="crt-widget-signal-list">
                {latestSignals.map(signal => (
                  <button key={signal.id || signal.label} type="button">
                    <span>{signal.label}</span>
                    <strong>{signal.text}</strong>
                    {'files' in signal && signal.files && signal.files.length > 0 && (
                      <small>{signal.files.slice(0, 2).map(file => file.split(/[\\/]/).pop()).join(' / ')}</small>
                    )}
                  </button>
                ))}
              </div>
            </div>
          )}

          {mode === 'markdown' && (
            <div className="crt-canvas-pane markdown">
              <ReactMarkdown className="crt-canvas-markdown">{MARKDOWN_SAMPLE}</ReactMarkdown>
            </div>
          )}

          {mode === 'html' && (
            <div className="crt-canvas-pane html">
              <iframe aria-label="HTML artifact preview" sandbox="" srcDoc={HTML_SAMPLE} />
            </div>
          )}

          {mode === 'image' && (
            <div className="crt-canvas-pane image">
              <div className="crt-canvas-image-preview" role="img" aria-label="Generated visual preview mock">
                <div>
                  <strong>AUIP Card Map</strong>
                  <span>{'manifest -> event -> response -> action'}</span>
                </div>
              </div>
              <p className="crt-canvas-caption">Image artifacts should open here first, with full detail one click away.</p>
            </div>
          )}

          {mode === 'table' && (
            <div className="crt-canvas-pane table">
              <div className="crt-canvas-metric-grid">
                <div className="crt-canvas-metric"><strong>04</strong><span>signals</span></div>
                <div className="crt-canvas-metric"><strong>2</strong><span>files</span></div>
                <div className="crt-canvas-metric"><strong>91%</strong><span>ready</span></div>
              </div>
              <div className="crt-canvas-rows">
                {TABLE_ROWS.map(row => (
                  <div className="crt-canvas-row" key={row.label}>
                    <strong>{row.label}</strong>
                    <span>{row.value} / {row.detail}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {mode === 'code' && (
            <div className="crt-canvas-pane code">
              <div className="crt-canvas-code-head">
                <span>work-surface.ts</span>
                <span>+2 / -1</span>
              </div>
              <div className="crt-canvas-code-list">
                {CODE_LINES.map(item => (
                  <div className={`crt-canvas-code-line ${item.kind}`} key={`${item.line}-${item.text}`}>
                    <span>{item.line}</span>
                    <span>{item.prefix}</span>
                    <b>{item.text}</b>
                  </div>
                ))}
              </div>
            </div>
          )}

          {showPermission && (
            <section className="crt-widget-permission">
              <span>Permission</span>
              <strong>Write 2 files inside workspace</strong>
              <p>Reversible local edit - Level 2</p>
              <div>
                <button onClick={() => setPermissionVisible(false)}>Allow once</button>
                <button onClick={() => setPermissionVisible(false)}>Queue</button>
              </div>
            </section>
          )}
        </section>
      )}
    </div>
  )
}
