import type { ProviderRun, WorkDockItem, WorkProjectSummary, WorkProjection } from './types'

export const ELECTRON_WORK_SURFACE = 'electron.work'

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function text(value: unknown): string {
  return value === undefined || value === null ? '' : String(value).trim()
}

function count(value: unknown): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.max(0, Math.floor(parsed)) : 0
}

function normalizeItem(value: unknown): WorkDockItem | null {
  const item = record(value)
  const activity = record(item.activity || item.activity_snapshot)
  const activityLiveness = record(activity.liveness)
  const activitySteering = record(activity.steering)
  const id = text(item.id || item.workItemId || item.work_item_id)
  if (!id) return null
  return {
    id,
    title: text(item.title) || 'Untitled task',
    execution: text(item.execution || item.executionStatus || item.execution_status).toLowerCase() || 'unknown',
    activity: Object.keys(activity).length
      ? {
          phase: text(activity.phase).toLowerCase() || 'unknown',
          elapsedSeconds: count(activity.elapsedSeconds ?? activity.elapsed_seconds),
          silentSeconds: count(activity.silentSeconds ?? activity.silent_seconds),
          lastEventAt: text(activity.lastEventAt || activity.last_event_at) || undefined,
          lastEventType: text(activity.lastEventType || activity.last_event_type) || undefined,
          semanticSummary: text(activity.semanticSummary || activity.latestSemanticSummary || activity.semantic_summary) || undefined,
          lastTool: text(activity.lastTool || activity.last_tool) || undefined,
          toolCount: count(activity.toolCount ?? activity.tool_count),
          artifactCount: count(activity.artifactCount ?? activity.artifact_count),
          uncertainty: text(activity.uncertainty) || undefined,
          liveness: Object.keys(activityLiveness).length ? activityLiveness : undefined,
          steering: Object.keys(activitySteering).length
            ? {
                state: text(activitySteering.state).toLowerCase() || undefined,
                revision: count(activitySteering.revision),
                safeBoundary: text(activitySteering.safeBoundary || activitySteering.safe_boundary) || undefined,
              }
            : undefined,
        }
      : undefined,
    liveness: text(item.liveness).toLowerCase() || undefined,
    livenessStage: text(item.livenessStage || item.liveness_stage) || undefined,
    probeStatus: text(item.probeStatus || item.probe_status) || undefined,
    silentForSeconds: item.silentForSeconds === undefined && item.silent_for_seconds === undefined
      ? undefined
      : count(item.silentForSeconds ?? item.silent_for_seconds),
    lastProviderEventAt: text(item.lastProviderEventAt || item.last_provider_event_at) || undefined,
    completion: text(item.completion || item.completeness).toLowerCase() || 'unknown',
    attention: text(item.attention).toLowerCase() || 'none',
    workspaceLabel: text(item.workspaceLabel || item.workspace_label),
    workspacePath: text(item.workspacePath || item.workspace_path) || undefined,
    workspaceMode: text(item.workspaceMode || item.workspace_mode) || undefined,
    branch: text(item.branch) || undefined,
    isolation: text(item.isolation) || undefined,
    selectionReason: text(item.selectionReason || item.selection_reason) || undefined,
    writerLeaseStatus: text(item.writerLeaseStatus || item.writer_lease_status) || undefined,
    artifactCount: item.artifactCount === undefined && item.artifact_count === undefined
      ? undefined
      : count(item.artifactCount ?? item.artifact_count),
    workspaceExists: item.workspaceExists === undefined && item.workspace_exists === undefined
      ? undefined
      : item.workspaceExists === true || item.workspace_exists === true,
    updatedAt: text(item.updatedAt || item.updated_at),
    projectId: text(item.projectId || item.project_id) || undefined,
    projectName: text(item.projectName || item.project_name) || undefined,
    projectState: text(item.projectState || item.project_state) || undefined,
    canRetry: item.canRetry === true || item.can_retry === true,
    canResume: item.canResume === true || item.can_resume === true,
    canReopen: item.canReopen === true || item.can_reopen === true,
    isScratch: item.isScratch === true || item.is_scratch === true,
    canPromoteToProject:
      item.canPromoteToProject === true || item.can_promote_to_project === true,
    currentRunId: text(item.currentRunId || item.current_run_id || item.providerRunId || item.provider_run_id) || undefined,
    sessionId: text(item.sessionId || item.session_id) || undefined,
    state: text(item.state || item.workItemState || item.work_item_state || item.disposition).toLowerCase() || undefined,
    provider: text(item.provider).toLowerCase() || undefined,
    mode: text(item.mode).toLowerCase() || undefined,
  }
}

function normalizeProject(value: unknown): WorkProjectSummary | null {
  const project = record(value)
  const counts = record(project.counts)
  const id = text(project.id || project.projectId || project.project_id)
  if (!id) return null
  return {
    id,
    name: text(project.name || project.projectName || project.project_name) || 'Untitled project',
    latestWorkItemId: text(project.latestWorkItemId || project.latest_work_item_id) || undefined,
    latestTaskTitle: text(project.latestTaskTitle || project.latest_task_title) || undefined,
    current: count(counts.current),
    running: count(counts.running),
    actionRequired: count(counts.needsYou ?? counts.needs_you),
    history: count(counts.history),
  }
}

