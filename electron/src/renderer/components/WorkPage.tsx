import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, PointerEvent as ReactPointerEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import FluentIcon from './FluentIcon'
import { preserveOrChooseProvider } from './providerPresentation'
import ProjectStateMapView from './work/ProjectStateMapView'
import WorkDetailDrawer from './work/WorkDetailDrawer'
import AuipExperienceCard from './work/AuipExperienceCard'
import type { AttentionRequest, AuipExperienceProjection, ProviderInspectionDetails, OverlayMode, ProviderEvent, ProviderRun, WorkDockItem, WorkPageProps, WorkProjection } from './work/types'
import { attentionRequestsFromEnvelope } from './work/attentionProjection'
import { getMockWorkboardFrame, MOCK_STAGE_INDEX } from './work/mockWorkboardPlayback'
import { mockWorkTurnFixture } from './work/mockWorkTurn.fixture'
import { buildProjectStateMap, providerRunToWorkTurn } from './work/workModels'
import {
  attentionActionLabel,
  ELECTRON_WORK_SURFACE,
  envelopeMatchesSurface,
  projectionFromEnvelope,
  resolveWorkItemRunId,
  workCountsLabel,
  workItemCapsuleTone,
  workItemBelongsToCurrentSession,
  workItemBelongsToHistory,
  workItemIsClosed,
  workItemNeedsAttention,
} from './work/workProjection'
import {
  STATUS_META,
  TASK_STEPS,
  inferStepState,
  normalizeRun,
  previewJson,
  summarizePayload,
  toolCount,
} from './work/workState'

type DisplayNarrationItem = {
  label: string
  text: string
  state?: 'expanded' | 'folded'
  previewKind?: 'diff' | 'terminal'
  detailLabel?: string
  files?: string[]
  added?: number
  removed?: number
  evidenceLabel?: string
  currentLine?: number
  patchLines?: Array<{
    kind: 'add' | 'remove' | 'context' | 'terminal' | 'success' | 'warn'
    line: number
    text: string
  }>
}

type WorkListFilter = 'current' | 'projects' | 'history'

function formatDeltaLabel(item: DisplayNarrationItem) {
  if (item.detailLabel) return item.detailLabel
  if (!item.files || typeof item.added !== 'number' || typeof item.removed !== 'number') return ''
  const firstFile = item.files[0]?.split(/[\\/]/).pop() || 'files'
  const suffix = item.files.length > 1 ? ` +${item.files.length - 1} more` : ''
  return `${firstFile}${suffix} - +${item.added} / -${item.removed}`
}

function formatPreviewPrefix(kind: NonNullable<DisplayNarrationItem['patchLines']>[number]['kind']) {
  if (kind === 'add') return '+ '
  if (kind === 'remove') return '- '
  return ''
}

