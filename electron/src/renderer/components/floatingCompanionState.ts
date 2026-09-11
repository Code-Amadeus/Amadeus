import type { ProviderEvent, ProviderRun } from './work/types'

export type FloatingCompanionPhase = 'idle' | 'running' | 'attention' | 'ready' | 'blocked'

export type FloatingCompanionTask = {
  id: string
  provider: string
  title: string
  detail: string
  phase: FloatingCompanionPhase
  key: string
  repeatable: boolean
  announce?: boolean
  sourceLabel?: string
  codexThreadId?: string
  projectName?: string
  projectId?: string
  parentTaskId?: string
  sourceKind?: 'task' | 'subagent' | 'sidechat'
  activities?: Array<{ id: string; kind: 'progress' | 'tool' | 'result'; text: string; at: number; status?: string }>
  /** Epoch milliseconds from the source's latest actual task activity. */
  lastActivityAt?: number
  /** View-only ancestor anchor. Never enters the notification queue. */
  contextOnly?: boolean
}

export type CompanionTaskContext = Pick<FloatingCompanionTask, 'id' | 'title' | 'parentTaskId' | 'projectId' | 'projectName'>

/** Project the identities required by visible branches AFTER retention/ack policy. */
export function withCompanionContexts(visible: FloatingCompanionTask[], contexts: CompanionTaskContext[]): FloatingCompanionTask[] {
  const result = new Map(visible.map(task => [task.id, task]))
  const identities = new Map(contexts.map(task => [task.id, task]))
  for (const task of visible) {
    let parent = task.parentTaskId
    const seen = new Set([task.id])
    while (parent && !seen.has(parent)) {
      seen.add(parent)
      const identity = identities.get(parent)
      if (!identity) break
      if (!result.has(parent)) result.set(parent, { id: identity.id, title: identity.title, parentTaskId: identity.parentTaskId,
        projectId: identity.projectId, projectName: identity.projectName, provider: task.provider, detail: '',
        phase: 'idle', key: `context:${parent}`, repeatable: false, announce: false, contextOnly: true,
        codexThreadId: task.codexThreadId ? parent : undefined, sourceKind: identity.parentTaskId ? 'sidechat' : 'task' })
      parent = identity.parentTaskId
    }
  }
  return [...result.values()]
}

export type FloatingCompanionPresence = {
  phase: FloatingCompanionPhase
  label: string
  headline: string
  detail: string
  running: number
  needsAttention: number
  tasks: FloatingCompanionTask[]
}

export function companionTaskHeading(task: FloatingCompanionTask): string {
  return task.projectName?.trim() || task.title
}

/** Stable per-task variation: status updates must never reshuffle the cards. */
export function companionCardOffset(id: string): { x: number; y: number } {
  let hash = 2166136261
  for (const character of id) hash = Math.imul(hash ^ character.charCodeAt(0), 16777619) >>> 0
  return { x: hash % 57, y: (hash >>> 8) % 85 }
}

export type CompanionTaskGroup = { id: string; title: string; tasks: FloatingCompanionTask[] }

export function groupCompanionTasks(tasks: FloatingCompanionTask[]): CompanionTaskGroup[] {
  const groups = new Map<string, CompanionTaskGroup>()
  const byId = new Map(tasks.map(task => [task.id, task]))
  for (const task of tasks) {
    let root = task
    const seen = new Set([root.id])
    while (root.parentTaskId && byId.has(root.parentTaskId) && !seen.has(root.parentTaskId)) {
      root = byId.get(root.parentTaskId)!
      seen.add(root.id)
    }
    const projectId = task.projectId || root.projectId
    // An inactive/expired parent need not have a visible card. Its side tasks
    // still share that conversation's group, including for projectless work.
    const id = projectId ? `project:${projectId}` : `task:${root.parentTaskId || root.id}`
    const group = groups.get(id) || { id, title: task.projectName || root.projectName || root.title, tasks: [] }
    group.tasks.push(task)
    groups.set(id, group)
  }
  return [...groups.values()]
}

const TERMINAL_EVENT_STATUS: Record<string, ProviderRun['status']> = {
  'run.finished': 'done',
  'run.failed': 'error',
  'run.cancelled': 'cancelled',
}

function unresolvedPermission(run: ProviderRun): ProviderEvent | undefined {
  if (!['queued', 'running'].includes(run.status)) return undefined
  const latestPermissionEvent = [...(run.events || [])].reverse().find(event => (
    event.type.startsWith('permission.')
  ))
  return latestPermissionEvent && [
    'permission.requested',
    'permission.required',
  ].includes(latestPermissionEvent.type)
    ? latestPermissionEvent
    : undefined
}

function runPhase(run: ProviderRun): FloatingCompanionPhase {
  if (unresolvedPermission(run)) return 'attention'
  if (run.status === 'error' || run.status === 'orphaned') return 'blocked'
  if (run.status === 'queued' || run.status === 'running') return 'running'
  if (run.status === 'done') return 'ready'
  return 'idle'
}

