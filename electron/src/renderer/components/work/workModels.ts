import type {
  ArtifactRef,
  EvidenceItem,
  ProviderInspectionDetails,
  ProjectStateMap,
  ProviderRun,
  RiskNote,
  ValidationResult,
  WorkSignal,
  WorkTurn,
  WorkTurnNode,
  WorkTurnPhase,
  WorkTurnStatus,
} from './types'
import { providerRunToWorkSignals } from './providerEventAdapter'

function workStatus(run?: ProviderRun): WorkTurnStatus {
  if (!run) return 'planning'
  if (run.status === 'error' || run.status === 'orphaned') return 'blocked'
  if (run.status === 'done') return 'review'
  if (run.status === 'cancelled') return 'reverted'
  if (run.status === 'queued') return 'planning'
  return 'running'
}

function workPhase(run?: ProviderRun, details?: ProviderInspectionDetails): WorkTurnPhase {
  if (!run) return 'intake'
  if (run.status === 'error' || run.status === 'orphaned' || details?.error) return 'review'
  if (run.status === 'done') return 'review'
  if (details?.diff || run.events?.some(event => event.type === 'diff.updated')) return 'review'
  const signals = providerRunToWorkSignals(run)
  if (signals.some(signal => signal.phase === 'validate')) return 'validate'
  if (signals.some(signal => signal.phase === 'edit')) return 'edit'
  if (signals.some(signal => signal.phase === 'inspect')) return 'inspect'
  if ((run.events?.length || 0) > 0) return 'plan'
  return 'contract'
}

function evidenceFromSignals(signals: WorkSignal[], details?: ProviderInspectionDetails): EvidenceItem[] {
  const items: EvidenceItem[] = []
  const seen = new Set<string>()
  for (const signal of signals) {
    for (const ref of signal.evidence || []) {
      const key = `${ref.type}:${ref.ref}`
      if (seen.has(key)) continue
      seen.add(key)
      items.push({
        id: `evidence:${key}`,
        kind: ref.type === 'command' ? 'command' : ref.type === 'test' ? 'test' : ref.type === 'diff' ? 'diff' : 'file',
        title: ref.label,
        summary: signal.summary,
        path: ref.type === 'file' ? ref.ref : undefined,
        command: ref.type === 'command' ? ref.ref : undefined,
      })
    }
  }
  if (details?.diff) {
    items.push({
      id: 'evidence:diff',
      kind: 'diff',
      title: 'Workspace diff',
      summary: 'A diff is available for review.',
    })
  }
  return items.slice(0, 8)
}

function validationFromSignals(signals: WorkSignal[], run?: ProviderRun): ValidationResult[] {
  const validationSignals = signals.filter(signal => signal.phase === 'validate')
  if (validationSignals.length === 0) {
    return [{
      id: 'validation:manual',
      kind: 'manual',
      status: run?.status === 'done' ? 'pending' : 'skipped',
      summary: run?.status === 'done' ? 'Manual review is pending.' : 'No validation signal has been observed yet.',
    }]
  }
  return validationSignals.slice(-3).map((signal, index) => ({
    id: `validation:${index}`,
    kind: signal.summary.toLowerCase().includes('build') ? 'build' : signal.summary.toLowerCase().includes('type') ? 'typecheck' : 'test',
    status: signal.importance === 'blocking' ? 'failed' : 'passed',
    summary: signal.summary,
    command: signal.evidence?.find(ref => ref.type === 'command')?.ref,
  }))
}

function risksFromRun(run?: ProviderRun, details?: ProviderInspectionDetails): RiskNote[] {
  if (!run) {
    return [{ id: 'risk:scope', level: 'low', summary: 'No active provider turn yet.' }]
  }
  if (run.status === 'error' || run.status === 'orphaned' || details?.error) {
    return [{
      id: 'risk:blocker',
      level: 'high',
      summary: String(run.error || details?.error || (run.status === 'orphaned' ? 'Run lost its live owner and needs recovery review.' : 'Provider reported an error.')),
      mitigation: 'Inspect the trace, then retry the failed instruction, resume an interrupted run, or submit revised intent as new work.',
    }]
  }
  if (run.status === 'done') {
    return [{
      id: 'risk:review',
      level: 'medium',
      summary: 'Changes need user review before acceptance.',
      mitigation: 'Open diff or audit before committing.',
    }]
  }
  return [{
    id: 'risk:normal',
    level: 'low',
    summary: 'No blocking risk has been observed.',
  }]
}

function artifactsFromRun(run?: ProviderRun, details?: ProviderInspectionDetails): ArtifactRef[] {
  const artifacts: ArtifactRef[] = []
  if (details?.diff) artifacts.push({ id: 'artifact:diff', kind: 'diff', title: 'Diff preview', ref: 'work.attempt.diff' })
  if (run?.result) artifacts.push({ id: 'artifact:summary', kind: 'markdown', title: 'Final summary', ref: 'run.result' })
  return artifacts
}

export function providerRunToWorkTurn(run: ProviderRun | undefined, details: ProviderInspectionDetails | undefined, fallback: {
  cwd: string
  provider: string
}): WorkTurn {
  if (!run) {
    return {
      id: 'turn:standby',
      title: 'Start a focused work turn',
      intent: 'Describe a task and choose a provider.',
      status: 'planning',
      phase: 'intake',
      provider: fallback.provider,
      summary: 'Waiting for a work request.',
      signals: [],
      evidence: [],
      validation: validationFromSignals([]),
      risks: risksFromRun(undefined),
      pendingInputs: [],
      permissions: [],
      artifacts: [],
    }
  }

  const signals = providerRunToWorkSignals(run)
  return {
    id: run.run_id,
    title: run.task || 'Active provider turn',
    intent: run.task || 'Provider task.',
    status: workStatus(run),
    phase: workPhase(run, details),
    provider: run.provider,
    branch: run.metadata?.branch ? String(run.metadata.branch) : undefined,
    worktree: run.metadata?.worktree ? String(run.metadata.worktree) : run.cwd || fallback.cwd || undefined,
    summary: run.result || run.error || details?.error || signals.slice(-3).map(signal => signal.summary).join(' '),
    signals,
    evidence: evidenceFromSignals(signals, details),
    validation: validationFromSignals(signals, run),
    risks: risksFromRun(run, details),
    pendingInputs: [],
    // Permission cards come only from the durable Host permission contract.
    // Provider activity and review-ready state are not permission requests.
    permissions: [],
    artifacts: artifactsFromRun(run, details),
  }
}

function nodeValidation(turn: WorkTurn): WorkTurnNode['validation'] {
  const failed = turn.validation.some(item => item.status === 'failed')
  if (failed) return 'failed'
  const running = turn.validation.some(item => item.status === 'running')
  if (running) return 'running'
  const passed = turn.validation.some(item => item.status === 'passed')
  if (passed) return 'passed'
  const pending = turn.validation.some(item => item.status === 'pending')
  return pending ? 'pending' : 'skipped'
}

export function buildProjectStateMap(turns: WorkTurn[], currentTurnId: string): ProjectStateMap {
  const nodes = turns.map(turn => ({
    id: turn.id,
    title: turn.title,
    status: turn.status,
    provider: turn.provider,
    changedFiles: turn.evidence.filter(item => item.kind === 'file').length,
    validation: nodeValidation(turn),
    artifactCount: turn.artifacts.length,
  }))
  return {
    currentTurnId,
    turns: nodes,
    edges: nodes.slice(1).map((node, index) => ({
      from: nodes[index].id,
      to: node.id,
      kind: 'sequence',
    })),
  }
}
