import type {
  ProviderEvent,
  ProviderRun,
  SignalRef,
  WorkSignal,
  WorkSignalPhase,
} from './types'
import { eventNarrative, summarizePayload } from './workState'
import { visibleProviderEvents } from './providerEventVisibility'

function eventId(event: ProviderEvent, index: number): string {
  return `${event.run_id}:${index}:${event.type}`
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? value as Record<string, unknown> : {}
}

function stringValue(value: unknown): string {
  return value === undefined || value === null ? '' : String(value)
}

function firstString(...values: unknown[]): string {
  for (const value of values) {
    const text = stringValue(value).trim()
    if (text) return text
  }
  return ''
}

function toolName(event: ProviderEvent): string {
  const payload = event.payload || {}
  const raw = asRecord(payload.raw)
  return firstString(payload.tool, payload.name, raw.tool, raw.toolName, raw.name)
}

function toolInput(event: ProviderEvent): Record<string, unknown> {
  const payload = event.payload || {}
  const raw = asRecord(payload.raw)
  return {
    ...asRecord(raw.input),
    ...asRecord(raw.args),
    ...asRecord(raw.arguments),
    ...asRecord(payload.input),
    ...asRecord(payload.args),
  }
}

function payloadPath(event: ProviderEvent): string {
  const payload = event.payload || {}
  const raw = asRecord(payload.raw)
  const input = toolInput(event)
  return firstString(
    payload.path,
    payload.file_path,
    payload.file,
    payload.command,
    payload.cwd,
    input.path,
    input.file_path,
    input.file,
    input.command,
    input.cwd,
    raw.path,
    raw.file_path,
    raw.file,
    raw.command,
    raw.cwd,
  )
}

function diffFiles(payload: Record<string, unknown>): SignalRef[] {
  const candidates = [
    payload.files,
    payload.changedFiles,
    payload.modifiedFiles,
    asRecord(payload.diff).files,
    asRecord(payload.data).files,
  ]
  const files = candidates.find(Array.isArray) as unknown[] | undefined
  if (!files) return []
  return files.slice(0, 8).map((file, index) => {
    if (typeof file === 'string') {
      return { type: 'file', label: file, ref: file } satisfies SignalRef
    }
    const item = asRecord(file)
    const path = firstString(item.path, item.file, item.filename, item.name) || `file ${index + 1}`
    return { type: 'file', label: path, ref: path } satisfies SignalRef
  })
}

function signalPhase(event: ProviderEvent): WorkSignalPhase {
  const payload = event.payload || {}
  const tool = toolName(event).toLowerCase()
  const path = payloadPath(event).toLowerCase()
  const text = `${event.type} ${tool} ${path}`.toLowerCase()
  if (event.type === 'run.failed') return 'error'
  if (event.type === 'run.cancelled') return 'error'
  if (event.type === 'diff.updated' || event.type === 'run.finished') return 'result'
  if (event.type.includes('permission')) return 'permission'
  if (text.includes('test') || text.includes('build') || text.includes('typecheck') || text.includes('lint')) return 'validate'
  if (text.includes('edit') || text.includes('write') || text.includes('patch') || text.includes('create') || text.includes('delete')) return 'edit'
  if (event.type === 'tool.call' || event.type === 'tool.result') return 'inspect'
  if (payload.status === 'running') return 'plan'
  return 'plan'
}

function signalImportance(event: ProviderEvent, phase: WorkSignalPhase): WorkSignal['importance'] {
  if (phase === 'error' || phase === 'permission') return 'blocking'
  if (phase === 'result' || phase === 'validate' || phase === 'edit') return 'important'
  if (event.type === 'assistant.delta' || event.type === 'run.status') return 'ambient'
  return 'normal'
}