function shortPath(path: string) {
  return path.split(/[\\/]/).slice(-3).join('/')
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function auipStatus(value: unknown): AuipExperienceProjection['status'] {
  const status = String(value || '').toLowerCase()
  return ['active', 'completed', 'disconnected', 'closed'].includes(status)
    ? status as AuipExperienceProjection['status']
    : 'connecting'
}

function projectAuipExperience(
  payload: Record<string, unknown>,
  previous?: AuipExperienceProjection,
): AuipExperienceProjection {
  const app = objectValue(payload.app)
  const event = objectValue(payload.event)
  const latestAction = objectValue(payload.latest_verified_self_action)
  const latestNarration = objectValue(payload.latest_delivered_narration)
  const capsule = objectValue(payload.experience_capsule)
  const terminal = objectValue(capsule.terminal)
  return {
    artifactRef: String(payload.artifact_ref || previous?.artifactRef || ''),
    appSessionId: String(payload.app_session_id || previous?.appSessionId || '') || undefined,
    title: String(app.title || previous?.title || 'AUIP application'),
    status: auipStatus(payload.status),
    stance: String(payload.stance || previous?.stance || '') || undefined,
    engagementMode: (String(payload.engagement_mode || previous?.engagementMode || 'observe') as AuipExperienceProjection['engagementMode']),
    operatorStatus: (String(payload.operator_status || previous?.operatorStatus || 'idle') as AuipExperienceProjection['operatorStatus']),
    operatorError: String(payload.operator_error || '') || undefined,
    latestEvent: String(event.type || previous?.latestEvent || '') || undefined,
    latestAction: String(latestAction.type || previous?.latestAction || '') || undefined,
    latestNarration: String(latestNarration.text || previous?.latestNarration || '') || undefined,
    terminal: String(terminal.type || previous?.terminal || '') || undefined,
  }
}

function formatQuietSeconds(value: number | undefined) {
  const seconds = Math.max(0, Math.floor(value || 0))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return seconds % 60 ? `${minutes}m ${seconds % 60}s` : `${minutes}m`
}

function workItemContextLabel(item: WorkDockItem, selected: boolean, history: boolean) {
  const labels: string[] = []
  if (selected) labels.push('Viewing')
  if (item.liveness === 'stalled') labels.push(`Stalled ${formatQuietSeconds(item.silentForSeconds)}`)
  else if (item.liveness === 'cancel_pending') labels.push('Stopping')
  else if (item.activity?.phase === 'waiting_for_user') labels.push('Waiting for you')
  else if (item.activity?.phase === 'cancelling') labels.push('Stopping')
  else if (item.activity?.phase === 'queued') labels.push('Queued')
  else if (['queued', 'running'].includes(item.execution)) labels.push('Working')
  if (workItemNeedsAttention(item)) labels.push(attentionActionLabel(item.attention))
  if (history) labels.push('History')
  else if (workItemIsClosed(item)) labels.push(item.state === 'archived' ? 'Archived' : 'Accepted')
  if (labels.length === 0) labels.push('Current')
  return labels.join(' / ')
}

const WORK_FOCUS_RUN_KEY = 'amadeus.work.focusRunId'
const WORK_FOCUS_ACTION_KEY = 'amadeus.work.focusAction'
const WORK_FOCUS_PROVIDER_KEY = 'amadeus.work.focusProvider'
const WORK_FOCUS_CWD_KEY = 'amadeus.work.focusCwd'

function consumeWorkFocusRequest() {
  const runId = localStorage.getItem(WORK_FOCUS_RUN_KEY) || ''
  const action = localStorage.getItem(WORK_FOCUS_ACTION_KEY) || ''
  const provider = localStorage.getItem(WORK_FOCUS_PROVIDER_KEY) || ''
  const cwd = localStorage.getItem(WORK_FOCUS_CWD_KEY) || ''
  localStorage.removeItem(WORK_FOCUS_RUN_KEY)
  localStorage.removeItem(WORK_FOCUS_ACTION_KEY)
  localStorage.removeItem(WORK_FOCUS_PROVIDER_KEY)
  localStorage.removeItem(WORK_FOCUS_CWD_KEY)
  if (!runId && !action) return null
  return { runId, action, provider, cwd }
}

export default function WorkPage({ send, subscribe, connected }: WorkPageProps) {
  const searchParams = new URLSearchParams(window.location.search)
  const desktopProjection = searchParams.get('desktopProjection') === '1'
  const sliceWindow = searchParams.get('sliceWindow') === '1'
  const panelWindow = searchParams.get('panelWindow') === '1'
  const forceCrtMode = searchParams.get('crt') === '1' || desktopProjection
  const demoPlaybackRequested = import.meta.env.DEV && searchParams.get('workDemo') === '1'
  const [providers, setProviders] = useState<string[]>([])
  const [provider, setProvider] = useState('')
  // An unlocked workspace is intent-routed.  Keep an advanced cwd override
  // only for this page session so an old history selection/localStorage value
  // cannot silently route a future instruction.
  const [cwd, setCwd] = useState('')
  const [task, setTask] = useState('')
  const [runs, setRuns] = useState<ProviderRun[]>([])
  const [selectedRunId, setSelectedRunId] = useState('')
  const [pendingExternalRunId, setPendingExternalRunId] = useState('')
  const [workProjection, setWorkProjection] = useState<WorkProjection | null>(null)
  const [attentionRequests, setAttentionRequests] = useState<AttentionRequest[]>([])
  const [attentionResolving, setAttentionResolving] = useState('')
  const [attentionError, setAttentionError] = useState('')
  const [workAction, setWorkAction] = useState<'focus' | 'retry' | 'resume' | 'reopen' | 'promote' | 'project' | 'accept' | 'archive' | ''>('')
  const [workActionError, setWorkActionError] = useState('')
  const [auipLaunchArtifactId, setAuipLaunchArtifactId] = useState('')
  const [auipLaunchFeedback, setAuipLaunchFeedback] = useState<{ status: 'success' | 'error'; message: string }>()
  const [auipExperience, setAuipExperience] = useState<AuipExperienceProjection>()
  const [workItemDetail, setWorkItemDetail] = useState<Record<string, unknown> | null>(null)
  const [workListFilter, setWorkListFilter] = useState<WorkListFilter>('current')
  const [details, setDetails] = useState<Record<string, ProviderInspectionDetails>>({})
  const [diffPreview, setDiffPreview] = useState<{
    attemptId: string
    runId: string
    diff: Record<string, unknown>
  }>()
  const [submitting, setSubmitting] = useState(false)
  const [cancelSubmitting, setCancelSubmitting] = useState(false)
  const [overlay, setOverlay] = useState<OverlayMode>('none')
  const [detailExpanded, setDetailExpanded] = useState(false)
  const [mapExpanded, setMapExpanded] = useState(false)
  const [planDockOpen, setPlanDockOpen] = useState(false)
  const [crtInsideMode] = useState(() => forceCrtMode || localStorage.getItem('amadeus.work.crtInsideMode') === '1')
  const [mockElapsedMs, setMockElapsedMs] = useState(0)
  const mockPlaybackOffsetRef = useRef(0)
  const mockPlaybackStartedAtRef = useRef(Date.now())
  const projectionPanelRef = useRef<HTMLDivElement | null>(null)
  const lastPresentedAttentionRef = useRef('')
  const [slicePanelPosition, setSlicePanelPosition] = useState<{ left: number; top: number } | null>(() => {
    if (typeof window === 'undefined') return null
    const raw = localStorage.getItem('amadeus.work.slicePanelPosition')
    if (!raw) return null
    try {
      const parsed = JSON.parse(raw) as { left?: unknown; top?: unknown }
      if (typeof parsed.left === 'number' && typeof parsed.top === 'number') return { left: parsed.left, top: parsed.top }
    } catch { /* ignore */ }
    return null
  })

  const activeWorkItem = useMemo(() => {
    if (!workProjection?.selectedWorkItemId) return undefined
    return workProjection.items.find(item => item.id === workProjection.selectedWorkItemId)
  }, [workProjection])
  const activeAttention = attentionRequests[0]
  const workspaceFocusLocked = workProjection?.workspaceFocusMode === 'pinned'
  const workspaceFocusPath = useMemo(() => {
    if (!workspaceFocusLocked || !workProjection) return ''
    if (workProjection.workspaceFocusPath) return workProjection.workspaceFocusPath
    return workProjection.items.find(item => item.id === workProjection.workspaceFocusWorkItemId)?.workspacePath || ''
  }, [workProjection, workspaceFocusLocked])
  const activeWorkRunId = useMemo(
    () => resolveWorkItemRunId(activeWorkItem, runs),
    [activeWorkItem, runs],
  )
  const activeRun = useMemo(() => {
    if (workProjection) {
      return activeWorkRunId ? runs.find(run => run.run_id === activeWorkRunId) : undefined
    }
    if (selectedRunId) {
      const selected = runs.find(run => run.run_id === selectedRunId)
      if (selected) return selected
    }
    return runs[0]
  }, [activeWorkRunId, runs, selectedRunId, workProjection])

  const runLiveness = activeRun?.metadata?.liveness && typeof activeRun.metadata.liveness === 'object'
    ? activeRun.metadata.liveness as Record<string, unknown>
    : undefined
  const activeLiveness = activeWorkItem?.liveness || String(runLiveness?.state || '')
  const activeSilentForSeconds = activeWorkItem?.silentForSeconds ?? Number(runLiveness?.silence_s || 0)
  const cancelPending = cancelSubmitting || activeLiveness === 'cancel_pending'

  const activeDetailsKey = activeRun?.run_id || activeWorkRunId
  const activeDetails = activeDetailsKey ? details[activeDetailsKey] : undefined
  const activeStatus = activeWorkItem
    ? {
        label: workItemIsClosed(activeWorkItem)
          ? activeWorkItem.state === 'archived' ? 'Archived' : 'Accepted'
          : activeLiveness === 'stalled'
            ? `Stalled / ${formatQuietSeconds(activeSilentForSeconds)} quiet`
            : activeLiveness === 'cancel_pending'
              ? 'Stopping / awaiting confirmation'
          : ['queued', 'running'].includes(activeWorkItem.execution)
            ? activeWorkItem.activity
              ? `${activeWorkItem.activity.phase} / ${formatQuietSeconds(activeWorkItem.activity.elapsedSeconds)} elapsed`
              : `Running / ${activeWorkItem.completion}`
            : workItemNeedsAttention(activeWorkItem)
              ? `${attentionActionLabel(activeWorkItem.attention)} / ${activeWorkItem.completion}`
              : `${activeWorkItem.execution} / ${activeWorkItem.completion}`,
        tone: workItemNeedsAttention(activeWorkItem)
          ? 'risk'
          : ['stalled', 'cancel_pending'].includes(activeLiveness)
            ? 'stalled'
          : ['queued', 'running'].includes(activeWorkItem.execution)
            ? 'active'
            : workItemIsClosed(activeWorkItem) || activeWorkItem.completion === 'complete'
              ? 'done'
              : 'idle',
      }
    : activeRun
      ? STATUS_META[activeRun.status]
      : STATUS_META.queued
  const recentEvents = (activeRun?.events || []).slice(-8)
  const detailEvents = (activeRun?.events || []).slice(-80)
  const hasAttention = Boolean(activeAttention || attentionError || activeRun?.error || activeDetails?.error || activeDetails?.busy || workActionError || workItemNeedsAttention(activeWorkItem))
  // Product empty/offline states must stay truthful. The old animated fixture
  // remains available only through an explicit development URL.
  const useMockPlayback = demoPlaybackRequested && !activeRun && !workProjection

  const activeTurn = useMemo(() => providerRunToWorkTurn(activeRun, activeDetails, { cwd, provider }), [activeDetails, activeRun, cwd, provider])
  const displayTurn = useMemo(() => useMockPlayback ? mockWorkTurnFixture.turn : activeTurn, [activeTurn, useMockPlayback])
  const workTurns = useMemo(() => {
    if (useMockPlayback) return [mockWorkTurnFixture.turn]
    if (runs.length === 0) return [activeTurn]
    return runs.map(run => providerRunToWorkTurn(run, details[run.run_id], { cwd, provider }))
  }, [activeTurn, cwd, details, provider, runs, useMockPlayback])
  const projectMap = useMemo(() => buildProjectStateMap(workTurns, displayTurn.id), [displayTurn.id, workTurns])
  const filteredWorkItems = useMemo(() => {
    if (!workProjection) return []
    if (workListFilter === 'projects') return []
    return workProjection.items.filter(item => {
      if (workListFilter === 'history') return workItemBelongsToHistory(item, workProjection)
      return workItemBelongsToCurrentSession(item, workProjection)
    })
  }, [workListFilter, workProjection])
  const visibleWorkItems = useMemo(() => {
    const items = filteredWorkItems.slice(0, 5)
    if (activeWorkItem && !items.some(item => item.id === activeWorkItem.id)) {
      if (items.length >= 5) items[items.length - 1] = activeWorkItem
      else items.push(activeWorkItem)
    }
    return items
  }, [activeWorkItem, filteredWorkItems])
  const visibleProjects = useMemo(
    () => (workProjection?.projects || []).slice(0, 5),
    [workProjection],
  )
  const workCountSummary = useMemo(() => workCountsLabel(workProjection, runs), [runs, workProjection])
  const compactSignals = displayTurn.signals.filter(signal => signal.importance !== 'ambient').slice(-3)
  const mockFrame = useMemo(() => useMockPlayback ? getMockWorkboardFrame(mockElapsedMs) : undefined, [mockElapsedMs, useMockPlayback])
  const mockStageIndex = mockFrame ? MOCK_STAGE_INDEX.findIndex(stage => stage.mode === mockFrame.mode) : -1
  const displayTitle = mockFrame?.title || activeWorkItem?.title || displayTurn.title
  const displayKicker = mockFrame?.kicker || (activeWorkItem
    ? [activeWorkItem.state, activeWorkItem.activity?.phase || activeWorkItem.execution, activeWorkItem.completion, activeWorkItem.attention !== 'none' ? activeWorkItem.attention : ''].filter(Boolean).join(' / ')
    : `${displayTurn.phase} / ${displayTurn.status}`)
  const displayLead = mockFrame?.lead
    || activeWorkItem?.activity?.semanticSummary
    || (activeRun ? displayTurn.summary : '')
    || (activeWorkItem?.workspaceLabel ? `Workspace: ${activeWorkItem.workspaceLabel}` : '')
    || displayTurn.summary
    || displayTurn.intent
  const displayNarrationItems: DisplayNarrationItem[] = mockFrame
    ? mockFrame.summaryLines.map(line => ({
        label: line.label,
        text: line.text,
        previewKind: line.previewKind,
        detailLabel: line.detailLabel,
        files: line.files,
        added: line.added,
        removed: line.removed,
        patchLines: line.patchLines,
        currentLine: line.currentLine,
        state: line.state,
      }))
    : activeTurn.signals.slice(-6).map(signal => ({
        label: signal.phase,
        text: signal.summary,
        evidenceLabel: signal.evidence?.length
          ? signal.evidence.map(ref => ref.label).slice(0, 2).join(' / ')
          : undefined,
      }))

  useEffect(() => {
    if (!forceCrtMode) localStorage.setItem('amadeus.work.crtInsideMode', crtInsideMode ? '1' : '0')
  }, [crtInsideMode, forceCrtMode])
  useEffect(() => {
    if (!desktopProjection) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        void (window as any).amadeus?.closeWorkOverlay?.()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [desktopProjection])
  const reportPanelBounds = useCallback(() => {
    if (!sliceWindow || !projectionPanelRef.current) return
    const selectors = [
      '.crt-projection-content',
      '.crt-task-dock',
      '.crt-timeline-compact',
      '.crt-command-bar',
      '.crt-detail-toggle',
      '.crt-detail-drawer',
      '.crt-popover',
      '.crt-stage-status',
    ]
    const bounds = selectors.flatMap(selector => {
      const elements = Array.from(document.querySelectorAll(selector))
      return elements.map(element => {
        const rect = element.getBoundingClientRect()
        return {
          x: rect.left,
          y: rect.top,
          width: rect.width,
          height: rect.height,
        }
      })
    }).filter(rect => rect.width > 0 && rect.height > 0)

    if (bounds.length > 0) {
      void (window as any).amadeus?.setWorkOverlayHitRegions?.(bounds)
      return
    }

    const rect = projectionPanelRef.current.getBoundingClientRect()
    void (window as any).amadeus?.setWorkOverlayPanelBounds?.({ x: rect.left, y: rect.top, width: rect.width, height: rect.height })
  }, [sliceWindow])

  useEffect(() => {
    if (!sliceWindow) return
    const id = window.requestAnimationFrame(reportPanelBounds)
    const timer = window.setInterval(reportPanelBounds, 250)
    window.addEventListener('resize', reportPanelBounds)
    return () => {
      window.cancelAnimationFrame(id)
      window.clearInterval(timer)
      window.removeEventListener('resize', reportPanelBounds)
    }
  }, [reportPanelBounds, slicePanelPosition, sliceWindow])

  const startSlicePanelDrag = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    if (!sliceWindow || !projectionPanelRef.current) return
    if ((event.target as HTMLElement).closest('button, input, textarea, select, a')) return

    const rect = projectionPanelRef.current.getBoundingClientRect()
    const startX = event.clientX
    const startY = event.clientY
    const startLeft = rect.left
    const startTop = rect.top
    const panelWidth = rect.width
    const panelHeight = rect.height
    void (window as any).amadeus?.setWorkOverlayMouseIgnore?.(false)

    const move = (moveEvent: PointerEvent) => {
      const margin = 18
      const nextLeft = Math.min(window.innerWidth - panelWidth - margin, Math.max(margin, startLeft + moveEvent.clientX - startX))
      const nextTop = Math.min(window.innerHeight - panelHeight - margin, Math.max(margin, startTop + moveEvent.clientY - startY))
      const next = { left: nextLeft, top: nextTop }
      setSlicePanelPosition(next)
      localStorage.setItem('amadeus.work.slicePanelPosition', JSON.stringify(next))
      void (window as any).amadeus?.setWorkOverlayPanelBounds?.({
        x: nextLeft,
        y: nextTop,
        width: panelWidth,
        height: panelHeight,
      })
    }

    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      reportPanelBounds()
    }

    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }, [reportPanelBounds, sliceWindow])
  useEffect(() => {
    if (!useMockPlayback) {
      setMockElapsedMs(0)
      return
    }
    mockPlaybackOffsetRef.current = 0
    mockPlaybackStartedAtRef.current = Date.now()
    const timer = window.setInterval(() => {
      setMockElapsedMs(mockPlaybackOffsetRef.current + Date.now() - mockPlaybackStartedAtRef.current)
    }, 240)
    return () => window.clearInterval(timer)
  }, [useMockPlayback])

  const jumpMockStage = useCallback((elapsedMs: number) => {
    mockPlaybackOffsetRef.current = elapsedMs
    mockPlaybackStartedAtRef.current = Date.now()
    setMockElapsedMs(elapsedMs)
  }, [])

  const upsertRun = useCallback((run: ProviderRun) => {
    setRuns(prev => {
      const index = prev.findIndex(item => item.run_id === run.run_id)
      if (index < 0) {
        return [run, ...prev]
      }
      const next = [...prev]
      const previous = next[index]
      next[index] = {
        ...previous,
        ...run,
        events: run.events && run.events.length > 0 ? run.events : previous.events,
      }
      return next
    })
  }, [])

  const updateDetails = useCallback((runId: string, patch: ProviderInspectionDetails) => {
    setDetails(prev => ({ ...prev, [runId]: { ...(prev[runId] || {}), ...patch } }))
  }, [])

  const applyWorkEnvelope = useCallback((payload: unknown) => {
    if (!envelopeMatchesSurface(payload, ELECTRON_WORK_SURFACE)) return null
    const projection = projectionFromEnvelope(payload)
    if (projection) setWorkProjection(projection)
    return projection
  }, [])

  const applyWorkResponse = useCallback((response: Record<string, unknown>) => {
    const projection = applyWorkEnvelope(response)
    const run = normalizeRun(response.run)
    if (run) upsertRun(run)
    return { projection, run }
  }, [applyWorkEnvelope, upsertRun])

  const refreshWorkProjection = useCallback(async () => {
    const response = await send('work.list', { surface: ELECTRON_WORK_SURFACE })
    return applyWorkEnvelope(response)
  }, [applyWorkEnvelope, send])

  const launchAuipApp = useCallback(async (artifactId: string, mode = 'observe') => {
    if (!artifactId || auipLaunchArtifactId) return
    setAuipLaunchArtifactId(artifactId)
    setAuipLaunchFeedback(undefined)
    try {
      const prepared = await send('auip.attach.prepare', { artifact_id: artifactId, mode })
      const launchUrl = String(prepared.launch_url || '')
      const artifactRef = String(prepared.artifact_ref || '')
      const hostSurfaceId = String(prepared.host_surface_id || '')
      const workItemId = String(prepared.work_item_id || '')
      if (!launchUrl) throw new Error('The host did not return an AUIP launch descriptor.')
      if (!artifactRef) throw new Error('The host did not bind the AUIP launch to an artifact.')
      if (!hostSurfaceId) throw new Error('The host did not bind an AUIP surface identity.')
      setAuipExperience({
        artifactRef,
        title: 'AUIP application',
        status: 'connecting',
      })
      const opened = await window.amadeus?.openAuipApp(launchUrl, hostSurfaceId, workItemId)
      if (!opened?.ok) {
        throw new Error(opened?.detail || 'The desktop host refused the AUIP launch URL.')
      }
      setAuipLaunchFeedback({
        status: 'success',
        message: 'Application opened. Its one-time Attach ticket is waiting for registration.',
      })
    } catch (error) {
      setAuipExperience(undefined)
      setAuipLaunchFeedback({
        status: 'error',
        message: error instanceof Error ? error.message : String(error),
      })
    } finally {
      setAuipLaunchArtifactId('')
    }
  }, [auipLaunchArtifactId, send])

  const loadAttemptDiff = useCallback(async (
    attemptId: string,
    runId: string,
    cwdHint?: string,
  ) => {
    const detailsKey = runId || (attemptId ? `attempt:${attemptId}` : '')
    if (!detailsKey) return
    updateDetails(detailsKey, { busy: 'Loading diff', error: undefined })
    try {
      const res = await send('provider.diff', {
        run_id: runId || undefined,
        attempt_id: attemptId || undefined,
        cwd: cwdHint || undefined,
      })
      const diff = objectValue(res.diff)
      updateDetails(detailsKey, { busy: undefined, error: undefined, diff })
      setDiffPreview({ attemptId, runId, diff })
      setOverlay('canvas')
    } catch (error) {
      updateDetails(detailsKey, { busy: undefined, error: error instanceof Error ? error.message : String(error) })
      setOverlay('permission')
    }
  }, [send, updateDetails])

  const applyProviderCanvasAction = useCallback((event: ProviderEvent) => {
    const payload = event.payload || {}
    const action = String(payload.action || '')
    const runId = String(payload.run_id || event.run_id || '')
    const actionProvider = String(payload.provider || event.provider || '').toLowerCase()
    if (!runId) return
    if (action !== 'open_details') return
    if (actionProvider) setProvider(actionProvider)
    const boundWorkItem = workProjection?.items.find(item => resolveWorkItemRunId(item, runs) === runId)
    if (boundWorkItem && boundWorkItem.id !== workProjection?.selectedWorkItemId) {
      void send('work.focus', {
        surface: ELECTRON_WORK_SURFACE,
        work_item_id: boundWorkItem.id,
      }).then(response => {
        const { projection } = applyWorkResponse(response)
        if (!projection) {
          setWorkProjection(current => current ? { ...current, selectedWorkItemId: boundWorkItem.id } : current)
        }
      }).catch(error => {
        setWorkActionError(error instanceof Error ? error.message : String(error))
      })
    }
    setSelectedRunId(runId)
    setDetailExpanded(true)
    if (!desktopProjection) void (window as any).amadeus?.openWorkOverlay?.()
    setOverlay('trace')
  }, [applyWorkResponse, desktopProjection, runs, send, workProjection])

  const appendEvent = useCallback((event: ProviderEvent) => {
    setRuns(prev => {
      const index = prev.findIndex(item => item.run_id === event.run_id)
      if (index < 0) {
        const status = event.type === 'run.failed'
          ? 'error'
          : event.type === 'run.finished'
            ? 'done'
            : 'running'
        return [{ run_id: event.run_id, provider: event.provider, task: '', cwd: String(event.payload?.cwd || ''), status, events: [event] }, ...prev]
      }
      const next = [...prev]
      const run = next[index]
      const status = event.type === 'run.failed'
        ? 'error'
        : event.type === 'run.cancelled'
          ? 'cancelled'
          : event.type === 'run.finished'
            ? 'done'
            : run.status
      const liveness = String(event.payload?.liveness || '')
      const metadata = liveness
        ? { ...(run.metadata || {}), liveness: { state: liveness, ...(event.payload || {}) } }
        : run.metadata
      next[index] = { ...run, status, metadata, events: [...(run.events || []), event].slice(-260) }
      return next
    })

    if (event.type === 'diff.updated') {
      setDetails(prev => ({
        ...prev,
        [event.run_id]: { ...(prev[event.run_id] || {}), diff: event.payload },
      }))
    }

    if (event.type === 'canvas.action') {
      applyProviderCanvasAction(event)
    }
    if (
      ['run.created', 'run.started', 'run.finished', 'run.failed', 'run.cancelled'].includes(event.type)
      || (event.type === 'run.status' && event.payload?.liveness)
    ) {
      void refreshWorkProjection().catch(() => {})
    }
  }, [applyProviderCanvasAction, refreshWorkProjection])

  useEffect(() => {
    if (!connected) return
    send('provider.list', {}).then(res => {
      if (Array.isArray(res.providers)) {
        const nextProviders = res.providers.map(String)
        setProviders(nextProviders)
        setProvider(current => preserveOrChooseProvider(
          current,
          nextProviders,
          res.provider_manifests,
        ))
      }
      if (Array.isArray(res.runs)) {
        const nextRuns = res.runs.map(normalizeRun).filter(Boolean) as ProviderRun[]
        setRuns(nextRuns)
        setSelectedRunId(current => current || nextRuns[0]?.run_id || '')
      }
    }).catch(() => {})
  }, [connected, send])

  useEffect(() => {
    if (!connected) return
    void refreshWorkProjection().catch(() => {})
  }, [connected, refreshWorkProjection])

  useEffect(() => {
    const unsubEvent = subscribe('provider.event', (p) => appendEvent(p as unknown as ProviderEvent))
    const unsubResult = subscribe('provider.result', (p) => {
      const run = normalizeRun(p)
      if (run) upsertRun(run)
      void refreshWorkProjection().catch(() => {})
    })
    return () => { unsubEvent(); unsubResult() }
  }, [appendEvent, refreshWorkProjection, subscribe, upsertRun])

  useEffect(() => {
    const unsubscribe = subscribe('work.updated', payload => {
      applyWorkEnvelope(payload)
    })
    return () => unsubscribe()
  }, [applyWorkEnvelope, subscribe])

  useEffect(() => {
    const unsubscribe = subscribe('auip.updated', payload => {
      setAuipExperience(previous => {
        if (!previous?.artifactRef) return previous
        if (String(payload.artifact_ref || '') !== previous.artifactRef) return previous
        return projectAuipExperience(payload, previous)
      })
    })
    return () => unsubscribe()
  }, [subscribe])

  useEffect(() => {
    if (!connected) return
    void send('attention.list', {}).then(payload => {
      setAttentionRequests(attentionRequestsFromEnvelope(payload))
    }).catch(() => {})
  }, [connected, send])

  useEffect(() => {
    const unsubscribe = subscribe('attention.updated', payload => {
      setAttentionRequests(attentionRequestsFromEnvelope(payload))
    })
    return () => unsubscribe()
  }, [subscribe])

  useEffect(() => {
    if (!activeAttention || lastPresentedAttentionRef.current === activeAttention.id) return
    lastPresentedAttentionRef.current = activeAttention.id
    setAttentionError('')
    setOverlay('attention')
  }, [activeAttention])

  useEffect(() => {
    if (!workProjection) return
    setSelectedRunId(activeWorkRunId)
  }, [activeWorkRunId, workProjection])

  useEffect(() => {
    if (!connected || !activeWorkItem?.id) {
      setWorkItemDetail(null)
      setDiffPreview(undefined)
      return
    }
    setDiffPreview(undefined)
    let cancelled = false
    void send('work.get', {
      surface: ELECTRON_WORK_SURFACE,
      work_item_id: activeWorkItem.id,
    }).then(response => {
      if (!cancelled) setWorkItemDetail(response.item && typeof response.item === 'object' ? response.item as Record<string, unknown> : null)
    }).catch(() => {
      if (!cancelled) setWorkItemDetail(null)
    })
    return () => { cancelled = true }
  }, [activeWorkItem?.id, connected, send])

  useEffect(() => {
    if (!connected) return
    const focus = consumeWorkFocusRequest()
    if (!focus) return
    if (focus.provider) setProvider(focus.provider)
    if (focus.runId) {
      setSelectedRunId(focus.runId)
      setPendingExternalRunId(focus.runId)
    }
    setDetailExpanded(true)
    setOverlay('trace')
  }, [connected])

  const selectWorkItem = useCallback(async (item: WorkDockItem) => {
    if (!connected || workAction) return
    setWorkAction('focus')
    setWorkActionError('')
    try {
      const response = await send('work.focus', {
        surface: ELECTRON_WORK_SURFACE,
        work_item_id: item.id,
      })
      const { projection } = applyWorkResponse(response)
      const canonicalItem = projection?.items.find(candidate => candidate.id === projection.selectedWorkItemId) || item
      setSelectedRunId(resolveWorkItemRunId(canonicalItem, runs))
      if (!projection) {
        setWorkProjection(current => current ? { ...current, selectedWorkItemId: item.id } : current)
      }
    } catch (error) {
      setWorkActionError(error instanceof Error ? error.message : String(error))
    } finally {
      setWorkAction('')
    }
  }, [applyWorkResponse, connected, runs, send, workAction])

  useEffect(() => {
    if (!pendingExternalRunId || !workProjection || workAction) return
    const item = workProjection.items.find(candidate => resolveWorkItemRunId(candidate, runs) === pendingExternalRunId)
    if (!item) return
    setPendingExternalRunId('')
    if (item.id === workProjection.selectedWorkItemId) {
      setSelectedRunId(pendingExternalRunId)
      return
    }
    void selectWorkItem(item)
  }, [pendingExternalRunId, runs, selectWorkItem, workAction, workProjection])

  const toggleWorkspaceFocus = useCallback(async () => {
    if (!connected || !workProjection || workAction) return
    const workspaceLocked = workProjection.workspaceFocusMode === 'pinned'
    if (!workspaceLocked && !activeWorkItem) return
    const focusMode = workspaceLocked ? 'auto' : 'pinned'
    setWorkAction('focus')
    setWorkActionError('')
    try {
      const response = await send('work.focus', {
        surface: ELECTRON_WORK_SURFACE,
        work_item_id: focusMode === 'pinned' ? activeWorkItem?.id : undefined,
        focus_mode: focusMode,
      })
      const { projection } = applyWorkResponse(response)
      if (!projection) {
        setWorkProjection(current => current ? {
          ...current,
          workspaceFocusMode: focusMode,
          workspaceFocusPath: focusMode === 'pinned' ? activeWorkItem?.workspacePath : undefined,
          workspaceFocusWorkItemId: focusMode === 'pinned' ? activeWorkItem?.id : undefined,
        } : current)
      }
    } catch (error) {
      setWorkActionError(error instanceof Error ? error.message : String(error))
    } finally {
      setWorkAction('')
    }
  }, [activeWorkItem, applyWorkResponse, connected, send, workAction, workProjection])

  const reopenWorkItem = useCallback(async () => {
    if (!connected || !activeWorkItem || !workItemIsClosed(activeWorkItem) || workAction) return
    setWorkAction('reopen')
    setWorkActionError('')
    try {
      const response = await send('work.reopen', { work_item_id: activeWorkItem.id, surface: ELECTRON_WORK_SURFACE })
      const { projection } = applyWorkResponse(response)
      if (!projection) {
        setWorkProjection(current => current
          ? { ...current, items: current.items.map(item => item.id === activeWorkItem.id ? { ...item, state: 'open' } : item) }
          : current)
        void refreshWorkProjection().catch(() => {})
      }
    } catch (error) {
      setWorkActionError(error instanceof Error ? error.message : String(error))
      setOverlay('permission')
    } finally {
      setWorkAction('')
    }
  }, [activeWorkItem, applyWorkResponse, connected, refreshWorkProjection, send, workAction])

  // Keeping a draft is the one decision the voice path deliberately never
  // offers, so every surface that shows the draft state needs to be able to act
  // on it -- Electron showed it and could not.
  const promoteWorkItem = useCallback(async () => {
    if (!connected || !activeWorkItem || !activeWorkItem.canPromoteToProject || workAction) return
    setWorkAction('promote')
    setWorkActionError('')
    try {
      const response = await send('work.promote', { work_item_id: activeWorkItem.id, surface: ELECTRON_WORK_SURFACE })
      const { projection } = applyWorkResponse(response)
      if (!projection) void refreshWorkProjection().catch(() => {})
    } catch (error) {
      setWorkActionError(error instanceof Error ? error.message : String(error))
      setOverlay('permission')
    } finally {
      setWorkAction('')
    }
  }, [activeWorkItem, applyWorkResponse, connected, refreshWorkProjection, send, workAction])

  // Keeping a place is otherwise a one-way ratchet: without this, something
  // worth two days of attention stays among the choices forever.
  const setProjectRetired = useCallback(async (retired: boolean) => {
    if (!connected || !activeWorkItem?.projectId || workAction) return
    setWorkAction('project')
    setWorkActionError('')
    try {
      const response = await send('work.project.state', {
        project_id: activeWorkItem.projectId,
        retired,
        surface: ELECTRON_WORK_SURFACE,
      })
      const { projection } = applyWorkResponse(response)
      if (!projection) void refreshWorkProjection().catch(() => {})
    } catch (error) {
      setWorkActionError(error instanceof Error ? error.message : String(error))
      setOverlay('permission')
    } finally {
      setWorkAction('')
    }
  }, [activeWorkItem, applyWorkResponse, connected, refreshWorkProjection, send, workAction])

  const acceptWorkItem = useCallback(async () => {
    if (!connected || !activeWorkItem || workAction) return
    setWorkAction('accept')
    setWorkActionError('')
    try {
      const response = await send('work.accept', {
        work_item_id: activeWorkItem.id,
        surface: ELECTRON_WORK_SURFACE,
      })
      applyWorkResponse(response)
    } catch (error) {
      setWorkActionError(error instanceof Error ? error.message : String(error))
      setOverlay('permission')
    } finally {
      setWorkAction('')
    }
  }, [activeWorkItem, applyWorkResponse, connected, send, workAction])

  const archiveWorkItem = useCallback(async () => {
    if (!connected || !activeWorkItem || workAction) return
    setWorkAction('archive')
    setWorkActionError('')
    try {
      const response = await send('work.archive', {
        work_item_id: activeWorkItem.id,
        surface: ELECTRON_WORK_SURFACE,
      })
      applyWorkResponse(response)
    } catch (error) {
      setWorkActionError(error instanceof Error ? error.message : String(error))
      setOverlay('permission')
    } finally {
      setWorkAction('')
    }
  }, [activeWorkItem, applyWorkResponse, connected, send, workAction])

  const resolveAttention = useCallback(async (requestId: string, optionId: string) => {
    if (!connected || attentionResolving) return
    setAttentionResolving(optionId)
    setAttentionError('')
    try {
      const response = await send('attention.resolve', {
        request_id: requestId,
        option_id: optionId,
      })
      if (response.ok !== true) {
        throw new Error(String(response.error || 'Unable to apply this choice.'))
      }
      setAttentionRequests(attentionRequestsFromEnvelope(response))
      setOverlay('none')
    } catch (error) {
      setAttentionError(error instanceof Error ? error.message : String(error))
    } finally {
      setAttentionResolving('')
    }
  }, [attentionResolving, connected, send])

  const retryWorkItem = useCallback(async (
    amendmentText = '',
    authorizationPermissionRequestId = '',
  ) => {
    if (!connected || !activeWorkItem?.canRetry || workAction) return
    setWorkAction('retry')
    setWorkActionError('')
    try {
      const response = await send('work.retry', {
        work_item_id: activeWorkItem.id,
        amendment_text: amendmentText,
        ...(authorizationPermissionRequestId
          ? { authorization_permission_request_id: authorizationPermissionRequestId }
          : {}),
        surface: ELECTRON_WORK_SURFACE,
      })
      const { projection, run } = applyWorkResponse(response)
      if (run) setSelectedRunId(run.run_id)
      if (!projection) void refreshWorkProjection().catch(() => {})
      setOverlay('none')
    } catch (error) {
      setWorkActionError(error instanceof Error ? error.message : String(error))
      setOverlay('permission')
    } finally {
      setWorkAction('')
    }
  }, [activeWorkItem, applyWorkResponse, connected, refreshWorkProjection, send, workAction])

  const resumeWorkItem = useCallback(async () => {
    if (!connected || !activeWorkItem?.canResume || workAction) return
    setWorkAction('resume')
    setWorkActionError('')
    try {
      const response = await send('work.resume', {
        work_item_id: activeWorkItem.id,
        surface: ELECTRON_WORK_SURFACE,
      })
      const { projection, run } = applyWorkResponse(response)
      if (run) setSelectedRunId(run.run_id)
      if (!projection) void refreshWorkProjection().catch(() => {})
      setOverlay('none')
    } catch (error) {
      setWorkActionError(error instanceof Error ? error.message : String(error))
      setOverlay('permission')
    } finally {
      setWorkAction('')
    }
  }, [activeWorkItem, applyWorkResponse, connected, refreshWorkProjection, send, workAction])

  const runProviderInspection = useCallback(async (
    run: ProviderRun,
    busy: string,
    action: () => Promise<Record<string, unknown>>,
    assign: (res: Record<string, unknown>) => ProviderInspectionDetails,
  ) => {
    updateDetails(run.run_id, { busy, error: undefined })
    setOverlay('permission')
    try {
      const res = await action()
      updateDetails(run.run_id, { busy: undefined, error: undefined, ...assign(res) })
    } catch (error) {
      updateDetails(run.run_id, { busy: undefined, error: error instanceof Error ? error.message : String(error) })
    }
  }, [updateDetails])

  const submit = useCallback(async () => {
    const prompt = task.trim()
    if (!connected || !prompt || workAction) return
    setSubmitting(true)
    try {
      const routedCwd = workProjection?.workspaceFocusMode === 'pinned'
        ? workspaceFocusPath
        : cwd.trim()
      const res = await send('work.start', {
        provider,
        task: prompt,
        cwd: routedCwd || undefined,
        mode: 'agent',
        metadata: {},
        surface: ELECTRON_WORK_SURFACE,
      })
      const { projection, run } = applyWorkResponse(res)
      if (run) {
        if (!projection || projection.focusMode === 'auto') setSelectedRunId(run.run_id)
      }
      if (!projection) void refreshWorkProjection().catch(() => {})
      setTask('')
      setOverlay('none')
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setWorkActionError(message)
      setOverlay('permission')
    } finally {
      setSubmitting(false)
    }
  }, [applyWorkResponse, connected, cwd, provider, refreshWorkProjection, send, task, workAction, workProjection, workspaceFocusPath])

  const cancelRun = useCallback(async () => {
    if (!activeRun || cancelPending) return
    setCancelSubmitting(true)
    try {
      await send('provider.cancel', { run_id: activeRun.run_id })
      await refreshWorkProjection()
    } catch {
      // The provider event is authoritative; a transport error leaves the run active.
    } finally {
      setCancelSubmitting(false)
    }
  }, [activeRun, cancelPending, refreshWorkProjection, send])

  const refreshDiff = useCallback((attemptId = '', runId = '') => {
    const targetRunId = runId || activeRun?.run_id || activeWorkItem?.currentRunId || ''
    if (!attemptId && !targetRunId) return
    setDetailExpanded(true)
    void loadAttemptDiff(
      attemptId,
      targetRunId,
      activeRun?.cwd || activeWorkItem?.workspacePath,
    )
  }, [activeRun, activeWorkItem, loadAttemptDiff])

  const refreshStatus = useCallback(() => {
    if (!activeRun) return
    runProviderInspection(activeRun, 'Loading status', () => send('provider.status', { run_id: activeRun.run_id, cwd: activeRun.cwd || undefined }), res => ({ status: res.status }))
  }, [activeRun, runProviderInspection, send])

  const openDesktopProjection = useCallback(() => {
    if (desktopProjection) {
      void (window as any).amadeus?.closeWorkOverlay?.()
      return
    }
    void (window as any).amadeus?.openWorkOverlay?.()
  }, [desktopProjection])

  const slicePanelStyle = useMemo<CSSProperties>(() => {
    if (!sliceWindow || !slicePanelPosition) return {}
    return {
      '--work-slice-left': `${slicePanelPosition.left}px`,
      '--work-slice-top': `${slicePanelPosition.top}px`,
    } as CSSProperties
  }, [slicePanelPosition, sliceWindow])
  const selectedWorkClosed = workItemIsClosed(activeWorkItem)
  const selectedAttemptActive = Boolean(
    activeRun?.status === 'queued'
    || activeRun?.status === 'running'
    || activeWorkItem?.execution === 'queued'
    || activeWorkItem?.execution === 'running',
  )
  const completionHistory = Array.isArray(workItemDetail?.completionHistory) ? workItemDetail.completionHistory : []
  const canAcceptWork = Boolean(
    activeWorkItem
    && !selectedWorkClosed
    && !selectedAttemptActive
    && activeWorkItem.execution === 'succeeded'
    && completionHistory.length > 0,
  )
  const canArchiveWork = Boolean(
    activeWorkItem
    && !selectedAttemptActive
    && activeWorkItem.state !== 'archived',
  )

  const canvasMarkdown = useMemo(() => {
    if (!activeRun && activeWorkItem) {
      return [
        `### ${activeWorkItem.title}`,
        activeWorkItem.state ? `State: ${activeWorkItem.state}` : '',
        `Execution: ${activeWorkItem.execution}`,
        activeWorkItem.activity?.phase ? `Current phase: ${activeWorkItem.activity.phase}` : '',
        activeWorkItem.activity?.elapsedSeconds ? `Elapsed: ${formatQuietSeconds(activeWorkItem.activity.elapsedSeconds)}` : '',
        activeWorkItem.activity?.silentSeconds ? `Last activity: ${formatQuietSeconds(activeWorkItem.activity.silentSeconds)} ago` : '',
        activeWorkItem.activity?.semanticSummary ? `Latest progress: ${activeWorkItem.activity.semanticSummary}` : '',
        activeWorkItem.activity?.lastTool ? `Latest tool: ${activeWorkItem.activity.lastTool}` : '',
        activeWorkItem.activity?.steering?.state ? `Change instruction: ${activeWorkItem.activity.steering.state}` : '',
        `Completion: ${activeWorkItem.completion}`,
        `Attention: ${activeWorkItem.attention}`,
        activeWorkItem.workspaceLabel ? `Workspace: ${activeWorkItem.workspaceLabel}` : '',
        activeWorkItem.workspacePath ? `Path: ${activeWorkItem.workspacePath}` : '',
        activeWorkItem.branch ? `Git branch: ${activeWorkItem.branch}` : '',
        activeWorkItem.isolation ? `Isolation: ${activeWorkItem.isolation}` : '',
        activeWorkItem.writerLeaseStatus ? `Writer lease: ${activeWorkItem.writerLeaseStatus}` : '',
        activeWorkItem.workspaceExists === false ? 'Workspace ownership: missing' : '',
        activeWorkItem.selectionReason ? `Reason: ${activeWorkItem.selectionReason}` : '',
        activeWorkItem.artifactCount !== undefined ? `Artifacts: ${activeWorkItem.artifactCount}` : '',
        '',
        activeWorkItem.canResume
          ? 'An interrupted provider run can be resumed from its checkpoint.'
          : activeWorkItem.canRetry
            ? 'The failed provider run can be retried with the same instruction.'
            : 'New instructions start a separate WorkItem.',
      ].filter(Boolean).join('\n')
    }
    if (!activeRun) return '### Canvas standby\nStart a provider task to render markdown, diffs, tables, HTML previews, and generated artifacts here.'
    if (activeRun.result) return activeRun.result
    if (activeRun.error || activeDetails?.error) return `### Intervention\n${activeRun.error || activeDetails?.error}`
    return [
      `### ${activeRun.task || 'Active provider run'}`,
      '',
      `Provider: ${activeRun.provider}`,
      `Status: ${activeStatus.label}`,
      `Events: ${(activeRun.events || []).length}`,
      `Tool calls: ${toolCount(activeRun)}`,
      '',
      recentEvents.length > 0
        ? recentEvents.slice(-5).map(event => `- ${event.type}: ${summarizePayload(event)}`).join('\n')
        : 'Waiting for provider events.',
    ].join('\n')
  }, [activeDetails?.error, activeRun, activeStatus.label, activeWorkItem, recentEvents])

  const renderOverlay = () => {
    if (overlay === 'none') return null
    return (
      <div className={`crt-popover crt-popover-${overlay}`}>
        <div className="crt-popover-head">
          <span>{overlay}</span>
          <button onClick={() => setOverlay('none')}>Close</button>
        </div>
        <div className="crt-popover-body">
          {overlay === 'canvas' && diffPreview && (
            <div className="crt-event-list">
              <h3>{diffPreview.attemptId ? `Attempt ${diffPreview.attemptId} diff` : 'Attributed diff'}</h3>
              <p>
                {Array.isArray(diffPreview.diff.changed_files)
                  ? `${diffPreview.diff.changed_files.length} attributed file(s)`
                  : 'Attributed file count unavailable'}
              </p>
              {String(diffPreview.diff.reason || '') && <p>{String(diffPreview.diff.reason)}</p>}
              <pre>{String(diffPreview.diff.patch || '') || 'No renderable patch was retained for this Attempt.'}</pre>
            </div>
          )}
          {overlay === 'canvas' && !diffPreview && <ReactMarkdown>{canvasMarkdown}</ReactMarkdown>}
          {overlay === 'trace' && (
            <div className="crt-event-list">
              {(activeRun?.events || []).slice(-28).map((event, index) => (
                <div key={`${event.type}-${index}`} className="crt-event-row">
                  <span>{event.type}</span>
                  <p>{summarizePayload(event)}</p>
                </div>
              ))}
            </div>
          )}
          {overlay === 'attention' && activeAttention && (
            <div className="crt-attention-card" role="dialog" aria-labelledby="attention-title">
              <h3 id="attention-title">{activeAttention.title}</h3>
              {activeAttention.prompt && <p>{activeAttention.prompt}</p>}
              {attentionError && <p className="crt-attention-error">{attentionError}</p>}
              <div className="crt-attention-options">
                {activeAttention.options.map(option => (
                  <button
                    key={option.id}
                    type="button"
                    className={`crt-attention-option ${option.entityKind}`}
                    disabled={Boolean(attentionResolving)}
                    onClick={() => { void resolveAttention(activeAttention.id, option.id) }}
                  >
                    <span className="crt-attention-kind">
                      {option.entityKind === 'project'
                        ? 'PROJECT'
                        : option.entityKind === 'work_item'
                          ? option.parentLabel ? 'WORKITEM IN PROJECT' : 'SESSION DRAFT'
                          : 'CHOICE'}
                    </span>
                    <strong>{option.label}</strong>
                    {option.parentLabel && <small>↳ {option.parentLabel}</small>}
                    {option.description && <p>{option.description}</p>}
                    {attentionResolving === option.id && <em>Applying…</em>}
                  </button>
                ))}
              </div>
            </div>
          )}
          {overlay === 'attention' && !activeAttention && attentionError && (
            <div className="crt-attention-card" role="alert">
              <h3>Selection could not be applied</h3>
              <p className="crt-attention-error">{attentionError}</p>
              <p>The original operation was not started again. You can repeat the request in Chat.</p>
            </div>
          )}
          {overlay === 'permission' && (
            <>
              <p>{workActionError || activeDetails?.busy || activeRun?.error || activeDetails?.error || (workAction ? `${workAction} in progress.` : 'No blocking permission request. Stage and commit remain explicit user actions.')}</p>
              <div className="crt-action-grid">
                <button
                  onClick={() => refreshDiff()}
                  disabled={!activeWorkRunId}
                >Load Diff</button>
                <button onClick={refreshStatus} disabled={!activeRun}>Git Status</button>
                <button onClick={() => { void acceptWorkItem() }} disabled={!canAcceptWork || !!workAction}>
                  {workAction === 'accept' ? 'Accepting' : 'Accept WorkItem'}
                </button>
                <button onClick={() => { void archiveWorkItem() }} disabled={!canArchiveWork || !!workAction}>
                  {workAction === 'archive' ? 'Archiving' : 'Archive WorkItem'}
                </button>
                <button onClick={() => { void reopenWorkItem() }} disabled={!selectedWorkClosed || !!workAction}>Reopen WorkItem</button>
                <button
                  onClick={() => { void promoteWorkItem() }}
                  disabled={!activeWorkItem?.canPromoteToProject || !!workAction}
                  title="Keep this scratch task as a project so later instructions can be sent to it by name."
                >
                  {workAction === 'promote' ? 'Keeping' : 'Keep as project'}
                </button>
                {activeWorkItem?.projectState === 'retired'
                  ? (
                    <button
                      onClick={() => { void setProjectRetired(false) }}
                      disabled={!!workAction}
                      title="Offer this project again as somewhere to send new work."
                    >
                      {workAction === 'project' ? 'Restoring' : 'Restore project'}
                    </button>
                  )
                  : (
                    <button
                      onClick={() => { void setProjectRetired(true) }}
                      disabled={!activeWorkItem?.projectId || !activeWorkItem?.projectState || !!workAction}
                      title="Stop offering this project for new work. Its files, tasks and history all stay."
                    >
                      {workAction === 'project' ? 'Retiring' : 'Retire project'}
                    </button>
                  )}
              </div>
            </>
          )}
          {overlay === 'audit' && <pre>{previewJson({ workItem: workItemDetail || activeWorkItem, work: workProjection, run: activeRun, provider: activeDetails })}</pre>}
        </div>
      </div>
    )
  }

  return (
    <div className={`crt-work-root ${crtInsideMode ? 'crt-inside-mode' : ''} ${desktopProjection ? 'crt-desktop-projection' : ''} ${sliceWindow ? 'crt-work-slice' : ''} ${panelWindow ? 'crt-work-panel-window' : ''}`} style={slicePanelStyle}>
      <div className="crt-wallpaper-scene">
        <div className="crt-work-bg" />
        <div className="crt-scanlines" />
        <div className="crt-projection-content" ref={projectionPanelRef}>
          <header className="crt-topbar" onPointerDown={startSlicePanelDrag}>
            <span>AMADEUS AI WORK INTERFACE</span>
            <span className="crt-topbar-right">
              {connected ? 'ACTIVE SESSION' : 'BACKEND OFFLINE'} <span className="crt-dot" /> {new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
            </span>
          </header>

          {auipExperience && (
            <AuipExperienceCard
              experience={auipExperience}
              onModeChange={mode => {
                if (!auipExperience.appSessionId) return
                void send('auip.mode.set', {
                  app_session_id: auipExperience.appSessionId,
                  mode,
                })
              }}
              onStep={() => {
                if (!auipExperience.appSessionId) return
                void send('auip.step', {
                  app_session_id: auipExperience.appSessionId,
                  instruction: 'Take one appropriate declared action now.',
                })
              }}
              onLeave={() => {
                if (!auipExperience.appSessionId) return
                void send('auip.leave', {
                  app_session_id: auipExperience.appSessionId,
                  reason: 'user_left_from_slice',
                })
              }}
            />
          )}

          <main className="crt-focus-layout">
        <section
          className={`crt-task-dock ${planDockOpen ? 'open' : ''}`}
          aria-label="Plan drawer"
          onMouseEnter={() => setPlanDockOpen(true)}
          onMouseLeave={() => setPlanDockOpen(false)}
          onFocus={() => setPlanDockOpen(true)}
          onBlur={() => setPlanDockOpen(false)}
        >
          <div className="crt-panel crt-timeline-compact">
            <div className="crt-panel-label">Dynamic Timeline</div>
            <div className="crt-taskline">
              {TASK_STEPS.map((step, index) => {
                const state = inferStepState(activeRun, index)
                return (
                  <div key={step.key} className={`crt-task-step ${state}`}>
                    <span className="crt-task-index">{index + 1}</span>
                    <div>
                      <strong>{step.label}</strong>
                      <small>{state === 'complete' ? 'Done' : state === 'active' ? 'Now' : 'Next'}</small>
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        </section>

        <section className="crt-work-stage">
          <div className="crt-stage-status">
            <button
              className={`crt-provider-pill icon-only ${activeStatus.tone}`}
              onClick={() => setOverlay('trace')}
              title={`${(activeRun?.provider || activeWorkItem?.provider || provider).toUpperCase()} / ${activeStatus.label}`}
              aria-label={`${(activeRun?.provider || activeWorkItem?.provider || provider).toUpperCase()} / ${activeStatus.label}`}
            >
              <FluentIcon name="Robot" size={16} />
            </button>
            <button
              className={hasAttention ? 'attention icon-only' : 'icon-only'}
              onClick={() => setOverlay(activeAttention ? 'attention' : 'permission')}
              title={activeAttention ? 'Needs your choice' : hasAttention ? 'Intervention' : 'Permissions'}
              aria-label={activeAttention ? 'Needs your choice' : hasAttention ? 'Intervention' : 'Permissions'}
            >
              <FluentIcon name="Pin" size={15} />
            </button>
          </div>

          <div className="crt-turn-title-ribbon plan">
            <span>{workProjection ? 'Work Items' : 'Project Map'}</span>
            <strong>{activeWorkItem?.title || 'Turn Timeline / Work Plan'}</strong>
            <small>{workCountSummary}</small>
          </div>

          <div className="crt-turn-map-compact">
            <div className="crt-turn-capsules">
              {workProjection ? (
                <>
                  {(['current', 'projects', 'history'] as WorkListFilter[]).map(filter => (
                    <button
                      key={`filter-${filter}`}
                      type="button"
                      className={`crt-turn-capsule ${workListFilter === filter ? 'current' : ''}`}
                      onClick={() => setWorkListFilter(filter)}
                      title={`Show ${filter === 'projects' ? 'projects' : `${filter} tasks`}`}
                    >
                      {filter[0].toUpperCase() + filter.slice(1)}
                    </button>
                  ))}
                  {workListFilter === 'projects' ? visibleProjects.map(project => {
                    const latest = workProjection.items.find(item => item.id === project.latestWorkItemId)
                    const taskCount = project.current + project.history
                    return (
                      <button
                        key={project.id}
                        type="button"
                        className="crt-turn-capsule"
                        onClick={() => { if (latest) void selectWorkItem(latest) }}
                        disabled={!latest || workAction === 'focus'}
                        title={`${project.running} running / ${project.actionRequired} action required / ${taskCount} tasks`}
                      >
                        {project.name} · {project.latestTaskTitle || `${taskCount} tasks`}
                      </button>
                    )
                  }) : visibleWorkItems.map(item => (
                    <button
                      key={item.id}
                      type="button"
                      className={`crt-turn-capsule ${item.id === workProjection.selectedWorkItemId ? 'current' : ''} ${workItemCapsuleTone(item)}`}
                      onClick={() => { void selectWorkItem(item) }}
                      disabled={workAction === 'focus'}
                      title={`${item.state ? `${item.state} / ` : ''}${item.execution} / ${item.completion}${item.attention !== 'none' ? ` / ${item.attention}` : ''}${item.workspaceLabel ? ` - ${item.workspaceLabel}` : ''}${item.isolation ? ` / ${item.isolation}` : ''}${item.branch ? ` / Git branch ${item.branch}` : ''}`}
                    >
                      {workItemContextLabel(
                        item,
                        item.id === workProjection.selectedWorkItemId,
                        workItemBelongsToHistory(item, workProjection),
                      )} · {item.title}
                    </button>
                  ))}
                  <button
                    type="button"
                    className={`crt-turn-capsule ${workspaceFocusLocked ? 'current' : ''}`}
                    onClick={() => { void toggleWorkspaceFocus() }}
                    disabled={(!workspaceFocusLocked && !activeWorkItem) || workAction === 'focus'}
                    title={workspaceFocusLocked
                      ? `Workspace routing is locked to ${workspaceFocusPath || 'the pinned task directory'}. Click to unlock.`
                      : `Restore ${activeWorkItem?.workspacePath || 'the selected historical task directory'} as the workspace for future work.`}
                  >
                    {workAction === 'focus' ? 'Updating' : workspaceFocusLocked ? 'Unlock workspace' : 'Restore workspace'}
                  </button>
                </>
              ) : projectMap.turns.slice(-5).map(node => (
                <button
                  key={node.id}
                  type="button"
                  className={`crt-turn-capsule ${node.id === projectMap.currentTurnId ? 'current' : ''} ${node.status}`}
                  onClick={() => setSelectedRunId(node.id)}
                >
                  {node.title || 'Turn'}
                </button>
              ))}
            </div>
            <button
              className="crt-map-expand"
              onClick={() => setMapExpanded(value => !value)}
              title={mapExpanded ? 'Hide project map' : 'Expand project map'}
              aria-label={mapExpanded ? 'Hide project map' : 'Expand project map'}
            >
              <span />
            </button>
          </div>

          {mapExpanded && (
            <div className="crt-map-popover">
              {workProjection ? (
                <div className="crt-action-grid">
                  {workListFilter === 'projects' ? workProjection.projects.slice(0, 40).map(project => {
                    const latest = workProjection.items.find(item => item.id === project.latestWorkItemId)
                    const taskCount = project.current + project.history
                    return (
                      <button
                        key={project.id}
                        type="button"
                        onClick={() => { if (latest) void selectWorkItem(latest) }}
                        disabled={!latest || workAction === 'focus'}
                        title={`${project.running} running / ${project.actionRequired} action required / ${taskCount} tasks`}
                      >
                        {project.name} - {project.latestTaskTitle || `${taskCount} tasks`}
                      </button>
                    )
                  }) : filteredWorkItems.slice(0, 40).map(item => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => { void selectWorkItem(item) }}
                      disabled={workAction === 'focus'}
                      title={[item.workspacePath || item.workspaceLabel, item.branch ? `Git branch ${item.branch}` : '', item.isolation, item.selectionReason].filter(Boolean).join(' / ') || item.updatedAt}
                    >
                      {item.title} - {item.state ? `${item.state} / ` : ''}{item.execution} / {item.completion}{item.attention !== 'none' ? ` / ${item.attention}` : ''}{item.canResume ? ' / resume interrupted run' : item.canRetry ? ' / retry failed run' : ''}
                    </button>
                  ))}
                </div>
              ) : <ProjectStateMapView map={projectMap} onSelectTurn={setSelectedRunId} />}
            </div>
          )}

          <div className={`crt-turn-surface ${mockFrame ? `mock-${mockFrame.mode}` : ''}`}>
            <div className={`crt-turn-main ${mockFrame ? `mock-playback ${mockFrame.mode}` : ''}`}>
              <div className="crt-turn-kicker">{displayKicker}</div>
              {mockFrame && (
                <div className="crt-stage-index" aria-label="Work turn stage index">
                  {MOCK_STAGE_INDEX.map((stage, index) => (
                    <button
                      key={stage.mode}
                      type="button"
                      className={`${mockFrame.mode === stage.mode ? 'active' : ''} ${mockStageIndex > index ? 'complete' : ''}`}
                      title={stage.label}
                      aria-label={`Show ${stage.label} stage`}
                      onClick={() => jumpMockStage(stage.elapsedMs)}
                    >
                      <span />
                    </button>
                  ))}
                </div>
              )}
              {mockFrame?.mode === 'cue' ? (
                <>
                  <h1 className="crt-stage-title">{displayTitle}</h1>
                  <p className="crt-turn-voice-line cue-lead">{displayLead}</p>
                  {mockFrame.carryover && (
                    <div className="crt-carryover-md">
                      <span>{mockFrame.carryover.title}</span>
                      <ul>
                        {mockFrame.carryover.items.map(item => (
                          <li key={item}>{item}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  <div className="crt-cue-stack">
                  {mockFrame.captionLines.map((line, index) => (
                    <div key={line.id} className={`crt-cue-line ${line.state}`} style={{ animationDelay: `${index * 80}ms` }}>
                      <span>{line.label}</span>
                      <p>{line.text}</p>
                    </div>
                  ))}
                  </div>
                </>
              ) : (
                <>
                  <h2 className="crt-board-title">{displayTitle}</h2>
                  <p className="crt-turn-voice-line">{displayLead}</p>
                  {mockFrame?.mode === 'review' && mockFrame.reviewSummary ? (
                    <div className="crt-review-summary">
                      <section className="crt-review-copy">
                        <span>Turn Summary</span>
                        <div className="crt-review-narrative">
                          {mockFrame.reviewSummary.lines.map((line, index) => (
                            <div key={line.id} className="crt-review-narrative-line" style={{ animationDelay: `${index * 90}ms` }}>
                              <ReactMarkdown>{line.text}</ReactMarkdown>
                              <div className="crt-review-evidence-strip" aria-label="Summary evidence">
                                {line.evidence.map(item => (
                                  <button key={item} type="button" onClick={() => setOverlay('trace')}>
                                    {item}
                                  </button>
                                ))}
                              </div>
                            </div>
                          ))}
                        </div>
                      </section>
                      {mockFrame.reviewSummary.revealCards && (
                        <div className="crt-review-card-stack">
                          <section className="crt-review-file-index">
                            <span>File Index</span>
                            <div>
                              {mockFrame.reviewSummary.files.map(file => (
                                <button key={file.path} type="button" title={file.path} onClick={() => setOverlay('trace')}>
                                  <b>{file.label}</b>
                                  <small>{shortPath(file.path)}</small>
                                  <em>+{file.added} / -{file.removed}</em>
                                  <p>{file.note}</p>
                                </button>
                              ))}
                            </div>
                          </section>
                          <div className="crt-review-grid">
                            <section>
                              <span>Validation</span>
                              <ul>
                                {mockFrame.reviewSummary.validation.map(item => <li key={item}>{item}</li>)}
                              </ul>
                            </section>
                            <section>
                              <span>Watchpoints</span>
                              <ul>
                                {mockFrame.reviewSummary.watchpoints.map(item => <li key={item}>{item}</li>)}
                              </ul>
                            </section>
                          </div>
                          <div className="crt-review-actions">
                            {mockFrame.reviewSummary.nextActions.map(action => (
                              <button key={action} type="button" onClick={() => setOverlay('trace')}>
                                {action}
                              </button>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  ) : (
                  <div className={mockFrame ? 'crt-summary-stack' : 'crt-turn-feed'}>
                    {displayNarrationItems.map((item, index) => (
                      <div key={`${item.label}-${index}`} className={mockFrame ? `crt-summary-line ${item.state || 'expanded'}` : 'crt-turn-line'} style={mockFrame ? { animationDelay: `${index * 80}ms` } : undefined}>
                        <span>{item.label}</span>
                        <p>{item.text}</p>
                        {mockFrame && item.files && typeof item.added === 'number' && typeof item.removed === 'number' && (
                          <small className="crt-summary-delta" title={item.files.join('\n')} data-label={formatDeltaLabel(item)}>
                            {item.files.length} files · +{item.added} / -{item.removed}
                          </small>
                        )}
                        {!mockFrame && item.evidenceLabel && (
                          <small className="crt-summary-delta" title={item.evidenceLabel}>
                            {item.evidenceLabel}
                          </small>
                        )}
                        {mockFrame?.mode === 'active' && item.state === 'expanded' && item.patchLines && (
                          <div className={`crt-patch-stream ${item.previewKind === 'terminal' ? 'terminal' : 'diff'}`} aria-label={item.previewKind === 'terminal' ? 'Terminal result preview' : 'Streaming patch preview'}>
                            <div className="crt-patch-line-indicator" title="Current edit line">{item.currentLine}</div>
                            <div className="crt-patch-lines">
                              {item.patchLines.map((line, lineIndex) => (
                                <code key={`${item.label}-${lineIndex}`} className={line.kind}>
                                  <span>{line.line}</span>
                                  <b>{formatPreviewPrefix(line.kind)}{line.text}</b>
                                </code>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    ))}
                    {!!mockFrame?.thoughtLines.length && (
                      <div className="crt-work-note-stack">
                        {mockFrame.thoughtLines.map((line, index) => (
                          <div key={line.id} className="crt-work-note" style={{ animationDelay: `${index * 80}ms` }}>
                            <span>{line.label}</span>
                            <p>{line.text}</p>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                  )}
                </>
              )}
            </div>
            <div className="crt-turn-side">
              <div className="crt-character-ghost"><span>{mockFrame?.mode === 'cue' || displayTurn.phase === 'plan' ? 'DIRECTION CUE' : 'NARRATION'}</span></div>
              <div className="crt-turn-metrics">
                <button onClick={() => setOverlay('trace')}>
                  <span>Signals</span>
                  <b>{displayTurn.signals.length}</b>
                </button>
                <button onClick={() => setOverlay('trace')}>
                  <span>Evidence</span>
                  <b>{displayTurn.evidence.length}</b>
                </button>
                <button onClick={() => setOverlay('canvas')}>
                  <span>Canvas</span>
                  <b>{displayTurn.artifacts.length > 0 ? 'Ready' : 'Idle'}</b>
                </button>
              </div>
            </div>
          </div>

          <div className="crt-context-strip">
            {compactSignals.length === 0 ? (
              <p>WorkSignals will surface here as compact, readable signals.</p>
            ) : compactSignals.map(signal => (
              <button key={signal.id} onClick={() => setOverlay('trace')}>
                <span>{signal.phase}</span>
                <strong>{signal.title}</strong>
              </button>
            ))}
          </div>

          <button
            className={`crt-detail-toggle ${detailExpanded ? 'expanded' : ''}`}
            onClick={() => setDetailExpanded(value => !value)}
            title={detailExpanded ? 'Hide detailed log' : 'Expand detailed log'}
            aria-label={detailExpanded ? 'Hide detailed log' : 'Expand detailed log'}
          >
            <span />
          </button>
          <WorkDetailDrawer
            activeDetails={activeDetails}
            activeRun={activeRun}
            activeStatusLabel={activeStatus.label}
            activeWorkItem={activeWorkItem}
            auipLaunchArtifactId={auipLaunchArtifactId}
            auipLaunchFeedback={auipLaunchFeedback}
            auipExperience={auipExperience}
            canAcceptWork={canAcceptWork}
            canArchiveWork={canArchiveWork}
            destinationLabel={workProjection?.destinationLabel}
            destinationFeedback={workProjection?.destinationFeedback}
            cwd={cwd}
            detailEvents={detailEvents}
            expanded={detailExpanded}
            onAccept={() => { void acceptWorkItem() }}
            onArchive={() => { void archiveWorkItem() }}
            onCollapse={() => setDetailExpanded(false)}
            onLaunchAuip={(artifactId) => { void launchAuipApp(artifactId) }}
            onRetry={(amendmentText, authorizationPermissionRequestId) => {
              void retryWorkItem(amendmentText, authorizationPermissionRequestId)
            }}
            phaseLabel={displayTurn.phase}
            provider={provider}
            refreshDiff={refreshDiff}
            refreshStatus={refreshStatus}
            retryBusy={workAction === 'retry'}
            setOverlay={setOverlay}
            workItemDetail={workItemDetail}
          />
          {renderOverlay()}
        </section>
          </main>

          <footer className="crt-command-bar">
            <div className="crt-command-input">
              <FluentIcon name="CommandPrompt" size={15} />
              <textarea
                value={task}
                onChange={event => setTask(event.target.value)}
                placeholder="Describe the next instruction; it starts a new WorkItem"
              />
              <button onClick={submit} disabled={!connected || submitting || !!workAction || !task.trim()}>{submitting ? 'Starting' : activeWorkItem ? 'New' : 'Run'}</button>
            </div>
            <select value={provider} onChange={event => setProvider(event.target.value)}>
              {providers.map(item => <option key={item} value={item}>{item}</option>)}
            </select>
            <input
              value={workspaceFocusLocked ? workspaceFocusPath : cwd}
              onChange={event => setCwd(event.target.value)}
              placeholder={workspaceFocusLocked ? 'workspace routing is locked' : 'workspace for the next WorkItem'}
              aria-label="Workspace for a new WorkItem"
              title={workspaceFocusLocked
                ? `Future work is pinned to ${workspaceFocusPath || 'the selected task workspace'}. Unlock the workspace to route elsewhere.`
                : 'Workspace hint for the next semantic instruction.'}
              disabled={workspaceFocusLocked}
            />
            {activeRun?.status === 'running' ? (
              <button onClick={cancelRun} disabled={cancelPending}>
                {cancelPending ? 'Stopping...' : 'Cancel'}
              </button>
            ) : selectedWorkClosed ? (
              <button onClick={() => { void reopenWorkItem() }} disabled={!connected || !!workAction}>
                {workAction === 'reopen' ? 'Reopening' : 'Reopen'}
              </button>
            ) : activeWorkItem?.canResume ? (
              <button onClick={() => { void resumeWorkItem() }} disabled={!connected || !!workAction}>
                {workAction === 'resume' ? 'Resuming interrupted run' : 'Resume interrupted run'}
              </button>
            ) : activeWorkItem?.canRetry ? (
              <button
                onClick={() => {
                  void retryWorkItem('', activeWorkItem.retryAuthorizationRequestId)
                }}
                disabled={!connected || !!workAction}
              >
                {workAction === 'retry'
                  ? 'Retrying'
                  : activeWorkItem.retryAuthorizationRequestId
                    ? 'Authorize & Retry'
                    : 'Retry'}
              </button>
            ) : null}
            <button onClick={() => setOverlay(overlay === 'canvas' ? 'none' : 'canvas')} className={overlay === 'canvas' ? 'active' : ''}>Canvas</button>
            <button onClick={() => setOverlay(overlay === 'trace' ? 'none' : 'trace')} className={overlay === 'trace' ? 'active' : ''}>Trace</button>
            <button onClick={() => setOverlay(overlay === 'permission' ? 'none' : 'permission')} className={overlay === 'permission' ? 'active' : ''}>Actions</button>
            <button onClick={() => setOverlay(overlay === 'audit' ? 'none' : 'audit')} className={overlay === 'audit' ? 'active' : ''}>Audit</button>
            <button
              onClick={openDesktopProjection}
              className={desktopProjection ? 'active' : ''}
              title={desktopProjection ? 'Close desktop work slice' : 'Open desktop work slice'}
            >
              CRT
            </button>
          </footer>
        </div>
      </div>
    </div>
  )
}
