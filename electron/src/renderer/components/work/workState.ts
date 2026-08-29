import type { ProviderInspectionDetails, ProviderEvent, ProviderRun, TurnPhase } from './types'

export const STATUS_META: Record<ProviderRun['status'], { label: string; tone: string }> = {
  queued: { label: 'Queued', tone: 'idle' },
  running: { label: 'Active', tone: 'active' },
  done: { label: 'Run ended', tone: 'done' },
  error: { label: 'Error', tone: 'risk' },
  cancelled: { label: 'Cancelled', tone: 'idle' },
  orphaned: { label: 'Interrupted', tone: 'risk' },
}

export const PHASE_META: Record<TurnPhase, { label: string; summary: string }> = {
  standby: {
    label: 'Standby',
    summary: 'Waiting for a work request.',
  },
  orienting: {
    label: 'Direction Cue',
    summary: 'Amadeus is forming a concise direction before deeper work.',
  },
  working: {
    label: 'Working Pass',
    summary: 'Provider is changing or inspecting the workspace.',
  },
  reviewing: {
    label: 'Review Pass',
    summary: 'Changes are ready to compare, summarize, or commit.',
  },
  complete: {
    label: 'Final Summary',
    summary: 'The provider attempt ended; WorkItem completion and acceptance remain separate.',
  },
  blocked: {
    label: 'Intervention',
    summary: 'The turn needs user attention before continuing.',
  },
}

export const TASK_STEPS = [
  { key: 'plan', label: 'Plan' },
  { key: 'inspect', label: 'Inspect' },
  { key: 'edit', label: 'Edit' },
  { key: 'test', label: 'Test' },
  { key: 'review', label: 'Review' },
  { key: 'commit', label: 'Commit' },
]

export function normalizeRun(raw: unknown): ProviderRun | null {
  if (!raw || typeof raw !== 'object') return null
  const item = raw as Record<string, unknown>
  const runId = String(item.run_id || '')
  const provider = String(item.provider || '')
  if (!runId || !provider) return null
  const rawStatus = String(item.status || 'running') as ProviderRun['status']
  const status = STATUS_META[rawStatus] ? rawStatus : 'running'
  return {
    run_id: runId,
    provider,
    task: String(item.task || ''),
    cwd: item.cwd ? String(item.cwd) : null,
    status,
    result: item.result ? String(item.result) : '',
    error: item.error ? String(item.error) : null,
    metadata: item.metadata && typeof item.metadata === 'object'
      ? item.metadata as Record<string, unknown>
      : {},
    events: Array.isArray(item.events) ? item.events as ProviderEvent[] : [],
  }
}

export function summarizePayload(event: ProviderEvent): string {
  const payload = event.payload || {}
  const tool = payload.tool || payload.name || payload.command
  if (tool) return String(tool)
  const text = payload.text || payload.delta || payload.error || payload.status || payload.result
  if (text) return String(text).replace(/\s+/g, ' ').slice(0, 180)
  try {
    return JSON.stringify(payload).slice(0, 180)
  } catch {
    return ''
  }
}

export function previewJson(value: unknown): string {
  if (value === undefined || value === null) return ''
  try {
    return JSON.stringify(value, null, 2).slice(0, 6000)
  } catch {
    return String(value)
  }
}

export function toolCount(run?: ProviderRun): number {
  return (run?.events || []).filter(event => event.type === 'tool.call').length
}

export function inferPhase(run?: ProviderRun, details?: ProviderInspectionDetails): TurnPhase {
  if (!run) return 'standby'
  if (run.status === 'error' || run.status === 'orphaned' || details?.error) return 'blocked'
  if (run.status === 'done') return 'complete'
  if (details?.diff || run.events?.some(event => event.type === 'diff.updated')) return 'reviewing'
  if ((run.events?.length || 0) <= 2) return 'orienting'
  return 'working'
}

export function eventNarrative(event: ProviderEvent): { label: string; text: string } {
  const payload = event.payload || {}
  if (event.type === 'assistant.delta') {
    return { label: 'summary', text: summarizePayload(event) || 'Assistant is updating the turn summary.' }
  }
  if (event.type === 'tool.call') {
    const tool = payload.tool || payload.name || 'tool'
    const path = payload.path || payload.file_path || payload.command || ''
    return { label: 'tool', text: path ? `${tool}: ${path}` : String(tool) }
  }
  if (event.type === 'tool.result') {
    return { label: 'result', text: summarizePayload(event) || 'Tool result received.' }
  }
  if (event.type === 'diff.updated') {
    return { label: 'diff', text: 'Diff is available for review.' }
  }
  if (event.type === 'run.finished') {
    return { label: 'done', text: 'Run finished. WorkItem completion assessment is available for review.' }
  }
  if (event.type === 'run.failed') {
    return { label: 'blocked', text: summarizePayload(event) || 'Run failed.' }
  }
  return { label: event.type, text: summarizePayload(event) || 'Runtime event received.' }
}

export function inferStepState(run: ProviderRun | undefined, index: number): 'complete' | 'active' | 'queued' {
  if (!run) return index === 0 ? 'active' : 'queued'
  if (run.status === 'done') return 'complete'
  if (run.status === 'error' || run.status === 'cancelled' || run.status === 'orphaned') return index === 0 ? 'active' : 'queued'
  const count = run.events?.length || 0
  const activeIndex = Math.min(TASK_STEPS.length - 1, Math.floor(count / 6))
  if (index < activeIndex) return 'complete'
  if (index === activeIndex) return 'active'
  return 'queued'
}