export function normalizeWorkProjection(value: unknown): WorkProjection | null {
  const projection = record(value)
  if (!Object.keys(projection).length) return null
  const rawCounts = record(projection.counts)
  const rawDestinationFeedback = record(
    projection.destinationFeedback || projection.destination_feedback,
  )
  const items = (Array.isArray(projection.items) ? projection.items : [])
    .map(normalizeItem)
    .filter((item): item is WorkDockItem => item !== null)
    .slice(0, 100)
  const projects = (Array.isArray(projection.projects) ? projection.projects : [])
    .map(normalizeProject)
    .filter((project): project is WorkProjectSummary => project !== null)
    .slice(0, 100)
  return {
    revision: text(projection.revision),
    currentSessionId: text(projection.currentSessionId || projection.current_session_id) || undefined,
    selectedWorkItemId: text(projection.selectedWorkItemId || projection.selected_work_item_id),
    focusMode: text(projection.focusMode || projection.focus_mode).toLowerCase() === 'pinned' ? 'pinned' : 'auto',
    workspaceFocusMode: text(projection.workspaceFocusMode || projection.workspace_focus_mode).toLowerCase() === 'pinned' ? 'pinned' : 'auto',
    workspaceFocusPath: text(projection.workspaceFocusPath || projection.workspace_focus_path) || undefined,
    workspaceFocusWorkItemId: text(projection.workspaceFocusWorkItemId || projection.workspace_focus_work_item_id) || undefined,
    destinationLabel: text(projection.destinationLabel || projection.destination_label) || undefined,
    destinationFeedback: text(rawDestinationFeedback.message)
      ? {
          status: text(rawDestinationFeedback.status).toLowerCase() || 'info',
          message: text(rawDestinationFeedback.message),
        }
      : undefined,
    counts: {
      running: count(rawCounts.running),
      needsAttention: count(rawCounts.needsAttention ?? rawCounts.needs_attention),
      active: count(rawCounts.active),
    },
    projects,
    items,
  }
}

export function projectionFromEnvelope(value: unknown): WorkProjection | null {
  const envelope = record(value)
  return normalizeWorkProjection(envelope.work || envelope.projection || value)
}

export function envelopeMatchesSurface(value: unknown, surface: string): boolean {
  const envelope = record(value)
  const projection = record(envelope.work || envelope.projection)
  const requestedSurface = text(envelope.surface || projection.surface)
  return !requestedSurface || requestedSurface === surface
}

export function runWorkItemId(run: ProviderRun): string {
  const metadata = record(run.metadata)
  const binding = record(metadata.work)
  return text(binding.work_item_id || binding.workItemId || metadata.work_item_id || metadata.workItemId)
}

export function resolveWorkItemRunId(item: WorkDockItem | undefined, runs: ProviderRun[]): string {
  if (!item) return ''
  if (item.currentRunId) return item.currentRunId
  return runs.find(run => runWorkItemId(run) === item.id)?.run_id || ''
}

export function workItemNeedsAttention(item: WorkDockItem | undefined): boolean {
  if (!item) return false
  return !['', 'none', 'clear', 'resolved', 'dismissed'].includes(item.attention)
}

export function attentionActionLabel(attention: string | undefined): string {
  const value = text(attention).toLowerCase()
  if (value === 'permission') return 'Approval required'
  if (value === 'input') return 'Input required'
  if (value === 'conflict') return 'Resolve conflict'
  if (value === 'review') return 'Review ready'
  if (value === 'error') return 'Inspect failure'
  return value && value !== 'none' ? 'Action required' : ''
}

export function workItemBelongsToCurrentSession(
  item: WorkDockItem | undefined,
  projection: WorkProjection | null,
): boolean {
  if (!item) return false
  const currentSessionId = text(projection?.currentSessionId)
  if (!currentSessionId) return !workItemIsClosed(item)
  return text(item.sessionId) === currentSessionId
}

export function workItemBelongsToHistory(
  item: WorkDockItem | undefined,
  projection: WorkProjection | null,
): boolean {
  if (!item) return false
  const currentSessionId = text(projection?.currentSessionId)
  if (!currentSessionId) return workItemIsClosed(item)
  return text(item.sessionId) !== currentSessionId
}

export function workItemIsClosed(item: WorkDockItem | undefined): boolean {
  return !!item && ['accepted', 'archived', 'closed'].includes(item.state || '')
}

export function workItemCapsuleTone(item: WorkDockItem): string {
  if (workItemIsClosed(item)) return 'history'
  if (item.liveness === 'stalled' || item.liveness === 'cancel_pending') return 'review'
  if (item.attention === 'review') return 'review'
  if (workItemNeedsAttention(item)) return 'blocked'
  if (item.completion === 'complete') return 'review'
  return item.execution === 'failed' ? 'reverted' : item.execution
}

export function workCountsLabel(projection: WorkProjection | null, runs: ProviderRun[]): string {
  if (projection) {
    const current = projection.items.filter(item => workItemBelongsToCurrentSession(item, projection)).length
    const actionRequired = projection.items.filter(item => workItemNeedsAttention(item)).length
    return `${projection.counts.running} running / ${actionRequired} action required / ${current} current`
  }
  const running = runs.filter(run => run.status === 'queued' || run.status === 'running').length
  const actionRequired = runs.filter(run => run.status === 'error' || run.status === 'orphaned').length
  return `${running} running / ${actionRequired} action required / ${runs.length} runs`
}
