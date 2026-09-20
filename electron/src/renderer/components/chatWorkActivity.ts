export type ChatWorkActivityStatus = 'running' | 'succeeded' | 'failed' | 'cancelled' | 'stalled'
export type ChatWorkActivityEntryState = 'running' | 'succeeded' | 'failed' | 'attention' | 'info'
export type ChatWorkActivityEntryKind = 'command' | 'file' | 'validation' | 'permission' | 'artifact' | 'milestone' | 'status'

export interface ChatWorkActivityEntry {
  id: string
  kind: ChatWorkActivityEntryKind
  state: ChatWorkActivityEntryState
  title: string
  detail?: string
  observedAt: number
}

export interface ChatWorkActivityRun {
  runId: string
  provider: string
  sessionId: string
  turnId: string
  workItemId?: string
  attemptId?: string
  task?: string
  status: ChatWorkActivityStatus
  startedAt: number
  updatedAt: number
  entries: ChatWorkActivityEntry[]
}

const TERMINAL_STATUSES = new Set(['done', 'succeeded', 'success', 'failed', 'error', 'cancelled', 'canceled'])
const TERMINAL_PERMISSION_EVENTS = new Set([
  'permission.allowed',
  'permission.approved',
  'permission.granted',
  'permission.denied',
  'permission.rejected',
  'permission.resolved',
  'permission.expired',
])
const VALIDATION_COMMAND = /(?:^|\s)(?:pytest|python\s+-m\s+(?:pytest|unittest)|npm\s+(?:test|run\s+(?:test|build|lint))|pnpm\s+(?:test|build|lint)|yarn\s+(?:test|build|lint)|cargo\s+test|go\s+test|dotnet\s+test)(?:\s|$)/i
const FILE_TOOL = /(?:file|write|edit|patch)/i

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function cleanText(value: unknown, limit = 2400): string {
  const raw = typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value)
  return raw
    .replace(/(bearer\s+)[a-z0-9._~+\/-]+/gi, '$1•••')
    .replace(/((?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+/gi, '$1•••')
    .slice(0, limit)
    .trim()
}

function eventTime(value: Record<string, unknown>): number {
  const observed = Number(value.observed_at || value.observedAt || 0)
  if (Number.isFinite(observed) && observed > 0) return observed * 1000
  return Date.now()
}

function origin(value: Record<string, unknown>) {
  const metadata = record(value.metadata)
  const work = record(metadata.work)
  return {
    sessionId: String(metadata.session_id || metadata.sessionId || ''),
    turnId: String(metadata.turn_id || metadata.turnId || ''),
    workItemId: String(value.task_id || work.work_item_id || work.workItemId || ''),
    attemptId: String(value.attempt_id || work.attempt_id || work.attemptId || ''),
  }
}

function normalizedStatus(value: unknown): ChatWorkActivityStatus {
  const status = String(value || '').toLowerCase()
  if (status === 'failed' || status === 'error') return 'failed'
  if (status === 'cancelled' || status === 'canceled') return 'cancelled'
  if (status === 'done' || status === 'succeeded' || status === 'success') return 'succeeded'
  if (status === 'stalled') return 'stalled'
  return 'running'
}

function toolName(payload: Record<string, unknown>): string {
  return String(payload.name || payload.tool || 'tool')
}

function toolInput(payload: Record<string, unknown>): Record<string, unknown> {
  return record(payload.input || payload.arguments || payload.raw)
}

function commandFrom(payload: Record<string, unknown>): string {
  const input = toolInput(payload)
  return cleanText(input.command || payload.command || '', 1400)
}

function changeRows(payload: Record<string, unknown>): Record<string, unknown>[] {
  const input = toolInput(payload)
  const raw = input.changes || payload.changes || payload.changed_files || payload.files
  if (!Array.isArray(raw)) return []
  return raw.map(record).filter(item => Object.keys(item).length > 0)
}

function fileSummary(payload: Record<string, unknown>): { title: string; detail: string } {
  const rows = changeRows(payload)
  const paths = rows
    .map(item => String(item.path || item.file || item.name || '').trim())
    .filter(Boolean)
  const direct = String(payload.path || payload.file || toolInput(payload).path || '').trim()
  if (direct && !paths.includes(direct)) paths.push(direct)
  const patch = cleanText(
    rows.map(item => item.diff || item.patch || '').filter(Boolean).join('\n')
      || payload.diff
      || payload.patch
      || '',
    3200,
  )
  const range = /@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@/.exec(patch)
  const lineLabel = range
    ? ` · lines ${range[1]}${Number(range[2] || 1) > 1 ? `–${Number(range[1]) + Number(range[2]) - 1}` : ''}`
    : ''
  const shown = paths.slice(0, 3).join(', ')
  const suffix = paths.length > 3 ? ` +${paths.length - 3}` : ''
  return {
    title: `${shown || 'Project files'}${suffix}${lineLabel}`,
    detail: patch,
  }
}

function entryForEvent(event: Record<string, unknown>): ChatWorkActivityEntry | null {
  const type = String(event.type || '').toLowerCase()
  const payload = record(event.payload)
  const observedAt = eventTime(event)
  const itemId = String(payload.item_id || payload.tool_id || payload.call_id || event.sequence || observedAt)
  const name = toolName(payload)
  if (type === 'tool.call' || type === 'tool.result') {
    const command = commandFrom(payload)
    const isFile = FILE_TOOL.test(name) || changeRows(payload).length > 0
    const isValidation = Boolean(command && VALIDATION_COMMAND.test(command))
    const success = payload.success !== false && !['failed', 'error'].includes(String(payload.status || '').toLowerCase())
    const state: ChatWorkActivityEntryState = type === 'tool.call' ? 'running' : success ? 'succeeded' : 'failed'
    if (isFile) {
      const file = fileSummary(payload)
      return {
        id: `tool:${itemId}`,
        kind: 'file',
        state,
        title: `${type === 'tool.call' ? 'Editing' : success ? 'Updated' : 'Could not update'} ${file.title}`,
        detail: file.detail || cleanText(payload.output),
        observedAt,
      }
    }
    return {
      id: `tool:${itemId}`,
      kind: isValidation ? 'validation' : 'command',
      state,
      title: command || `${type === 'tool.call' ? 'Running' : success ? 'Finished' : 'Failed'} ${name}`,
      detail: type === 'tool.result' ? cleanText(payload.output || payload.result, 3200) : '',
      observedAt,
    }
  }
  if (type === 'semantic.progress') {
    const summary = cleanText(payload.summary || payload.text, 600)
    if (!summary) return null
    return {
      id: `milestone:${String(payload.milestone || 'progress')}:${summary}`,
      kind: String(payload.milestone || '').toLowerCase() === 'validation' ? 'validation' : 'milestone',
      state: 'info',
      title: summary,
      observedAt,
    }
  }
  if (type === 'permission.requested' || type === 'permission.required') {
    return {
      id: `permission:${String(payload.request_id || payload.requestId || itemId)}`,
      kind: 'permission',
      state: 'attention',
      title: cleanText(payload.reason || payload.action || 'Provider needs permission', 500),
      detail: cleanText(payload.scope, 1200),
      observedAt,
    }
  }
  if (TERMINAL_PERMISSION_EVENTS.has(type)) {
    const resolution = String(
      payload.status || payload.decision || type.slice('permission.'.length),
    ).toLowerCase()
    const allowed = ['allow', 'allowed', 'allow_once', 'approved', 'granted'].includes(resolution)
    const expired = type === 'permission.expired' || resolution === 'expired'
    const denied = ['deny', 'denied', 'rejected'].includes(resolution)
    return {
      id: `permission:${String(payload.request_id || payload.requestId || itemId)}`,
      kind: 'permission',
      state: allowed ? 'succeeded' : denied || expired ? 'failed' : 'info',
      title: allowed
        ? 'Permission allowed'
        : expired
          ? 'Permission expired'
          : denied
            ? 'Permission denied'
            : 'Permission resolved',
      observedAt,
    }
  }
  if (type === 'artifact.created' || type === 'diff.updated') {
    const files = fileSummary(payload)
    return {
      id: `artifact:${String(payload.artifact_id || payload.id || itemId)}`,
      kind: 'artifact',
      state: 'succeeded',
      title: cleanText(payload.title || payload.name || files.title || 'Artifact ready', 500),
      detail: files.detail,
      observedAt,
    }
  }
  if (type === 'run.status') {
    const stage = String(payload.stage || payload.liveness || payload.status || '').trim()
    if (!stage || stage === 'running' || stage === 'active') return null
    return {
      id: `status:${stage}:${event.sequence || observedAt}`,
      kind: 'status',
      state: stage === 'stalled' || stage === 'cancel_pending' ? 'attention' : 'info',
      title: stage.replaceAll('_', ' '),
      observedAt,
    }
  }
  return null
}

function upsertEntry(entries: ChatWorkActivityEntry[], entry: ChatWorkActivityEntry): ChatWorkActivityEntry[] {
  const index = entries.findIndex(item => item.id === entry.id)
  const existing = index >= 0 ? entries[index] : undefined
  const merged = existing && entry.id.startsWith('tool:')
    ? {
        ...existing,
        ...entry,
        title: entry.kind === 'file'
          ? !entry.title.includes('Project files')
            ? entry.title
            : existing.title.replace(
                /^(Editing|Updated|Could not update)\s+/,
                entry.state === 'succeeded' ? 'Updated ' : entry.state === 'failed' ? 'Could not update ' : 'Editing ',
              )
          : existing.title,
        detail: entry.detail || existing.detail,
      }
    : entry
  const next = index >= 0
    ? entries.map((item, offset) => offset === index ? merged : item)
    : [...entries, merged]
  return next.sort((left, right) => left.observedAt - right.observedAt).slice(-80)
}

export function applyProviderEvent(
  runs: ChatWorkActivityRun[],
  rawEvent: unknown,
): ChatWorkActivityRun[] {
  const event = record(rawEvent)
  const runId = String(event.run_id || event.runId || '')
  const provider = String(event.provider || '')
  if (!runId || !provider || event.replay === true) return runs
  const metadata = record(event.metadata)
  if (metadata.replay === true) return runs
  const eventOrigin = origin(event)
  if (!eventOrigin.sessionId || !eventOrigin.turnId) return runs
  const payload = record(event.payload)
  const type = String(event.type || '').toLowerCase()
  const at = eventTime(event)
  const index = runs.findIndex(item => item.runId === runId)
  const current: ChatWorkActivityRun = index >= 0 ? runs[index] : {
    runId,
    provider,
    sessionId: eventOrigin.sessionId,
    turnId: eventOrigin.turnId,
    workItemId: eventOrigin.workItemId || undefined,
    attemptId: eventOrigin.attemptId || undefined,
    task: cleanText(payload.task, 800),
    status: 'running',
    startedAt: at,
    updatedAt: at,
    entries: [],
  }
  let status = current.status
  if (type === 'run.failed') status = 'failed'
  else if (type === 'run.cancelled') status = 'cancelled'
  else if (type === 'run.finished') status = normalizedStatus(payload.status || 'done')
  else if (type === 'run.status') {
    const next = normalizedStatus(payload.status || payload.liveness)
    status = next === 'running' && String(payload.liveness || '') === 'stalled' ? 'stalled' : next
  }
  const entry = entryForEvent(event)
  const updated: ChatWorkActivityRun = {
    ...current,
    provider,
    sessionId: eventOrigin.sessionId,
    turnId: eventOrigin.turnId,
    workItemId: eventOrigin.workItemId || current.workItemId,
    attemptId: eventOrigin.attemptId || current.attemptId,
    task: cleanText(payload.task, 800) || current.task,
    status,
    updatedAt: Math.max(current.updatedAt, at),
    entries: entry ? upsertEntry(current.entries, entry) : current.entries,
  }
  return index >= 0
    ? runs.map((item, offset) => offset === index ? updated : item)
    : [...runs, updated]
}

export function applyProviderResult(
  runs: ChatWorkActivityRun[],
  rawResult: unknown,
): ChatWorkActivityRun[] {
  const result = record(rawResult)
  const valueOrigin = origin(result)
  const runId = String(result.run_id || result.runId || '')
  const provider = String(result.provider || '')
  if (!runId || !provider || !valueOrigin.sessionId || !valueOrigin.turnId) return runs
  const status = normalizedStatus(result.status)
  const at = Number(result.updated_at || 0) > 0 ? Number(result.updated_at) * 1000 : Date.now()
  const index = runs.findIndex(item => item.runId === runId)
  const current = index >= 0 ? runs[index] : {
    runId,
    provider,
    sessionId: valueOrigin.sessionId,
    turnId: valueOrigin.turnId,
    workItemId: valueOrigin.workItemId || undefined,
    attemptId: valueOrigin.attemptId || undefined,
    task: cleanText(result.task, 800),
    status: 'running' as ChatWorkActivityStatus,
    startedAt: Number(result.created_at || 0) > 0 ? Number(result.created_at) * 1000 : at,
    updatedAt: at,
    entries: [],
  }
  const detail = cleanText(result.error || result.result, 3200)
  const terminal: ChatWorkActivityEntry = {
    id: 'terminal',
    kind: 'status',
    state: status === 'succeeded' ? 'succeeded' : status === 'cancelled' ? 'attention' : 'failed',
    title: status === 'succeeded' ? 'Work completed' : status === 'cancelled' ? 'Work cancelled' : 'Work failed',
    detail,
    observedAt: at,
  }
  const updated: ChatWorkActivityRun = {
    ...current,
    provider,
    sessionId: valueOrigin.sessionId,
    turnId: valueOrigin.turnId,
    workItemId: valueOrigin.workItemId || current.workItemId,
    attemptId: valueOrigin.attemptId || current.attemptId,
    task: cleanText(result.task, 800) || current.task,
    status,
    updatedAt: at,
    entries: upsertEntry(current.entries, terminal),
  }
  return index >= 0
    ? runs.map((item, offset) => offset === index ? updated : item)
    : [...runs, updated]
}

export function activitiesFromProviderRuns(rawRuns: unknown, sessionId: string): ChatWorkActivityRun[] {
  if (!Array.isArray(rawRuns) || !sessionId) return []
  let result: ChatWorkActivityRun[] = []
  for (const value of rawRuns) {
    const run = record(value)
    const metadata = record(run.metadata)
    if (String(metadata.session_id || metadata.sessionId || '') !== sessionId) continue
    const events = Array.isArray(run.events) ? run.events : []
    for (const rawEvent of events) {
      const event = record(rawEvent)
      result = applyProviderEvent(result, {
        ...event,
        provider: event.provider || run.provider,
        run_id: event.run_id || run.run_id,
        task_id: event.task_id || run.task_id,
        attempt_id: event.attempt_id || run.attempt_id,
        metadata: { ...metadata, ...record(event.metadata) },
      })
    }
    if (TERMINAL_STATUSES.has(String(run.status || '').toLowerCase())) {
      result = applyProviderResult(result, run)
    }
  }
  return result.sort((left, right) => left.startedAt - right.startedAt)
}