function signalTitle(phase: WorkSignalPhase, event: ProviderEvent): string {
  const tool = toolName(event)
  if (phase === 'inspect') return tool ? `Inspected with ${tool}` : 'Inspected workspace'
  if (phase === 'edit') return 'Changed workspace files'
  if (phase === 'validate') return 'Validated behavior'
  if (phase === 'permission') return 'Permission checkpoint'
  if (phase === 'error') return event.type === 'run.cancelled' ? 'Provider cancelled' : 'Provider blocked'
  if (phase === 'result') return event.type === 'diff.updated' ? 'Diff ready for review' : 'Provider result ready'
  return event.type === 'run.created' ? 'Turn created' : 'Direction signal'
}

function signalEvidence(event: ProviderEvent, phase: WorkSignalPhase): SignalRef[] {
  if (event.type === 'diff.updated') {
    const files = diffFiles(event.payload || {})
    return files.length > 0 ? files : [{ type: 'diff', label: 'Workspace diff', ref: event.run_id }]
  }

  const path = payloadPath(event)
  if (!path) return []
  if (phase === 'validate' || path.includes('npm ') || path.includes('python ') || path.includes('git ') || path.includes('pnpm ')) {
    return [{ type: 'command', label: path, ref: path }]
  }
  return [{ type: 'file', label: path, ref: path }]
}

function assistantDeltaSignal(run: ProviderRun, events: ProviderEvent[]): WorkSignal | null {
  const deltas = events.filter(event => event.type === 'assistant.delta')
  if (deltas.length === 0) return null
  const text = deltas
    .slice(-8)
    .map(event => summarizePayload(event))
    .join('')
    .replace(/\s+/g, ' ')
    .trim()
  if (!text) return null
  return {
    id: `${run.run_id}:assistant-summary`,
    turnId: run.run_id,
    phase: 'plan',
    importance: 'ambient',
    title: 'Narration updated',
    summary: text.slice(-220),
    rawEventRefs: deltas.slice(-8).map((event, index) => eventId(event, events.indexOf(event) >= 0 ? events.indexOf(event) : index)),
    expandable: true,
  }
}

function eventToSignal(event: ProviderEvent, index: number): WorkSignal {
  const phase = signalPhase(event)
  const narrative = eventNarrative(event)
  return {
    id: eventId(event, index),
    turnId: event.run_id,
    phase,
    importance: signalImportance(event, phase),
    title: signalTitle(phase, event),
    summary: narrative.text || summarizePayload(event),
    evidence: signalEvidence(event, phase),
    rawEventRefs: [eventId(event, index)],
    expandable: phase !== 'plan' || event.type !== 'run.status',
  }
}

function dedupeSignals(signals: WorkSignal[]): WorkSignal[] {
  const result: WorkSignal[] = []
  const seen = new Set<string>()
  for (const signal of signals) {
    const evidenceKey = (signal.evidence || []).map(ref => `${ref.type}:${ref.ref}`).join('|')
    const key = `${signal.phase}:${signal.title}:${evidenceKey || signal.summary}`
    if (seen.has(key) && signal.importance !== 'blocking') continue
    seen.add(key)
    result.push(signal)
  }
  return result
}

export function providerRunToWorkSignals(run: ProviderRun): WorkSignal[] {
  const events = visibleProviderEvents(run.events || [])
  if (events.length === 0) {
    return [{
      id: `${run.run_id}:queued`,
      turnId: run.run_id,
      phase: 'plan',
      importance: 'ambient',
      title: 'Run queued',
      summary: 'Waiting for provider events.',
      rawEventRefs: [],
      expandable: false,
    }]
  }

  const nonNarration = events
    .map((event, index) => ({ event, index }))
    .filter(({ event }) => event.type !== 'assistant.delta')
    .slice(-32)
    .map(({ event, index }) => eventToSignal(event, index))

  const narration = assistantDeltaSignal(run, events)
  const signals = narration ? [...nonNarration, narration] : nonNarration
  return dedupeSignals(signals).slice(-18)
}