function latestDetail(run: ProviderRun): string {
  const permission = unresolvedPermission(run)?.payload?.permissionRequest
  if (permission && typeof permission === 'object') {
    const request = permission as Record<string, unknown>
    return String(request.reason || request.command || '需要你确认操作')
  }
  if (run.status === 'done') return String(run.result || '本轮结束，可以查看结果。').trim()
  const event = [...(run.events || [])].reverse().find(candidate => (
    ['semantic.progress', 'assistant.update', 'tool.call', 'permission.requested', 'run.failed']
      .includes(candidate.type)
  ))
  if (event) {
    const payload = event.payload || {}
    const value = payload.text || payload.summary || payload.tool || payload.name
      || payload.command || payload.reason || payload.error || payload.status
    if (value) return String(value).trim()
  }
  return String(run.error || run.result || '').trim()
}

function taskFromRun(run: ProviderRun): FloatingCompanionTask {
  const phase = runPhase(run)
  const permission = unresolvedPermission(run)?.payload?.permissionRequest as Record<string, unknown> | undefined
  return {
    id: run.run_id,
    provider: run.provider,
    title: run.task || `${run.provider} task`,
    detail: latestDetail(run),
    lastActivityAt: run.events?.reduce<number | undefined>((latest, event) => (
      event.time_ms === undefined ? latest : Math.max(latest ?? 0, event.time_ms)
    ), undefined),
    phase,
    key: `${run.run_id}:${phase}:${String(permission?.request_id || '')}`,
    // Provider runs have no Codex Desktop read receipt. Never invent one.
    repeatable: phase === 'attention' || phase === 'blocked',
    // These are Amadeus-owned runs, already narrated by WorkObserver. Display
    // them while the external Desktop source is unavailable, without a second
    // voice lane repeating the same Work output.
    announce: false,
  }
}

function phasePriority(phase: FloatingCompanionPhase): number {
  return ({ attention: 0, running: 1, blocked: 2, ready: 3, idle: 4 })[phase]
}

export function upsertCompanionRun(runs: ProviderRun[], run: ProviderRun): ProviderRun[] {
  const index = runs.findIndex(candidate => candidate.run_id === run.run_id)
  if (index < 0) return [run, ...runs]
  const next = [...runs]
  const previous = next[index]
  next[index] = {
    ...previous,
    ...run,
    events: run.events?.length ? run.events : previous.events,
  }
  return next
}

export function applyCompanionProviderEvent(
  runs: ProviderRun[],
  event: ProviderEvent,
): ProviderRun[] {
  const existing = runs.find(run => run.run_id === event.run_id)
  const status = TERMINAL_EVENT_STATUS[event.type]
    || existing?.status
    || 'running'
  const nextRun: ProviderRun = {
    run_id: event.run_id,
    provider: event.provider,
    task: existing?.task || '',
    cwd: existing?.cwd || String(event.payload?.cwd || '') || null,
    status,
    result: existing?.result || '',
    error: event.type === 'run.failed'
      ? String(event.payload?.error || existing?.error || 'Provider run failed')
      : existing?.error,
    metadata: existing?.metadata || event.metadata || {},
    events: [...(existing?.events || []), event].slice(-80),
  }
  return upsertCompanionRun(runs, nextRun)
}

export function deriveFloatingCompanionPresence(
  runs: ProviderRun[],
): FloatingCompanionPresence {
  const tasks = runs
    .map(taskFromRun)
    .filter(task => task.phase !== 'idle')
    .sort((left, right) => phasePriority(left.phase) - phasePriority(right.phase))
  const running = tasks.filter(task => task.phase === 'running' || task.phase === 'attention').length
  const needsAttention = tasks.filter(task => task.phase === 'attention').length
  const newest = taskFromRun(runs[0] || {
    run_id: 'idle',
    provider: '',
    task: '',
    status: 'cancelled',
  })

  if (needsAttention) {
    const task = tasks.find(candidate => candidate.phase === 'attention')!
    return {
      phase: 'attention',
      label: 'Needs you',
      headline: task.title,
      detail: task.detail || 'Codex needs an approval or another decision.',
      running,
      needsAttention,
      tasks,
    }
  }
  if (running) {
    const task = tasks.find(candidate => candidate.phase === 'running')!
    return {
      phase: 'running',
      label: running > 1 ? `${running} tasks running` : 'Working',
      headline: task.title,
      detail: task.detail || 'Waiting for the next observable provider event.',
      running,
      needsAttention,
      tasks,
    }
  }
  if (newest.phase === 'blocked') {
    return {
      phase: 'blocked',
      label: 'Blocked',
      headline: newest.title,
      detail: newest.detail || 'The latest provider run failed.',
      running,
      needsAttention,
      tasks,
    }
  }
  if (newest.phase === 'ready') {
    return {
      phase: 'ready',
      label: 'Ready',
      headline: newest.title,
      detail: newest.detail || 'The latest provider run has finished.',
      running,
      needsAttention,
      tasks,
    }
  }
  return {
    phase: 'idle',
    label: 'Standing by',
    headline: 'Amadeus is here',
    detail: 'Open the full interface to chat or start work.',
    running: 0,
    needsAttention: 0,
    tasks: [],
  }
}
